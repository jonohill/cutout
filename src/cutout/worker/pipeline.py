"""The media pipeline: the one place that knows the stages and their order.

A job flows download -> transcribe -> chapters -> encode -> upload. Stage
handlers (``media.py``) each do one stage and never name another; this module
owns every "what comes next" decision, so adding, reordering or retuning a
stage is a one-line edit to ``_STAGES`` and nothing else changes.

Each stage has its own queue, so a backlog in one stage (say a slow transcribe)
can't starve another, and ``concurrency`` can be tuned per stage independently.

State lives in storage, not memory: each stage's output artifact records that
the stage is done. ``start`` therefore resumes a job at the first stage whose
artifact is missing, so a restart re-runs only the unfinished tail rather than
redoing an expensive download/transcribe from scratch. A stage's output may
live in *local working storage* (the intermediate media files the next stage's
tools read) or in *remote object storage* (the final, served audio), so each
``Stage`` carries the ``store`` its artifact belongs to. Once the last stage
finishes, the episode's local working files are cleaned up.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..common import audio_path, work_path
from ..common.storage import LocalStorage, Storage
from ..config import Settings
from ..draining import Handler, spawn_drainers
from ..fair_queue import FairQueue
from .media import MediaWorker

logger = logging.getLogger(__name__)

# A job -> the storage key the stage produces. Existence of that key means the
# stage is done; this is what makes the pipeline resumable.
OutputKey = Callable[[dict], str]

# Called once a job clears the final stage (e.g. to re-run feed reconciliation).
# ``last`` is True when no other episode of the same feed is still in flight —
# i.e. this completes a batch of new episodes.
OnComplete = Callable[[dict, bool], Awaitable[None]]

QueueFactory = Callable[[], Any]


@dataclass(frozen=True)
class Stage:
    name: str
    handler: Handler  # does the work + persists ``output``; never enqueues
    output: OutputKey
    store: Storage  # where ``output`` lives — local working or remote object storage
    concurrency: int = 1


class Pipeline:
    """Owns stage ordering, hand-off between stages, and resume-on-restart.

    Handlers are wrapped so that after one returns, the *pipeline* — not the
    handler — enqueues the next stage (or calls ``on_complete`` at the end).
    """

    def __init__(
        self,
        stages: list[Stage],
        *,
        on_complete: OnComplete,
        job_key: Callable[[dict], str],
        queue_factory: QueueFactory = asyncio.Queue,
    ) -> None:
        if not stages:
            raise ValueError("pipeline needs at least one stage")
        self._stages = list(stages)
        self._order = [s.name for s in self._stages]
        self._queues = {s.name: queue_factory() for s in self._stages}
        self._on_complete = on_complete
        self._job_key = job_key
        self._in_flight: set[str] = set()

    async def start(self, job: dict) -> None:
        """Enqueue ``job`` at the first stage whose output artifact is missing.

        Callers (the feed processor) just say "process this episode" — they
        don't know or care which stage is first, or how far a previous run got.
        Each stage's artifact is looked up in that stage's own ``store``.

        If the job is already in flight (enqueued but not yet completed),
        ``start`` is a no-op — the episode is being processed and a duplicate
        would just waste work and risk file-level races on local storage.
        """
        key = self._job_key(job)
        if key in self._in_flight:
            logger.debug("skip %s: already in flight", key)
            return
        for stage in self._stages:
            if await stage.store.head(stage.output(job)) is None:
                self._in_flight.add(key)
                logger.info("start %s at stage %s", key, stage.name)
                await self._queues[stage.name].put(job)
                return
        # Every artifact already present: the pipeline has nothing left to do.
        logger.debug("skip %s: all stages already complete", key)

    def spawn(self) -> list[asyncio.Task]:
        """Start the drain workers for every stage queue."""
        tasks: list[asyncio.Task] = []
        for stage in self._stages:
            tasks += spawn_drainers(
                self._queues[stage.name],
                self._runner(stage),
                name=stage.name,
                concurrency=stage.concurrency,
            )
        return tasks

    def _next(self, name: str) -> str | None:
        index = self._order.index(name)
        if index + 1 < len(self._order):
            return self._order[index + 1]
        return None

    def _runner(self, stage: Stage) -> Handler:
        """Wrap a stage handler so completion advances to the next stage.

        The handler raising (e.g. an unimplemented stage) propagates to
        ``drain``, which logs and drops the job — so a failed stage neither
        advances nor triggers ``on_complete``. A failed job is also removed
        from the in-flight set so it can be retried on the next feed refresh.
        """
        next_stage = self._next(stage.name)

        async def run(job: dict) -> None:
            try:
                await stage.handler(job)
            except BaseException:
                # Drop the job — drain logs the error. Release the in-flight
                # slot so a later feed refresh can retry the episode.
                self._in_flight.discard(self._job_key(job))
                raise
            if next_stage is not None:
                await self._queues[next_stage].put(job)
            else:
                # Drop this episode from in-flight *before* checking whether any
                # sibling remains, so the check is atomic: if two of a feed's
                # episodes finish back-to-back, exactly the last-to-clear sees an
                # empty remainder. (Check first and both could see the other
                # still in-flight, so neither would be "last".)
                key = self._job_key(job)
                self._in_flight.discard(key)
                feed_prefix = f"{job['feed_id']}/"
                last = not any(
                    k.startswith(feed_prefix) for k in self._in_flight
                )
                await self._on_complete(job, last)

        return run


def _source_key(job: dict) -> str:
    return work_path(job["feed_id"], job["episode_id"], "source")


def _transcript_key(job: dict) -> str:
    return work_path(job["feed_id"], job["episode_id"], "transcript.json")


def _chapters_key(job: dict) -> str:
    return work_path(job["feed_id"], job["episode_id"], "chapters.json")


def _encoded_key(job: dict) -> str:
    return work_path(job["feed_id"], job["episode_id"], "encoded")


def _audio_key(job: dict) -> str:
    return audio_path(job["feed_id"], job["episode_id"])


def _job_key(job: dict) -> str:
    """Deterministic key for an episode — the same GUID always produces the
    same ID, so a repeated call to ``start`` is correctly detected."""
    return f"{job['feed_id']}/{job['episode_id']}"


def _episode_prefix(job: dict) -> str:
    """The key prefix shared by all of an episode's working artifacts."""
    return work_path(job["feed_id"], job["episode_id"], "")


