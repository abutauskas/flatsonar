from flatsonar.text import funding_label, name_sort_key, nicedate, paragraphs


def test_paragraphs_join_hard_wrapped_lines_and_keep_bullets():
    text = "Play the highly addictive 2048 game.\n 2048 is a clone.\n\n- one\n- two\n\n\nLast."
    assert paragraphs(text) == ["Play the highly addictive 2048 game. 2048 is a clone.", ["one", "two"], "Last."]
    assert paragraphs(None) == []


def test_funding_label_names_the_handle_except_for_custom_links():
    assert funding_label({"platform": "github", "url": "https://github.com/sponsors/alice"}) == "GitHub Sponsors · alice"
    assert funding_label({"platform": "custom", "url": "https://example.org/donate"}) == "Donate"
    assert funding_label({"platform": "some_new_site", "url": ""}) == "Some New Site"


def test_nicedate():
    assert nicedate("2024-03-05T12:00:00Z") == "Mar 5, 2024"
    assert nicedate("2024-03-05T12:00:00+00:00", "%b %Y") == "Mar 2024"
    assert nicedate("not a date") == "not a date"
    assert nicedate(None) == ""


def test_name_sort_key_puts_punctuation_names_after_z():
    names = ["Zed", "@APP_NAME@", "-", "abacus", "2048", "Beta", ""]
    assert sorted(names, key=name_sort_key) == ["2048", "abacus", "Beta", "Zed", "", "-", "@APP_NAME@"]
