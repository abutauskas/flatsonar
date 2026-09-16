"""App details: who made it, how to sponsor them, what it can touch, and the Install button."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk

from ..api import AppInfo
from ..icons import load_into
from ..install.flatpak_cli import InstalledApp
from ..widgets import TRUST_TIP, permission_row, risk_pill, sponsor_button, trust_pill, trust_row

SOURCE_TEXT = {
    "flathub": "Installs from Flathub",
    "remote": "Installs from the project's own Flatpak remote",
    "bundle": "Installs from a .flatpak bundle attached to a release",
    "manifest": "Built locally from the project's manifest with flatpak-builder",
}


class AppPage(Adw.NavigationPage):
    def __init__(self, app: AppInfo, installed: InstalledApp | None, update: bool,
                 on_install: Callable[[AppInfo], None], on_uninstall: Callable[[AppInfo], None],
                 on_launch: Callable[[AppInfo], None], on_update: Callable[[AppInfo], None]):
        super().__init__(title=app.name, tag=f"app:{app.app_id}")
        self.app = app
        self._on_install, self._on_uninstall, self._on_launch = on_install, on_uninstall, on_launch
        self._on_update = on_update

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=760, margin_top=18, margin_bottom=36, margin_start=18, margin_end=18)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        clamp.set_child(body)
        scroller.set_child(clamp)
        toolbar.set_content(scroller)
        self.set_child(toolbar)

        body.append(self._hero(installed, update))
        if app.sponsor_links:
            body.append(self._sponsor_group())
        body.append(self._credit_group())
        body.append(self._publisher_group())
        if app.description:
            desc = Gtk.Label(label=app.description, wrap=True, xalign=0, selectable=True)
            body.append(desc)
        if app.screenshots:
            body.append(self._screenshots())
        body.append(self._permissions_group())
        body.append(self._source_group())

    # --- sections ---------------------------------------------------------------

    def _hero(self, installed: InstalledApp | None, update: bool) -> Gtk.Widget:
        row = Gtk.Box(spacing=18)
        icon = Gtk.Image(pixel_size=96, valign=Gtk.Align.START)
        icon.add_css_class("hero-icon")
        load_into(icon, self.app.icon_url)
        row.append(icon)

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        title = Gtk.Label(label=self.app.name, xalign=0, wrap=True)
        title.add_css_class("title-1")
        col.append(title)
        by = Gtk.Label(label=f"by {self.app.developer_name}" if self.app.developer_name else "", xalign=0)
        by.add_css_class("dim-label")
        col.append(by)
        summary = Gtk.Label(label=self.app.summary, xalign=0, wrap=True)
        col.append(summary)
        pills = Gtk.Box(spacing=8)
        pills.append(risk_pill(self.app.risk_level))
        if self.app.license:
            lic = Gtk.Label(label=self.app.license)
            lic.add_css_class("risk-pill")
            lic.add_css_class("dim-label")
            pills.append(lic)
        pills.append(trust_pill(self.app.trust))
        col.append(pills)
        self.version_lbl = Gtk.Label(xalign=0, wrap=True)
        self.version_lbl.add_css_class("dim-label")
        self.version_lbl.add_css_class("caption")
        col.append(self.version_lbl)
        row.append(col)

        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.START)
        self.install_btn = Gtk.Button(label="Install")
        self.install_btn.add_css_class("suggested-action")
        self.install_btn.add_css_class("pill")
        self.install_btn.connect("clicked", lambda _b: self._on_install(self.app))
        self.update_btn = Gtk.Button(label="Update")
        self.update_btn.add_css_class("suggested-action")
        self.update_btn.add_css_class("pill")
        self.update_btn.connect("clicked", lambda _b: self._on_update(self.app))
        self.launch_btn = Gtk.Button(label="Open")
        self.launch_btn.add_css_class("pill")
        self.launch_btn.connect("clicked", lambda _b: self._on_launch(self.app))
        self.remove_btn = Gtk.Button(label="Uninstall")
        self.remove_btn.add_css_class("destructive-action")
        self.remove_btn.add_css_class("pill")
        self.remove_btn.connect("clicked", lambda _b: self._on_uninstall(self.app))
        self.spinner = Gtk.Spinner(spinning=False, visible=False)
        for w in (self.install_btn, self.update_btn, self.launch_btn, self.remove_btn, self.spinner):
            actions.append(w)
        row.append(actions)
        self.set_installed(installed, update)
        return row

    def _sponsor_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title="Support the creators",
                                     description="Flatsonar is just the shop window. The people below made this.")
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=8, row_spacing=8,
                           max_children_per_line=4, homogeneous=False)
        for link in self.app.sponsor_links:
            flow.insert(sponsor_button(link), -1)
        group.add(flow)
        return group

    def _credit_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title="Made by")
        if self.app.developer_name:
            group.add(Adw.ActionRow(title=self.app.developer_name, subtitle="Developer"))
        for label, url in (("Source code", self.app.upstream_url), ("Homepage", self.app.homepage)):
            if not url:
                continue
            row = Adw.ActionRow(title=label, subtitle=url, activatable=True)
            row.add_suffix(Gtk.Image.new_from_icon_name("external-link-symbolic"))
            row.connect("activated", lambda _r, u=url: Gtk.UriLauncher(uri=u).launch(None, None, None))
            group.add(row)
        return group

    def _publisher_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title="Who publishes this", description=TRUST_TIP.get(self.app.trust, ""))
        findings = self.app.trust_findings or []
        if not findings:
            group.add(Adw.ActionRow(title="No provenance information",
                                    subtitle="The index has not assessed this publisher yet"))
        for f in sorted(findings, key=lambda f: {"red": 0, "yellow": 1, "green": 2}.get(f.get("level"), 1)):
            group.add(trust_row(f))
        if self.app.repo_created_at:
            group.add(Adw.ActionRow(title="Repository created", subtitle=self.app.repo_created_at[:10]))
        return group

    def _screenshots(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, height_request=240)
        strip = Gtk.Box(spacing=12)
        for url in self.app.screenshots[:6]:
            pic = Gtk.Image(pixel_size=220)  # replaced by a texture once loaded
            load_into(pic, url, fallback="image-x-generic-symbolic")
            strip.append(pic)
        scroller.set_child(strip)
        return scroller

    def _permissions_group(self) -> Gtk.Widget:
        n_bad = sum(1 for p in self.app.permissions if p["level"] != "green")
        desc = ("Everything this app can reach outside its sandbox."
                if not n_bad else f"{n_bad} permission(s) weaken the sandbox. You'll be asked to confirm.")
        group = Adw.PreferencesGroup(title="Permissions", description=desc)
        if not self.app.permissions:
            group.add(Adw.ActionRow(title="Fully sandboxed", subtitle="No extra permissions requested"))
        ordered = sorted(self.app.permissions, key=lambda p: {"red": 0, "yellow": 1, "green": 2}[p["level"]])
        for perm in ordered:
            group.add(permission_row(perm))
        return group

    def _source_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title="Where it comes from")
        if not self.app.sources:
            group.add(Adw.ActionRow(title="No install source known"))
        for s in self.app.sources:
            detail = s.remote_url or s.bundle_url or s.manifest_url or s.ref or ""
            group.add(Adw.ActionRow(title=SOURCE_TEXT.get(s.kind, s.kind), subtitle=detail))
        return group

    # --- state ------------------------------------------------------------------

    def set_installed(self, installed: InstalledApp | None, update: bool = False) -> None:
        self.installed = installed
        self.install_btn.set_visible(installed is None)
        self.update_btn.set_visible(installed is not None and update)
        self.launch_btn.set_visible(installed is not None)
        self.remove_btn.set_visible(installed is not None)
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
        self.spinner.set_visible(busy)
        self.spinner.set_spinning(busy)
        for b in (self.install_btn, self.update_btn, self.launch_btn, self.remove_btn):
            b.set_sensitive(not busy)
