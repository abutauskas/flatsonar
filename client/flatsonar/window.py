"""Main window: category sidebar, search, app grid, and navigation into app pages."""

from __future__ import annotations

import logging

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from .api import AppInfo, FlatsonarAPI, Page
from .asyncjob import run_async
from .install import flatpak_cli as fp
from .install import pipeline
from .install.confirm import DialogConfirmer
from .pages.app_page import AppPage
from .pages.installed_page import InstalledPage
from .state import Decisions
from .widgets import CSS, AppCard

log = logging.getLogger("flatsonar.window")

RISK_FILTERS = [("Any risk", None), ("Sandboxed only", "green"), ("Broad permissions", "yellow"),
                ("Dangerous", "red")]
TRUST_FILTERS = [("Any publisher", None), ("Verified creators", "verified"),
                 ("Verified or Flathub", "verified,reviewed"), ("Unverified only", "unverified,suspicious")]
SORTS = [("Name", "name"), ("Most starred", "stars"), ("Recently updated", "updated"), ("Newest", "newest")]


def _clear(flowbox: Gtk.FlowBox) -> None:
    if hasattr(flowbox, "remove_all"):  # GTK >= 4.12
        flowbox.remove_all()
        return
    while (child := flowbox.get_child_at_index(0)) is not None:
        flowbox.remove(child)


class FlatsonarWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, api: FlatsonarAPI):
        super().__init__(application=app, title="Flatsonar", default_width=1100, default_height=720)
        self.api = api
        self.decisions = Decisions()
        self.confirmer = DialogConfirmer(self)
        self.installed: dict[str, fp.InstalledApp] = {}
        self.updates: set[str] = set()  # app ids with an update: remotes plus index versions (see _find_updates)
        self._query = ""
        self._category: str | None = None
        self._risk: str | None = None
        self._trust: str | None = None
        self._sort = "name"
        self._page = 1
        self._search_timer: int | None = None
        self._pages: dict[str, AppPage] = {}
        self.installed_page: InstalledPage | None = None

        css = Gtk.CssProvider()
        if hasattr(css, "load_from_string"):  # GTK >= 4.12
            css.load_from_string(CSS)
        else:
            css.load_from_data(CSS.encode(), -1)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.nav.add(self._build_browse_page())

        self.refresh_installed()
        self.load_categories()
        self.reload()

    # --- layout -------------------------------------------------------------------

    def _build_browse_page(self) -> Adw.NavigationPage:
        split = Adw.NavigationSplitView(min_sidebar_width=200, max_sidebar_width=260)

        # Sidebar: categories.
        side_tb = Adw.ToolbarView()
        side_tb.add_top_bar(Adw.HeaderBar(title_widget=Adw.WindowTitle(title="Flatsonar")))
        self.category_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.category_list.add_css_class("navigation-sidebar")
        self.category_list.connect("row-selected", self._on_category_selected)
        side_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        side_scroller.set_child(self.category_list)
        side_tb.set_content(side_scroller)
        split.set_sidebar(Adw.NavigationPage(title="Categories", child=side_tb))

        # Content: search + filters + grid.
        content_tb = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.search = Gtk.SearchEntry(placeholder_text="Search apps", hexpand=True, width_chars=32)
        self.search.connect("search-changed", self._on_search_changed)
        header.set_title_widget(self.search)

        self.installed_btn = Gtk.Button(tooltip_text="Installed apps and updates")
        self.installed_btn_content = Adw.ButtonContent(icon_name="emblem-ok-symbolic", label="Installed")
        self.installed_btn.set_child(self.installed_btn_content)
        self.installed_btn.connect("clicked", lambda _b: self.show_installed())
        header.pack_start(self.installed_btn)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Menu")
        menu_btn.set_menu_model(self._app_menu())
        header.pack_end(menu_btn)

        self.risk_dd = Gtk.DropDown.new_from_strings([t for t, _ in RISK_FILTERS])
        self.risk_dd.set_tooltip_text("Filter by sandbox risk")
        self.risk_dd.connect("notify::selected", self._on_filter_changed)
        header.pack_end(self.risk_dd)
        self.trust_dd = Gtk.DropDown.new_from_strings([t for t, _ in TRUST_FILTERS])
        self.trust_dd.set_tooltip_text("Filter by publisher trust")
        self.trust_dd.connect("notify::selected", self._on_filter_changed)
        header.pack_end(self.trust_dd)
        self.sort_dd = Gtk.DropDown.new_from_strings([t for t, _ in SORTS])
        self.sort_dd.set_tooltip_text("Sort")
        self.sort_dd.connect("notify::selected", self._on_filter_changed)
        header.pack_end(self.sort_dd)
        content_tb.add_top_bar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                column_spacing=6, row_spacing=6, valign=Gtk.Align.START,
                                margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
                                max_children_per_line=8, min_children_per_line=2)
        grid_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        grid_box.append(self.grid)
        self.more_btn = Gtk.Button(label="Load more", halign=Gtk.Align.CENTER, margin_bottom=24, visible=False)
        self.more_btn.add_css_class("pill")
        self.more_btn.connect("clicked", lambda _b: self.reload(append=True))
        grid_box.append(self.more_btn)
        grid_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        grid_scroller.set_child(grid_box)
        self.stack.add_named(grid_scroller, "grid")
        self.stack.add_named(Adw.StatusPage(icon_name="edit-find-symbolic", title="No apps found",
                                            description="Try another search or category."), "empty")
        self.stack.add_named(Adw.StatusPage(icon_name="network-offline-symbolic", title="Can't reach the Flatsonar server",
                                            description=f"Is it running at {self.api.base}?"), "offline")
        loading = Adw.StatusPage(title="Loading…")
        spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
        loading.set_child(spinner)
        self.stack.add_named(loading, "loading")
        content_tb.set_content(self.stack)

        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("status-bar")
        content_tb.add_bottom_bar(self.status)

        split.set_content(Adw.NavigationPage(title="Apps", child=content_tb))
        return Adw.NavigationPage(title="Browse", tag="browse", child=split)

    def _app_menu(self):
        from gi.repository import Gio

        menu = Gio.Menu()
        menu.append("Installed apps", "app.installed")
        menu.append("Refresh", "app.refresh")
        menu.append("Forget accepted warnings", "app.forget")
        menu.append("About Flatsonar", "app.about")
        return menu

    # --- data -------------------------------------------------------------------------

    def load_categories(self) -> None:
        def _done(cats):
            while (row := self.category_list.get_row_at_index(0)) is not None:
                self.category_list.remove(row)
            self.category_list.append(self._cat_row("All apps", None, None))
            for name, count in cats:
                self.category_list.append(self._cat_row(name, name, count))
            self.category_list.select_row(self.category_list.get_row_at_index(0))

        run_async(self.api.categories, _done, lambda e: None)

    def _cat_row(self, label: str, value: str | None, count: int | None) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.category = value  # type: ignore[attr-defined]
        box = Gtk.Box(spacing=8, margin_top=4, margin_bottom=4)
        box.append(Gtk.Label(label=label, xalign=0, hexpand=True))
        if count is not None:
            c = Gtk.Label(label=str(count))
            c.add_css_class("dim-label")
            box.append(c)
        row.set_child(box)
        return row

    def reload(self, append: bool = False) -> None:
        if not append:
            self._page = 1
            self.stack.set_visible_child_name("loading")
        else:
            self._page += 1
            self.more_btn.set_sensitive(False)

        page_no = self._page
        q, cat, risk, trust, sort = self._query, self._category, self._risk, self._trust, self._sort

        def _done(page: Page):
            if (q, cat, risk, trust, sort) != (self._query, self._category, self._risk, self._trust, self._sort):
                return  # stale
            if not append:
                _clear(self.grid)
            for app in page.items:
                card = AppCard(app)
                card.connect("clicked", lambda b: self.open_app(b.app.app_id))
                self.grid.append(card)
            self.more_btn.set_visible(page.has_more)
            self.more_btn.set_sensitive(True)
            self.stack.set_visible_child_name("grid" if page.total else "empty")
            self.status.set_text(f"{page.total} apps" + (f" matching “{q}”" if q else "")
                                 + (f" in {cat}" if cat else ""))

        def _err(exc):
            log.warning("list failed: %s", exc)
            self.stack.set_visible_child_name("offline")

        run_async(lambda: self.api.list_apps(q=q, category=cat, risk=risk, trust=trust, sort=sort, page=page_no),
                  _done, _err)

    def refresh_installed(self) -> None:
        """Two steps: the local list is instant, asking the remotes for updates is not."""

        def _have_list(apps: list[fp.InstalledApp]):
            self.installed = {a.app_id: a for a in apps}
            self.updates &= set(self.installed)
            self._apply_installed()
            snapshot = dict(self.installed)
            run_async(lambda: self._find_updates(snapshot), _have_updates, lambda e: None)

        def _have_updates(ids: set[str]):
            self.updates = ids & set(self.installed)
            self._apply_installed()

        run_async(fp.installed_apps, _have_list, lambda e: None)

    def _find_updates(self, installed: dict[str, fp.InstalledApp]) -> set[str]:
        """Worker thread. Remote-installed apps: what the remotes say. Local builds and
        bundles: whether the index lists a newer version. Same rule as the Installed page."""
        remote = fp.updates_available()
        infos: dict[str, AppInfo] = {}
        try:
            infos = self.api.apps_by_id(list(installed))
        except Exception as exc:  # offline: remote updates are still worth showing
            log.warning("index lookup failed: %s", exc)
        return {app_id for app_id, inst in installed.items()
                if pipeline.update_available(infos.get(app_id) or AppInfo.unknown(app_id, inst.name, inst.origin),
                                             inst, remote)}

    def update_available(self, app: AppInfo) -> bool:
        return app.app_id in self.installed and app.app_id in self.updates

    def _apply_installed(self) -> None:
        for page in self._pages.values():
            page.set_installed(self.installed.get(page.app.app_id), self.update_available(page.app))
        n = len(self.updates)
        self.installed_btn_content.set_label(f"{n} update{'s' if n != 1 else ''}" if n else "Installed")
        self.installed_btn_content.set_icon_name("software-update-available-symbolic" if n else "emblem-ok-symbolic")
        if self.installed_page is not None:
            self.installed_page.refresh()

    # --- handlers -----------------------------------------------------------------------

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        if self._search_timer:
            GLib.source_remove(self._search_timer)

        def fire():
            self._search_timer = None
            self._query = entry.get_text().strip()
            self.reload()
            return False

        self._search_timer = GLib.timeout_add(250, fire)

    def _on_category_selected(self, _list, row) -> None:
        cat = getattr(row, "category", None) if row else None
        if cat != self._category:
            self._category = cat
            self.reload()

    def _on_filter_changed(self, *_a) -> None:
        self._risk = RISK_FILTERS[self.risk_dd.get_selected()][1]
        self._trust = TRUST_FILTERS[self.trust_dd.get_selected()][1]
        self._sort = SORTS[self.sort_dd.get_selected()][1]
        self.reload()

    def open_app(self, app_id: str) -> None:
        if app_id in self._pages:
            page = self._pages[app_id]
            if page.get_parent() is not None:  # already somewhere in the stack
                self.nav.pop_to_page(page)
            else:
                self.nav.push(page)
            return
        self.status.set_text(f"Loading {app_id}…")

        def _done(app: AppInfo):
            page = AppPage(app, self.installed.get(app.app_id), self.update_available(app),
                           self.install_app, self.uninstall_app, self.launch_app, self.update_app)
            self._pages[app.app_id] = page
            self.nav.push(page)
            self.status.set_text("")

        run_async(lambda: self.api.get_app(app_id), _done,
                  lambda e: self.toast(f"Could not load {app_id}: {e}"))

    def show_installed(self) -> None:
        if self.installed_page is None:
            self.installed_page = InstalledPage(self)
            self.installed_page.refresh()
        page = self.installed_page
        if self.nav.get_visible_page() is page:
            return
        if page.get_parent() is not None:
            self.nav.pop_to_page(page)
        else:
            self.nav.push(page)

    def toast(self, text: str, timeout: int = 4) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=timeout))

    # --- install / update / uninstall ---------------------------------------------------

    def _set_busy(self, app_id: str, busy: bool) -> None:
        page = self._pages.get(app_id)
        if page:
            page.set_busy(busy)
        if self.installed_page is not None:
            self.installed_page.set_busy(app_id, busy)

    def _status_cb(self):
        return lambda msg: GLib.idle_add(lambda: (self.status.set_text(msg), False)[1])

    def _run_pipeline(self, app: AppInfo, job, verb: str) -> None:
        """Run an install/update job on a worker thread and report the outcome."""
        self._set_busy(app.app_id, True)

        def _done(outcome: pipeline.Outcome):
            self._set_busy(app.app_id, False)
            self.status.set_text("")
            if outcome.installed:
                self.toast(outcome.message)
                self.refresh_installed()
            elif outcome.cancelled:
                self.toast(f"Did not {verb} {app.name}.")
            else:
                self.toast(outcome.message or f"{verb.capitalize()} of {app.name} failed.", timeout=8)

        def _err(exc: Exception):
            self._set_busy(app.app_id, False)
            self.status.set_text("")
            self.toast(f"{app.name}: {str(exc).splitlines()[-1][:160]}", timeout=8)

        run_async(job, _done, _err)

    def install_app(self, app: AppInfo) -> None:
        status = self._status_cb()
        self._run_pipeline(app, lambda: pipeline.install(app, self.api, self.confirmer, self.decisions, status),
                           "install")

    def update_app(self, app: AppInfo, installed: fp.InstalledApp | None = None) -> None:
        installed = installed or self.installed.get(app.app_id)
        if installed is None:
            self.toast(f"{app.name} is not installed.")
            return
        status = self._status_cb()
        self._run_pipeline(app, lambda: pipeline.update(app, installed, self.api, self.confirmer, self.decisions,
                                                        status), "update")

    def update_all(self, pairs: list[tuple[AppInfo, fp.InstalledApp]]) -> None:
        """One worker, apps in sequence; each still gets its own dialogs if it needs them."""
        if not pairs:
            return
        status = self._status_cb()
        for app, _ in pairs:
            self._set_busy(app.app_id, True)

        def _job():
            results = []
            for app, inst in pairs:
                try:
                    results.append((app, pipeline.update(app, inst, self.api, self.confirmer, self.decisions, status)))
                except Exception as exc:  # keep going with the rest
                    log.exception("update of %s failed", app.app_id)
                    results.append((app, pipeline.Outcome(installed=False, message=str(exc).splitlines()[-1][:160])))
            return results

        def _done(results):
            self.status.set_text("")
            done = [a.name for a, o in results if o.installed]
            skipped = [a.name for a, o in results if o.cancelled]
            failed = [f"{a.name}: {o.message}" for a, o in results if not o.installed and not o.cancelled]
            for app, _ in pairs:
                self._set_busy(app.app_id, False)
            if done:
                self.toast(f"Updated {', '.join(done)}.")
            if skipped:
                self.toast(f"Skipped {', '.join(skipped)}.")
            for f in failed:
                self.toast(f, timeout=8)
            self.refresh_installed()

        run_async(_job, _done, lambda e: (self.toast(str(e)[:160]), self.refresh_installed()))

    def uninstall_app(self, app: AppInfo) -> None:
        inst = self.installed.get(app.app_id)
        installation = inst.installation if inst else "user"
        self._set_busy(app.app_id, True)

        def _done(_):
            self._set_busy(app.app_id, False)
            self.toast(f"{app.name} removed.")
            self.refresh_installed()

        def _err(exc):
            self._set_busy(app.app_id, False)
            self.toast(str(exc)[:160])

        run_async(lambda: fp.uninstall(app.app_id, installation=installation), _done, _err)

    def launch_app(self, app: AppInfo) -> None:
        try:
            fp.launch(app.app_id)
        except OSError as exc:
            self.toast(str(exc))
