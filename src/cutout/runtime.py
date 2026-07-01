"""Wire the full app: HTTP server + worker, joined by in-process queues.

HTTP  --feed_queue-->  FeedProcessor  --start-->  Pipeline
                            ^                         |
                            +----- feed refresh ------+

The Pipeline runs an episode through download -> transcribe -> chapters ->
encode -> upload (one queue per stage). The intermediate stages work over files
in local working storage; only ``upload`` writes the served audio to remote
object storage. When a job clears the final stage, the pipeline asks for a feed
refresh so reconciliation links the now-stored audio.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

# Re-exported so existing imports (and tests) keep resolving them here.
from .app import create_app
from .common import feed_path
from .common.storage import LocalStorage, S3Storage
from .config import Settings, get_settings
from .draining import Handler, drain, spawn_drainers  # noqa: F401  (re-export)
from .podcasts import META_LAST_REQUESTED, is_stale, list_feed_ids
from .worker import FeedProcessor, build_media_pipeline

logger = logging.getLogger(__name__)


def create_full_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    storage = S3Storage(settings)
    work = LocalStorage(settings.work_dir)

    feed_queue: asyncio.Queue = asyncio.Queue()

    async def refresh_feed(job: dict) -> None:
        """Terminal pipeline action: re-run reconciliation now the episode's
        audio exists, so the rewritten feed links to it.

        ``notify`` means that new episodes were added to the feed."""
        await feed_queue.put({"feed_id": job["feed_id"], "notify": True})

    pipeline = build_media_pipeline(
        storage=storage, work=work, settings=settings, on_complete=refresh_feed
    )
    processor = FeedProcessor(
        storage=storage, start_job=pipeline.start, settings=settings
    )

    async def auto_refresh_loop() -> None:
        """Periodically refresh every non-stale feed, as if each were requested.

        Enqueues the same bare ``{"feed_id": ...}`` message a fetch does (no
        ``requested`` flag, so the sweep never resets a feed's staleness clock).
        A feed not requested within ``auto_refresh_ttl`` is skipped until it is
        fetched again. Disabled when the interval is ``0``.
        """
        interval = settings.auto_refresh_interval_secs
        if interval <= 0:
            logger.info("auto-refresh disabled")
            return
        ttl = settings.auto_refresh_ttl_secs
        while True:
            await asyncio.sleep(interval)
            try:
                refreshed = skipped = 0
                for feed_id in await list_feed_ids(storage):
                    metadata = await storage.head(feed_path(feed_id)) or {}
                    if is_stale(metadata.get(META_LAST_REQUESTED), ttl):
                        skipped += 1
                        continue
                    await feed_queue.put({"feed_id": feed_id})
                    refreshed += 1
                logger.info(
                    "auto-refresh sweep: %d refreshed, %d stale-skipped",
                    refreshed,
                    skipped,
                )
            except Exception:
                logger.exception("auto-refresh sweep failed")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [
            *spawn_drainers(
                feed_queue,
                processor.process,
                name="feed",
                concurrency=settings.feed_concurrency,
            ),
            *pipeline.spawn(),
            asyncio.create_task(auto_refresh_loop()),
        ]
        logger.info("workers started (%d task(s))", len(tasks))
        try:
            yield
        finally:
            logger.info("stopping workers")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return create_app(
        settings=settings, storage=storage, queue=feed_queue, lifespan=lifespan
    )
