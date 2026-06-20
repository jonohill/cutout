"""Generic queue-draining helpers, shared by the feed queue and the media
pipeline. Kept dependency-free (only asyncio) so any module can import them
without risking an import cycle.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

Handler = Callable[[dict], Awaitable[None]]


async def drain(queue: asyncio.Queue, handler: Handler, *, name: str) -> None:
    """Process queue messages forever, one at a time, until cancelled.

    A failing message is logged and dropped — no retry, which is fine because
    the queue contents are regenerated on the next feed refresh.
    """
    while True:
        message = await queue.get()
        try:
            await handler(message)
        except Exception:
            logger.exception("%s: failed to process message: %r", name, message)
        finally:
            queue.task_done()


def spawn_drainers(
    queue: asyncio.Queue, handler: Handler, *, name: str, concurrency: int
) -> list[asyncio.Task]:
    """Spawn ``concurrency`` workers draining ``queue`` in parallel.

    Each worker pulls independently, so ``asyncio.Queue`` hands each message to
    exactly one of them — raising ``concurrency`` lets that many messages be
    in flight at once at the cost of in-order processing.
    """
    return [
        asyncio.create_task(drain(queue, handler, name=f"{name}-{i}"))
        for i in range(concurrency)
    ]
