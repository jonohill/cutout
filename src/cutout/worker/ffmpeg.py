"""ffmpeg/ffprobe helpers for the media pipeline.

These shell out to the ffmpeg toolchain (installed in the container image; see
the Dockerfile), run asynchronously so the worker's event loop keeps draining
other stages while an encode runs, and raise ``FfmpegError`` on a non-zero exit
with the captured stderr so a failed stage's diagnosis isn't lost.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Literal

from .edit import plan_cuts

logger = logging.getLogger(__name__)

# Largest Opus bitrate we bother with. Mono speech is transparent well below
# this, so capping here stops a short-but-bulky source from being re-encoded
# larger than it needs. Longer episodes get a lower bitrate derived from their
# duration (below) so the result still fits the size target.
_MAX_BITRATE = 32_000

# Rate below which it's a lost cause (ep just too long)
_MIN_BITRATE = 6_000

# webm has a fair bit of overhead, but we live with it because that's what the OpenAI api supports
_WEBM_OVERHEAD_BPS = 3_000

# Output sample-rate cap for the published audio. Voice fits comfortably below
# this; downsampling from 44.1/48 kHz keeps the AAC bitrate spent on audible
# content.
_MAX_OUTPUT_SAMPLE_RATE = 32_000

# Default VBR quality when no explicit bitrate is configured. libfdk_aac's VBR
# runs 1 (smallest) to 5 (largest); a mid target suits speech at the 32 kHz
# sample rate. The native encoder has no comparable VBR, so it falls back to a
# quality scale (best-effort — production uses libfdk_aac; see the Dockerfile).
_DEFAULT_FDK_VBR = 3
_DEFAULT_NATIVE_AAC_QSCALE = 1.2

Codec = Literal["aac", "xhe-aac"]

_XHE_ENCODER = "libfdk_xhe_aac"
# libfdk_xhe_aac rejects every other rate.
_XHE_SAMPLE_RATES = (48_000, 44_100)
# Lowest rates that sound acceptable for speech.
_DEFAULT_XHE_KBPS = {1: 24, 2: 32}

_encoders_cache: bytes | None = None


class FfmpegError(RuntimeError):
    """An ffmpeg or ffprobe invocation exited non-zero."""


async def _run(*args: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise FfmpegError(
            f"{args[0]} exited {proc.returncode}: "
            f"{stderr.decode(errors='replace').strip()}"
        )
    return stdout


async def probe_duration(path: Path) -> float:
    """Return ``path``'s duration in seconds, via ffprobe."""
    out = await _run(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    )
    text = out.decode().strip()
    try:
        return float(text)
    except ValueError as exc:
        raise FfmpegError(f"ffprobe gave no duration for {path}: {text!r}") from exc


