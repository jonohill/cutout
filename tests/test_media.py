import asyncio
import json

import pytest

from cutout.common import audio_path, work_path
from cutout.common.storage import LocalStorage
from cutout.config import Settings
from cutout.worker.media import MediaWorker

FEED_ID = "f"
EPISODE_ID = "e"
SOURCE_URL = "https://src.example/ep.mp3"
SOURCE_KEY = work_path(FEED_ID, EPISODE_ID, "source")
WEBM_KEY = work_path(FEED_ID, EPISODE_ID, "audio.webm")
TRANSCRIPT_KEY = work_path(FEED_ID, EPISODE_ID, "transcript.json")
CHAPTERS_KEY = work_path(FEED_ID, EPISODE_ID, "chapters.json")
ENCODED_KEY = work_path(FEED_ID, EPISODE_ID, "encoded")
JOB = {"feed_id": FEED_ID, "episode_id": EPISODE_ID, "source_url": SOURCE_URL}


def _settings(**overrides):
    return Settings(
        s3_access_key_id="x",
        s3_secret_access_key="y",
        **overrides,
    )


def _worker(work, download_stream=None, *, settings=None, **handlers):
    # ``storage`` (remote) is unused until the later upload stage; the stage
    # handlers (download/compress/transcribe) are injectable so tests don't hit
    # the network or shell out to ffmpeg.
    kwargs = {k: v for k, v in handlers.items() if v is not None}
    if download_stream is not None:
        kwargs["download_stream"] = download_stream
    return MediaWorker(
        work=work,
        storage=None,
        settings=settings or _settings(),
        **kwargs,
    )


def _write_source(tmp_path, data):
    path = tmp_path / SOURCE_KEY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_transcript(tmp_path, transcript):
    path = tmp_path / TRANSCRIPT_KEY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(transcript))


def _write_chapters(tmp_path, chapters):
    path = tmp_path / CHAPTERS_KEY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(chapters))


def test_download_streams_source_into_local_source_artifact(tmp_path):
    work = LocalStorage(tmp_path)

    async def download_stream(url):
        assert url == SOURCE_URL
        for chunk in (b"abc", b"def", b"ghi"):
            yield chunk

    asyncio.run(_worker(work, download_stream).download(JOB))

    # Lands as a real file on disk for the later stages' tools to read,
    # and is recorded as done for the pipeline's resume check.
    assert (tmp_path / SOURCE_KEY).read_bytes() == b"abcdefghi"
    assert asyncio.run(work.head(SOURCE_KEY)) == {}


def test_download_leaves_no_artifact_when_stream_fails_partway(tmp_path):
    work = LocalStorage(tmp_path)

    async def download_stream(url):
        yield b"partial"
        raise RuntimeError("connection dropped")

    with pytest.raises(RuntimeError):
        asyncio.run(_worker(work, download_stream).download(JOB))

    # No source key and no stray .part file, so the resume check re-runs
    # download rather than feeding a truncated file downstream.
    assert asyncio.run(work.head(SOURCE_KEY)) is None
    assert not any(p.is_file() for p in tmp_path.rglob("*"))


