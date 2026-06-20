import pytest

from cutout.opml import OpmlPodcast, build_opml, parse_opml


def test_build_and_parse_round_trip():
    podcasts = [
        OpmlPodcast(xml_url="https://example.com/a.xml", title="Show A"),
        OpmlPodcast(xml_url="https://example.com/b.xml", title=None),
    ]
    parsed = parse_opml(build_opml(podcasts))
    assert [p.xml_url for p in parsed] == [
        "https://example.com/a.xml",
        "https://example.com/b.xml",
    ]
    assert parsed[0].title == "Show A"
    # A title-less podcast serialises with the URL as its text.
    assert parsed[1].title == "https://example.com/b.xml"


def test_parse_walks_nested_outlines():
    opml = """<opml version="2.0"><body>
      <outline text="folder">
        <outline type="rss" text="Nested" xmlUrl="https://example.com/n.xml"/>
      </outline>
      <outline text="no-feed-here"/>
    </body></opml>"""
    parsed = parse_opml(opml)
    assert len(parsed) == 1
    assert parsed[0].xml_url == "https://example.com/n.xml"
    assert parsed[0].title == "Nested"


def test_parse_rejects_malformed_xml():
    with pytest.raises(ValueError):
        parse_opml("totally not xml <<<")
