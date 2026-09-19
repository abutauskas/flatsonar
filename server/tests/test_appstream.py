"""AppStream metainfo parsing, including recovering screenshot URLs some projects
paste in straight off a rendered GitHub page (see appstream._decamo)."""

from flatsonar_server.crawler.appstream import _decamo, parse_metainfo

CAMO_URL = "https://camo.githubusercontent.com/9e2f7346616dfdb67a77c2e6b5db148d9b4669ca/68747470733a2f2f692e696d6775722e636f6d2f35313668526b532e706e67"
REAL_URL = "https://i.imgur.com/516hRkS.png"

METAINFO = """<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>io.github.alice.Foo</id>
  <name>Foo</name>
  <summary>A thing</summary>
  <screenshots>
    <screenshot type="default"><image>{camo}</image></screenshot>
    <screenshot><image>https://raw.githubusercontent.com/alice/foo/main/shot.png</image></screenshot>
  </screenshots>
</component>
""".format(camo=CAMO_URL)


def test_decamo_recovers_the_original_url():
    assert _decamo(CAMO_URL) == REAL_URL


def test_decamo_leaves_ordinary_urls_alone():
    assert _decamo(REAL_URL) == REAL_URL
    assert _decamo("https://camo.githubusercontent.com/not-forty-hex-chars/616263") == \
        "https://camo.githubusercontent.com/not-forty-hex-chars/616263"


def test_decamo_survives_bad_hex_without_crashing():
    bogus = "https://camo.githubusercontent.com/" + "a" * 40 + "/zzzz"
    assert _decamo(bogus) == bogus


def test_parse_metainfo_decamos_screenshots():
    info = parse_metainfo(METAINFO)
    assert info is not None
    assert info.screenshots == [REAL_URL, "https://raw.githubusercontent.com/alice/foo/main/shot.png"]
