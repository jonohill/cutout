import asyncio

from cutout.common import audio_path, work_path
from cutout.config import Settings
from cutout.fair_queue import FairQueue
from cutout.worker.pipeline import Pipeline, Stage, build_media_pipeline


def _settings() -> Settings:
    return Settings(
        s3_access_key_id="t",
        s3_secret_access_key="t",
    )


JOB = {"feed_id": "f", "episode_id": "e", "source_url": "u"}

# Stage name -> the artifact it produces, mirroring build_media_pipeline. The
# pipeline treats an artifact's existence as "this stage is done".
OUTPUTS = {
    "download": work_path("f", "e", "source"),
    "transcribe": work_path("f", "e", "transcript.json"),
    "chapters": work_path("f", "e", "chapters.json"),
    "encode": work_path("f", "e", "encoded"),
    "upload": audio_path("f", "e"),
}
NAMES = list(OUTPUTS)


def _job_key(job: dict) -> str:
    return f"{job['feed_id']}/{job['episode_id']}"


class FakeStorage:
    def __init__(self, existing=()):
        self.existing = set(existing)

    async def head(self, key):
        return {} if key in self.existing else None

    async def list_keys(self, prefix):
        return {k for k in self.existing if k.startswith(prefix)}


def _pipeline(storage, calls, done, *, raise_at=None, reached=None):
    """A pipeline whose stage handlers just record that they ran (and optionally
    raise), so we can assert ordering and hand-off without real media work."""

    def make(name):
        async def handler(job):
            calls.append(name)
            if name == raise_at:
                if reached is not None:
                    reached.set()
                raise RuntimeError("boom")

        return handler

    async def on_complete(job):
        calls.append("complete")
        done.set()

    stages = [
        Stage(n, make(n), (lambda k: lambda job: k)(OUTPUTS[n]), storage) for n in NAMES
    ]
    return Pipeline(stages, on_complete=on_complete, job_key=_job_key)


def _drive(pipeline, done, *, timeout=1.0):
    async def scenario():
        tasks = pipeline.spawn()
        await pipeline.start(JOB)
        try:
            await asyncio.wait_for(done.wait(), timeout)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_runs_every_stage_in_order_then_completes():
    calls: list[str] = []
    done = asyncio.Event()
    pipeline = _pipeline(FakeStorage(), calls, done)
    _drive(pipeline, done)
    # Each stage handed off to the next without knowing about it; on_complete
    # fired exactly once, at the end.
    assert calls == [*NAMES, "complete"]


def test_resumes_at_first_stage_with_a_missing_artifact():
    calls: list[str] = []
    done = asyncio.Event()
    # download + transcribe already produced their outputs.
    storage = FakeStorage({OUTPUTS["download"], OUTPUTS["transcribe"]})
    pipeline = _pipeline(storage, calls, done)
    _drive(pipeline, done)
    # Skips the finished stages, resumes at chapters, runs the rest.
    assert calls == ["chapters", "encode", "upload", "complete"]


def test_does_nothing_when_all_artifacts_present():
    calls: list[str] = []
    done = asyncio.Event()
    storage = FakeStorage(set(OUTPUTS.values()))
    pipeline = _pipeline(storage, calls, done)

    async def scenario():
        tasks = pipeline.spawn()
        await pipeline.start(JOB)
        await asyncio.sleep(0.05)  # give any wrongly-enqueued work a chance to run
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())
    assert calls == []
    assert not done.is_set()


def test_failing_stage_stops_the_pipeline():
    calls: list[str] = []
    done = asyncio.Event()
    reached = asyncio.Event()
    pipeline = _pipeline(
        FakeStorage(), calls, done, raise_at="transcribe", reached=reached
    )

    async def scenario():
        tasks = pipeline.spawn()
        await pipeline.start(JOB)
        await asyncio.wait_for(reached.wait(), 1.0)
        await asyncio.sleep(0.05)  # let any (unexpected) downstream work run
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())
    # transcribe raised, so it neither advanced to chapters nor completed.
    assert calls == ["download", "transcribe"]
    assert not done.is_set()


