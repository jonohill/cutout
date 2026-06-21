import pytest
from fastapi.testclient import TestClient

from cutout.app import create_app
from cutout.config import Settings
from cutout.common.paths import feed_path


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}

    async def get_bytes(self, key: str) -> bytes | None:
        return self.objects.get(key)

    async def head(self, key: str) -> dict[str, str] | None:
        if key not in self.objects:
            return None
        return self.metadata.get(key, {})

    async def list_keys(self, prefix: str) -> set[str]:
        return {key for key in self.objects if key.startswith(prefix)}

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self.objects[key] = data
        if metadata:
            self.metadata[key] = {k.lower(): v for k, v in metadata.items()}

    def add_feed(
        self, feed_id: str, feed_url: str, *, title: str | None = None
    ) -> None:
        channel = f"<title>{title}</title>" if title else ""
        self.objects[feed_path(feed_id)] = (
            f"<rss><channel>{channel}</channel></rss>".encode("utf-8")
        )
        self.metadata[feed_path(feed_id)] = {"feedurl": feed_url}


class FakeQueue:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def put(self, message: dict) -> None:
        self.messages.append(message)


def _make_client(storage, queue, **settings_kwargs):
    settings = Settings(
        s3_access_key_id="x",
        s3_secret_access_key="y",
        **settings_kwargs,
    )
    app = create_app(settings=settings, storage=storage, queue=queue)
    return TestClient(app)


@pytest.fixture
def fakes():
    storage = FakeStorage()
    queue = FakeQueue()
    return _make_client(storage, queue), storage, queue


@pytest.fixture
def opml_fakes():
    storage = FakeStorage()
    queue = FakeQueue()
    return _make_client(storage, queue, enable_opml=True), storage, queue


def test_healthz(fakes):
    client, _, _ = fakes
    assert client.get("/healthz").json() == {"status": "ok"}


def test_create_podcast(fakes):
    client, _, queue = fakes
    resp = client.post("/podcast", json={"feed_url": "https://example.com/feed.xml"})
    assert resp.status_code == 200
    feed_id = resp.json()["feed_id"]
    assert feed_id
    assert queue.messages == [
        {"feed_id": feed_id, "feed_url": "https://example.com/feed.xml"}
    ]


def test_create_podcast_with_title_and_delay(fakes):
    client, _, queue = fakes
    resp = client.post(
        "/podcast",
        json={"feed_url": "https://example.com/feed.xml", "title": "Show", "delay": "2w"},
    )
    assert resp.status_code == 200
    msg = queue.messages[0]
    assert msg["title"] == "Show"
    assert msg["delay"] == "2w"


def test_create_podcast_rejects_bad_url(fakes):
    client, _, queue = fakes
    resp = client.post("/podcast", json={"feed_url": "not-a-url"})
    assert resp.status_code == 400
    assert resp.text == "Bad Request"
    assert queue.messages == []


def test_create_podcast_rejects_bad_delay(fakes):
    client, _, queue = fakes
    resp = client.post(
        "/podcast", json={"feed_url": "https://example.com/feed.xml", "delay": "5"}
    )
    assert resp.status_code == 400
    assert queue.messages == []


def test_get_podcast_found(fakes):
    client, storage, queue = fakes
    storage.objects[feed_path("abc")] = b"<rss></rss>"
    resp = client.get("/podcast/abc", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "http://localhost:8080/abc/feed.xml"
    assert queue.messages == [{"feed_id": "abc"}]


def test_get_podcast_missing(fakes):
    client, _, queue = fakes
    resp = client.get("/podcast/missing")
    assert resp.status_code == 404
    assert queue.messages == []


def test_opml_disabled_returns_404(fakes):
    client, _, _ = fakes
    assert client.get("/opml").status_code == 404
    assert client.post("/opml", content=b"<opml/>").status_code == 404


def test_opml_export(opml_fakes):
    client, storage, _ = opml_fakes
    storage.add_feed("a", "https://example.com/a.xml", title="Show A")
    storage.add_feed("b", "https://example.com/b.xml")

    resp = client.get("/opml")
    assert resp.status_code == 200
    body = resp.text
    # Subscriptions point at this server's feed, not the original source URL.
    assert 'xmlUrl="http://localhost:8080/podcast/a"' in body
    assert 'text="Show A"' in body
    # No channel title -> falls back to this server's feed URL.
    assert 'xmlUrl="http://localhost:8080/podcast/b"' in body
    assert 'text="http://localhost:8080/podcast/b"' in body
    # The original source feed URL is not exported.
    assert "example.com" not in body


def test_opml_import_creates_missing(opml_fakes):
    client, storage, queue = opml_fakes
    storage.add_feed("existing", "https://example.com/keep.xml")
    opml = """<?xml version="1.0"?>
    <opml version="2.0"><body>
      <outline type="rss" text="Keep" xmlUrl="https://example.com/keep.xml"/>
      <outline type="rss" text="New" xmlUrl="https://example.com/new.xml"/>
      <outline text="folder">
        <outline type="rss" xmlUrl="https://example.com/nested.xml"/>
      </outline>
    </body></opml>"""

    resp = client.post("/opml", content=opml.encode("utf-8"))
    assert resp.status_code == 202
    assert resp.content == b""

    created = {msg["feed_url"] for msg in queue.messages}
    assert created == {"https://example.com/new.xml", "https://example.com/nested.xml"}
    for msg in queue.messages:
        assert msg["feed_id"]


def test_opml_import_dedupes_within_document(opml_fakes):
    client, _, queue = opml_fakes
    opml = """<opml version="2.0"><body>
      <outline type="rss" xmlUrl="https://example.com/dup.xml"/>
      <outline type="rss" xmlUrl="https://example.com/dup.xml"/>
    </body></opml>"""

    resp = client.post("/opml", content=opml.encode("utf-8"))
    assert resp.status_code == 202
    assert len(queue.messages) == 1


def test_opml_import_rejects_bad_xml(opml_fakes):
    client, _, queue = opml_fakes
    resp = client.post("/opml", content=b"not xml <<<")
    assert resp.status_code == 400
    assert resp.text == "Bad Request"
    assert queue.messages == []
