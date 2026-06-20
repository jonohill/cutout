import asyncio

import pytest

from cutout.draining import spawn_drainers
from cutout.fair_queue import FairQueue

pytestmark = pytest.mark.anyio


def _by_feed() -> FairQueue:
    return FairQueue(key=lambda job: job["feed_id"])


def _job(feed: str, ep: str) -> dict:
    return {"feed_id": feed, "episode_id": ep}


async def test_round_robins_across_feeds():
    # One feed's burst then a single episode of another: the lone episode
    # should be served second (fairly), not last behind the whole burst.
    q = _by_feed()
    for ep in ("a", "b", "c"):
        await q.put(_job("f1", ep))
    await q.put(_job("f2", "x"))

    drained = [(await q.get())["feed_id"] for _ in range(4)]
    assert drained == ["f1", "f2", "f1", "f1"]


async def test_preserves_within_feed_order():
    q = _by_feed()
    for ep in ("1", "2", "3"):
        await q.put(_job("f1", ep))

    eps = [(await q.get())["episode_id"] for _ in range(3)]
    assert eps == ["1", "2", "3"]  # RSS order kept within a feed


async def test_new_feed_interleaves_without_waiting_for_full_backlog():
    # A feed added mid-flight starts interleaving rather than queueing behind
    # the entire existing backlog.
    q = _by_feed()
    for ep in ("a", "b", "c", "d", "e"):
        await q.put(_job("f1", ep))

    first = await q.get()  # f1/a
    await q.put(_job("f2", "x"))  # f2 arrives after one item drained

    rest = [await q.get() for _ in range(5)]
    order = [first, *rest]
    feeds = [j["feed_id"] for j in order]

    f2_index = feeds.index("f2")
    last_f1_index = max(i for i, f in enumerate(feeds) if f == "f1")
    # f2 is served before f1's backlog is exhausted — i.e. not starved.
    assert f2_index < last_f1_index


async def test_get_blocks_until_put():
    q = _by_feed()
    getter = asyncio.ensure_future(q.get())
    await asyncio.sleep(0)  # let it reach the wait
    assert not getter.done()

    await q.put(_job("f1", "a"))
    item = await asyncio.wait_for(getter, timeout=1)
    assert item["episode_id"] == "a"


async def test_qsize_empty_and_task_done_accounting():
    q = _by_feed()
    assert q.empty() and q.qsize() == 0

    await q.put(_job("f1", "a"))
    await q.put(_job("f2", "b"))
    assert not q.empty()
    assert q.qsize() == 2

    await q.get()
    await q.get()
    assert q.empty() and q.qsize() == 0

    q.task_done()
    q.task_done()
    with pytest.raises(ValueError):
        q.task_done()  # called more times than items


async def test_join_waits_for_completion():
    q = _by_feed()
    await q.put(_job("f1", "a"))
    await q.get()

    joiner = asyncio.ensure_future(q.join())
    await asyncio.sleep(0)
    assert not joiner.done()  # task_done not yet called

    q.task_done()
    await asyncio.wait_for(joiner, timeout=1)


async def test_drop_in_for_drain_loop_interleaves():
    # Used through the real drain workers, two feeds' jobs come out interleaved.
    q = _by_feed()
    for ep in ("a", "b", "c"):
        await q.put(_job("f1", ep))
    for ep in ("x", "y", "z"):
        await q.put(_job("f2", ep))

    seen: list[str] = []
    done = asyncio.Event()

    async def handler(job: dict) -> None:
        seen.append(job["feed_id"])
        if len(seen) == 6:
            done.set()

    # Single worker so ordering is deterministic.
    tasks = spawn_drainers(q, handler, name="t", concurrency=1)
    try:
        await asyncio.wait_for(done.wait(), timeout=1)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert seen == ["f1", "f2", "f1", "f2", "f1", "f2"]


async def test_put_wakes_only_one_of_many_waiters():
    # No thundering herd: one put completes exactly one parked getter.
    q = _by_feed()
    getters = [asyncio.ensure_future(q.get()) for _ in range(3)]
    await asyncio.sleep(0)  # let them all park

    await q.put(_job("f1", "a"))
    await asyncio.sleep(0)  # let the woken getter resume

    done = [g for g in getters if g.done()]
    assert len(done) == 1
    assert (await done[0])["episode_id"] == "a"

    for g in getters:
        g.cancel()
    await asyncio.gather(*getters, return_exceptions=True)


async def test_cancelled_getter_passes_wakeup_to_next():
    # If a getter is cancelled after being signalled, the item it was woken for
    # must go to another waiter rather than being stranded.
    q = _by_feed()
    a = asyncio.ensure_future(q.get())
    b = asyncio.ensure_future(q.get())
    await asyncio.sleep(0)  # both park; `a` is first in line

    await q.put(_job("f1", "a"))  # wakes `a`
    a.cancel()  # ...but `a` is cancelled before it can take the item

    item = await asyncio.wait_for(b, timeout=1)
    assert item["episode_id"] == "a"  # `b` got it instead
    with pytest.raises(asyncio.CancelledError):
        await a


async def test_join_returns_immediately_when_idle():
    q = _by_feed()
    await asyncio.wait_for(q.join(), timeout=1)  # nothing outstanding
