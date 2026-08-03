import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

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

    async def delete(self, key):
        self.objects.pop(key, None)
        self.keys.discard(key)


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

    # itunes:new-feed-url wins as the persisted source URL, and every store
    # stamps a lastrequested timestamp.
    meta = storage.metas[feed_path(FEED_ID)]
    assert meta["feedurl"] == "https://moved.example/feed.xml"
    assert "lastrequested" in meta


def _cleanup_settings(cleanup_ttl):
    return Settings(
        s3_access_key_id="x",
        s3_secret_access_key="y",
        public_service_url="https://app.example",
        public_storage_url="https://media.example",
        cleanup_ttl=cleanup_ttl,
    )


def _dated_feed():
    """A two-episode feed with one old and one recent episode."""
    from email.utils import format_datetime

    old = format_datetime(datetime.now(timezone.utc) - timedelta(days=400))
    recent = format_datetime(datetime.now(timezone.utc) - timedelta(days=1))
    return f"""<?xml version='1.0'?>
<rss xmlns:itunes="{ITUNES}" xmlns:atom="{ATOM}">
  <channel>
    <title>Show</title>
    <item>
      <guid>old-ep</guid>
      <pubDate>{old}</pubDate>
      <enclosure url="https://src.example/old.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <guid>new-ep</guid>
      <pubDate>{recent}</pubDate>
      <enclosure url="https://src.example/new.mp3" type="audio/mpeg"/>
    </item>
  </channel>
</rss>"""


def _run_cleanup(cleanup_ttl):
    """Process ``_dated_feed`` with both episodes stored plus an orphan file.

    Returns (storage, feed_items_by_guid).
    """
    storage = FakeStorage()
    old_key = audio_path(FEED_ID, get_feed_id("old-ep"))
    new_key = audio_path(FEED_ID, get_feed_id("new-ep"))
    orphan_key = f"{FEED_ID}/orphan-no-episode"
    # Both episodes have stored audio, plus a stray file from an episode that has
    # since dropped off the feed, plus the feed's own XML object.
    storage.keys = {feed_path(FEED_ID), old_key, new_key, orphan_key}
    for key in storage.keys:
        storage.objects[key] = b"data"

    feed_xml_body = _dated_feed()

    async def fetch(url):
        return feed_xml_body, url

    processor = FeedProcessor(
        storage=storage,
        start_job=FakeQueue().put,
        settings=_cleanup_settings(cleanup_ttl),
        fetch=fetch,
    )
    asyncio.run(processor.process({"feed_id": FEED_ID, "feed_url": SOURCE_URL}))
    items = _items_by_guid(storage.objects[feed_path(FEED_ID)])
    return storage, items


def test_cleanup_removes_old_and_orphaned_files():
    storage, items = _run_cleanup("90d")

    old_key = audio_path(FEED_ID, get_feed_id("old-ep"))
    new_key = audio_path(FEED_ID, get_feed_id("new-ep"))
    orphan_key = f"{FEED_ID}/orphan-no-episode"

    # Aged-out and unmappable files are gone; recent episode audio and the feed
    # XML itself are preserved.
    assert storage.keys == {feed_path(FEED_ID), new_key}
    assert old_key not in storage.objects
    assert orphan_key not in storage.objects
    assert new_key in storage.objects

    # The old episode is also dropped from the published feed; the recent one stays.
    assert set(items) == {"new-ep"}


def _feed_with_dates(guids_to_ages):
    """Build a feed whose items carry the given guid -> age-in-days pubDates."""
    from email.utils import format_datetime

    items = ""
    for guid, days in guids_to_ages.items():
        pub = format_datetime(datetime.now(timezone.utc) - timedelta(days=days))
        items += f"""
    <item>
      <guid>{guid}</guid>
      <pubDate>{pub}</pubDate>
      <enclosure url="https://src.example/{guid}.mp3" type="audio/mpeg"/>
    </item>"""
    return f"""<?xml version='1.0'?>
<rss xmlns:itunes="{ITUNES}" xmlns:atom="{ATOM}">
  <channel>
    <title>Show</title>{items}
  </channel>
</rss>"""


