import httpx

from cutout.worker.fetch import _permanent_url


def _redirect(url: str, status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", url))


def _final(url: str, history: list[httpx.Response]) -> httpx.Response:
    resp = httpx.Response(200, request=httpx.Request("GET", url))
    resp.history = history
    return resp


def test_permanent_url_returns_the_requested_url_when_nothing_redirected():
    assert _permanent_url(_final("https://a.example/rss", [])) == (
        "https://a.example/rss"
    )


def test_permanent_url_follows_permanent_redirects():
    history = [
        _redirect("https://a.example/rss", 301),
        _redirect("https://b.example/rss", 308),
    ]
    assert _permanent_url(_final("https://c.example/rss", history)) == (
        "https://c.example/rss"
    )


def test_permanent_url_stops_at_a_temporary_redirect():
    history = [_redirect("https://atp.fm/rss", 302)]
    final = _final("https://cdn.atp.fm/rss/public?r3zros1m", history)
    assert _permanent_url(final) == "https://atp.fm/rss"


def test_permanent_url_keeps_the_permanent_prefix_before_a_temporary_hop():
    history = [
        _redirect("https://old.example/rss", 301),
        _redirect("https://atp.fm/rss", 302),
    ]
    final = _final("https://cdn.atp.fm/rss/public?r3zros1m", history)
    assert _permanent_url(final) == "https://atp.fm/rss"
