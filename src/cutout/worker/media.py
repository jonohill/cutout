"""Media stage handlers — the per-stage work of the cut-out pipeline.

Each method does exactly one stage and persists that stage's output artifact;
none of them knows which stage runs next. Ordering and hand-off live entirely
in ``pipeline.py`` (the ``Pipeline`` orchestrator), so stages stay ignorant of
one another. ``upload`` writes the finished audio to the object store directly.
Per stage:

  download   -> fetch ``source_url`` to ``work_path(.., "source")``
  transcribe -> transcribe the audio to ``work_path(.., "transcript.json")``
  chapters   -> mark chapters (incl. ads) to ``work_path(.., "chapters.json")``
  encode     -> cut the ads + re-encode to ``work_path(.., "encoded")``
  upload     -> publish the result to ``audio_path(feed_id, episode_id)``

Once ``upload`` writes the served audio, the Pipeline's ``on_complete`` re-runs
feed reconciliation so the rewritten feed links to it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from ..common import audio_path, work_path
from ..common.storage import LocalStorage, Storage
from ..config import Settings
from .chapters import generate_chapters
from .ffmpeg import compress_audio, cut_audio
from .fetch import post_transcription, stream_download

logger = logging.getLogger(__name__)

# Yields a URL's body in chunks; injectable so tests don't hit the network.
Downloader = Callable[[str], AsyncIterator[bytes]]

# Re-encode an oversized source (src, dst, target_bytes) to fit the endpoint's
# upload cap; injectable so tests don't shell out to ffmpeg.
Compressor = Callable[[Path, Path, int], Awaitable[None]]

# Upload an audio file to the transcription endpoint, returning the parsed
# transcript JSON; injectable so tests don't hit the network.
Transcriber = Callable[..., Awaitable[dict]]

# Ask the chapter model to mark chapters (incl. ads) from the transcript's timed
# segments, returning ``{start, end, title, is_ad}`` dicts; injectable so tests
# don't hit the network.
ChapterMaker = Callable[..., Awaitable[list[dict]]]

# Cut the ad chapters out of a source and write the encoded M4A (src, dst,
# chapters, bitrate_kbps); injectable so tests don't shell out to ffmpeg.
CutAudio = Callable[..., Awaitable[None]]

# Aim the re-encode a little under the hard cap. The compressor already models
# CBR audio plus the webm container's framing overhead, so this is just a small
# extra safety margin (e.g. for the trailing cues index a very long file grows).
_SIZE_HEADROOM = 0.95

# Cut episodes never change once published, so let clients and any CDN cache
# them hard.
_AUDIO_CACHE_CONTROL = "public, max-age=604800, immutable"

# The encode stage writes AAC in an MP4 container.
_AUDIO_CONTENT_TYPE = "audio/mp4"


class MediaWorker:
    """Holds the stage handlers; one instance is shared across all stages.

    Intermediate stages read and write files in local working storage
    (``work``); only ``upload`` publishes to remote object storage
    (``storage``). Grouping the handlers on one object keeps these shared
    dependencies in one place without coupling the stages to each other.
    """

    def __init__(
        self,
        *,
        work: LocalStorage,
        storage: Storage,
        settings: Settings,
        download_stream: Downloader = stream_download,
        compress_audio: Compressor = compress_audio,
        transcribe_post: Transcriber = post_transcription,
        chapters_generate: ChapterMaker = generate_chapters,
        cut_audio: CutAudio = cut_audio,
    ) -> None:
        self._work = work
        self._storage = storage
        self._settings = settings
        self._download_stream = download_stream
        self._compress_audio = compress_audio
        self._transcribe_post = transcribe_post
        self._generate_chapters = chapters_generate
        self._cut_audio = cut_audio

    async def download(self, job: dict) -> None:
        """Stream the episode's source audio into the local ``source`` artifact.

        ``open_write`` only publishes the file once the whole stream has landed,
        so an interrupted download leaves no ``source`` key and the pipeline
        simply re-runs this stage rather than handing a truncated file to
        ``transcribe``. The file lands on disk for the later stages' tools.
        """
        source_url = job["source_url"]
        key = work_path(job["feed_id"], job["episode_id"], "source")
        logger.info("download: %s -> %s", source_url, key)
        async with self._work.open_write(key) as out:
            async for chunk in self._download_stream(source_url):
                out.write(chunk)
        logger.info("download: stored %s", key)

    async def transcribe(self, job: dict) -> None:
        """Transcribe the source audio into the ``transcript.json`` artifact.

        The configured endpoint caps upload size; anything larger is first
        re-encoded to mono-Opus webm aimed just under the cap (Opus is tiny for
        speech, so even a multi-hour episode fits). The endpoint's
        ``verbose_json`` reply — segment timings and all — is stored verbatim
        for the chapters stage to read.
        """
        settings = self._settings
        source = self._work.path(
            work_path(job["feed_id"], job["episode_id"], "source")
        )

        upload = source
        size = source.stat().st_size
        limit = settings.transcribe_max_bytes
        if size > limit:
            target = int(limit * _SIZE_HEADROOM)
            upload = self._work.path(
                work_path(job["feed_id"], job["episode_id"], "audio.webm")
            )
            logger.info(
                "transcribe: source %d bytes over %d limit; compressing to %s",
                size, limit, upload,
            )
            await self._compress_audio(source, upload, target)

        logger.info("transcribe: uploading %s to %s", upload, settings.transcribe_url)
        transcript = await self._transcribe_post(
            upload,
            url=settings.transcribe_url,
            model=settings.transcribe_model,
            api_key=settings.transcribe_api_key,
        )

        key = work_path(job["feed_id"], job["episode_id"], "transcript.json")
        await self._work.put_bytes(
            key,
            json.dumps(transcript).encode(),
            content_type="application/json",
        )
        logger.info("transcribe: stored %s", key)

    async def chapters(self, job: dict) -> None:
        """Mark chapters (incl. ads) from the transcript into ``chapters.json``.

        Stored shape is ``{start, end, title, is_ad}`` with whole-second times.
        """
        settings = self._settings
        transcript_key = work_path(
            job["feed_id"], job["episode_id"], "transcript.json"
        )
        raw = await self._work.get_bytes(transcript_key)
        if raw is None:
            raise FileNotFoundError(f"transcribe output missing: {transcript_key}")
        segments = json.loads(raw).get("segments") or []

        logger.info("chapters: generating from %d segment(s)", len(segments))
        chapters = await self._generate_chapters(
            segments,
            url=settings.chapters_url,
            model=settings.chapters_model,
            api_key=settings.chapters_api_key,
            language=settings.chapters_language,
            context=job.get("context"),
        )

        key = work_path(job["feed_id"], job["episode_id"], "chapters.json")
        await self._work.put_bytes(
            key,
            json.dumps(chapters).encode(),
            content_type="application/json",
        )
        ads = sum(1 for c in chapters if c["is_ad"])
        logger.info(
            "chapters: stored %d chapter(s) (%d ad) -> %s", len(chapters), ads, key
        )

    async def encode(self, job: dict) -> None:
        """Cut the ad chapters out of the source and write the ``encoded`` M4A.

        Feeds the marked chapter list to ``cut_audio``, which does the splice and
        re-encode; the result is the published audio for the upload stage.
        """
        chapters_key = work_path(
            job["feed_id"], job["episode_id"], "chapters.json"
        )
        raw = await self._work.get_bytes(chapters_key)
        if raw is None:
            raise FileNotFoundError(f"chapters output missing: {chapters_key}")
        chapters = json.loads(raw)

        source = self._work.path(
            work_path(job["feed_id"], job["episode_id"], "source")
        )
        dst = self._work.path(
            work_path(job["feed_id"], job["episode_id"], "encoded")
        )
        logger.info("encode: cutting %s from %d chapter(s)", source, len(chapters))
        await self._cut_audio(
            source,
            dst,
            chapters,
            bitrate_kbps=self._settings.encode_bitrate_kbps,
        )
        logger.info("encode: stored %s", dst)

    async def upload(self, job: dict) -> None:
        """Publish the encoded audio as the served file in remote object storage.

        The encode stage left the finished M4A in local working storage; this
        streams it up to ``audio_path(feed_id, episode_id)`` — the durable,
        served result. Writing that key is exactly what the pipeline's resume
        check and the feed reconciler read as "this episode is done", so it is
        the last thing to happen: an interrupted upload leaves no key and the
        stage simply re-runs. Streamed straight off disk so a large episode
        never sits whole in memory.
        """
        encoded = self._work.path(
            work_path(job["feed_id"], job["episode_id"], "encoded")
        )
        if not encoded.is_file():
            raise FileNotFoundError(f"encode output missing: {encoded}")

        key = audio_path(job["feed_id"], job["episode_id"])
        logger.info("upload: %s -> %s", encoded, key)
        with encoded.open("rb") as body:
            await self._storage.put(
                key,
                body,
                cache_control=_AUDIO_CACHE_CONTROL,
                content_type=_AUDIO_CONTENT_TYPE,
            )
        logger.info("upload: stored %s", key)
