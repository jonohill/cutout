"""Programmatic (no-LLM, deterministic) checks over a contender's chapters.

These grade the things that are objectively measurable from the transcript and
the raw model reply — format conformance, full coverage/tiling, timestamps that
were copied (not hallucinated), title brevity, and basic sanity — leaving the
subjective criteria (title accuracy, ad-marker correctness, recall) to the
agent judge.

Everything here is a pure function of its arguments so it is unit-testable
without the network.
"""

from __future__ import annotations

import json
import re

# A chapter boundary may legitimately sit a couple of seconds off a transcript
# cue boundary, so timestamp matching allows this slack before calling a
# boundary "hallucinated".
_TIMESTAMP_TOLERANCE_SECONDS = 2

# A title much longer than this is flagged as not-brief; the prompt asks for a
# "very brief description".
_BREVITY_WORD_LIMIT = 10
_BREVITY_CHAR_LIMIT = 60

_HMS = re.compile(r"^[0-9]{2}:[0-9]{2}:[0-9]{2}$")


def _trunc(seconds: float) -> int:
    """Whole-second truncation matching ``chapters._format_hms``."""
    return max(0, int(seconds))


def _extract_payload(raw: dict | None):
    """Pull and JSON-decode the assistant message content from a raw chat
    completion reply. Returns the decoded value, or ``None`` if absent/undecodable."""
    if not isinstance(raw, dict):
        return None
    choices = raw.get("choices")
    if not choices:
        return None
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str):
        return None
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None


def check_schema(raw: dict | None) -> dict:
    """Does the *raw* reply conform to the production ``response_format`` schema?

    Independent of ``generate_chapters``' lenient parse: an endpoint that ignores
    ``response_format`` may still return something parseable but off-shape, and
    that should score as a format failure.
    """
    payload = _extract_payload(raw)
    decoded = payload is not None
    obj_with_chapters = isinstance(payload, dict) and isinstance(
        payload.get("chapters"), list
    )
    items = payload["chapters"] if obj_with_chapters else []
    bad_items = 0
    for it in items:
        ok = (
            isinstance(it, dict)
            and isinstance(it.get("start"), str)
            and bool(_HMS.match(it["start"]))
            and isinstance(it.get("end"), str)
            and bool(_HMS.match(it["end"]))
            and isinstance(it.get("title"), str)
            and isinstance(it.get("is_ad"), bool)
        )
        if not ok:
            bad_items += 1
    return {
        "decoded": decoded,
        "object_with_chapters_array": obj_with_chapters,
        "n_items": len(items),
        "bad_items": bad_items,
        "conforms": obj_with_chapters and bad_items == 0,
    }


def check_coverage(chapters: list[dict], segments: list[dict]) -> dict:
    """Do the chapters tile the whole transcript contiguously, with no gaps?

    The prompt requires the first chapter's start to equal the first line's
    start, the last chapter's end to equal the last line's end, and chapters to
    abut with no gaps or overlaps.
    """
    if not chapters or not segments:
        return {
            "covers_start": False,
            "covers_end": False,
            "contiguous": False,
            "n_gaps": 0,
            "gaps": [],
        }
    expected_start = _trunc(segments[0].get("start", 0))
    expected_end = _trunc(segments[-1].get("end", 0))
    gaps = []
    for a, b in zip(chapters, chapters[1:]):
        if a["end"] != b["start"]:
            gaps.append({"after_end": a["end"], "next_start": b["start"]})
    return {
        "covers_start": chapters[0]["start"] == expected_start,
        "covers_end": chapters[-1]["end"] == expected_end,
        "contiguous": not gaps,
        "n_gaps": len(gaps),
        "gaps": gaps,
    }


def check_timestamps(
    chapters: list[dict],
    segments: list[dict],
    tolerance: int = _TIMESTAMP_TOLERANCE_SECONDS,
) -> dict:
    """Were chapter boundaries *copied* from real cue boundaries, or invented?

    Builds the set of whole-second segment start/end times and counts chapter
    boundaries that don't land within ``tolerance`` of any of them.
    """
    boundaries = set()
    for seg in segments:
        boundaries.add(_trunc(seg.get("start", 0)))
        boundaries.add(_trunc(seg.get("end", 0)))

    def near(t: int) -> bool:
        return any(abs(t - b) <= tolerance for b in boundaries)

    hallucinated = []
    for ch in chapters:
        for edge in ("start", "end"):
            if not near(ch[edge]):
                hallucinated.append({"chapter": ch.get("title", ""), "edge": edge, "value": ch[edge]})
    total_edges = 2 * len(chapters)
    return {
        "total_boundaries": total_edges,
        "n_hallucinated": len(hallucinated),
        "hallucinated": hallucinated,
        "all_copied": not hallucinated,
    }


def check_brevity(
    chapters: list[dict],
    word_limit: int = _BREVITY_WORD_LIMIT,
    char_limit: int = _BREVITY_CHAR_LIMIT,
) -> dict:
    """Title length distribution and how many breach the brevity limits."""
    words = [len(str(c.get("title", "")).split()) for c in chapters]
    chars = [len(str(c.get("title", ""))) for c in chapters]
    over = sum(1 for c in chapters if len(str(c.get("title", "")).split()) > word_limit
               or len(str(c.get("title", ""))) > char_limit)
    n = len(chapters)
    return {
        "max_words": max(words) if words else 0,
        "mean_words": round(sum(words) / n, 1) if n else 0,
        "max_chars": max(chars) if chars else 0,
        "n_over_limit": over,
        "word_limit": word_limit,
        "char_limit": char_limit,
    }


def check_sanity(chapters: list[dict]) -> dict:
    """Basic well-formedness: each chapter has start < end, and starts are
    non-decreasing in order."""
    bad_range = [c for c in chapters if not c["start"] < c["end"]]
    out_of_order = [
        {"prev_start": a["start"], "next_start": b["start"]}
        for a, b in zip(chapters, chapters[1:])
        if b["start"] < a["start"]
    ]
    return {
        "n_bad_range": len(bad_range),
        "n_out_of_order": len(out_of_order),
        "ok": not bad_range and not out_of_order,
    }


def run_checks(
    chapters: list[dict] | None,
    segments: list[dict],
    *,
    error: str | None = None,
    raw: dict | None = None,
) -> dict:
    """Aggregate every programmatic check into one scorecard.

    ``chapters`` is ``None`` (and ``error`` set) when ``generate_chapters`` raised
    — a hard format failure. The schema check still runs against ``raw`` so a
    near-miss reply is visible.
    """
    schema = check_schema(raw)
    generated_ok = error is None and chapters is not None
    result: dict = {
        "generated_ok": generated_ok,
        "error": error,
        "n_chapters": len(chapters) if chapters else 0,
        "schema": schema,
    }
    if generated_ok:
        result["coverage"] = check_coverage(chapters, segments)
        result["timestamps"] = check_timestamps(chapters, segments)
        result["brevity"] = check_brevity(chapters)
        result["sanity"] = check_sanity(chapters)
    return result