def build_media_pipeline(
    *,
    storage: Storage,
    work: LocalStorage,
    settings: Settings,
    on_complete: OnComplete,
) -> Pipeline:
    """Assemble the media pipeline. The stage list below is the single source of
    truth for the order, for what each stage produces, and for where it lands.

    The first four stages produce intermediate files in local working storage
    (``work``) — the next stage's tools read them off disk; only ``upload``
    writes to remote object storage (``storage``), the served audio.
    """
    worker = MediaWorker(work=work, storage=storage, settings=settings)

    # Every stage queue round-robins across podcasts (keyed by feed_id) so a
    # backlog of one podcast's episodes can't monopolise a stage; order within a
    # podcast (RSS order) is preserved.
    def queue_factory() -> FairQueue:
        return FairQueue(key=lambda job: job["feed_id"])

    # Each stage drains its own queue with its own worker count, so an I/O-bound
    # stage (chapters) can run many episodes at once while a CPU-bound one
    # (encode) stays serial. The per-stage knobs live in ``Settings``.
    stages = [
        Stage(
            "download",
            worker.download,
            _source_key,
            work,
            concurrency=settings.download_concurrency,
        ),
        Stage(
            "transcribe",
            worker.transcribe,
            _transcript_key,
            work,
            concurrency=settings.transcribe_concurrency,
        ),
        Stage(
            "chapters",
            worker.chapters,
            _chapters_key,
            work,
            concurrency=settings.chapters_concurrency,
        ),
        Stage(
            "encode",
            worker.encode,
            _encoded_key,
            work,
            concurrency=settings.encode_concurrency,
        ),
        Stage(
            "upload",
            worker.upload,
            _audio_key,
            storage,
            concurrency=settings.upload_concurrency,
        ),
    ]

    async def finish(job: dict, last: bool) -> None:
        # The served audio is now in remote storage. Refresh the feed so it
        # links to it, then drop the episode's local working files — they have
        # done their job and the remote audio is the durable result.
        await on_complete(job, last)
        await work.cleanup(_episode_prefix(job))

    return Pipeline(
        stages, on_complete=finish, job_key=_job_key, queue_factory=queue_factory
    )
