"""AppStream metainfo parsing, including recovering screenshot URLs some projects
paste in straight off a rendered GitHub page (see appstream._decamo)."""

from flatsonar_server.crawler.appstream import _decamo, parse_collection, parse_metainfo

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


# AppStream ships translations two different ways: several sibling <description
# xml:lang="X"> elements (what a collection catalogue - the multi-app file a
# Flatpak remote publishes - uses), or one <description> whose own <p> children
# carry xml:lang instead (what a single project's own *.metainfo.xml commonly
# uses). Picking the wrong one for the first form was a real bug: root.find()
# grabbed whichever language sorted first in the file, not the source text - found
# because a live app page showed a paragraph of Belarusian for an English app.

SIBLING_DESCRIPTIONS = """<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>org.example.Sibling</id>
  <name>Sibling</name>
  <description xml:lang="be">
    <p>Belarusian text that sorts before the untranslated element.</p>
  </description>
  <description>
    <p>The real, untranslated English description.</p>
  </description>
  <description xml:lang="uk">
    <p>Ukrainian text.</p>
  </description>
</component>
"""

CHILD_TAGGED_DESCRIPTION = """<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>org.example.ChildTagged</id>
  <name>ChildTagged</name>
  <description>
    <p>The untranslated paragraph.</p>
    <p xml:lang="uk">Перекладений абзац.</p>
  </description>
</component>
"""


def test_sibling_language_descriptions_pick_the_untranslated_one():
    info = parse_metainfo(SIBLING_DESCRIPTIONS)
    assert info.description == "The real, untranslated English description."


def test_child_tagged_description_paragraphs_still_filter_by_language():
    info = parse_metainfo(CHILD_TAGGED_DESCRIPTION)
    assert info.description == "The untranslated paragraph."


ICONS = """<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>org.example.Icons</id>
  <name>Icons</name>
  <icon type="stock">org.example.Icons</icon>
  <icon type="cached" width="64" height="64">org.example.Icons.png</icon>
  <icon type="cached" width="128" height="128">org.example.Icons.png</icon>
  <icon type="remote" width="128" height="128">some/relative/path.png</icon>
</component>
"""


def test_picks_the_largest_cached_icon_ignoring_stock_and_remote():
    # "stock" has no file at all, and "remote" is what real remotes turned out not
    # to serve consistently (see remotes.py) - only "cached" is both always
    # present and, per remotes.py, servable at a known static path.
    info = parse_metainfo(ICONS)
    assert info.icon == "128x128/org.example.Icons.png"


def test_no_cached_icon_is_fine():
    info = parse_metainfo(METAINFO)
    assert info.icon is None


def test_parse_collection_picks_untranslated_description_per_component():
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <components version="0.8" origin="flatpak">
    {SIBLING_DESCRIPTIONS.split('<?xml version="1.0" encoding="UTF-8"?>')[1]}
    </components>
    """
    catalogue = parse_collection(xml)
    assert catalogue["org.example.Sibling"].description == "The real, untranslated English description."
