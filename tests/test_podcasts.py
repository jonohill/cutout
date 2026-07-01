from datetime import datetime, timedelta, timezone

from cutout.podcasts import is_stale, now_timestamp


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def test_is_stale_within_ttl():
    assert is_stale(_ago(60), ttl_secs=3600) is False


def test_is_stale_beyond_ttl():
    assert is_stale(_ago(7200), ttl_secs=3600) is True


def test_is_stale_ttl_zero_never_stale():
    # ttl "0" disables staleness even for an ancient timestamp.
    assert is_stale(_ago(10**9), ttl_secs=0) is False


def test_is_stale_missing_timestamp_is_grace():
    assert is_stale(None, ttl_secs=3600) is False


def test_is_stale_unparseable_is_grace():
    assert is_stale("not-a-date", ttl_secs=3600) is False


def test_is_stale_naive_timestamp_treated_as_utc():
    # A timestamp without tzinfo must not raise on the aware/naive subtraction.
    naive = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(
        tzinfo=None
    ).isoformat()
    assert is_stale(naive, ttl_secs=3600) is True


def test_now_timestamp_roundtrips():
    assert datetime.fromisoformat(now_timestamp())
