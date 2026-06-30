import asyncio
import xml.etree.ElementTree as ET

import pytest

from cutout.config import Settings
from cutout.common import get_feed_id
from cutout.common.paths import audio_path, feed_path
from cutout.worker import feed_xml
from cutout.worker.processor import FeedProcessor

ATOM = "http://www.w3.org/2005/Atom"
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
FEED_ID = "fid"
SOURCE_URL = "https://src.example/feed.xml"

PROC_FEED = f"""<?xml version='1.0'?>
<rss xmlns:itunes="{ITUNES}" xmlns:atom="{ATOM}">
  <channel>
    <title>Show</title>
    <itunes:new-feed-url>https://moved.example/feed.xml</itunes:new-feed-url>
    <item>
      <guid>ep-1</guid>
      <enclosure url="https://src.example/1.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <guid>ep-2</guid>
      <enclosure url="https://src.example/2.mp3" type="audio/mpeg"/>
    </item>
  </channel>
</rss>"""


class FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.metas: dict[str, dict] = {}
        self.head_meta: dict[str, dict] = {}
        self.keys: set[str] = set()

    async def get_bytes(self, key):
        return self.objects.get(key)

    async def head(self, key):
        return self.head_meta.get(key)

    async def list_keys(self, prefix):
        return {k for k in self.keys if k.startswith(prefix)}

    async def put(self, key, body, *, cache_control=None, content_type=None):
        body.seek(0)
        self.objects[key] = body.read()

    async def put_bytes(self, key, data, *, content_type=None, metadata=None):
        self.objects[key] = data
        self.metas[key] = metadata or {}


class FakeQueue:
    def __init__(self):
        self.messages: list[dict] = []

    async def put(self, message):
        self.messages.append(message)


def _settings():
    return Settings(
        s3_access_key_id="x",
        s3_secret_access_key="y",
        public_service_url="https://app.example",
        public_storage_url="https://media.example",
    )


def _items_by_guid(xml_bytes: bytes) -> dict[str, ET.Element]:
    feed = feed_xml.parse_feed(xml_bytes.decode("utf-8"))
    out = {}
    for item in feed.channel.findall("item"):
        guid = item.find("guid").text
        out[guid] = item
    return out


def test_process_reconciles_episodes():
    storage = FakeStorage()
    media = FakeQueue()
    ep1_id = get_feed_id("ep-1")
    ep2_id = get_feed_id("ep-2")
    # ep-1 already has stored audio; ep-2 does not.
    storage.keys = {audio_path(FEED_ID, ep1_id)}

    async def fetch(url):
        return PROC_FEED, url

    processor = FeedProcessor(
        storage=storage, start_job=media.put, settings=_settings(), fetch=fetch
    )
    asyncio.run(
        processor.process({"feed_id": FEED_ID, "feed_url": SOURCE_URL})
    )

    stored = storage.objects[feed_path(FEED_ID)]
    items = _items_by_guid(stored)

    # ep-1 kept, enclosure rewritten to our audio host.
    assert set(items) == {"ep-1"}
    assert items["ep-1"].find("enclosure").get("url") == (
        f"https://media.example/{audio_path(FEED_ID, ep1_id)}"
    )

    # ep-2 missing audio -> media job enqueued + item dropped. The podcast name
    # rides along as reference context for the chapters model.
    assert media.messages == [
        {
            "feed_id": FEED_ID,
            "episode_id": ep2_id,
            "source_url": "https://src.example/2.mp3",
            "context": {"podcast": "Show"},
        }
    ]

    # Channel links rewritten and source-feed markers stripped.
    feed = feed_xml.parse_feed(stored.decode("utf-8"))
    assert feed.channel.find(f"{{{ITUNES}}}new-feed-url") is None
    self_link = feed.channel.find(f"{{{ATOM}}}link")
    assert self_link.get("href") == f"https://app.example/podcast/{FEED_ID}"

    # itunes:new-feed-url wins as the persisted source URL.
    assert storage.metas[feed_path(FEED_ID)] == {
        "feedurl": "https://moved.example/feed.xml"
    }


def test_process_stores_title_metadata():
    storage = FakeStorage()
    media = FakeQueue()

    async def fetch(url):
        return PROC_FEED, url

    processor = FeedProcessor(
        storage=storage, start_job=media.put, settings=_settings(), fetch=fetch
    )
    asyncio.run(
        processor.process(
            {"feed_id": FEED_ID, "feed_url": SOURCE_URL, "title": "Renamed"}
        )
    )
    meta = storage.metas[feed_path(FEED_ID)]
    assert meta["title"] == "Renamed"
    feed = feed_xml.parse_feed(storage.objects[feed_path(FEED_ID)].decode("utf-8"))
    assert feed.channel.find("title").text == "Renamed"


def _ping_settings():
    return Settings(
        s3_access_key_id="x",
        s3_secret_access_key="y",
        public_service_url="https://app.example",
        public_storage_url="https://media.example",
        enable_overcast_ping=True,
        overcast_ping_url="https://overcast.example/ping",
    )


def _run_with_recorded_ping(monkeypatch, settings, body):
    """Run ``process(body)`` capturing any Overcast ping as (ping_url, feed_url)."""
    calls: list[tuple[str, str]] = []

    async def fake_ping(ping_url, feed_url):
        calls.append((ping_url, feed_url))

    monkeypatch.setattr("cutout.worker.processor.ping_overcast", fake_ping)

    storage = FakeStorage()
    storage.keys = {audio_path(FEED_ID, get_feed_id("ep-1"))}

    async def fetch(url):
        return PROC_FEED, url

    processor = FeedProcessor(
        storage=storage, start_job=FakeQueue().put, settings=settings, fetch=fetch
    )
    asyncio.run(processor.process(body))
    return calls


def test_publication_refresh_pings_overcast(monkeypatch):
    calls = _run_with_recorded_ping(
        monkeypatch,
        _ping_settings(),
        {"feed_id": FEED_ID, "feed_url": SOURCE_URL, "notify": True},
    )
    assert calls == [
        ("https://overcast.example/ping", f"https://app.example/podcast/{FEED_ID}")
    ]


def test_plain_refresh_does_not_ping(monkeypatch):
    # A feed GET enqueues a refresh with no ``notify`` flag; Overcast's own crawl
    # must not be able to trigger a ping.
    calls = _run_with_recorded_ping(
        monkeypatch,
        _ping_settings(),
        {"feed_id": FEED_ID, "feed_url": SOURCE_URL},
    )
    assert calls == []


def test_no_ping_when_disabled(monkeypatch):
    calls = _run_with_recorded_ping(
        monkeypatch,
        _settings(),
        {"feed_id": FEED_ID, "feed_url": SOURCE_URL, "notify": True},
    )
    assert calls == []


def test_resolve_uses_stored_metadata():
    storage = FakeStorage()
    storage.head_meta[feed_path(FEED_ID)] = {
        "feedurl": SOURCE_URL,
        "title": "Stored",
        "delay": "1w",
    }
    processor = FeedProcessor(
        storage=storage, start_job=FakeQueue().put, settings=_settings()
    )
    resolved = asyncio.run(processor._resolve({"feed_id": FEED_ID}))
    assert resolved == (FEED_ID, SOURCE_URL, "Stored", "1w")


def test_resolve_unknown_feed_raises():
    storage = FakeStorage()
    processor = FeedProcessor(
        storage=storage, start_job=FakeQueue().put, settings=_settings()
    )
    with pytest.raises(ValueError):
        asyncio.run(processor._resolve({"feed_id": "nope"}))