def test_cleanup_ttl_accounts_for_delay():
    # delay=7d shifts each episode's effective appearance in our feed forward by
    # a week, so the 5d retention window must be measured from pub_date + delay.
    # "recent" (8d old) has only been live ~1d, so it must survive; "stale" (20d
    # old) has been live ~13d and must be purged. Without the delay offset the 8d
    # episode would be wrongly deleted the moment it cleared the delay window.
    storage = FakeStorage()
    recent_key = audio_path(FEED_ID, get_feed_id("recent"))
    stale_key = audio_path(FEED_ID, get_feed_id("stale"))
    storage.keys = {feed_path(FEED_ID), recent_key, stale_key}
    for key in storage.keys:
        storage.objects[key] = b"data"

    feed_xml_body = _feed_with_dates({"recent": 8, "stale": 20})

    async def fetch(url):
        return feed_xml_body, url

    processor = FeedProcessor(
        storage=storage,
        start_job=FakeQueue().put,
        settings=_cleanup_settings("5d"),
        fetch=fetch,
    )
    asyncio.run(
        processor.process(
            {"feed_id": FEED_ID, "feed_url": SOURCE_URL, "delay": "7d"}
        )
    )

    items = _items_by_guid(storage.objects[feed_path(FEED_ID)])
    assert storage.keys == {feed_path(FEED_ID), recent_key}
    assert stale_key not in storage.objects
    assert set(items) == {"recent"}


def test_unknown_zone_pub_dates_are_handled():
    # Megaphone and Art19 stamp every <pubDate> with a "-0000" offset, which
    # parses to a naive datetime. Both the delay and the retention filter
    # compare pub_date against an aware now(), so a naive value used to raise
    # TypeError and abort the whole refresh — leaving the feed XML unwritten and
    # the feed's storage never swept.
    storage = FakeStorage()
    recent_key = audio_path(FEED_ID, get_feed_id("recent"))
    stale_key = audio_path(FEED_ID, get_feed_id("stale"))
    storage.keys = {feed_path(FEED_ID), recent_key, stale_key}
    for key in storage.keys:
        storage.objects[key] = b"data"

    # delayed: inside the 7d delay window. recent: live ~1d. stale: live ~13d,
    # past the 5d retention window.
    feed_xml_body = _feed_with_dates(
        {"delayed": 2, "recent": 8, "stale": 20}
    ).replace("+0000", "-0000")
    assert "-0000" in feed_xml_body

    async def fetch(url):
        return feed_xml_body, url

    processor = FeedProcessor(
        storage=storage,
        start_job=FakeQueue().put,
        settings=_cleanup_settings("5d"),
        fetch=fetch,
    )
    asyncio.run(
        processor.process({"feed_id": FEED_ID, "feed_url": SOURCE_URL, "delay": "7d"})
    )

    # The refresh completes: feed XML republished, aged-out audio actually swept.
    items = _items_by_guid(storage.objects[feed_path(FEED_ID)])
    assert set(items) == {"recent"}
    assert storage.keys == {feed_path(FEED_ID), recent_key}
    assert stale_key not in storage.objects


def _process_with_broken(method, cleanup_ttl="90d", delay=None):
    """Run a refresh whose ``method`` raises; return the storage afterwards."""
    storage = FakeStorage()
    old_key = audio_path(FEED_ID, get_feed_id("old-ep"))
    new_key = audio_path(FEED_ID, get_feed_id("new-ep"))
    storage.keys = {feed_path(FEED_ID), old_key, new_key}
    for key in storage.keys:
        storage.objects[key] = b"data"

    feed_xml_body = _dated_feed()

    async def fetch(url):
        return feed_xml_body, url

    processor = FeedProcessor(
        storage=storage,
        start_job=FakeQueue().put,
        settings=_cleanup_settings(cleanup_ttl),
        fetch=fetch,
    )

    def boom(*args, **kwargs):
        raise RuntimeError(f"{method} exploded")

    setattr(processor, method, boom)
    body = {"feed_id": FEED_ID, "feed_url": SOURCE_URL}
    if delay:
        body["delay"] = delay
    asyncio.run(processor.process(body))
    return storage


