"""Small reusable widgets, and the stylesheet that makes the client look like the website.

The palette, radii and component shapes follow server/flatsonar_server/web/static/style.css:
white cards on a pale teal-grey page (navy in dark mode), a teal accent, round "pill"
controls, the radar mark, and the site's trust badges. The libadwaita colour variables
are mapped onto the same palette so stock widgets (dialogs, buttons, lists) match too.
Needs GTK >= 4.16 / libadwaita >= 1.6 for CSS variables; older versions ignore those
declarations and fall back to stock Adwaita colours. LIGHT or DARK goes in front of CSS
depending on the style manager (see :func:`install_style`).
"""

from __future__ import annotations

import math
from pathlib import Path

from gi.repository import Adw, Gdk, Gtk, Pango

from .api import AppInfo
from .icons import load_into
from .text import RISK_TEXT, RISK_TIP, TRUST_TEXT, TRUST_TIP, funding_label

ASSETS = Path(__file__).parent / "assets"

# Full-colour badge icons, the same files the website serves. Unverified gets none:
# the absence of a badge is the signal, as on the site.
TRUST_ICON = {"verified": "flatsonar-verified", "reviewed": "flatsonar-reviewed",
              "suspicious": "flatsonar-suspicious"}

LIGHT = """
:root {
  --fs-bg: #f4f7f6;
  --fs-surface: #ffffff;
  --fs-surface-2: #e9efed;
  --fs-text: #1e2933;
  --fs-muted: #5b6771;
  --fs-border: #e2e8e6;
  --fs-accent: #0f9d8a;
  --fs-accent-ink: #0b7f70;
  --fs-accent-soft: #d6f2ec;
  --fs-green: #2b7a4c;
  --fs-yellow: #8f5a1a;
  --fs-red: #bb4747;
  --fs-blue: #3f6fc4;
  --fs-pink: #b04a80;
  --fs-green-soft: rgba(43, 122, 76, .11);
  --fs-yellow-soft: rgba(143, 90, 26, .12);
  --fs-red-soft: rgba(187, 71, 71, .10);
  --fs-pink-soft: rgba(176, 74, 128, .14);
  --fs-pink-line: rgba(176, 74, 128, .30);
  --fs-support-bg: #faf2f6;
  --fs-support-line: #d6c1cd;
  --fs-hover-line: #6fbfb3;
  --fs-focus: rgba(15, 157, 138, .25);
  --fs-card-shadow: 0 1px 2px rgba(15, 23, 32, .03);
  --fs-shadow: 0 1px 2px rgba(15, 23, 32, .04), 0 8px 24px -12px rgba(15, 23, 32, .13);
}
"""

DARK = """
:root {
  --fs-bg: #0b1116;
  --fs-surface: #121a21;
  --fs-surface-2: #1a242d;
  --fs-text: #dde4e8;
  --fs-muted: #94a3ad;
  --fs-border: #232f38;
  --fs-accent: #4fc3ac;
  --fs-accent-ink: #7fd9c2;
  --fs-accent-soft: #0f2f2b;
  --fs-green: #7cc99a;
  --fs-yellow: #e0b563;
  --fs-red: #e08a8a;
  --fs-blue: #a8c6ee;
  --fs-pink: #e0a0c0;
  --fs-green-soft: rgba(124, 201, 154, .11);
  --fs-yellow-soft: rgba(224, 181, 99, .12);
  --fs-red-soft: rgba(224, 138, 138, .10);
  --fs-pink-soft: rgba(224, 160, 192, .14);
  --fs-pink-line: rgba(224, 160, 192, .30);
  --fs-support-bg: #202329;
  --fs-support-line: #524b5a;
  --fs-hover-line: #3b8078;
  --fs-focus: rgba(79, 195, 172, .25);
  --fs-card-shadow: 0 1px 2px rgba(0, 0, 0, .12);
  --fs-shadow: 0 1px 2px rgba(0, 0, 0, .22), 0 10px 30px -14px rgba(0, 0, 0, .45);
}
"""

