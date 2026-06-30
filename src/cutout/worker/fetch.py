"""HTTP fetch helpers for the media pipeline."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0)

# Episode audio is large and trickles in over time, so the read timeout is per
# chunk (each must arrive within the window), not for the whole download.
_DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=300.0)

# Transcription is slow: after the upload completes the server still has to run
# the whole episode through the model before it replies. A multi-hour episode on
# a CPU model can take far longer than the old 15-minute window, so the read cap
# is generous; the 30s connect timeout still catches a dead/unreachable service.
_TRANSCRIBE_TIMEOUT = httpx.Timeout(30.0, read=7200.0)


async def fetch_text(url: str) -> tuple[str, str]:
    """Fetch ``url`` as text. Returns ``(body, final_url)`` where ``final_url``
    reflects any HTTP redirects followed."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text, str(resp.url)


async def ping_overcast(ping_url: str, feed_url: str) -> None:
    """Nudge Overcast to re-crawl ``feed_url`` now (see overcast.fm/podcasterinfo).
    Best effort.

    ``feed_url`` is sent as the ``urlprefix`` parameter — the URL prefix Overcast
    matches against subscribed feeds.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(ping_url, params={"urlprefix": feed_url})
            resp.raise_for_status()
    except Exception:
        logger.warning("overcast ping failed for %s", feed_url, exc_info=True)


async def stream_download(url: str) -> AsyncIterator[bytes]:
    """Yield ``url``'s body in chunks, following redirects, raising on non-2xx.

    Streamed rather than buffered so a multi-hundred-MB episode never sits whole
    in memory; the caller spills the chunks to a temp file as they arrive.
    """
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT
    ) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                yield chunk


async def post_transcription(
    path: Path,
    *,
    url: str,
    model: str,
    api_key: str | None = None,
) -> dict:
    """Upload ``path`` to an OpenAI-compatible transcription endpoint and return
    the parsed JSON response.

    ``verbose_json`` is requested so the reply carries per-segment start/end
    times, which the chapters stage needs — not just the flat text. Auth is
    optional: the Bearer header is sent only when ``api_key`` is given, so an
    unauthenticated or locally hosted endpoint works too.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    data = {"model": model, "response_format": "verbose_json"}
    async with httpx.AsyncClient(timeout=_TRANSCRIBE_TIMEOUT) as client:
        with path.open("rb") as audio:
            resp = await client.post(
                url,
                headers=headers,
                data=data,
                files={"file": (path.name, audio, "application/octet-stream")},
            )
    resp.raise_for_status()
    return resp.json()
