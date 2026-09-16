"""Everything installed on this machine, scored the way the store scores the catalogue,
with an Update button wherever something newer exists.

Apps Flatsonar did not install are listed too: the point of the page is to audit what
is already here. Rows for apps the index knows open their store page."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gi.repository import Adw, Gtk

from flatsonar_core import RiskReport, metadata_to_finish_args, score_finish_args

from ..api import AppInfo
from ..asyncjob import run_async
from ..icons import load_into
from ..install import flatpak_cli as fp
from ..install.flatpak_cli import InstalledApp
from ..widgets import risk_pill

log = logging.getLogger("flatsonar.installed")

GROUP_TITLES = {"user": "Your apps", "system": "System-wide"}


@dataclass
class Entry:
    installed: InstalledApp
    report: RiskReport | None = None  # scored from the deployed metadata file
    info: AppInfo | None = None  # what the index knows, if anything
    update: bool = False

    @property
    def app(self) -> AppInfo:
        return self.info or AppInfo.unknown(self.installed.app_id, self.installed.name, self.installed.origin)

    @property
    def name(self) -> str:
        return (self.info.name if self.info else "") or self.installed.name or self.installed.app_id

    @property
    def risk_level(self) -> str | None:
        if self.report is not None:
            return self.report.level.label
        return self.info.risk_level if self.info else None

    @property
    def origin_text(self) -> str:
        o = self.installed.origin
        if o == "flathub":
            return "Flathub"
        if o == fp.LOCAL_REMOTE:
            return "built from source by Flatsonar"
        if not o:
            return "a .flatpak bundle"
        return f"the {o} remote"


def load_entries(installed: dict[str, InstalledApp], updates: set[str], api) -> list[Entry]:
    """Worker-thread half: read every deployed ``metadata``, score it, ask the index.
    ``updates`` is the window's resolved set (remotes plus index versions)."""
    entries = [Entry(a) for a in installed.values()]
    for e in entries:
        meta = fp.deployed_metadata(e.installed.app_id, e.installed.installation)
        if meta:
            e.report = score_finish_args(metadata_to_finish_args(meta), e.installed.app_id)
    infos: dict[str, AppInfo] = {}
    try:
        infos = api.apps_by_id(list(installed))
    except Exception as exc:  # offline: the local half is still useful
        log.warning("index lookup failed: %s", exc)
    for e in entries:
        e.info = infos.get(e.installed.app_id)
        e.update = e.installed.app_id in updates
    return sorted(entries, key=lambda e: e.name.lower())


