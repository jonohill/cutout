"""The OPML feature: serialise the stored podcasts to an OPML subscription
list, and import one back.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from . import podcasts
from .common import feed_path
from .common.storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class OpmlPodcast:
    """One subscription: the source feed URL and an optional display title."""

    xml_url: str
    title: str | None = None


def build_opml(entries: list[OpmlPodcast], *, title: str = "cutout") -> str:
    """Serialise ``entries`` to an OPML 2.0 document."""
    opml = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = title
    body = ET.SubElement(opml, "body")
    for entry in entries:
        text = entry.title or entry.xml_url
        ET.SubElement(
            body,
            "outline",
            {"type": "rss", "text": text, "title": text, "xmlUrl": entry.xml_url},
        )
    return ET.tostring(opml, encoding="unicode", xml_declaration=True)


def parse_opml(xml_text: str) -> list[OpmlPodcast]:
    """Return every ``<outline>`` carrying an ``xmlUrl``.

    Outlines nest (clients group subscriptions into folders), so this walks the
    whole tree rather than just the top level. Raises ``ValueError`` if the
    document is not well-formed XML.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"invalid OPML: {exc}") from exc
    entries: list[OpmlPodcast] = []
    for outline in root.iter("outline"):
        xml_url = outline.get("xmlUrl") or outline.get("xmlurl")
        if not xml_url:
            continue
        title = outline.get("text") or outline.get("title")
        entries.append(OpmlPodcast(xml_url=xml_url, title=title))
    return entries


async def export_opml(storage: Storage) -> str:
    """Build an OPML document for every podcast stored in the bucket."""
    entries: list[OpmlPodcast] = []
    for feed_id in await podcasts.list_feed_ids(storage):
        feed_url = await podcasts.feed_source_url(storage, feed_id)
        if not feed_url:
            continue
        title = _channel_title(await storage.get_bytes(feed_path(feed_id)))
        entries.append(OpmlPodcast(xml_url=feed_url, title=title))
    logger.info("opml export: %d podcast(s)", len(entries))
    return build_opml(entries)


async def import_opml(storage: Storage, queue, body: bytes) -> int:
    """Create any podcast in ``body`` not already stored; return the count.

    Matches existing podcasts on their original feed URL and skips them (and
    duplicate URLs within the document). Raises ``ValueError`` if the body is
    not valid UTF-8 OPML.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("OPML body is not valid UTF-8") from exc
    entries = parse_opml(text)

    seen = await podcasts.stored_feed_urls(storage)
    created = 0
    for entry in entries:
        if entry.xml_url in seen:
            continue
        seen.add(entry.xml_url)
        await podcasts.enqueue_create(queue, feed_url=entry.xml_url)
        created += 1
    logger.info("opml import: %d new podcast(s) from %d entries", created, len(entries))
    return created


def _channel_title(raw: bytes | None) -> str | None:
    """The channel <title> from a stored feed document, or None if unavailable."""
    if not raw:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    title = root.findtext("channel/title")
    return title.strip() if title and title.strip() else None
