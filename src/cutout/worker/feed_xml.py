"""Parse and rewrite podcast RSS feeds."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

# Descriptions can be very long (full show notes); we trim so they stay a light
# context hint for the chapters model rather than rivalling the transcript.
_MAX_DESCRIPTION_CHARS = 1500

_PODCAST_NAMESPACES = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
    "podcast": "https://podcastindex.org/namespace/1.0",
    "googleplay": "http://www.google.com/schemas/play-podcasts/1.0",
    "media": "http://search.yahoo.com/mrss/",
    "dc": "http://purl.org/dc/elements/1.1/",
}
for _prefix, _uri in _PODCAST_NAMESPACES.items():
    ET.register_namespace(_prefix, _uri)


@dataclass
class Episode:
    item: ET.Element
    enclosure: ET.Element
    audio_url: str
    guid: str
    media_audio: list[ET.Element]
    pub_date: datetime | None


@dataclass
class Feed:
    root: ET.Element
    channel: ET.Element

    def remove_item(self, item: ET.Element) -> None:
        self.channel.remove(item)

    def serialize(self) -> str:
        return ET.tostring(self.root, encoding="unicode", xml_declaration=True)


def parse_feed(xml_text: str) -> Feed:
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("feed has no <channel>")
    return Feed(root=root, channel=channel)


def parse_episodes(feed: Feed) -> list[Episode]:
    """Return parsed Episodes for every item in the feed.

    Items missing an enclosure URL or GUID are removed from the channel and
    skipped. The feed is otherwise left untouched.
    """
    episodes: list[Episode] = []
    for item in feed.channel.findall("item"):
        enclosure = item.find("enclosure")
        audio_url = enclosure.get("url") if enclosure is not None else None
        guid = _episode_guid(item, audio_url)
        if enclosure is None or not audio_url or not guid:
            feed.remove_item(item)
            continue
        episodes.append(
            Episode(
                item=item,
                enclosure=enclosure,
                audio_url=audio_url,
                guid=guid,
                media_audio=_audio_media_contents(item),
                pub_date=_pub_date(item),
            )
        )
    return episodes


def _pub_date(item: ET.Element) -> datetime | None:
    el = item.find("pubDate")
    if el is None or not el.text:
        return None
    try:
        return parsedate_to_datetime(el.text.strip())
    except (TypeError, ValueError):
        return None


def set_audio_url(episode: Episode, new_url: str) -> None:
    episode.enclosure.set("url", new_url)
    for el in episode.media_audio:
        el.set("url", new_url)


def get_new_feed_url(feed: Feed) -> str | None:
    """Return the value of <itunes:new-feed-url>, the source's "feed has
    permanently moved" signal, or None if absent."""
    itunes_ns = _PODCAST_NAMESPACES["itunes"]
    el = feed.channel.find(f"{{{itunes_ns}}}new-feed-url")
    if el is None or not el.text:
        return None
    value = el.text.strip()
    return value or None


def set_channel_title(feed: Feed, title: str) -> None:
    """Set the channel's <title> (and <itunes:title>, if present) to ``title``."""
    channel = feed.channel
    itunes_ns = _PODCAST_NAMESPACES["itunes"]

    title_el = channel.find("title")
    if title_el is None:
        title_el = ET.SubElement(channel, "title")
    title_el.text = title

    itunes_title = channel.find(f"{{{itunes_ns}}}title")
    if itunes_title is not None:
        itunes_title.text = title


def rewrite_channel_links(feed: Feed, self_url: str) -> None:
    """Strip source-feed identity markers and set <atom:link rel="self">.

    Removes <itunes:new-feed-url> (the explicit "feed has moved" signal that
    makes clients silently switch the subscription URL back to the source)
    and <atom:link rel="first"|"last"> pagination links. Sets (or inserts)
    <atom:link rel="self"> to self_url.
    """
    channel = feed.channel
    atom_ns = _PODCAST_NAMESPACES["atom"]
    itunes_ns = _PODCAST_NAMESPACES["itunes"]

    for el in channel.findall(f"{{{itunes_ns}}}new-feed-url"):
        channel.remove(el)

    self_link: ET.Element | None = None
    for el in list(channel.findall(f"{{{atom_ns}}}link")):
        rel = el.get("rel")
        if rel in ("first", "last"):
            channel.remove(el)
        elif rel == "self":
            self_link = el

    if self_link is None:
        self_link = ET.SubElement(channel, f"{{{atom_ns}}}link")
        self_link.set("rel", "self")
        self_link.set("type", "application/rss+xml")
    self_link.set("href", self_url)


def _audio_media_contents(item: ET.Element) -> list[ET.Element]:
    media_ns = _PODCAST_NAMESPACES["media"]
    return [
        el
        for el in item.findall(f"{{{media_ns}}}content")
        if (el.get("type") or "").startswith("audio/")
    ]


def _episode_guid(item: ET.Element, fallback: str | None) -> str | None:
    guid_el = item.find("guid")
    if guid_el is not None and guid_el.text:
        return guid_el.text.strip()
    return fallback


class _TextExtractor(HTMLParser):
    """Collect text content while dropping tags; entities are unescaped by the
    parser (``convert_charrefs`` defaults on)."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self._parts)


def _clean_text(value: str | None) -> str | None:
    """Collapse whitespace; return None for empty/whitespace-only input."""
    if not value:
        return None
    text = " ".join(value.split())
    return text or None


def _strip_html(value: str) -> str | None:
    """Plain text from a possibly-HTML string, whitespace collapsed."""
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return _clean_text(value)
    return _clean_text(parser.text)


def channel_title(feed: Feed) -> str | None:
    """The podcast's display name, or None."""
    return _clean_text(feed.channel.findtext("title"))


def episode_title(item: ET.Element) -> str | None:
    """The episode title (preferring <title>, then <itunes:title>), or None."""
    itunes_ns = _PODCAST_NAMESPACES["itunes"]
    return _clean_text(
        item.findtext("title") or item.findtext(f"{{{itunes_ns}}}title")
    )


def episode_description(item: ET.Element) -> str | None:
    """Episode summary as length-bounded plain text, or None.

    Prefers <itunes:summary>, then <description>, then <content:encoded> —
    whichever is first present and non-empty. The copy is often HTML and
    sometimes embeds its own chapter/timestamp list, so tags are stripped and
    the result is truncated to keep it a context hint, not a rival chapter
    source.
    """
    itunes_ns = _PODCAST_NAMESPACES["itunes"]
    content_ns = _PODCAST_NAMESPACES["content"]
    raw = (
        item.findtext(f"{{{itunes_ns}}}summary")
        or item.findtext("description")
        or item.findtext(f"{{{content_ns}}}encoded")
    )
    if not raw:
        return None
    text = _strip_html(raw)
    if not text:
        return None
    if len(text) > _MAX_DESCRIPTION_CHARS:
        text = text[:_MAX_DESCRIPTION_CHARS].rstrip() + "…"
    return text


def episode_context(feed: Feed, episode: Episode) -> dict[str, str]:
    """Reference metadata for the chapters model: podcast name, episode title,
    and a trimmed description. Only non-empty fields are included; an empty dict
    means there is nothing useful to pass on."""
    context: dict[str, str] = {}
    if podcast := channel_title(feed):
        context["podcast"] = podcast
    if title := episode_title(episode.item):
        context["title"] = title
    if description := episode_description(episode.item):
        context["description"] = description
    return context