class InstalledPage(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Installed", tag="installed")
        self.window = window
        self.entries: list[Entry] = []
        self._rows: dict[str, tuple[Adw.ActionRow, Gtk.Spinner, Gtk.Button | None]] = {}

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.update_all_btn = Gtk.Button(label="Update all", visible=False)
        self.update_all_btn.add_css_class("suggested-action")
        self.update_all_btn.connect("clicked", lambda _b: self.window.update_all(
            [(e.app, e.installed) for e in self.entries if e.update]))
        header.pack_end(self.update_all_btn)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Check for updates")
        refresh.connect("clicked", lambda _b: self.window.refresh_installed())
        header.pack_end(refresh)
        toolbar.add_top_bar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        loading = Adw.StatusPage(title="Looking at what is installed…")
        loading.set_child(Gtk.Spinner(spinning=True, width_request=32, height_request=32))
        self.stack.add_named(loading, "loading")
        self.stack.add_named(Adw.StatusPage(icon_name="application-x-executable-symbolic", title="Nothing installed",
                                            description="Flatpak apps you install show up here, "
                                                        "whether or not Flatsonar installed them."), "empty")
        self.stack.add_named(Adw.StatusPage(icon_name="dialog-warning-symbolic", title="flatpak is not available",
                                            description="Install the flatpak package to see and manage apps."),
                             "no-flatpak")

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=760, margin_top=18, margin_bottom=36, margin_start=18, margin_end=18)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        clamp.set_child(self.body)
        scroller.set_child(clamp)
        self.stack.add_named(scroller, "list")
        toolbar.set_content(self.stack)
        self.set_child(toolbar)

    # --- data ----------------------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild from ``window.installed`` / ``window.updates`` (already loaded)."""
        if not fp.available():
            self.stack.set_visible_child_name("no-flatpak")
            return
        installed = dict(self.window.installed)
        updates = set(self.window.updates)
        if not installed:
            self.stack.set_visible_child_name("empty")
            return
        if not self.entries:
            self.stack.set_visible_child_name("loading")
        run_async(lambda: load_entries(installed, updates, self.window.api), self._populate,
                  lambda e: self.window.toast(f"Could not read installed apps: {e}"))

    def _populate(self, entries: list[Entry]) -> None:
        self.entries = entries
        self._rows.clear()
        while (child := self.body.get_first_child()) is not None:
            self.body.remove(child)

        n_updates = sum(1 for e in entries if e.update)
        n_risky = sum(1 for e in entries if e.risk_level in ("yellow", "red"))
        bits = [f"{len(entries)} app(s)"]
        if n_updates:
            bits.append(f"{n_updates} update(s) available")
        if n_risky:
            bits.append(f"{n_risky} with permissions that weaken the sandbox")
        summary = Gtk.Label(label=", ".join(bits) + ".", xalign=0, wrap=True)
        summary.add_css_class("dim-label")
        self.body.append(summary)
        self.update_all_btn.set_visible(n_updates > 0)
        self.update_all_btn.set_label(f"Update all ({n_updates})" if n_updates else "Update all")

        for installation in sorted({e.installed.installation for e in entries}, key=lambda i: (i != "user", i)):
            group = Adw.PreferencesGroup(title=GROUP_TITLES.get(installation, f"Installation {installation}"))
            for e in entries:
                if e.installed.installation == installation:
                    group.add(self._row(e))
            self.body.append(group)
        self.stack.set_visible_child_name("list")

    def _row(self, e: Entry) -> Adw.ActionRow:
        version = e.installed.version or "unknown version"
        row = Adw.ActionRow(title=e.name, subtitle=f"{version} · from {e.origin_text}")
        icon = Gtk.Image(pixel_size=32, valign=Gtk.Align.CENTER)
        icon.add_css_class("app-icon")
        load_into(icon, e.info.icon_url if e.info else None)
        row.add_prefix(icon)

        level = e.risk_level
        if level:
            row.add_suffix(risk_pill(level))
        spinner = Gtk.Spinner(spinning=False, visible=False, valign=Gtk.Align.CENTER)
        row.add_suffix(spinner)
        update_btn = None
        if e.update:
            latest = e.info.latest_version if e.info else None
            update_btn = Gtk.Button(label="Update", valign=Gtk.Align.CENTER,
                                    tooltip_text=f"Update to {latest}" if latest else "An update is available")
            update_btn.add_css_class("suggested-action")
            update_btn.add_css_class("pill")
            update_btn.connect("clicked", lambda _b, e=e: self.window.update_app(e.app, e.installed))
            row.add_suffix(update_btn)
        if e.info:
            row.set_activatable(True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, app_id=e.installed.app_id: self.window.open_app(app_id))
        else:
            row.set_tooltip_text("Not in the Flatsonar index")
        self._rows[e.installed.app_id] = (row, spinner, update_btn)
        return row

    # --- state -----------------------------------------------------------------------

    def set_busy(self, app_id: str, busy: bool) -> None:
        found = self._rows.get(app_id)
        if not found:
            return
        row, spinner, btn = found
        spinner.set_visible(busy)
        spinner.set_spinning(busy)
        if btn is not None:
            btn.set_sensitive(not busy)
        row.set_sensitive(not busy)
