from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from ..common import audio_path, feed_path, get_feed_id, parse_delay
from ..common.storage import Storage
from ..config import Settings
from ..podcasts import META_DELAY as _META_DELAY
from ..podcasts import META_FEED_URL as _META_FEED_URL
from ..podcasts import META_TITLE as _META_TITLE
from . import feed_xml
from .feed_xml import Episode, Feed
from .fetch import fetch_text

logger = logging.getLogger(__name__)

# (body, final_url) fetcher; injectable so tests don't hit the network.
Fetcher = Callable[[str], Awaitable[tuple[str, str]]]

# Hands an episode to the media pipeline. The processor decides *which* episodes
# need processing; it does not know what the pipeline does with them or which
# stage runs first — that lives in ``pipeline.py``.
StartJob = Callable[[dict], Awaitable[None]]


def _pick_source_url(
    *, current: str, fetched: str | None, announced: str | None, own_base: str
) -> str:
    """Pick the source URL to persist: itunes:new-feed-url, then HTTP redirect target, else current. Skips loops to our own host."""
    own_host = urlsplit(own_base).hostname
    for candidate in (announced, fetched):
        if not candidate or candidate == current:
            continue
        if own_host and urlsplit(candidate).hostname == own_host:
            continue
        return candidate
    return current


class FeedProcessor:
    """Owns the per-message workflow. One instance is fine for all messages."""

    def __init__(
        self,
        *,
        storage: Storage,
        start_job: StartJob,
        settings: Settings,
        fetch: Fetcher = fetch_text,
    ) -> None:
        self._storage = storage
        self._start_job = start_job
        self._settings = settings
        self._fetch = fetch

    async def process(self, body: dict) -> None:
        feed_id, feed_url, title, delay = await self._resolve(body)
        logger.info("processing feed %s (%s)", feed_id, feed_url)
        original_xml, fetched_url = await self._fetch(feed_url)
        feed = feed_xml.parse_feed(original_xml)
        announced_url = feed_xml.get_new_feed_url(feed)
        episodes = feed_xml.parse_episodes(feed)
        if delay:
            episodes = self._apply_delay(feed, episodes, delay)
        await self._reconcile_episodes(feed_id, feed, episodes)
        public_base = self._settings.public_service_url.rstrip("/")
        feed_xml.rewrite_channel_links(feed, f"{public_base}/podcast/{feed_id}")
        if title:
            feed_xml.set_channel_title(feed, title)
        effective_url = _pick_source_url(
            current=feed_url,
            fetched=fetched_url,
            announced=announced_url,
            own_base=public_base,
        )
        await self._store_feed(feed_id, effective_url, title, delay, feed)

    async def _resolve(self, body: dict) -> tuple[str, str, str | None, str | None]:
        feed_id = body.get("feed_id") or body.get("podcast_id")
        feed_url = body.get("feed_url") or body.get("podcast_url")
        title = body.get("title")
        delay = body.get("delay")

        if not feed_id:
            raise ValueError("message missing feed_id")
        if not feed_url:
            stored_url, stored_title, stored_delay = await self._lookup_feed_metadata(
                feed_id
            )
            feed_url = stored_url
            if title is None:
                title = stored_title
            if delay is None:
                delay = stored_delay
        if not feed_url:
            raise ValueError(f"feed {feed_id} missing feedUrl metadata")
        return feed_id, feed_url, title, delay

    async def _lookup_feed_metadata(
        self, feed_id: str
    ) -> tuple[str | None, str | None, str | None]:
        metadata = await self._storage.head(feed_path(feed_id))
        if metadata is None:
            raise ValueError(f"unknown feed_id: {feed_id}")
        return (
            metadata.get(_META_FEED_URL),
            metadata.get(_META_TITLE),
            metadata.get(_META_DELAY),
        )

    def _apply_delay(
        self, feed: Feed, episodes: list[Episode], delay: str
    ) -> list[Episode]:
        """Remove episodes within the delay window from ``feed`` and return the rest.

        An episode whose ``pub_date`` is at or after ``now - delay`` is treated as
        within the delay window and dropped entirely. Episodes without a parsable
        ``pub_date`` are kept.
        """
        cutoff = datetime.now(timezone.utc) - parse_delay(delay)
        kept: list[Episode] = []
        for episode in episodes:
            if episode.pub_date is not None and episode.pub_date >= cutoff:
                feed.remove_item(episode.item)
            else:
                kept.append(episode)
        return kept

    async def _reconcile_episodes(
        self, feed_id: str, feed: Feed, episodes: list[Episode]
    ) -> None:
        audio_base = self._settings.public_storage_url.rstrip("/")
        existing_keys = await self._storage.list_keys(f"{feed_id}/")

        present = queued = dropped = 0
        for index, episode in enumerate(episodes):
            episode_id = get_feed_id(episode.guid)
            audio_key = audio_path(feed_id, episode_id)

            if audio_key in existing_keys:
                feed_xml.set_audio_url(episode, f"{audio_base}/{audio_key}")
                present += 1
                continue

            if index < self._settings.max_episodes:
                await self._start_pipeline(feed_id, episode_id, feed, episode)
                queued += 1
            else:
                dropped += 1
            feed.remove_item(episode.item)

        logger.info(
            "feed %s: %d episode(s) — %d present, %d queued, %d over-limit",
            feed_id, len(episodes), present, queued, dropped,
        )

    async def _start_pipeline(
        self, feed_id: str, episode_id: str, feed: Feed, episode: Episode
    ) -> None:
        """Hand the episode to the media pipeline.

        The pipeline takes the episode from ``source_url`` through to stored
        audio at ``audio_path(feed_id, episode_id)``; how it does that, and in
        what order, is the pipeline's concern, not ours.

        Publisher metadata (podcast name, episode title, description) rides along
        as ``context`` when present, to steer the chapters model's name
        spellings; the key is omitted when there is nothing useful to pass.
        """
        job = {
            "feed_id": feed_id,
            "episode_id": episode_id,
            "source_url": episode.audio_url,
        }
        context = feed_xml.episode_context(feed, episode)
        if context:
            job["context"] = context
        await self._start_job(job)

    async def _store_feed(
        self,
        feed_id: str,
        feed_url: str,
        title: str | None,
        delay: str | None,
        feed: Feed,
    ) -> None:
        metadata: dict[str, str] = {_META_FEED_URL: feed_url}
        if title:
            metadata[_META_TITLE] = title
        if delay:
            metadata[_META_DELAY] = delay
        await self._storage.put_bytes(
            feed_path(feed_id),
            feed.serialize().encode("utf-8"),
            content_type="application/xml",
            metadata=metadata,
        )