TRANSCRIPT = {"text": "hello", "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}]}


def test_transcribe_uploads_source_unchanged_when_within_limit(tmp_path):
    work = LocalStorage(tmp_path)
    _write_source(tmp_path, b"small audio")

    posted = {}

    async def transcribe_post(path, *, url, model, api_key):
        posted.update(path=path, url=url, model=model, api_key=api_key)
        return TRANSCRIPT

    async def compress(src, dst, target):
        raise AssertionError("must not compress audio that already fits")

    settings = _settings(
        transcribe_url="https://stt.example/v1/audio/transcriptions",
        transcribe_model="whisper-x",
        transcribe_api_key="secret",
    )
    worker = _worker(
        work, settings=settings, transcribe_post=transcribe_post, compress_audio=compress
    )

    asyncio.run(worker.transcribe(JOB))

    # Uploaded the source itself (no re-encode) with the configured endpoint,
    # model and optional auth forwarded.
    assert posted["path"] == tmp_path / SOURCE_KEY
    assert posted["url"] == "https://stt.example/v1/audio/transcriptions"
    assert posted["model"] == "whisper-x"
    assert posted["api_key"] == "secret"
    # The verbose_json reply is stored verbatim for the chapters stage.
    assert json.loads((tmp_path / TRANSCRIPT_KEY).read_bytes()) == TRANSCRIPT
    assert asyncio.run(work.head(TRANSCRIPT_KEY)) == {}


def test_transcribe_compresses_oversized_source_then_uploads_the_webm(tmp_path):
    work = LocalStorage(tmp_path)
    # max_mb=1 -> 1 MB cap (decimal); make the source just over it.
    limit = 1 * 1000 * 1000
    _write_source(tmp_path, b"\0" * (limit + 1))

    compressed = {}

    async def compress(src, dst, target):
        compressed.update(src=src, dst=dst, target=target)
        dst.write_bytes(b"opus")  # stand in for the re-encoded audio

    posted = {}

    async def transcribe_post(path, *, url, model, api_key):
        posted["path"] = path
        return TRANSCRIPT

    worker = _worker(
        work,
        settings=_settings(transcribe_max_mb=1),
        transcribe_post=transcribe_post,
        compress_audio=compress,
    )

    asyncio.run(worker.transcribe(JOB))

    # Compressed the source into the webm artifact, aiming under the cap, then
    # uploaded that webm rather than the oversized source.
    assert compressed["src"] == tmp_path / SOURCE_KEY
    assert compressed["dst"] == tmp_path / WEBM_KEY
    assert compressed["target"] < limit
    assert posted["path"] == tmp_path / WEBM_KEY
    assert json.loads((tmp_path / TRANSCRIPT_KEY).read_bytes()) == TRANSCRIPT


def test_chapters_feeds_transcript_segments_to_the_model_and_stores_the_result(tmp_path):
    work = LocalStorage(tmp_path)
    transcript = {
        "text": "...",
        "segments": [
            {"start": 0.0, "end": 30.0, "text": "Intro"},
            {"start": 30.0, "end": 90.0, "text": "A sponsor read"},
        ],
    }
    _write_transcript(tmp_path, transcript)

    chapters = [
        {"start": 0, "end": 30, "title": "Intro", "is_ad": False},
        {"start": 30, "end": 90, "title": "Sponsor", "is_ad": True},
    ]
    seen = {}

    async def generate(segments, *, url, model, api_key, language):
        seen.update(
            segments=segments, url=url, model=model, api_key=api_key, language=language
        )
        return chapters

    settings = _settings(
        chapters_url="https://g.example/openai/chat/completions",
        chapters_api_key="key",
        chapters_model="gemini-x",
    )
    worker = _worker(work, settings=settings, chapters_generate=generate)

    asyncio.run(worker.chapters(JOB))

    # Fed the transcript's segments through, with the configured endpoint,
    # model and key.
    assert seen["segments"] == transcript["segments"]
    assert seen["url"] == "https://g.example/openai/chat/completions"
    assert seen["model"] == "gemini-x"
    assert seen["api_key"] == "key"
    # Stored the chapter list verbatim for the encode stage, recorded as done.
    assert json.loads((tmp_path / CHAPTERS_KEY).read_bytes()) == chapters
    assert asyncio.run(work.head(CHAPTERS_KEY)) == {}


CHAPTERS = [
    {"start": 0, "end": 200, "title": "Intro", "is_ad": False},
    {"start": 200, "end": 260, "title": "Sponsor", "is_ad": True},
    {"start": 260, "end": 519, "title": "Outro", "is_ad": False},
]


def test_encode_cuts_source_using_chapters_and_records_done(tmp_path):
    work = LocalStorage(tmp_path)
    _write_source(tmp_path, b"audio")
    _write_chapters(tmp_path, CHAPTERS)

    captured = {}

    async def cut_audio(src, dst, chapters, *, bitrate_kbps):
        captured.update(src=src, dst=dst, chapters=chapters, bitrate_kbps=bitrate_kbps)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"m4a")  # stand in for the encoded output

    asyncio.run(_worker(work, cut_audio=cut_audio).encode(JOB))

    # Cut the downloaded source into the encoded artifact using the marked
    # chapters; no bitrate configured -> high-quality VBR default.
    assert captured["src"] == tmp_path / SOURCE_KEY
    assert captured["dst"] == tmp_path / ENCODED_KEY
    assert captured["chapters"] == CHAPTERS
    assert captured["bitrate_kbps"] is None
    assert asyncio.run(work.head(ENCODED_KEY)) == {}


def test_encode_forwards_configured_bitrate(tmp_path):
    work = LocalStorage(tmp_path)
    _write_source(tmp_path, b"audio")
    _write_chapters(tmp_path, CHAPTERS)

    captured = {}

    async def cut_audio(src, dst, chapters, *, bitrate_kbps):
        captured["bitrate_kbps"] = bitrate_kbps
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"m4a")

    worker = _worker(
        work, settings=_settings(encode_bitrate_kbps=64), cut_audio=cut_audio
    )
    asyncio.run(worker.encode(JOB))

    assert captured["bitrate_kbps"] == 64


def test_encode_raises_when_chapters_missing(tmp_path):
    work = LocalStorage(tmp_path)
    _write_source(tmp_path, b"audio")

    async def cut_audio(src, dst, chapters, *, bitrate_kbps):
        raise AssertionError("must not encode without a chapter list")

    with pytest.raises(FileNotFoundError):
        asyncio.run(_worker(work, cut_audio=cut_audio).encode(JOB))


class _FakeRemote:
    """Captures the single upload the upload stage makes to remote storage."""

    def __init__(self):
        self.put_args: dict = {}

    async def put(self, key, body, *, cache_control=None, content_type=None):
        body.seek(0)
        self.put_args = {
            "key": key,
            "data": body.read(),
            "cache_control": cache_control,
            "content_type": content_type,
        }


def _write_encoded(tmp_path, data):
    path = tmp_path / ENCODED_KEY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_upload_streams_encoded_audio_to_the_served_key(tmp_path):
    work = LocalStorage(tmp_path)
    _write_encoded(tmp_path, b"m4a-bytes")
    remote = _FakeRemote()

    worker = MediaWorker(work=work, storage=remote, settings=_settings())
    asyncio.run(worker.upload(JOB))

    # Published the encode stage's M4A to the served-audio key, cacheable and
    # typed so a CDN/client treats it as immutable audio.
    assert remote.put_args["key"] == audio_path(FEED_ID, EPISODE_ID)
    assert remote.put_args["data"] == b"m4a-bytes"
    assert remote.put_args["cache_control"] == "public, max-age=604800, immutable"
    assert remote.put_args["content_type"] == "audio/mp4"


def test_upload_raises_when_encoded_missing(tmp_path):
    work = LocalStorage(tmp_path)

    class _NoPut:
        async def put(self, *args, **kwargs):
            raise AssertionError("must not upload without an encoded file")

    worker = MediaWorker(work=work, storage=_NoPut(), settings=_settings())
    with pytest.raises(FileNotFoundError):
        asyncio.run(worker.upload(JOB))
