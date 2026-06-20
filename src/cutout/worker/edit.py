"""Cut planning — the pure (ffmpeg-free) half of the encode stage.

Decides which chapters survive (dropping long ad runs), turns the survivors
into source-time segments to splice, and remaps each kept chapter onto the
output timeline. None of this touches audio — it is plain arithmetic over the
chapter list, so it is unit-tested directly while ``ffmpeg.py`` owns the
decode/cut/encode itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Contiguous ad runs no longer than this (seconds) are kept rather than cut, so
# the audio doesn't jump on a brief, unavoidable spot.
_MAX_AD_RUN_SECONDS = 10

# Two ad chapters belong to the same run if the gap between them is at most this
# (seconds); transcript cue boundaries rarely abut exactly.
_ADJACENCY_TOLERANCE_SECONDS = 2


class NoKeptChaptersError(RuntimeError):
    """Every chapter is an ad to cut — there is nothing left to encode."""


@dataclass(frozen=True)
class CutPlan:
    """The resolved edit.

    ``segments`` are ``(start, end)`` source-time ranges to splice together, in
    order; ``end`` is ``None`` for a segment that runs to the end of the file.
    ``chapters`` are the kept chapters remapped onto the output timeline as
    ``{title, start, end}`` with whole-second times.
    """

    segments: list[tuple[int, int | None]]
    chapters: list[dict]


def keeping_short_ad_runs(
    chapters: list[dict],
    max_ad_run_seconds: int = _MAX_AD_RUN_SECONDS,
    adjacency_tolerance_seconds: int = _ADJACENCY_TOLERANCE_SECONDS,
) -> list[dict]:
    """Return the chapters worth keeping: every non-ad chapter, plus runs of
    contiguous ad chapters whose combined duration is within
    ``max_ad_run_seconds``. Two ad chapters count as one run when the gap
    between them is at most ``adjacency_tolerance_seconds``."""
    result: list[dict] = []
    i = 0
    n = len(chapters)
    while i < n:
        if not chapters[i]["is_ad"]:
            result.append(chapters[i])
            i += 1
            continue
        j = i
        while (
            j + 1 < n
            and chapters[j + 1]["is_ad"]
            and chapters[j + 1]["start"] - chapters[j]["end"]
            <= adjacency_tolerance_seconds
        ):
            j += 1
        run_duration = chapters[j]["end"] - chapters[i]["start"]
        if run_duration <= max_ad_run_seconds:
            result.extend(chapters[i : j + 1])
        i = j + 1
    return result


def _round(seconds: float) -> int:
    """Round half away from zero.

    Output times here are never negative, so this is just ``floor(x + 0.5)``.
    """
    return math.floor(seconds + 0.5)


def plan_cuts(
    chapters: list[dict],
    source_duration: float,
    *,
    max_ad_run_seconds: int = _MAX_AD_RUN_SECONDS,
) -> CutPlan:
    """Plan the splice for ``chapters`` against a source of ``source_duration``.

    Chapter boundaries come from transcript cue times, which can sit slightly
    inside the audio. So when the kept chapter *is* the source's first (or last)
    chapter and is not an ad, its segment is extended to the file's true start
    (0) or end, so no real audio is clipped at the head or tail.

    Raises ``NoKeptChaptersError`` if every chapter is a long-enough ad to cut.
    """
    kept = keeping_short_ad_runs(chapters, max_ad_run_seconds)
    if not kept:
        raise NoKeptChaptersError("all chapters are ads to cut — nothing to keep")

    first, last = chapters[0], chapters[-1]
    segments: list[tuple[int, int | None]] = []
    remapped: list[dict] = []
    cursor = 0.0
    for ch in kept:
        extend_start = (
            not ch["is_ad"]
            and ch["start"] == first["start"]
            and ch["end"] == first["end"]
        )
        extend_end = (
            not ch["is_ad"]
            and ch["start"] == last["start"]
            and ch["end"] == last["end"]
        )
        start = 0 if extend_start else ch["start"]
        if extend_end:
            segments.append((start, None))
            duration = source_duration - start
        else:
            segments.append((start, ch["end"]))
            duration = ch["end"] - start

        new_start = _round(cursor)
        cursor += duration
        remapped.append(
            {"title": ch["title"], "start": new_start, "end": _round(cursor)}
        )

    return CutPlan(segments=segments, chapters=remapped)