def test_retention_failure_still_publishes_the_feed():
    # Pruning is housekeeping; publishing is the job. A retention failure must
    # cost storage, not freeze the feed — the 13-day outage in reverse.
    storage = _process_with_broken("_apply_retention")

    items = _items_by_guid(storage.objects[feed_path(FEED_ID)])
    # The feed was rewritten and still lists both episodes (nothing was pruned).
    assert set(items) == {"old-ep", "new-ep"}
    # Cleanup is skipped rather than run off an unpruned list, so no audio is
    # deleted — the leak this trades for staying published.
    assert storage.keys == {
        feed_path(FEED_ID),
        audio_path(FEED_ID, get_feed_id("old-ep")),
        audio_path(FEED_ID, get_feed_id("new-ep")),
    }
    # lastrefreshed still advances: the refresh genuinely did its job.
    assert storage.metas[feed_path(FEED_ID)]["lastrefreshed"]


def test_cleanup_failure_still_publishes_the_feed():
    storage = _process_with_broken("_cleanup_files")

    items = _items_by_guid(storage.objects[feed_path(FEED_ID)])
    # Retention ran, so the aged-out episode is gone from the XML even though
    # deleting its audio failed.
    assert set(items) == {"new-ep"}
    assert storage.metas[feed_path(FEED_ID)]["lastrefreshed"]


def test_delay_failure_aborts_the_refresh():
    # The one filter that must stay fatal: publishing without the delay applied
    # would expose episodes the subscriber chose to withhold.
    with pytest.raises(RuntimeError):
        _process_with_broken("_apply_delay", delay="7d")


def test_retention_failure_leaves_the_feed_unmodified():
    # Decisions are made before any removal, so a filter that raises partway
    # cannot leave the channel half-pruned and desynced from ``episodes`` —
    # which would make _reconcile_episodes remove an already-removed item.
    feed = feed_xml.parse_feed(_feed_with_dates({"a": 1, "b": 400, "c": 800}))
    episodes = feed_xml.parse_episodes(feed)
    processor = FeedProcessor(
        storage=FakeStorage(),
        start_job=FakeQueue().put,
        settings=_cleanup_settings("30d"),
    )
    for episode in episodes:
        if episode.guid == "c":
            episode.pub_date = "not a datetime"  # raises mid-partition

    with pytest.raises(TypeError):
        processor._apply_retention(feed, episodes, 30 * 86400, None)

    # Nothing removed: 'b' was past the window but the raise came after it.
    assert len(feed.channel.findall("item")) == 3


def test_cleanup_disabled_keeps_everything():
    storage, items = _run_cleanup("0")

    old_key = audio_path(FEED_ID, get_feed_id("old-ep"))
    orphan_key = f"{FEED_ID}/orphan-no-episode"

    # With cleanup off nothing is deleted and no episode is aged out of the feed.
    assert old_key in storage.objects
    assert orphan_key in storage.objects
    assert set(items) == {"old-ep", "new-ep"}


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


def _run_process_over_stored(body, stored):
    """Run ``process(body)`` against a feed that already has ``stored`` metadata."""
    storage = FakeStorage()
    storage.head_meta[feed_path(FEED_ID)] = stored

    async def fetch(url):
        return PROC_FEED, url

    processor = FeedProcessor(
        storage=storage, start_job=FakeQueue().put, settings=_settings(), fetch=fetch
    )
    asyncio.run(processor.process(body))
    return storage.metas[feed_path(FEED_ID)]