CSS = """
/* libadwaita's own colours, pointed at the palette so stock widgets match */
:root {
  --accent-bg-color: var(--fs-accent);
  --accent-fg-color: #041210;
  --accent-color: var(--fs-accent-ink);
  --window-bg-color: var(--fs-bg);
  --window-fg-color: var(--fs-text);
  --view-bg-color: var(--fs-surface);
  --view-fg-color: var(--fs-text);
  --headerbar-bg-color: var(--fs-bg);
  --headerbar-fg-color: var(--fs-text);
  --headerbar-backdrop-color: var(--fs-bg);
  --sidebar-bg-color: var(--fs-bg);
  --sidebar-fg-color: var(--fs-text);
  --sidebar-backdrop-color: var(--fs-bg);
  --sidebar-border-color: var(--fs-border);
  --card-bg-color: var(--fs-surface);
  --card-fg-color: var(--fs-text);
  --dialog-bg-color: var(--fs-surface);
  --dialog-fg-color: var(--fs-text);
  --popover-bg-color: var(--fs-surface);
  --popover-fg-color: var(--fs-text);
  --success-color: var(--fs-green);
  --warning-color: var(--fs-yellow);
  --error-color: var(--fs-red);
}
/* --- type ------------------------------------------------------------------------ */
.fs-brand { font-weight: 800; font-size: 1.3em; letter-spacing: -.02em; }
.fs-radar { color: var(--fs-accent); }
.fs-muted { color: var(--fs-muted); }
.fs-fine { color: var(--fs-muted); font-size: .92em; }
.fs-kicker { font-size: .78em; font-weight: 700; letter-spacing: .1em; color: var(--fs-muted); }
.fs-h1 { font-size: 2.3em; font-weight: 800; letter-spacing: -.02em; }
.fs-h2 { font-size: 1.45em; font-weight: 700; letter-spacing: -.02em; }
.fs-h3 { font-size: 1.15em; font-weight: 700; }
.fs-h4 { font-weight: 700; }
.fs-lede { font-size: 1.15em; }
.fs-code { font-family: monospace; font-size: .86em; color: var(--fs-muted); }
.fs-link { color: var(--fs-accent-ink); }

/* --- header, toolbar ------------------------------------------------------------- */
headerbar { box-shadow: none; }
button.fs-nav { border-radius: 999px; padding: 6px 14px; background: none; color: var(--fs-muted); font-weight: 500; }
button.fs-nav:hover { background: var(--fs-surface-2); color: var(--fs-text); }
button.fs-nav.fs-has-updates { background: var(--fs-accent-soft); color: var(--fs-accent-ink); font-weight: 600; }
entry.fs-search { border-radius: 999px; min-height: 40px; padding: 0 14px; background: var(--fs-surface);
  border: 1px solid var(--fs-border); box-shadow: none; }
entry.fs-search:focus-within { border-color: var(--fs-accent); box-shadow: 0 0 0 3px var(--fs-focus); }
dropdown.fs-select > button { border-radius: 999px; min-height: 40px; padding: 0 14px; background: var(--fs-surface);
  border: 1px solid var(--fs-border); box-shadow: none; font-weight: 500; }
dropdown.fs-select > button:hover { border-color: var(--fs-hover-line); }
dropdown.fs-select > button:checked { border-color: var(--fs-accent); box-shadow: 0 0 0 3px var(--fs-focus); }

/* --- sidebar --------------------------------------------------------------------- */
list.fs-cats { background: none; }
list.fs-cats > row { border-radius: 8px; padding: 7px 10px; margin: 1px 10px; }
list.fs-cats > row:hover { background: var(--fs-surface-2); }
list.fs-cats > row:selected { background: var(--fs-accent-soft); color: var(--fs-accent-ink); }
list.fs-cats > row:selected .fs-cat-name { font-weight: 600; }
list.fs-cats > row:selected .fs-muted { color: var(--fs-accent-ink); }

/* --- app cards ------------------------------------------------------------------- */
flowbox.fs-grid > flowboxchild { padding: 0; background: none; }
button.fs-card { font-weight: normal; background: var(--fs-surface); color: var(--fs-text); border: 1px solid var(--fs-border);
  border-radius: 20px; padding: 22px; box-shadow: var(--fs-card-shadow);
  transition: box-shadow 140ms ease, border-color 140ms ease, background 140ms ease; }
button.fs-card:hover { background: var(--fs-surface); border-color: var(--fs-hover-line); box-shadow: var(--fs-shadow); }
button.fs-card:active { background: var(--fs-surface-2); }
.fs-app-icon { border-radius: 18px; background: var(--fs-surface-2); }
.fs-card-name { font-weight: 700; font-size: 1.2em; }
.fs-count { color: var(--fs-muted); }
.fs-dim { opacity: .4; }
.fs-empty { background: var(--fs-surface); border: 1px dashed var(--fs-border); border-radius: 14px; padding: 56px 20px; }
button.fs-pager { border-radius: 999px; border: 1px solid var(--fs-border); background: none; padding: 8px 16px;
  box-shadow: none; font-weight: 500; }
button.fs-pager:hover { border-color: var(--fs-accent); background: none; }

/* --- pills and badges ------------------------------------------------------------ */
.fs-pill { border-radius: 999px; padding: 3px 10px; font-size: .8em; font-weight: 600;
  background: var(--fs-surface-2); color: var(--fs-muted); }
.fs-pill.big { font-size: .9em; padding: 5px 12px; }
.fs-pill.green { background: var(--fs-green-soft); color: var(--fs-green); }
.fs-pill.yellow { background: var(--fs-yellow-soft); color: var(--fs-yellow); }
.fs-pill.red { background: var(--fs-red-soft); color: var(--fs-red); }
.fs-dot { min-width: 7px; min-height: 7px; border-radius: 999px; background: currentColor; }
.fs-trust { font-weight: 600; font-size: .9em; }
.fs-trust.verified { color: var(--fs-accent-ink); }
.fs-trust.reviewed { color: var(--fs-blue); }
.fs-trust.suspicious { color: var(--fs-red); }
.fs-trust-corner { border-radius: 999px; background: var(--fs-surface); box-shadow: 0 0 0 2px var(--fs-surface);
  transform: translate(4px, 4px); }

/* --- app page -------------------------------------------------------------------- */
.fs-hero-icon { border-radius: 26px; background: var(--fs-surface-2); box-shadow: var(--fs-shadow); }
button.fs-ghost { background: none; border: 1px solid alpha(currentColor, .3); box-shadow: none; }
button.fs-ghost:hover { background: alpha(currentColor, .08); }
button.fs-support { border-radius: 999px; padding: 7px 14px; background: var(--fs-pink-soft); color: var(--fs-pink);
  border: 1px solid var(--fs-pink-line); box-shadow: none; font-weight: 600; }
button.fs-support:hover { background: var(--fs-pink-line); }
.fs-support-box { background: var(--fs-support-bg); border: 1px solid var(--fs-support-line); border-radius: 14px;
  padding: 22px 26px; }
.fs-panel { background: var(--fs-surface); border: 1px solid var(--fs-border); border-radius: 14px; padding: 22px; }
.fs-panel-rule { border-top: 1px solid var(--fs-border); padding-top: 14px; }
.fs-findings { border-top: 1px solid var(--fs-border); }
.fs-finding { border-bottom: 1px solid var(--fs-border); padding: 12px 0; }
.fs-lvl { min-width: 10px; min-height: 10px; border-radius: 999px; background: var(--fs-muted); margin-top: 5px; }
.fs-lvl.green { background: var(--fs-green); }
.fs-lvl.yellow { background: var(--fs-yellow); }
.fs-lvl.red { background: var(--fs-red); }
.fs-what { font-weight: 500; }
.fs-shot { border-radius: 12px; border: 1px solid var(--fs-border); background: var(--fs-surface-2); }
button.fs-shot-btn { padding: 0; background: none; border-radius: 12px; box-shadow: none; }
.fs-progress-area { padding: 12px 28px 14px; background: var(--fs-surface); border-top: 1px solid var(--fs-border); }
progressbar.fs-progress > trough { min-height: 8px; border-radius: 999px; background: var(--fs-surface-2); }
progressbar.fs-progress > trough > progress { min-height: 8px; border-radius: 999px; background: var(--fs-accent); }

/* --- installed page: boxed lists as the site's panels ---------------------------- */
list.boxed-list { background: var(--fs-surface); border: 1px solid var(--fs-border); border-radius: 14px; box-shadow: none; }
"""