async def compress_audio(src: Path, dst: Path, target_bytes: int) -> None:
    """Re-encode ``src`` to mono-Opus webm at ``dst``, aiming to fit ``target_bytes``.

    webm is the container because it is the one Opus-capable format OpenAI's
    transcription API documents as accepted (alongside mp3/mp4/m4a/wav/...); Ogg
    would be smaller but isn't on that list.
    """
    duration = await probe_duration(src)
    if duration <= 0:
        raise FfmpegError(f"cannot target a size for zero-length audio: {src}")
    # Total bits/sec the file may spend, then hand the audio only what's left
    # once the container takes its (roughly fixed) framing cut.
    budget_bps = int(target_bytes * 8 / duration)
    bitrate = max(_MIN_BITRATE, min(budget_bps - _WEBM_OVERHEAD_BPS, _MAX_BITRATE))
    logger.info(
        "compress: %s -> %s (%.0fs, %d bps audio, target %d bytes)",
        src,
        dst,
        duration,
        bitrate,
        target_bytes,
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    await _run(
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-c:a",
        "libopus",
        "-b:a",
        str(bitrate),
        "-vbr",
        "off",
        "-f",
        "webm",
        str(dst),
    )


async def _encoders() -> bytes:
    global _encoders_cache
    if _encoders_cache is None:
        _encoders_cache = await _run("ffmpeg", "-hide_banner", "-encoders")
    return _encoders_cache


async def _aac_encoder(codec: Codec) -> str:
    """The ffmpeg encoder for ``codec``.

    For ``aac``: ``libfdk_aac`` if this build has it, else the native ``aac``.
    For ``xhe-aac``: ``libfdk_xhe_aac``, raising ``FfmpegError`` if the build
    lacks it rather than silently publishing plain AAC.
    """
    out = await _encoders()
    if codec == "xhe-aac":
        if _XHE_ENCODER.encode() not in out:
            raise FfmpegError(f"xhe-aac requested but ffmpeg has no {_XHE_ENCODER}")
        return _XHE_ENCODER
    return "libfdk_aac" if b"libfdk_aac" in out else "aac"


def _aac_quality_args(
    encoder: str, bitrate_kbps: int | None, channels: int
) -> list[str]:
    """The encoder args selecting the target quality.

    A configured bitrate pins the output to that target. Otherwise AAC uses VBR
    at a high-quality default (libfdk's VBR, or the native encoder's quality
    scale as a fallback) and xHE-AAC uses a per-channel-count bitrate."""
    if bitrate_kbps is not None:
        return ["-b:a", f"{bitrate_kbps}k"]
    if encoder == _XHE_ENCODER:
        return ["-b:a", f"{_DEFAULT_XHE_KBPS[min(max(channels, 1), 2)]}k"]
    if encoder == "libfdk_aac":
        return ["-vbr", str(_DEFAULT_FDK_VBR)]
    return ["-q:a", str(_DEFAULT_NATIVE_AAC_QSCALE)]


async def _probe_audio(path: Path) -> tuple[int | None, int | None]:
    """Return ``path``'s first audio stream's (sample rate, channel count), via
    ffprobe. Either is ``None`` when ffprobe doesn't report it."""
    out = await _run(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate,channels",
        "-of",
        "default=noprint_wrappers=1",
        str(path),
    )
    fields = dict(
        line.split("=", 1) for line in out.decode().splitlines() if "=" in line
    )

    def as_int(key: str) -> int | None:
        try:
            return int(fields[key])
        except (KeyError, ValueError):
            return None

    return as_int("sample_rate"), as_int("channels")


def _output_sample_rate(encoder: str, source_rate: int | None) -> int:
    if encoder == _XHE_ENCODER:
        # USAC resamples internally to its core rate, so the AAC cap would only
        # be rejected.
        if source_rate is None or source_rate >= _XHE_SAMPLE_RATES[0]:
            return _XHE_SAMPLE_RATES[0]
        return _XHE_SAMPLE_RATES[1]
    # Unknown rate: use the cap so we never blindly upsample.
    return min(source_rate or _MAX_OUTPUT_SAMPLE_RATE, _MAX_OUTPUT_SAMPLE_RATE)


async def _probe_has_cover(path: Path) -> bool:
    """Whether ``path`` carries cover art (an attached-picture video stream)."""
    out = await _run(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    )
    return bool(out.decode().strip())


def _build_filtergraph(segments: list[tuple[int, int | None]]) -> str:
    """Build the ``filter_complex`` that splices ``segments`` together.

    Everything happens on *decoded* audio: the input is split, each segment is
    trimmed to its source-time range and rebased to start at zero, then the
    pieces are concatenated. Cutting in the PCM domain (rather than splicing
    compressed frames) is what keeps the joins clean — no MP3 bit-reservoir
    garbage or AAC priming/padding artefacts at the cut points. A single encode
    follows, so there is one set of priming samples, at the very start.
    """

    def trim(label_in: str, label_out: str, start: int, end: int | None) -> str:
        expr = f"[{label_in}]atrim=start={start}"
        if end is not None:
            expr += f":end={end}"
        return expr + f",asetpts=PTS-STARTPTS[{label_out}]"

    n = len(segments)
    if n == 1:
        start, end = segments[0]
        return trim("0:a", "aout", start, end)

    split_labels = [f"s{i}" for i in range(n)]
    parts = ["[0:a]asplit=" + str(n) + "".join(f"[{s}]" for s in split_labels)]
    seg_labels = []
    for i, (start, end) in enumerate(segments):
        parts.append(trim(split_labels[i], f"a{i}", start, end))
        seg_labels.append(f"[a{i}]")
    parts.append("".join(seg_labels) + f"concat=n={n}:v=0:a=1[aout]")
    return ";".join(parts)


def _escape_ffmetadata(value: str) -> str:
    """Escape a value for an ffmetadata file (``=``, ``;``, ``#``, ``\\`` and
    newlines are special and must be backslash-escaped)."""
    out = []
    for ch in value:
        if ch in "=;#\\\n":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _ffmetadata(chapters: list[dict]) -> str:
    """Render the kept chapters as an ffmetadata document ffmpeg maps into the
    output's chapter track."""
    lines = [";FFMETADATA1"]
    for ch in chapters:
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={ch['start'] * 1000}",
            f"END={ch['end'] * 1000}",
            f"title={_escape_ffmetadata(ch['title'])}",
        ]
    return "\n".join(lines) + "\n"


