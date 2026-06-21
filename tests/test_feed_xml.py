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


CONTENT = "http://purl.org/rss/1.0/modules/content/"

CONTEXT_FEED = f"""<?xml version='1.0'?>
<rss xmlns:itunes="{ITUNES}" xmlns:content="{CONTENT}">
  <channel>
    <title>The Show &amp; Friends</title>
    <item>
      <title>Episode 7: Guests</title>
      <guid>ep-7</guid>
      <itunes:summary>Host &lt;b&gt;Renée&lt;/b&gt; chats with Tāmati.</itunes:summary>
      <description>fallback should not win</description>
      <enclosure url="https://src.example/7.mp3" type="audio/mpeg"/>
    </item>
  </channel>
</rss>"""


def test_episode_context_extracts_and_cleans_metadata():
    feed = feed_xml.parse_feed(CONTEXT_FEED)
    episode = feed_xml.parse_episodes(feed)[0]
    context = feed_xml.episode_context(feed, episode)
    assert context["podcast"] == "The Show & Friends"
    assert context["title"] == "Episode 7: Guests"
    # itunes:summary wins over description; HTML stripped, diacritics preserved.
    assert context["description"] == "Host Renée chats with Tāmati."


def test_episode_description_truncates_long_copy():
    long = "word " * 1000
    item = ET.fromstring(f"<item><description>{long}</description></item>")
    description = feed_xml.episode_description(item)
    assert description is not None
    assert len(description) <= feed_xml._MAX_DESCRIPTION_CHARS + 1  # plus ellipsis
    assert description.endswith("…")


def test_episode_context_omits_missing_fields():
    feed = feed_xml.parse_feed(FEED)  # items here carry no title/description
    episode = feed_xml.parse_episodes(feed)[0]
    context = feed_xml.episode_context(feed, episode)
    assert context == {"podcast": "Original"}


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