def install_style(display: Gdk.Display) -> None:
    """The stylesheet above, plus the bundled icons (trust badges, heart, fallback).

    The palette is swapped by hand when libadwaita goes light <-> dark rather than with
    ``@media (prefers-color-scheme)``: GTK evaluates that query against the system
    setting, which is not the same thing as the style manager's answer (an app or the
    user forcing a scheme), and GTK < 4.16 does not support it at all."""
    css = Gtk.CssProvider()
    style = Adw.StyleManager.get_default()

    def _load(*_args) -> None:
        text = (DARK if style.get_dark() else LIGHT) + CSS
        if hasattr(css, "load_from_string"):  # GTK >= 4.12
            css.load_from_string(text)
        else:
            css.load_from_data(text.encode(), -1)

    _load()
    style.connect("notify::dark", _load)
    Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    Gtk.IconTheme.get_for_display(display).add_search_path(str(ASSETS))


def label(text: str = "", *classes: str, **kw) -> Gtk.Label:
    kw.setdefault("xalign", 0)
    lbl = Gtk.Label(label=text, **kw)
    for c in classes:
        lbl.add_css_class(c)
    return lbl


class RadarMark(Gtk.DrawingArea):
    """The site's logo, drawn in the accent colour: rings, a sonar sweep, one blip.
    ``spinning`` turns the sweep into the catalogue's loading indicator."""

    def __init__(self, size: int = 28, spinning: bool = False):
        super().__init__(content_width=size, content_height=size, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.add_css_class("fs-radar")
        self._angle = 0.0
        self.set_draw_func(self._draw)
        if spinning:
            self.add_tick_callback(self._tick)

    def _tick(self, _widget, clock) -> bool:
        self._angle = (clock.get_frame_time() / 1_100_000) % 1 * 2 * math.pi  # one turn per 1.1 s
        self.queue_draw()
        return True

    def _draw(self, _area, cr, w: int, h: int) -> None:
        c = self.get_color() if hasattr(self, "get_color") else self.get_style_context().get_color()
        s = min(w, h) / 32
        cr.translate((w - 32 * s) / 2, (h - 32 * s) / 2)
        cr.scale(s, s)

        def ink(alpha: float) -> None:
            cr.set_source_rgba(c.red, c.green, c.blue, c.alpha * alpha)

        for r, width, alpha in ((11, 1.3, .55), (6, 1.0, .4)):
            cr.set_line_width(width)
            ink(alpha)
            cr.arc(16, 16, r, 0, 2 * math.pi)
            cr.stroke()
        ink(.3)
        cr.move_to(16, 5)
        cr.line_to(16, 27)
        cr.move_to(5, 16)
        cr.line_to(27, 16)
        cr.stroke()

        cr.save()
        cr.translate(16, 16)
        cr.rotate(self._angle)
        cr.translate(-16, -16)
        top = -math.pi / 2
        for end, alpha in ((0.0, .12), (math.atan2(-6.3, 9), .22), (math.atan2(-9.97, 4.65), .4)):
            ink(alpha)
            cr.move_to(16, 16)
            cr.arc(16, 16, 11, top, end)
            cr.close_path()
            cr.fill()
        cr.restore()

        ink(1)
        cr.arc(20, 9.07, 2.2, 0, 2 * math.pi)
        cr.fill()
        cr.arc(16, 16, 1.3, 0, 2 * math.pi)
        cr.fill()


def brand(size: int = 28) -> Gtk.Widget:
    box = Gtk.Box(spacing=10)
    box.append(RadarMark(size))
    box.append(label("Flatsonar", "fs-brand"))
    return box


def risk_pill(level: str, big: bool = False) -> Gtk.Widget:
    pill = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER, halign=Gtk.Align.START)
    pill.add_css_class("fs-pill")
    pill.add_css_class(level)
    if big:
        pill.add_css_class("big")
    dot = Gtk.Box(valign=Gtk.Align.CENTER)
    dot.add_css_class("fs-dot")
    pill.append(dot)
    pill.append(Gtk.Label(label=RISK_TEXT.get(level, level)))
    pill.set_tooltip_text(RISK_TIP.get(level, ""))
    return pill


