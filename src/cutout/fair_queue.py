from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Hashable
from typing import Any, Callable

KeyFn = Callable[[Any], Hashable]


class FairQueue:
    """An ``asyncio.Queue``-compatible queue that round-robins across a key."""

    def __init__(self, key: KeyFn) -> None:
        self._key = key
        # key -> its pending items, in arrival order
        self._subqueues: dict[Hashable, deque] = {}
        # keys with pending work, in service order; head is served next
        self._rotation: deque[Hashable] = deque()
        # Futures for get()s parked on an empty queue; woken one-per-item so a
        # put never wakes more consumers than it has work for.
        self._getters: deque[asyncio.Future] = deque()
        self._unfinished = 0
        # Set exactly when no work is outstanding, so join() can await it
        # instead of spinning. Starts set: an empty queue has nothing to wait
        # for.
        self._finished = asyncio.Event()
        self._finished.set()

    def qsize(self) -> int:
        return sum(len(sub) for sub in self._subqueues.values())

    def empty(self) -> bool:
        return not self._rotation

    def _wakeup_next(self) -> None:
        """Wake the first still-pending parked getter, if any."""
        while self._getters:
            getter = self._getters.popleft()
            if not getter.done():
                getter.set_result(None)
                return

    def put_nowait(self, item: Any) -> None:
        k = self._key(item)
        sub = self._subqueues.get(k)
        if sub is None:
            sub = self._subqueues[k] = deque()
            self._rotation.append(k)  # new key joins the rotation immediately
        sub.append(item)
        self._unfinished += 1
        self._finished.clear()
        self._wakeup_next()

    async def put(self, item: Any) -> None:
        # Unbounded: enqueue synchronously, keeping the awaitable interface.
        self.put_nowait(item)

    def get_nowait(self) -> Any:
        if not self._rotation:
            raise asyncio.QueueEmpty
        k = self._rotation.popleft()
        sub = self._subqueues[k]
        item = sub.popleft()
        if sub:
            self._rotation.append(k)  # still has work: back of the line
        else:
            del self._subqueues[k]  # drained: leaves until it has work again
        return item

    async def get(self) -> Any:
        # Park on a dedicated future while empty; put_nowait wakes exactly one
        # waiter. Mirrors asyncio.Queue.get, including handing our wakeup to the
        # next waiter if we're cancelled after having been signalled — otherwise
        # the item that woke us would be stranded with no one to take it.
        while not self._rotation:
            getter = asyncio.get_running_loop().create_future()
            self._getters.append(getter)
            try:
                await getter
            except BaseException:
                getter.cancel()  # in case we were cancelled before resuming
                try:
                    self._getters.remove(getter)
                except ValueError:
                    pass
                if self._rotation and not getter.cancelled():
                    # We were signalled (an item is waiting) but are being
                    # cancelled; pass the wakeup on so the item isn't lost.
                    self._wakeup_next()
                raise
        return self.get_nowait()

    def task_done(self) -> None:
        if self._unfinished <= 0:
            raise ValueError("task_done() called too many times")
        self._unfinished -= 1
        if self._unfinished == 0:
            self._finished.set()

    async def join(self) -> None:
        if self._unfinished > 0:
            await self._finished.wait()
