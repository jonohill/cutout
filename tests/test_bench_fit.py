import asyncio

from cutout.bench.__main__ import _fit_upload
from cutout.worker.media import _SIZE_HEADROOM


def _write(path, nbytes):
    path.write_bytes(b"\0" * nbytes)
    return path


def test_under_cap_uploads_source_unchanged(tmp_path):
    audio = _write(tmp_path / "ep.mp3", 1000)
    dest = tmp_path / "out"
    dest.mkdir()

    calls = []

    async def fake_compress(src, dst, target):  # pragma: no cover - must not run
        calls.append((src, dst, target))

    out = asyncio.run(
        _fit_upload(audio, dest, {"max_mb": 25}, compress=fake_compress)
    )
    assert out == audio
    assert calls == []  # nothing over the cap, so no re-encode


def test_over_cap_compresses_to_fit_target(tmp_path):
    audio = _write(tmp_path / "ep.mp3", 3_000_000)
    dest = tmp_path / "out"
    dest.mkdir()

    calls = []

    async def fake_compress(src, dst, target):
        calls.append((src, dst, target))
        dst.write_bytes(b"webm")  # stand in for the re-encoded upload

    out = asyncio.run(
        _fit_upload(audio, dest, {"max_mb": 1}, compress=fake_compress)
    )
    expected = dest / "upload.webm"
    assert out == expected
    # Aimed at the same headroom-shaved target the worker uses, off a decimal-MB cap.
    assert calls == [(audio, expected, int(1 * 1000 * 1000 * _SIZE_HEADROOM))]


def test_cached_webm_skips_recompression(tmp_path):
    audio = _write(tmp_path / "ep.mp3", 3_000_000)
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "upload.webm").write_bytes(b"cached")  # from a prior run

    async def fake_compress(src, dst, target):  # pragma: no cover - must not run
        raise AssertionError("should reuse the cached webm")

    out = asyncio.run(
        _fit_upload(audio, dest, {"max_mb": 1}, compress=fake_compress)
    )
    assert out == dest / "upload.webm"
