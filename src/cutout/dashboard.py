"""Read-model for the dashboard UI.

Gathers a per-podcast summary (title, source URL, delay, when it was last
requested, episode count) plus roll-up totals from committed storage state.
This is a *view* over what the worker has already written; freshly added feeds
are queued and only appear here once the worker has stored their ``feed.xml``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

from . import podcasts
from .common import feed_path
from .common.storage import Storage
from .config import Settings
from .podcasts import META_DELAY, META_FEED_URL, META_LAST_REQUESTED, META_TITLE


@dataclass
class FeedSummary:
    """One row of the dashboard: what a stored podcast currently looks like."""

    feed_id: str
    title: str | None
    source_url: str | None
    delay: str | None
    last_requested: str | None
    episode_count: int
    # This server's subscribe URL for the podcast — what a client points at, and
    # what the OPML export writes as each entry's xmlUrl.
    cutout_url: str

    @property
    def display_title(self) -> str:
        return self.title or self.feed_id

    @property
    def last_requested_display(self) -> str | None:
        return _humanise(self.last_requested)


@dataclass
class DashboardData:
    """Everything the dashboard template renders."""

    feeds: list[FeedSummary]

    @property
    def total_feeds(self) -> int:
        return len(self.feeds)

    @property
    def total_episodes(self) -> int:
        return sum(feed.episode_count for feed in self.feeds)


async def gather(storage: Storage, settings: Settings) -> DashboardData:
    """Build the dashboard read-model for every stored podcast, title-sorted."""
    public_base = settings.public_service_url.rstrip("/")
    feeds: list[FeedSummary] = []
    for feed_id in await podcasts.list_feed_ids(storage):
        key = feed_path(feed_id)
        metadata = await storage.head(key) or {}
        channel_title, episode_count = _parse_feed(await storage.get_bytes(key))
        feeds.append(
            FeedSummary(
                feed_id=feed_id,
                # The channel's own <title> is what listeners see; fall back to
                # the title the feed was created with, then to the feed_id.
                title=channel_title or metadata.get(META_TITLE),
                source_url=metadata.get(META_FEED_URL),
                delay=metadata.get(META_DELAY),
                last_requested=metadata.get(META_LAST_REQUESTED),
                episode_count=episode_count,
                cutout_url=f"{public_base}/podcast/{feed_id}",
            )
        )
    feeds.sort(key=lambda feed: feed.display_title.casefold())
    return DashboardData(feeds=feeds)


def _parse_feed(raw: bytes | None) -> tuple[str | None, int]:
    """The channel <title> and <item> count from a stored feed document.

    Returns ``(None, 0)`` for a missing or unparseable document rather than
    raising, so one bad feed never breaks the whole dashboard.
    """
    if not raw:
        return None, 0
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None, 0
    title = root.findtext("channel/title")
    title = title.strip() if title and title.strip() else None
    return title, len(root.findall("channel/item"))


def _humanise(timestamp: str | None) -> str | None:
    """Render an ISO 8601 timestamp as a short "x ago", or None if absent."""
    if not timestamp:
        return None
    try:
        when = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - when).total_seconds()
    if seconds < 0:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"
