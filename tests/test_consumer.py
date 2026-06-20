import asyncio

from cutout.runtime import drain, spawn_drainers


def _run_drain(messages, handler):
    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(drain(queue, handler, name="t"))
        for message in messages:
            await queue.put(message)
        await queue.join()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_drain_processes_in_order():
    seen: list[dict] = []

    async def handler(message):
        seen.append(message)

    _run_drain([{"a": 1}, {"a": 2}], handler)
    assert seen == [{"a": 1}, {"a": 2}]


def test_drain_failure_does_not_stop_consumer():
    seen: list[dict] = []

    async def handler(message):
        if message["a"] == 1:
            raise RuntimeError("boom")
        seen.append(message)

    # First message raises and is dropped; the second is still processed.
    _run_drain([{"a": 1}, {"a": 2}], handler)
    assert seen == [{"a": 2}]


def test_spawn_drainers_processes_concurrently():
    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()
        # A barrier of two parties only releases once both handlers are
        # in flight, so this would deadlock with a single worker.
        barrier = asyncio.Barrier(2)

        async def handler(message):
            await barrier.wait()

        tasks = spawn_drainers(queue, handler, name="t", concurrency=2)
        await queue.put({"a": 1})
        await queue.put({"a": 2})
        await asyncio.wait_for(queue.join(), timeout=1)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_spawn_drainers_count_matches_concurrency():
    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()

        async def handler(message):
            pass

        tasks = spawn_drainers(queue, handler, name="t", concurrency=3)
        assert len(tasks) == 3
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())
