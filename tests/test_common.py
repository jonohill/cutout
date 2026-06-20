from datetime import timedelta

import pytest

from cutout.common import get_feed_id, new_feed_id, parse_delay
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