def test_duplicate_start_is_deduplicated():
    """Calling start twice with the same job key while the job is still in flight
    should enqueue it only once — the second call is a no-op."""
    calls: list[str] = []
    done = asyncio.Event()
    pipeline = _pipeline(FakeStorage(), calls, done)

    async def scenario():
        # Don't spawn workers — just test start's dedup logic directly.
        await pipeline.start(JOB)
        # The job is now in the download queue and tracked as in-flight.
        assert _job_key(JOB) in pipeline._in_flight
        assert pipeline._queues["download"].qsize() == 1

        # Second start with the same job: should be a no-op.
        await pipeline.start(JOB)
        assert pipeline._queues["download"].qsize() == 1  # still 1, not 2

        # A different episode should still be enqueued normally.
        other = {"feed_id": "f", "episode_id": "other", "source_url": "u"}
        await pipeline.start(other)
        assert _job_key(other) in pipeline._in_flight
        assert pipeline._queues["download"].qsize() == 2

    asyncio.run(scenario())


def test_in_flight_cleared_on_completion():
    """When a job completes the full pipeline, its in-flight slot is released
    so a later start can re-process it (e.g. after a source change)."""
    calls: list[str] = []
    done = asyncio.Event()
    pipeline = _pipeline(FakeStorage(), calls, done)

    async def scenario():
        tasks = pipeline.spawn()
        await pipeline.start(JOB)
        await asyncio.wait_for(done.wait(), 1.0)
        assert _job_key(JOB) not in pipeline._in_flight
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())
    assert calls == [*NAMES, "complete"]


def test_in_flight_cleared_on_failure():
    """When a stage raises, the in-flight slot is released so the episode can
    be retried on a later feed refresh."""
    calls: list[str] = []
    done = asyncio.Event()
    reached = asyncio.Event()
    pipeline = _pipeline(
        FakeStorage(), calls, done, raise_at="transcribe", reached=reached
    )

    async def scenario():
        tasks = pipeline.spawn()
        await pipeline.start(JOB)
        await asyncio.wait_for(reached.wait(), 1.0)
        await asyncio.sleep(0.05)
        # The job failed, so its in-flight slot should be released.
        assert _job_key(JOB) not in pipeline._in_flight
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_build_uses_fair_queue():
    async def noop(job):
        pass

    pipeline = build_media_pipeline(
        storage=FakeStorage(), work=FakeStorage(),
        settings=_settings(), on_complete=noop,
    )
    assert all(isinstance(q, FairQueue) for q in pipeline._queues.values())


def test_fair_pipeline_interleaves_two_podcasts():
    # With fair scheduling, two podcasts' episodes interleave through a
    # concurrency-1 stage instead of one podcast draining entirely first.
    order: list[str] = []

    async def record(job):
        order.append(job["feed_id"])

    async def noop(job):
        pass

    stages = [
        Stage(
            "download",
            record,
            lambda j: f"src/{j['feed_id']}/{j['episode_id']}",
            FakeStorage(),
        )
    ]
    pipeline = Pipeline(
        stages,
        on_complete=noop,
        job_key=lambda j: f"{j['feed_id']}/{j['episode_id']}",
        queue_factory=lambda: FairQueue(key=lambda job: job["feed_id"]),
    )

    async def scenario():
        # Podcast A's whole burst is enqueued before podcast B's.
        for ep in ("a1", "a2", "a3"):
            await pipeline.start({"feed_id": "A", "episode_id": ep})
        for ep in ("b1", "b2", "b3"):
            await pipeline.start({"feed_id": "B", "episode_id": ep})
        tasks = pipeline.spawn()
        await asyncio.sleep(0.05)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())
    assert order == ["A", "B", "A", "B", "A", "B"]