def test_delay_in_the_message_replaces_the_stored_one():
    # How the dashboard's delay edit lands: a bare feed_id + delay message.
    meta = _run_process_over_stored(
        {"feed_id": FEED_ID, "delay": "2w"},
        {"feedurl": SOURCE_URL, "title": "Stored", "delay": "1d"},
    )
    assert meta["delay"] == "2w"
    # The rest of the feed's metadata still comes from storage.
    assert meta["title"] == "Stored"


def test_empty_delay_in_the_message_clears_the_stored_one():
    meta = _run_process_over_stored(
        {"feed_id": FEED_ID, "delay": ""},
        {"feedurl": SOURCE_URL, "delay": "1d"},
    )
    assert "delay" not in meta


def test_resolve_unknown_feed_raises():
    storage = FakeStorage()
    processor = FeedProcessor(
        storage=storage, start_job=FakeQueue().put, settings=_settings()
    )
    with pytest.raises(ValueError):
        asyncio.run(processor._resolve({"feed_id": "nope"}))


def _run_process(body):
    """Run ``process(body)`` and return the persisted feed metadata."""
    storage = FakeStorage()
    storage.keys = {audio_path(FEED_ID, get_feed_id("ep-1"))}

    async def fetch(url):
        return PROC_FEED, url

    processor = FeedProcessor(
        storage=storage, start_job=FakeQueue().put, settings=_settings(), fetch=fetch
    )
    asyncio.run(processor.process(body))
    return storage.metas[feed_path(FEED_ID)]


def test_requested_refresh_stamps_now():
    meta = _run_process(
        {"feed_id": FEED_ID, "feed_url": SOURCE_URL, "requested": True}
    )
    # A request-origin refresh writes a fresh, parseable timestamp.
    from datetime import datetime

    assert datetime.fromisoformat(meta["lastrequested"])


def test_sweep_refresh_preserves_last_requested():
    # A non-requested (sweep/notify) refresh carries the stored timestamp forward
    # rather than resetting the staleness clock.
    storage = FakeStorage()
    storage.keys = {audio_path(FEED_ID, get_feed_id("ep-1"))}
    stamped = "2020-01-01T00:00:00+00:00"
    storage.head_meta[feed_path(FEED_ID)] = {
        "feedurl": SOURCE_URL,
        "lastrequested": stamped,
    }

    async def fetch(url):
        return PROC_FEED, url

    processor = FeedProcessor(
        storage=storage, start_job=FakeQueue().put, settings=_settings(), fetch=fetch
    )
    asyncio.run(processor.process({"feed_id": FEED_ID}))
    assert storage.metas[feed_path(FEED_ID)]["lastrequested"] == stamped


def test_sweep_refresh_seeds_missing_last_requested():
    # A feed created before this feature has no stored timestamp; a sweep refresh
    # seeds one rather than leaving it blank (so it isn't instantly stale).
    meta = _run_process({"feed_id": FEED_ID, "feed_url": SOURCE_URL})
    from datetime import datetime

    assert datetime.fromisoformat(meta["lastrequested"])


def test_successful_refresh_stamps_last_refreshed():
    # Unlike lastrequested, this advances on *every* completed refresh, sweep or
    # not — it is the dashboard's liveness signal.
    meta = _run_process({"feed_id": FEED_ID, "feed_url": SOURCE_URL})
    assert datetime.fromisoformat(meta["lastrefreshed"])


def test_failed_refresh_leaves_last_refreshed_untouched():
    # The whole point of the signal: a refresh that dies partway must not stamp,
    # so a crash-looping feed's timestamp visibly stops advancing.
    storage = FakeStorage()
    storage.keys = {audio_path(FEED_ID, get_feed_id("ep-1"))}

    async def fetch(url):
        raise RuntimeError("source feed unreachable")

    processor = FeedProcessor(
        storage=storage, start_job=FakeQueue().put, settings=_settings(), fetch=fetch
    )
    with pytest.raises(RuntimeError):
        asyncio.run(processor.process({"feed_id": FEED_ID, "feed_url": SOURCE_URL}))

    assert feed_path(FEED_ID) not in storage.metas
