"""flatpak's non-terminal output -> status text and an overall fraction."""

from flatsonar.install.progress import FlatpakProgress

# Captured from `flatpak install --user -y --no-deploy flathub app/org.gnome.TwentyFortyEight/x86_64/stable`
# with stdout piped (tabs and non-breaking spaces as flatpak prints them).
OUTPUT = [
    "Looking for matches…",
    "",
    "org.gnome.TwentyFortyEight permissions:",
    "    ipc\tfallback-x11\twayland\tx11\tdri",
    "",
    " 1.\t   \torg.gnome.TwentyFortyEight.Locale\tstable\ti\tflathub\t< 196.4\xa0kB (partial)",
    " 2.\t   \torg.gnome.TwentyFortyEight\tstable\ti\tflathub\t< 1.0\xa0MB",
    "",
    "Installing 1/2…",
    "Installing 1/2…                        0%  0 bytes/s",
    "Installing 1/2… ████████████▌         66%  6.3\xa0kB/s",
    "Installing 1/2… ████████████          65%  6.3\xa0kB/s",
    "Installing 1/2… ████████████████████ 100%  72.1\xa0kB/s",
    "Installing 2/2…",
    "Installing 2/2… ████                  20%  2.1\xa0MB/s  00:12",
    "Installing 2/2… ████████████████████ 100%  2.4\xa0MB/s",
    "Installation complete.",
]


def _feed(parser, lines):
    return [tick for line in lines if (tick := parser.feed(line)) is not None]


def test_only_progress_lines_produce_status():
    ticks = _feed(FlatpakProgress("org.gnome.TwentyFortyEight", "2048"), OUTPUT)
    assert len(ticks) == 8  # the table, permissions and "complete" lines are not status
    assert ticks[0][0] == "Downloading 2048 translations · 1 of 2"
    assert ticks[-1] == ("Downloading 2048 · 2 of 2 · 100% · 2.4 MB/s", 1.0)


def test_speed_and_eta_are_shown_zero_speed_is_not():
    ticks = _feed(FlatpakProgress("org.gnome.TwentyFortyEight", "2048"), OUTPUT)
    texts = [t for t, _ in ticks]
    assert "Downloading 2048 translations · 1 of 2 · 0%" in texts
    assert "Downloading 2048 · 2 of 2 · 20% · 2.1 MB/s · 00:12 left" in texts


def test_fraction_is_weighted_by_download_size_and_never_goes_back():
    ticks = _feed(FlatpakProgress("org.gnome.TwentyFortyEight", "2048"), OUTPUT)
    fractions = [f for _, f in ticks]
    assert fractions == sorted(fractions)  # flatpak's 66% -> 65% does not move the bar back
    # The 196 kB locale is ~16% of the 1.2 MB total, so finishing it is not "50%".
    done_locale = fractions[4]  # "Installing 1/2… 100%"
    assert 0.15 < done_locale < 0.18


def test_without_a_table_steps_count_equally():
    p = FlatpakProgress("org.x.App", "App", verb="Updating")
    text, fraction = p.feed("Updating 2/4… ██████████           50%  1.0\xa0MB/s")
    assert text == "Updating App · 2 of 4 · 50% · 1.0 MB/s"
    assert fraction == 0.375


def test_runtimes_are_named_by_id_and_branch():
    p = FlatpakProgress("org.x.App", "App")
    p.feed(" 1.\t   \torg.gnome.Platform\t50\ti\tflathub\t< 391.3\xa0MB")
    p.feed(" 2.\t   \torg.x.App\tstable\ti\tflathub\t< 2.0\xa0MB")
    text, _ = p.feed("Installing 1/2… ███   15%  8.0\xa0MB/s  00:40")
    assert text == "Downloading org.gnome.Platform 50 · 1 of 2 · 15% · 8.0 MB/s · 00:40 left"
