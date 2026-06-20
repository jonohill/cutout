import json

from cutout.bench.checks import (
    check_brevity,
    check_coverage,
    check_sanity,
    check_schema,
    check_timestamps,
    run_checks,
)

# A three-segment transcript: intro, sponsor read, topic — whole-second cue
# boundaries at 0, 5, 65, 120.
SEGMENTS = [
    {"start": 0.0, "end": 5.0, "text": "Welcome to the show."},
    {"start": 5.0, "end": 65.0, "text": "Brought to you by ACME."},
    {"start": 65.0, "end": 120.0, "text": "Back to the topic."},
]

GOOD_CHAPTERS = [
    {"start": 0, "end": 65, "title": "Intro", "is_ad": False},
    {"start": 65, "end": 120, "title": "Topic", "is_ad": False},
]


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def test_coverage_passes_for_contiguous_full_tiling():
    cov = check_coverage(GOOD_CHAPTERS, SEGMENTS)
    assert cov["covers_start"] and cov["covers_end"]
    assert cov["contiguous"] and cov["n_gaps"] == 0


def test_coverage_flags_a_gap_and_missing_ends():
    chapters = [
        {"start": 5, "end": 60, "title": "A", "is_ad": False},
        {"start": 65, "end": 110, "title": "B", "is_ad": False},
    ]
    cov = check_coverage(chapters, SEGMENTS)
    assert not cov["covers_start"]  # starts at 5, not 0
    assert not cov["covers_end"]  # ends at 110, not 120
    assert not cov["contiguous"] and cov["n_gaps"] == 1  # 60 -> 65 gap


def test_timestamps_all_copied_from_cue_boundaries():
    ts = check_timestamps(GOOD_CHAPTERS, SEGMENTS)
    assert ts["all_copied"] and ts["n_hallucinated"] == 0


def test_timestamps_flag_a_hallucinated_boundary():
    chapters = [
        {"start": 0, "end": 42, "title": "A", "is_ad": False},  # 42 is no cue
        {"start": 42, "end": 120, "title": "B", "is_ad": False},
    ]
    ts = check_timestamps(chapters, SEGMENTS)
    assert ts["n_hallucinated"] == 2  # the 42 boundary on both sides
    assert not ts["all_copied"]


def test_timestamp_tolerance_allows_small_drift():
    chapters = [{"start": 0, "end": 66, "title": "A", "is_ad": False}]  # 66 ~ 65
    ts = check_timestamps(chapters, SEGMENTS, tolerance=2)
    assert ts["all_copied"]


def test_brevity_flags_a_long_title():
    chapters = [
        {"start": 0, "end": 65, "title": "Intro", "is_ad": False},
        {
            "start": 65,
            "end": 120,
            "title": "An extremely long winded chapter title that drones on well past any reasonable brevity limit",
            "is_ad": False,
        },
    ]
    brev = check_brevity(chapters)
    assert brev["n_over_limit"] == 1
    assert brev["max_words"] >= 11


def test_sanity_flags_bad_range_and_disorder():
    chapters = [
        {"start": 65, "end": 120, "title": "A", "is_ad": False},
        {"start": 0, "end": 0, "title": "B", "is_ad": False},  # not start<end, and earlier
    ]
    san = check_sanity(chapters)
    assert not san["ok"]
    assert san["n_bad_range"] == 1
    assert san["n_out_of_order"] == 1


def test_schema_conforms_for_a_well_formed_reply():
    raw = _reply(
        json.dumps(
            {
                "chapters": [
                    {"start": "00:00:00", "end": "00:01:05", "title": "Intro", "is_ad": False}
                ]
            }
        )
    )
    s = check_schema(raw)
    assert s["decoded"] and s["object_with_chapters_array"]
    assert s["conforms"] and s["bad_items"] == 0


def test_schema_fails_on_bare_array_and_bad_timestamp():
    # Top-level array (not wrapped under "chapters") — generate_chapters tolerates
    # it, but it does NOT match the schema.
    bare = _reply(json.dumps([{"start": "00:00:00", "end": "00:01:05", "title": "x", "is_ad": False}]))
    assert not check_schema(bare)["object_with_chapters_array"]
    assert not check_schema(bare)["conforms"]

    bad_ts = _reply(
        json.dumps({"chapters": [{"start": "0:0:0", "end": "00:01:05", "title": "x", "is_ad": False}]})
    )
    s = check_schema(bad_ts)
    assert s["object_with_chapters_array"] and s["bad_items"] == 1 and not s["conforms"]


def test_schema_undecodable_reply():
    s = check_schema(_reply("not json at all"))
    assert not s["decoded"] and not s["conforms"]


def test_run_checks_records_a_hard_failure():
    result = run_checks(None, SEGMENTS, error="ChaptersError: boom", raw=None)
    assert not result["generated_ok"]
    assert result["error"] == "ChaptersError: boom"
    assert result["n_chapters"] == 0
    assert "coverage" not in result  # skipped when generation failed
    assert result["schema"]["conforms"] is False


def test_run_checks_full_scorecard_on_success():
    raw = _reply(
        json.dumps(
            {
                "chapters": [
                    {"start": "00:00:00", "end": "00:01:05", "title": "Intro", "is_ad": False},
                    {"start": "00:01:05", "end": "00:02:00", "title": "Topic", "is_ad": False},
                ]
            }
        )
    )
    result = run_checks(GOOD_CHAPTERS, SEGMENTS, error=None, raw=raw)
    assert result["generated_ok"] and result["n_chapters"] == 2
    assert result["coverage"]["contiguous"]
    assert result["timestamps"]["all_copied"]
    assert result["sanity"]["ok"]
    assert result["schema"]["conforms"]
