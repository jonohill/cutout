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
from .config import Settings

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


async def export_opml(storage: Storage, settings: Settings) -> str:
    """Build an OPML document for every podcast stored in the bucket."""
    public_base = settings.public_service_url.rstrip("/")
    entries: list[OpmlPodcast] = []
    for feed_id in await podcasts.list_feed_ids(storage):
        title = _channel_title(await storage.get_bytes(feed_path(feed_id)))
        entries.append(
            OpmlPodcast(xml_url=f"{public_base}/podcast/{feed_id}", title=title)
        )
    logger.info("opml export: %d podcast(s)", len(entries))
    return build_opml(entries)


async def import_opml(storage: Storage, queue, body: bytes, settings: Settings) -> int:
    """Create any podcast in ``body`` not already stored; return the count.

    Matches existing podcasts on their original feed URL and skips them (and
    duplicate URLs within the document). URLs pointing back at this server's
    own ``/podcast/{feed_id}`` endpoint are skipped too, so re-importing a
    document this server exported doesn't create self-referential feeds.
    Raises ``ValueError`` if the body is not valid UTF-8 OPML.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("OPML body is not valid UTF-8") from exc
    entries = parse_opml(text)

    self_prefix = f"{settings.public_service_url.rstrip('/')}/podcast/"
    stored_ids = set(await podcasts.list_feed_ids(storage))
    seen = await podcasts.stored_feed_urls(storage)
    created = 0
    skipped_self = 0
    for entry in entries:
        if entry.xml_url in seen:
            continue
        feed_id = _self_feed_id(entry.xml_url, self_prefix)
        if feed_id is not None and feed_id in stored_ids:
            skipped_self += 1
            continue
        seen.add(entry.xml_url)
        await podcasts.enqueue_create(queue, feed_url=entry.xml_url)
        created += 1
    logger.info(
        "opml import: %d new podcast(s) from %d entries (%d self-feed skipped)",
        created,
        len(entries),
        skipped_self,
    )
    return created


def _self_feed_id(xml_url: str, self_prefix: str) -> str | None:
    """The feed_id if ``xml_url`` is one of this server's own feeds, else None.

    Only a bare ``{prefix}/podcast/{feed_id}`` is treated as a self-feed; a
    URL with a deeper path is not one of ours.
    """
    if not xml_url.startswith(self_prefix):
        return None
    remainder = xml_url[len(self_prefix) :]
    if not remainder or "/" in remainder:
        return None
    return remainder


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
