import xml.etree.ElementTree as ET

from cutout.worker import feed_xml

ATOM = "http://www.w3.org/2005/Atom"
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"

FEED = f"""<?xml version='1.0'?>
<rss xmlns:itunes="{ITUNES}" xmlns:atom="{ATOM}">
  <channel>
    <title>Original</title>
    <itunes:new-feed-url>https://moved.example/feed.xml</itunes:new-feed-url>
    <atom:link rel="first" href="https://src.example/1"/>
    <atom:link rel="self" href="https://src.example/feed.xml"/>
    <item>
      <guid>ep-1</guid>
      <pubDate>Tue, 01 Jan 2030 00:00:00 +0000</pubDate>
      <enclosure url="https://src.example/1.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <title>no enclosure</title>
      <guid>ep-2</guid>
    </item>
  </channel>
</rss>"""


def test_parse_episodes_drops_items_without_enclosure():
    feed = feed_xml.parse_feed(FEED)
    episodes = feed_xml.parse_episodes(feed)
    assert [e.guid for e in episodes] == ["ep-1"]
    # ep-2 (no enclosure) is removed from the channel.
    assert len(feed.channel.findall("item")) == 1
    assert episodes[0].audio_url == "https://src.example/1.mp3"
    assert episodes[0].pub_date is not None


def test_get_new_feed_url():
    feed = feed_xml.parse_feed(FEED)
    assert feed_xml.get_new_feed_url(feed) == "https://moved.example/feed.xml"


def test_set_channel_title():
    feed = feed_xml.parse_feed(FEED)
    feed_xml.set_channel_title(feed, "Renamed")
    assert feed.channel.find("title").text == "Renamed"


def test_set_audio_url():
    feed = feed_xml.parse_feed(FEED)
    episode = feed_xml.parse_episodes(feed)[0]
    feed_xml.set_audio_url(episode, "https://media.example/x")
    assert episode.enclosure.get("url") == "https://media.example/x"


def test_rewrite_channel_links():
    feed = feed_xml.parse_feed(FEED)
    feed_xml.rewrite_channel_links(feed, "https://app.example/podcast/abc")

    # new-feed-url and rel=first stripped.
    assert feed.channel.find(f"{{{ITUNES}}}new-feed-url") is None
    links = feed.channel.findall(f"{{{ATOM}}}link")
    assert [link.get("rel") for link in links] == ["self"]
    assert links[0].get("href") == "https://app.example/podcast/abc"