def trust_mark(level: str, size: int = 22) -> Gtk.Image | None:
    """Just the badge icon (cards, the icon's corner); None for unverified."""
    name = TRUST_ICON.get(level)
    if not name:
        return None
    img = Gtk.Image(icon_name=name, pixel_size=size, valign=Gtk.Align.CENTER)
    img.set_tooltip_text(TRUST_TEXT.get(level, level))
    return img


def trust_badge(level: str) -> Gtk.Widget | None:
    """Badge icon and its label (the app page); None for unverified, as on the site."""
    img = trust_mark(level, 16)
    if img is None:
        return None
    box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
    box.add_css_class("fs-trust")
    box.add_css_class(level)
    box.append(img)
    box.append(Gtk.Label(label=TRUST_TEXT.get(level, level)))
    box.set_tooltip_text(TRUST_TIP.get(level, ""))
    return box


def maintenance_note(app: AppInfo) -> str:
    if app.maintenance == "abandoned" or app.archived:
        return "looks abandoned"
    if app.maintenance == "stale":
        return "quiet for a while"
    return ""


class AppCard(Gtk.Button):
    """Catalogue tile, laid out like the website's: icon left; name with its trust
    badge, "by developer", and a two-line summary on the right."""

    def __init__(self, app: AppInfo):
        super().__init__(has_frame=False, valign=Gtk.Align.START, width_request=250)
        self.app = app
        self.add_css_class("fs-card")
        row = Gtk.Box(spacing=18)

        icon = Gtk.Image(pixel_size=80, valign=Gtk.Align.START)
        icon.add_css_class("fs-app-icon")
        icon.set_overflow(Gtk.Overflow.HIDDEN)
        load_into(icon, app.icon_url)
        row.append(icon)

        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.START)
        name_row = Gtk.Box(spacing=6)
        name_row.append(label(app.name, "fs-card-name", ellipsize=Pango.EllipsizeMode.END))
        mark = trust_mark(app.trust, 24)
        if mark is not None:
            name_row.append(mark)
        meta.append(name_row)
        if app.developer_name:
            note = maintenance_note(app)
            meta.append(label(f"by {app.developer_name}" + (f" · {note}" if note else ""), "fs-fine",
                              ellipsize=Pango.EllipsizeMode.END))
        if app.summary:
            # WORD_CHAR: a wrapping label's minimum width is its longest word, and some
            # summaries are one long token (an unexpanded "@CMAKE_PROJECT_DESCRIPTION@").
            meta.append(label(app.summary, "fs-muted", wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, lines=2,
                              ellipsize=Pango.EllipsizeMode.END, max_width_chars=30, margin_top=3))
        row.append(meta)
        self.set_child(row)
        # Measured by do_measure below instead of the button's BinLayout.
        self.set_layout_manager(None)

    def do_measure(self, orientation, for_size):
        """Natural width = minimum width. FlowBox fits as many columns as the widest
        card's *natural* width allows, so one long name would otherwise cost the whole
        grid a column; this way the grid decides the width (3 columns, as on the site,
        when there is room) and names ellipsize within it."""
        minimum, natural, _mb, _nb = self.get_child().measure(orientation, for_size)
        if orientation == Gtk.Orientation.HORIZONTAL:
            natural = minimum
        return minimum, natural, -1, -1

    def do_size_allocate(self, width, height, baseline):
        self.get_child().allocate(width, height, baseline, None)


