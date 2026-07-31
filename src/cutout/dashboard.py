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
from .podcasts import (
    META_DELAY,
    META_FEED_URL,
    META_LAST_REFRESHED,
    META_LAST_REQUESTED,
    META_TITLE,
)

# How many auto-refresh intervals a feed may go without a successful refresh
# before the dashboard flags it. Above 1 so a single missed or slow sweep is not
# reported as a fault.
_MISSED_REFRESHES_BEFORE_UNHEALTHY = 3


@dataclass
class FeedSummary:
    """One row of the dashboard: what a stored podcast currently looks like."""

    feed_id: str
    title: str | None
    source_url: str | None
    delay: str | None
    last_requested: str | None
    last_refreshed: str | None
    episode_count: int
    # This server's subscribe URL for the podcast — what a client points at, and
    # what the OPML export writes as each entry's xmlUrl.
    cutout_url: str
    # True when the sweep should have refreshed this feed by now but its
    # lastrefreshed has not advanced — see :func:`_is_unhealthy`.
    unhealthy: bool
    # Set when the feed is being skipped by the sweep for being unrequested;
    # such a feed is stale by design, not broken.
    stale: bool

    @property
    def display_title(self) -> str:
        return self.title or self.feed_id

    @property
    def last_requested_display(self) -> str | None:
        return _humanise(self.last_requested)

    @property
    def last_refreshed_display(self) -> str | None:
        return _humanise(self.last_refreshed)

    @property
    def status(self) -> str:
        """One-word health for the dashboard's status column."""
        if self.unhealthy:
            return "failing"
        if self.stale:
            return "paused"
        return "ok"


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

    @property
    def failing_feeds(self) -> int:
        return sum(1 for feed in self.feeds if feed.unhealthy)


async def gather(storage: Storage, settings: Settings) -> DashboardData:
    """Build the dashboard read-model for every stored podcast, title-sorted."""
    public_base = settings.public_service_url.rstrip("/")
    interval = settings.auto_refresh_interval_secs
    ttl = settings.auto_refresh_ttl_secs
    feeds: list[FeedSummary] = []
    for feed_id in await podcasts.list_feed_ids(storage):
        key = feed_path(feed_id)
        metadata = await storage.head(key) or {}
        channel_title, episode_count = _parse_feed(await storage.get_bytes(key))
        last_requested = metadata.get(META_LAST_REQUESTED)
        last_refreshed = await _last_refreshed(storage, key, metadata)
        stale = podcasts.is_stale(last_requested, ttl)
        feeds.append(
            FeedSummary(
                feed_id=feed_id,
                # The channel's own <title> is what listeners see; fall back to
                # the title the feed was created with, then to the feed_id.
                title=channel_title or metadata.get(META_TITLE),
                source_url=metadata.get(META_FEED_URL),
                delay=metadata.get(META_DELAY),
                last_requested=last_requested,
                last_refreshed=last_refreshed,
                episode_count=episode_count,
                cutout_url=f"{public_base}/podcast/{feed_id}",
                unhealthy=_is_unhealthy(last_refreshed, stale=stale, interval=interval),
                stale=stale,
            )
        )
    feeds.sort(key=lambda feed: feed.display_title.casefold())
    return DashboardData(feeds=feeds)


async def _last_refreshed(
    storage: Storage, key: str, metadata: dict[str, str]
) -> str | None:
    """When this feed last refreshed successfully, as an ISO 8601 string.

    Prefers the ``lastrefreshed`` metadata, and falls back to when the feed
    document itself was last written.
    """
    stamped = metadata.get(META_LAST_REFRESHED)
    if stamped:
        return stamped
    written = await storage.last_modified(key)
    return written.isoformat() if written else None


def _is_unhealthy(last_refreshed: str | None, *, stale: bool, interval: int) -> bool:
    """Whether a feed the sweep keeps picking up has stopped refreshing.

    Returns False when auto-refresh is off.
    """

    # ``lastrefreshed`` only advances when a refresh completes, so a feed the
    # sweep enqueues every ``interval`` whose timestamp has not moved for several
    # intervals is failing every attempt — whatever the cause.
    if interval <= 0 or stale or not last_refreshed:
        return False
    age = _age_seconds(last_refreshed)
    if age is None:
        return False
    return age > interval * _MISSED_REFRESHES_BEFORE_UNHEALTHY


def _age_seconds(timestamp: str) -> float | None:
    """Seconds since ``timestamp``, or None if it cannot be parsed."""
    try:
        when = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


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
    seconds = _age_seconds(timestamp)
    if seconds is None:
        return timestamp
    if seconds < 0:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"
