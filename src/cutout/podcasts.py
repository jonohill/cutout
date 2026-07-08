"""Podcast domain operations over storage and the work queue.

The thin layer the HTTP handlers (``cutout.app``) and the OPML feature
(``cutout.opml``) share: how a create is enqueued, how stored feeds are
enumerated, and the metadata contract for a feed's stored object.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .common import feed_path, new_feed_id
from .common.storage import Storage

# Object-metadata keys for a stored feed. S3 lowercases user-metadata keys, so
# these are lowercase for both the worker's writes and the reads here. This is
# the single source of truth for the contract; the worker imports these.
META_FEED_URL = "feedurl"
META_TITLE = "title"
META_DELAY = "delay"
# ISO 8601 UTC timestamp of when the feed was last *requested* (fetched or
# created), distinct from when it was last refreshed. The auto-refresh sweep
# uses it to decide staleness; only request-origin refreshes bump it.
META_LAST_REQUESTED = "lastrequested"


def now_timestamp() -> str:
    """Current time as an ISO 8601 UTC string, for the lastrequested metadata."""
    return datetime.now(timezone.utc).isoformat()


def is_stale(last_requested: str | None, ttl_secs: int) -> bool:
    """Whether a feed last requested at ``last_requested`` has exceeded its TTL.

    ``ttl_secs <= 0`` disables staleness (never stale). A missing or unparseable
    timestamp is treated as *not* stale — it will be stamped on the next refresh,
    so legacy feeds get a grace period rather than being dropped immediately.
    """
    if ttl_secs <= 0 or not last_requested:
        return False
    try:
        ts = datetime.fromisoformat(last_requested)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age > ttl_secs


async def enqueue_create(
    queue: asyncio.Queue,
    *,
    feed_url: str,
    title: str | None = None,
    delay: str | None = None,
) -> str:
    """Queue a feed for creation and return its freshly minted feed_id."""
    feed_id = new_feed_id()
    # A create is a user request, so it stamps lastrequested (see the worker's
    # _store_feed). OPML import goes through here too, so it counts as well.
    message: dict = {"feed_id": feed_id, "feed_url": feed_url, "requested": True}
    if title is not None:
        message["title"] = title
    if delay is not None:
        message["delay"] = delay
    await queue.put(message)
    return feed_id


async def list_feed_ids(storage: Storage) -> list[str]:
    """Every stored podcast's feed_id, from its ``{feed_id}/feed.xml`` key."""
    suffix = "/feed.xml"
    return sorted(
        key[: -len(suffix)]
        for key in await storage.list_keys("")
        if key.endswith(suffix)
    )


async def delete_feed(storage: Storage, feed_id: str) -> int:
    """Remove a podcast and every object under it, returning the count deleted.

    A feed's rewritten ``feed.xml``, its episode audio and any in-flight remote
    artifacts all share the ``{feed_id}/`` prefix, so listing that prefix and
    deleting each key drops the whole podcast. Returns 0 for an unknown feed_id.
    """
    keys = sorted(await storage.list_keys(f"{feed_id}/"))
    for key in keys:
        await storage.delete(key)
    return len(keys)


async def feed_source_url(storage: Storage, feed_id: str) -> str | None:
    """The original feed URL a podcast was created from, or None if unknown."""
    metadata = await storage.head(feed_path(feed_id))
    return (metadata or {}).get(META_FEED_URL)


async def stored_feed_urls(storage: Storage) -> set[str]:
    """The set of original feed URLs across every stored podcast."""
    urls: set[str] = set()
    for feed_id in await list_feed_ids(storage):
        url = await feed_source_url(storage, feed_id)
        if url:
            urls.add(url)
    return urls
