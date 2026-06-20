import pytest

from cutout.worker.edit import (
    NoKeptChaptersError,
    keeping_short_ad_runs,
    plan_cuts,
)


def ad(start, end, title="ad"):
    return {"start": start, "end": end, "title": title, "is_ad": True}


def content(start, end, title="content"):
    return {"start": start, "end": end, "title": title, "is_ad": False}


def ranges(chapters):
    return [[c["start"], c["end"]] for c in chapters]


# ---------------------------------------------------------------------------
# keeping_short_ad_runs — ported from the macOS CLI's AdRunsTests (15s here).
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty():
    assert keeping_short_ad_runs([], 15) == []


def test_single_non_ad_always_kept():
    assert ranges(keeping_short_ad_runs([content(0, 600)], 15)) == [[0, 600]]


def test_single_short_ad_kept():
    assert ranges(keeping_short_ad_runs([ad(0, 15)], 15)) == [[0, 15]]


def test_single_long_ad_dropped():
    assert keeping_short_ad_runs([ad(0, 16)], 15) == []


def test_back_to_back_ads_exceeding_threshold_both_dropped():
    assert keeping_short_ad_runs([ad(0, 10), ad(10, 20)], 15) == []


def test_back_to_back_ads_under_threshold_both_kept():
    assert ranges(keeping_short_ad_runs([ad(0, 5), ad(5, 10)], 15)) == [[0, 5], [5, 10]]


def test_ads_separated_by_content_evaluated_independently():
    chapters = [ad(0, 10), content(10, 100), ad(100, 110)]
    assert ranges(keeping_short_ad_runs(chapters, 15)) == [[0, 10], [10, 100], [100, 110]]


def test_ads_within_adjacency_tolerance_are_merged():
    # gap of 2s -> one run of 0..22 = 22s > 15s, so both go.
    assert keeping_short_ad_runs([ad(0, 10), ad(12, 22)], 15) == []


def test_ads_beyond_adjacency_tolerance_evaluated_independently():
    # gap of 3s -> two separate 10s runs, both kept.
    assert ranges(keeping_short_ad_runs([ad(0, 10), ad(13, 23)], 15)) == [[0, 10], [13, 23]]


def test_three_contiguous_short_ads_under_threshold_kept():
    chapters = [ad(0, 4), ad(4, 8), ad(8, 12)]
    assert ranges(keeping_short_ad_runs(chapters, 15)) == [[0, 4], [4, 8], [8, 12]]


def test_three_contiguous_short_ads_over_threshold_all_dropped():
    assert keeping_short_ad_runs([ad(0, 6), ad(6, 12), ad(12, 18)], 15) == []


def test_mixed_timeline_keeps_content_and_short_runs_only():
    chapters = [
        content(0, 100),
        ad(100, 108),
        ad(108, 118),
        content(118, 200),
        ad(200, 210),
        content(210, 300),
    ]
    assert ranges(keeping_short_ad_runs(chapters, 15)) == [
        [0, 100],
        [118, 200],
        [200, 210],
        [210, 300],
    ]


def test_custom_threshold_respected():
    assert ranges(keeping_short_ad_runs([ad(0, 30)], 30)) == [[0, 30]]
    assert keeping_short_ad_runs([ad(0, 30)], 29) == []


def test_default_threshold_is_ten_seconds():
    # The production default the Swift ships: a 10s ad stays, an 11s one goes.
    assert ranges(keeping_short_ad_runs([ad(0, 10)])) == [[0, 10]]
    assert keeping_short_ad_runs([ad(0, 11)]) == []


# ---------------------------------------------------------------------------
# plan_cuts — segment building, first/last extension, and timeline remap.
# ---------------------------------------------------------------------------


def test_plan_drops_ad_and_extends_first_and_last_to_file_bounds():
    # Intro is the source's first chapter and Outro its last, so each extends
    # to the real file bounds (start 0, end = true duration), and the 309s ad
    # between them is cut.
    chapters = [
        content(5, 200, "Intro"),
        ad(200, 509, "Sponsor"),
        content(509, 700, "Outro"),
    ]
    plan = plan_cuts(chapters, 700.4)

    # First segment extends its start to 0; last runs to EOF (None).
    assert plan.segments == [(0, 200), (509, None)]
    # Output timeline: Intro 0..200, Outro right after for the remaining 191.4s.
    assert plan.chapters == [
        {"title": "Intro", "start": 0, "end": 200},
        {"title": "Outro", "start": 200, "end": 391},
    ]


def test_plan_keeps_short_ad_as_its_own_segment_without_extending_it():
    # A 5s ad is within the 10s default, so it survives — but ads are never
    # extended to file bounds, only the bracketing non-ad first/last are.
    chapters = [
        content(0, 100, "A"),
        ad(100, 105, "Spot"),
        content(105, 200, "B"),
    ]
    plan = plan_cuts(chapters, 200.0)

    assert plan.segments == [(0, 100), (100, 105), (105, None)]
    assert plan.chapters == [
        {"title": "A", "start": 0, "end": 100},
        {"title": "Spot", "start": 100, "end": 105},
        {"title": "B", "start": 105, "end": 200},
    ]


def test_plan_raises_when_every_chapter_is_a_long_ad():
    with pytest.raises(NoKeptChaptersError):
        plan_cuts([ad(0, 100), ad(100, 200)], 200.0)
