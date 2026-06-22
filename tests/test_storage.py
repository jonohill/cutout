import asyncio
import io

from cutout.common.storage import LocalStorage, _decode_metadata_value


def _run(coro):
    return asyncio.run(coro)


def test_decode_metadata_value_unwraps_rfc2047_encoded_word():
    # R2 returns non-ASCII user metadata as RFC 2047 encoded-words on head.
    encoded = (
        "=?utf-8?Q?https=3A=2F=2F?= =?utf-8?Q?cdn=2Eatp=2E?= "
        "=?utf-8?Q?fm=2Frss?="
    )
    assert _decode_metadata_value(encoded) == "https://cdn.atp.fm/rss"
    assert _decode_metadata_value("=?utf-8?Q?Caf=C3=A9?=") == "Café"


def test_decode_metadata_value_passes_plain_ascii_through():
    assert _decode_metadata_value("https://plain.example.com/feed") == (
        "https://plain.example.com/feed"
    )


def test_put_bytes_get_head_roundtrip(tmp_path):
    store = LocalStorage(tmp_path)
    assert _run(store.head("f/e.x")) is None
    assert _run(store.get_bytes("f/e.x")) is None

    _run(store.put_bytes("f/e.x", b"hi"))

    assert _run(store.get_bytes("f/e.x")) == b"hi"
    assert _run(store.head("f/e.x")) == {}
    # Written through to a real nested file under the root.
    assert (tmp_path / "f" / "e.x").read_bytes() == b"hi"


def test_put_streams_body_to_disk(tmp_path):
    store = LocalStorage(tmp_path)
    _run(store.put("f/e.source", io.BytesIO(b"audio-bytes")))
    assert (tmp_path / "f" / "e.source").read_bytes() == b"audio-bytes"


def test_path_maps_key_under_root(tmp_path):
    store = LocalStorage(tmp_path)
    assert store.path("f/e.source") == tmp_path / "f" / "e.source"


def test_open_write_is_atomic_on_failure(tmp_path):
    store = LocalStorage(tmp_path)

    async def write_then_fail():
        async with store.open_write("f/e.source") as out:
            out.write(b"partial")
            raise RuntimeError("boom")

    try:
        _run(write_then_fail())
    except RuntimeError:
        pass

    # Nothing published, and the .part scratch file is gone.
    assert _run(store.head("f/e.source")) is None
    assert not any(p.is_file() for p in tmp_path.rglob("*"))


def test_cleanup_removes_only_the_matching_episode(tmp_path):
    store = LocalStorage(tmp_path)
    _run(store.put_bytes("f/e.source", b"1"))
    _run(store.put_bytes("f/e.transcript.json", b"2"))
    _run(store.put_bytes("f/e2.source", b"3"))  # a different episode

    _run(store.cleanup("f/e."))

    # The "." delimiter keeps the e. prefix from sweeping up e2.
    assert _run(store.head("f/e.source")) is None
    assert _run(store.head("f/e.transcript.json")) is None
    assert _run(store.head("f/e2.source")) == {}


def test_list_keys_filters_by_prefix(tmp_path):
    store = LocalStorage(tmp_path)
    _run(store.put_bytes("f/e.source", b"1"))
    _run(store.put_bytes("g/e.source", b"2"))
    assert _run(store.list_keys("f/")) == {"f/e.source"}
