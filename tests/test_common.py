from datetime import timedelta

import pytest

from cutout.common import get_feed_id, new_feed_id, parse_delay, parse_duration
from cutout.common.paths import audio_path, feed_path


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1d", timedelta(days=1)),
        ("2w", timedelta(days=14)),
        ("3m", timedelta(days=90)),
        ("1y", timedelta(days=365)),
        ("0d", timedelta(0)),
    ],
)
def test_parse_delay_valid(value, expected):
    assert parse_delay(value) == expected


@pytest.mark.parametrize("value", ["", "5", "d", "-1d", "1x", "1.5d", "1 d"])
def test_parse_delay_invalid(value):
    with pytest.raises(ValueError):
        parse_delay(value)


@pytest.mark.parametrize(
    "value, seconds",
    [
        ("60m", 3600),
        ("60min", 3600),
        ("90d", 90 * 86400),
        ("36h", 36 * 3600),
        ("2w", 14 * 86400),
        ("1h30min", 5400),
        ("2d 4h", 2 * 86400 + 4 * 3600),
        ("1.5h", 5400),
        ("45sec", 45),
        ("1M", 2629800),  # capital M is months, not minutes
        ("1y", 31557600),
        ("0", 0),
    ],
)
def test_parse_duration_valid(value, seconds):
    assert parse_duration(value) == seconds


def test_parse_duration_case_disambiguates_minutes_and_months():
    # The whole point of the systemd grammar: lowercase m != capital M.
    assert parse_duration("1m") == 60
    assert parse_duration("1M") == 2629800


@pytest.mark.parametrize("value", ["", "60", "m", "-1m", "1x", "1mm", "1 2m"])
def test_parse_duration_invalid(value):
    with pytest.raises(ValueError):
        parse_duration(value)


def test_get_feed_id_is_deterministic():
    assert get_feed_id("channel") == get_feed_id("channel")
    assert get_feed_id("channel") != get_feed_id("other")


def test_new_feed_id_is_random_and_urlsafe():
    a, b = new_feed_id(), new_feed_id()
    assert a != b
    assert "=" not in a and "/" not in a and "+" not in a


def test_paths():
    assert feed_path("abc") == "abc/feed.xml"
    assert audio_path("abc", "ep1") == "abc/ep1"
