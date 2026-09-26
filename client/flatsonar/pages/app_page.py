"""App details, laid out like the website's app page: header with icon, trust badge and
actions; the creators' support box; then permissions, publisher and maintenance on
the left and the install sources and facts on the right (one column when narrow).

The page opens immediately with what the catalogue card already knew and fills in the
rest when the full record arrives, so a click never waits on the network."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk, Pango

from ..api import AppInfo
from ..icons import load_into, load_texture
from ..install.flatpak_cli import InstalledApp
from ..text import MAINTENANCE_TIP, SOURCE_TEXT, TRUST_TIP, grouped, nicedate, paragraphs
from ..widgets import (
    RadarMark,
    findings_list,
    label,
    maintenance_note,
    open_uri,
    panel,
    risk_pill,
    support_button,
    trust_badge,
    trust_mark,
    wrap_box,
)

REPORT_URL = "https://github.com/abutauskas/flatsonar/issues/new?title="
# Implementation tags, not something to browse by (the site's sidebar drops them too).
TOOLKIT_CATEGORIES = {"COSMIC", "DDE", "GNOME", "GTK", "Java", "KDE", "LXQt", "Motif", "Qt", "XFCE", "ConsoleOnly",
                      "Core"}
LEVEL_ORDER = {"red": 0, "yellow": 1, "green": 2}


def _clear(box: Gtk.Box) -> None:
    while (child := box.get_first_child()) is not None:
        box.remove(child)


def _section(title: str, sub: str = "") -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.append(label(title, "fs-h2"))
    if sub:
        box.append(label(sub, "fs-muted", wrap=True))
    return box


class AppPage(Adw.NavigationPage):
    def __init__(self, app: AppInfo, installed: InstalledApp | None, update: bool,
                 on_install: Callable[[AppInfo], None], on_uninstall: Callable[[AppInfo], None],
                 on_launch: Callable[[AppInfo], None], on_update: Callable[[AppInfo], None],
                 complete: bool = True):
        super().__init__(title=app.name, tag=f"app:{app.app_id}")
        self.app = app
        self.complete = complete  # False: only the catalogue summary so far
        self.installed = installed
        self._update = update
        self._busy = False
        self._pulse: int | None = None
        self._on_install, self._on_uninstall, self._on_launch = on_install, on_uninstall, on_launch
        self._on_update = on_update

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=1180, tightening_threshold=900)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=12, margin_bottom=64, margin_start=28,
                       margin_end=28)
        clamp.set_child(page)
        scroller.set_child(clamp)
        toolbar.set_content(scroller)

        # Install/update progress: a bottom bar, so it stays in view while scrolling.
        self.toolbar = toolbar
        progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        progress_box.add_css_class("fs-progress-area")
        self.progress = Gtk.ProgressBar()
        self.progress.add_css_class("fs-progress")
        self.progress_lbl = label("", "fs-fine", ellipsize=Pango.EllipsizeMode.END)
        progress_box.append(self.progress_lbl)
        progress_box.append(self.progress)
        toolbar.add_bottom_bar(progress_box)
        toolbar.set_reveal_bottom_bars(False)

        # Header: icon (trust badge on its corner) | titles | actions.
        self._level = 0  # 0 wide, 1 one column, 2 narrow (see _layout)
        self.support_box: Gtk.Box | None = None
        self.head = head = Gtk.Box(spacing=26)
        overlay = Gtk.Overlay(valign=Gtk.Align.START)
        self.icon = Gtk.Image(pixel_size=112)
        self.icon.add_css_class("fs-hero-icon")
        self.icon.set_overflow(Gtk.Overflow.HIDDEN)
        overlay.set_child(self.icon)
        self.corner = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.END)
        overlay.add_overlay(self.corner)
        head.append(overlay)
        self.titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        head.append(self.titles)
        self.actions = self._actions()
        head.append(self.actions)
        page.append(head)
        self.actions_slot = Gtk.Box(margin_top=18, visible=False)  # where the actions go when narrow
        page.append(self.actions_slot)


        self.support_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=32)
        page.append(self.support_slot)

        self.body = Gtk.Box(spacing=40, margin_top=32)
        self.main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=40, hexpand=True)
        self.aside = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, width_request=340)
        self.body.append(self.main)
        self.body.append(self.aside)
        page.append(self.body)

        # Narrower windows: one column with the Install/Details panels first (as on the
        # site), then the action buttons under the title. Only one breakpoint is active at
        # a time, and moving widgets needs code, so each one just sets a level.
        bin_ = Adw.BreakpointBin(width_request=360, height_request=300)
        bin_.set_child(toolbar)
        for level, condition in ((1, "max-width: 880sp"), (2, "max-width: 640sp")):
            bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(condition))
            bp.connect("apply", lambda _bp, lvl=level: self._layout(lvl))
            bp.connect("unapply", lambda _bp: self._layout(0))
            bin_.add_breakpoint(bp)
        self.set_child(bin_)

        self._render()

    # --- building -------------------------------------------------------------------

    def _layout(self, level: int) -> None:
        self._level = level
        one_col, narrow = level >= 1, level >= 2
        self.body.set_orientation(Gtk.Orientation.VERTICAL if one_col else Gtk.Orientation.HORIZONTAL)
        self.body.reorder_child_after(self.aside, None if one_col else self.main)
        self.aside.set_size_request(-1 if one_col else 340, -1)
        self.icon.set_pixel_size(72 if one_col else 112)
        target = self.actions_slot if narrow else self.head
        if self.actions.get_parent() is not target:
            self.actions.get_parent().remove(self.actions)
            target.append(self.actions)
        self.actions.set_orientation(Gtk.Orientation.HORIZONTAL if narrow else Gtk.Orientation.VERTICAL)
        self.actions_slot.set_visible(narrow)
        if self.support_box is not None:
            self.support_box.set_orientation(Gtk.Orientation.VERTICAL if narrow else Gtk.Orientation.HORIZONTAL)

    def show_details(self, app: AppInfo) -> None:
        """The full record arrived: re-render with it."""
        self.app, self.complete = app, True
        self.set_title(app.name)
        self._render()

    def show_loading(self) -> None:
        self.complete = False
        self._render()

    def show_error(self, message: str, retry: Callable[[], None]) -> None:
        _clear(self.main)
        _clear(self.aside)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.START)
        box.append(label("Couldn't load the details", "fs-h3"))
        box.append(label(message, "fs-muted", wrap=True))
        again = Gtk.Button(label="Try again", halign=Gtk.Align.START)
        again.add_css_class("pill")
        again.connect("clicked", lambda _b: retry())
        box.append(again)
        self.main.append(box)

    def _render(self) -> None:
        app = self.app
        load_into(self.icon, app.icon_url)
        _clear(self.corner)
        mark = trust_mark(app.trust, 28)
        if mark is not None:
            mark.add_css_class("fs-trust-corner")
            self.corner.append(mark)
        self._titles()
        self._support()
        _clear(self.main)
        _clear(self.aside)
        if not self.complete:
            loading = Gtk.Box(spacing=10, halign=Gtk.Align.START)
            loading.append(RadarMark(28, spinning=True))
            loading.append(label("Loading details…", "fs-muted"))
            self.main.append(loading)
        else:
            if app.screenshots:
                self.main.append(self._screenshots())
            if app.description:
                self.main.append(self._about())
            self.main.append(self._permissions())
            self.main.append(self._publisher())
            self.main.append(self._maintenance())
            self.aside.append(self._install_panel())
            self.aside.append(self._facts_panel())
        self.set_installed(self.installed, self._update)

    def _titles(self) -> None:
        app = self.app
        _clear(self.titles)
        self.titles.append(label(app.name, "fs-h1", wrap=True))
        if app.developer_name:
            self.titles.append(label(f"by {app.developer_name}", "fs-muted"))
        if app.summary:
            self.titles.append(label(app.summary, "fs-lede", wrap=True, margin_top=6))
        pills = wrap_box(10)
        pills.set_margin_top(8)
        pills.append(risk_pill(app.risk_level, big=True))
        badge = trust_badge(app.trust)
        if badge is not None:
            pills.append(badge)
        self.titles.append(pills)
        notes = []
        note = maintenance_note(app)
        if note:
            notes.append(note.capitalize())
        if self.complete and not app.manifest_ok:
            notes.append("Build may be broken")
        if notes:
            self.titles.append(label(" · ".join(notes), "fs-fine", margin_top=6))
        self.version_lbl = label("", "fs-fine", wrap=True, margin_top=2)
        self.titles.append(self.version_lbl)

    def _actions(self) -> Gtk.Widget:
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.START)

        def pill(text: str, *classes: str, cb) -> Gtk.Button:
            b = Gtk.Button(label=text)
            for c in ("pill", *classes):
                b.add_css_class(c)
            b.connect("clicked", lambda _b: cb())
            actions.append(b)
            return b

        self.install_btn = pill("Install", "suggested-action", cb=lambda: self._on_install(self.app))
        self.update_btn = pill("Update", "suggested-action", cb=lambda: self._on_update(self.app))
        self.launch_btn = pill("Open", "suggested-action", cb=lambda: self._on_launch(self.app))
        self.remove_btn = pill("Uninstall", "fs-ghost", cb=lambda: self._on_uninstall(self.app))
        self.source_btn = pill("Source code", "fs-ghost", cb=lambda: open_uri(self.app.upstream_url))
        return actions

    def _support(self) -> None:
        _clear(self.support_slot)
        app = self.app
        self.support_box = None
        self.support_slot.set_visible(bool(app.funding_links))
        if not app.funding_links:
            return
        narrow = self._level >= 2
        box = Gtk.Box(spacing=24, orientation=Gtk.Orientation.VERTICAL if narrow else Gtk.Orientation.HORIZONTAL)
        self.support_box = box
        box.add_css_class("fs-support-box")
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        words.append(label("Support the creators", "fs-h3"))
        words.append(label(f"Flatsonar is just the shop window. The people below made {app.name}.", "fs-muted",
                           wrap=True))
        box.append(words)
        buttons = wrap_box(8)
        buttons.set_valign(Gtk.Align.CENTER)
        for link in app.funding_links:
            buttons.append(support_button(link))
        box.append(buttons)
        self.support_slot.append(box)

    def _screenshots(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        strip = Gtk.Box(spacing=12, margin_bottom=10)
        for url in self.app.screenshots[:8]:
            pic = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.COVER, width_request=420, height_request=280)
            pic.add_css_class("fs-shot")
            pic.set_overflow(Gtk.Overflow.HIDDEN)
            btn = Gtk.Button(child=pic, tooltip_text="View screenshot")
            btn.add_css_class("fs-shot-btn")
            btn.connect("clicked", lambda _b, u=url: self._lightbox(u))
            strip.append(btn)

            def _loaded(tex, pic=pic):
                if tex is None:
                    return
                pic.set_paintable(tex)
                w, h = tex.get_width(), tex.get_height()
                if h:
                    pic.set_size_request(max(160, int(280 * w / h)), 280)

            load_texture(url, _loaded)
        scroller.set_child(strip)
        return scroller

    def _lightbox(self, url: str) -> None:
        dialog = Adw.Dialog(title=self.app.name, content_width=1100, content_height=720)
        tb = Adw.ToolbarView()
        tb.add_top_bar(Adw.HeaderBar())
        pic = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN, margin_start=12, margin_end=12,
                          margin_bottom=12)
        load_texture(url, lambda tex: tex is not None and pic.set_paintable(tex))
        tb.set_content(pic)
        dialog.set_child(tb)
        dialog.present(self)

    def _about(self) -> Gtk.Widget:
        box = _section("About")
        for block in paragraphs(self.app.description):
            if isinstance(block, list):
                for item in block:
                    box.append(label(f"•  {item}", wrap=True, margin_start=8))
            else:
                box.append(label(block, wrap=True))
        return box

    def _permissions(self) -> Gtk.Widget:
        perms = sorted(self.app.permissions, key=lambda p: LEVEL_ORDER.get(p.get("level"), 1))
        n_bad = sum(1 for p in perms if p.get("level") != "green")
        if not perms:
            sub = "Fully sandboxed: no extra permissions requested."
        elif not n_bad:
            sub = "Everything this app can reach outside its sandbox. Nothing here weakens it."
        else:
            sub = (f"{n_bad} permission{' weakens' if n_bad == 1 else 's weaken'} the sandbox. "
                   "You'll be asked to confirm before installing.")
        box = _section("Permissions", sub)
        if perms:
            box.append(findings_list([(p.get("level", "yellow"), p.get("reason", ""), p.get("arg", ""))
                                      for p in perms]))
        return box

    def _publisher(self) -> Gtk.Widget:
        box = _section("Who publishes this", TRUST_TIP.get(self.app.trust, ""))
        findings = sorted(self.app.trust_findings or [], key=lambda f: LEVEL_ORDER.get(f.get("level"), 1))
        if findings:
            box.append(findings_list([(f.get("level", "yellow"), f.get("reason", ""), f.get("check", ""))
                                      for f in findings]))
        else:
            box.append(label("The index has not assessed this publisher yet.", "fs-fine"))
        return box

    def _maintenance(self) -> Gtk.Widget:
        app = self.app
        box = _section("Maintenance", MAINTENANCE_TIP.get(app.maintenance, ""))
        if not app.manifest_ok:
            reason = app.manifest_error or "the crawler's last visit could not use this app's manifest"
            box.append(label(f"Build may be broken: {reason}.", "fs-fine", wrap=True))
        if app.maintenance_findings:
            box.append(findings_list([(f.get("level", "yellow"), f.get("reason", ""), f.get("check", ""))
                                      for f in app.maintenance_findings]))
        else:
            box.append(label("No staleness signals: recent activity, or built and reviewed by Flathub.", "fs-fine",
                             wrap=True))
        return box

    def _install_panel(self) -> Gtk.Widget:
        box = panel("Install")
        if not self.app.sources:
            box.append(label("No install source is known for this app yet.", "fs-fine", wrap=True))
        for s in self.app.sources:
            title, note = SOURCE_TEXT.get(s.kind, (s.kind.capitalize(), ""))
            option = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=4)
            option.append(label(title, "fs-h4"))
            detail = s.remote_url or s.bundle_url or s.manifest_url or s.ref or ""
            if detail:
                option.append(label(detail, "fs-code", wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                    selectable=True))
            if note:
                option.append(label(note, "fs-fine", wrap=True))
            box.append(option)
        tip = label("Flatsonar downloads it without installing it, unpacks and scans the files with ClamAV, "
                    "re-scores the real permissions, and asks twice if anything is off.", "fs-fine", "fs-panel-rule",
                    wrap=True, margin_top=8)
        box.append(tip)
        return box

    def _facts_panel(self) -> Gtk.Widget:
        app = self.app
        box = panel("Details")
        grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        rows: list[tuple[str, str]] = [("App id", app.app_id)]
        if app.license:
            rows.append(("License", app.license))
        if app.latest_version:
            rows.append(("Version", app.latest_version))
        cats = [c for c in app.categories if c not in TOOLKIT_CATEGORIES]
        if cats:
            rows.append(("Categories", ", ".join(cats)))
        if app.stars:
            rows.append(("Stars", grouped(app.stars) + (f" · {grouped(app.forks)} forks" if app.forks else "")))
        if app.repo_created_at:
            rows.append(("Repo since", nicedate(app.repo_created_at, "%b %Y")))
        if app.repo_pushed_at:
            rows.append(("Last push", nicedate(app.repo_pushed_at)))
        rows.append(("Where", "On Flathub" if app.on_flathub else "Outside Flathub"))
        if app.first_seen:
            rows.append(("Listed since", nicedate(app.first_seen)))
        for i, (k, v) in enumerate(rows):
            grid.attach(label(k, "fs-muted", yalign=0), 0, i, 1, 1)
            value = label(v, *(("fs-code",) if k == "App id" else ()), wrap=True, selectable=True, hexpand=True,
                          wrap_mode=Pango.WrapMode.WORD_CHAR)
            grid.attach(value, 1, i, 1, 1)
        box.append(grid)

        links = [("Homepage", app.homepage), ("Source code", app.upstream_url),
                 ("On Flathub", f"https://flathub.org/apps/{app.app_id}" if app.on_flathub else None),
                 ("Report this listing", REPORT_URL + GLib.Uri.escape_string(f"Listing: {app.app_id}", None, True))]
        row = wrap_box(16, 4)
        row.set_margin_top(4)
        row.add_css_class("fs-panel-rule")
        for text, url in links:
            if url:
                lbl = label(f'<a href="{GLib.markup_escape_text(url)}">{GLib.markup_escape_text(text)}</a>',
                            use_markup=True)
                lbl.set_tooltip_text(url)
                row.append(lbl)
        box.append(row)
        return box

    # --- state ----------------------------------------------------------------------

    def set_installed(self, installed: InstalledApp | None, update: bool = False) -> None:
        self.installed, self._update = installed, update
        ready = self.complete and not self._busy
        self.install_btn.set_visible(installed is None)
        self.install_btn.set_sensitive(ready)
        self.update_btn.set_visible(installed is not None and update)
        self.launch_btn.set_visible(installed is not None)
        self.remove_btn.set_visible(installed is not None)
        self.source_btn.set_visible(bool(self.app.upstream_url))
        latest = self.app.latest_version
        if installed is None:
            text = f"Latest version {latest}" if latest else ""
        else:
            where = " system-wide" if installed.installation == "system" else ""
            text = f"Installed{where}: {installed.version or 'unknown version'}"
            if update and latest and latest != installed.version:
                text += f" · {latest} available"
        self.version_lbl.set_text(text)
        self.version_lbl.set_visible(bool(text))

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        for b in (self.install_btn, self.update_btn, self.launch_btn, self.remove_btn):
            b.set_sensitive(not busy and (self.complete or b is not self.install_btn))
        self.toolbar.set_reveal_bottom_bars(busy)
        if busy:
            self.set_progress("Starting…", None)
        else:
            self._stop_pulse()

    def set_progress(self, text: str, fraction: float | None) -> None:
        """Worker status for this app: a fraction while flatpak reports one, a pulsing
        bar for the steps that cannot say how far along they are."""
        if not self._busy:  # a straggler queued just before the job finished
            return
        self.progress_lbl.set_text(text)
        if fraction is None:
            if self._pulse is None:
                self._pulse = GLib.timeout_add(120, lambda: (self.progress.pulse(), True)[1])
        else:
            self._stop_pulse()
            self.progress.set_fraction(fraction)

    def _stop_pulse(self) -> None:
        if self._pulse is not None:
            GLib.source_remove(self._pulse)
            self._pulse = None