async def cut_audio(
    src: Path,
    dst: Path,
    chapters: list[dict],
    *,
    bitrate_kbps: int | None = None,
    codec: Codec = "aac",
) -> None:
    """Cut the ad chapters out of ``src`` and write the result to ``dst`` as an
    M4A — re-encoded to AAC, with a chapter track and the source's metadata and
    artwork carried over.

    ``chapters`` is the chapters-stage output (``{start, end, title, is_ad}``,
    whole seconds). Long ad runs are dropped; the survivors are spliced in the
    decoded (PCM) domain and encoded once, so the joins are free of the
    frame-boundary artefacts that plague compressed-domain cutting. The output
    is downsampled to <=32 kHz; ``bitrate_kbps`` pins the AAC target when given,
    otherwise a high-quality VBR default is used.

    ``codec="xhe-aac"`` encodes xHE-AAC at 44.1 or 48 kHz in two passes. The
    first measures loudness so the output carries accurate loudness metadata
    for client-side normalisation.

    Raises ``NoKeptChaptersError`` (from ``plan_cuts``) if every chapter is a
    long-enough ad to cut.
    """
    duration = await probe_duration(src)
    plan = plan_cuts(chapters, duration)
    source_rate, channels = await _probe_audio(src)
    has_cover = await _probe_has_cover(src)
    encoder = await _aac_encoder(codec)
    sample_rate = _output_sample_rate(encoder, source_rate)
    two_pass = encoder == _XHE_ENCODER

    logger.info(
        "encode: %s -> %s (keeping %d of %d chapter(s), %d Hz, %s%s)",
        src,
        dst,
        len(plan.chapters),
        len(chapters),
        sample_rate,
        encoder,
        ", two-pass" if two_pass else "",
    )

    dst.parent.mkdir(parents=True, exist_ok=True)
    # ffmpeg infers the M4A muxer from the .m4a suffix; the rename into the
    # extensionless output key is atomic, so the pipeline's resume check never
    # sees a half-written ``encoded`` artifact.
    tmp = dst.with_name(dst.name + ".m4a")
    meta = dst.with_name(dst.name + ".ffmeta")
    # ffmpeg appends "-<output stream index>.log". The audio is mapped first,
    # so the log is always "-0.log".
    passlog = dst.with_name(dst.name + ".pass")

    audio_args = [
        "-c:a",
        encoder,
        *_aac_quality_args(encoder, bitrate_kbps, channels or 1),
        "-ar",
        str(sample_rate),
    ]
    filtergraph = _build_filtergraph(plan.segments)
    pass1 = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(src),
        "-filter_complex",
        filtergraph,
        "-map",
        "[aout]",
        *audio_args,
        "-pass",
        "1",
        "-passlogfile",
        str(passlog),
        "-f",
        "null",
        "-",
    ]

    args = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(src),
        "-i",
        str(meta),
        "-filter_complex",
        filtergraph,
        "-map",
        "[aout]",
    ]
    if has_cover:
        # Carry the cover image straight through, tagged as the attached picture.
        args += ["-map", "0:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    args += [
        "-map_metadata",
        "0",  # title/artist/album/... from the source
        "-map_chapters",
        "1",  # chapters from the ffmetadata input
        *audio_args,
    ]
    if two_pass:
        args += ["-pass", "2", "-passlogfile", str(passlog)]
    args += ["-movflags", "+faststart", str(tmp)]
    try:
        meta.write_text(_ffmetadata(plan.chapters), encoding="utf-8")
        if two_pass:
            await _run(*pass1)
        await _run(*args)
        os.replace(tmp, dst)
    finally:
        meta.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
        passlog.with_name(passlog.name + "-0.log").unlink(missing_ok=True)