def findings_list(items: list[tuple[str, str, str]]) -> Gtk.Widget:
    """``(level, what, code)`` rows with a coloured dot, like the site's permission list."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.add_css_class("fs-findings")
    for level, what, code in items:
        row = Gtk.Box(spacing=14)
        row.add_css_class("fs-finding")
        dot = Gtk.Box(valign=Gtk.Align.START)
        dot.add_css_class("fs-lvl")
        dot.add_css_class(level)
        dot.set_tooltip_text(level)
        row.append(dot)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        col.append(label(what, "fs-what", wrap=True))
        if code:
            col.append(label(code, "fs-code", wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True))
        row.append(col)
        box.append(row)
    return box


def panel(title: str | None = None) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.add_css_class("fs-panel")
    if title:
        box.append(label(title, "fs-h3"))
    return box


def wrap_box(spacing: int = 8, line_spacing: int | None = None) -> Gtk.Widget:
    """A row whose children flow onto more lines when narrow: Adw.WrapBox (libadwaita
    >= 1.7), else a FlowBox. Both have ``append``."""
    lines = spacing if line_spacing is None else line_spacing
    if hasattr(Adw, "WrapBox"):
        return Adw.WrapBox(child_spacing=spacing, line_spacing=lines)
    return Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=spacing, row_spacing=lines)


def open_uri(uri: str) -> None:
    Gtk.UriLauncher(uri=uri).launch(None, None, None)


def support_button(link: dict[str, str]) -> Gtk.Button:
    btn = Gtk.Button(tooltip_text=link["url"])
    box = Gtk.Box(spacing=8)
    box.append(Gtk.Image(icon_name="flatsonar-heart-symbolic"))
    box.append(Gtk.Label(label=funding_label(link)))
    btn.set_child(box)
    btn.add_css_class("fs-support")
    btn.connect("clicked", lambda _b: open_uri(link["url"]))
    return btn
