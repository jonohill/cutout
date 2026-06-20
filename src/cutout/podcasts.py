"""Podcast domain operations over storage and the work queue.

The thin layer the HTTP handlers (``cutout.app``) and the OPML feature
(``cutout.opml``) share: how a create is enqueued, how stored feeds are
enumerated, and the metadata contract for a feed's stored object.
"""

from __future__ import annotations

import asyncio

from .common import feed_path, new_feed_id
from .common.storage import Storage

# Object-metadata keys for a stored feed. S3 lowercases user-metadata keys, so
# these are lowercase for both the worker's writes and the reads here. This is
# the single source of truth for the contract; the worker imports these.
META_FEED_URL = "feedurl"
META_TITLE = "title"
META_DELAY = "delay"


async def enqueue_create(
    queue: asyncio.Queue,
    *,
    feed_url: str,
    title: str | None = None,
    delay: str | None = None,
) -> str:
    """Queue a feed for creation and return its freshly minted feed_id."""
    feed_id = new_feed_id()
    message: dict = {"feed_id": feed_id, "feed_url": feed_url}
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
