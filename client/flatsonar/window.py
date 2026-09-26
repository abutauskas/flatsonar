"""Main window, laid out like the website's catalogue: categories on the left; search,
filters, the result count, a grid of app cards and Previous/Next paging on the right."""

from __future__ import annotations

import logging

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from .api import DEFAULT_API, AppInfo, FlatsonarAPI, Page
from .asyncjob import run_async
from .install import flatpak_cli as fp
from .install import pipeline
from .install.confirm import DialogConfirmer
from .pages.app_page import AppPage
from .pages.installed_page import InstalledPage
from .state import Decisions
from .text import grouped
from .widgets import AppCard, RadarMark, brand, install_style, label, wrap_box

log = logging.getLogger("flatsonar.window")

# Same choices and wording as the website's filter toolbar.
RISK_FILTERS = [("Any risk", None), ("Sandboxed only", "green"), ("Broad permissions", "yellow"),
                ("Extensive permissions", "red")]
TRUST_FILTERS = [("Any publisher", None), ("Verified creators", "verified"),
                 ("Verified or Flathub", "verified,reviewed"), ("Unverified only", "unverified,suspicious")]
MAINTENANCE_FILTERS = [("Any maintenance", None), ("Actively maintained", "active"),
                       ("Quiet or abandoned", "stale,abandoned")]
SORTS = [("Name", "name"), ("Most starred", "stars"), ("Recently updated", "updated"), ("Newest", "newest")]
PER_PAGE = 36  # the site's page size
AUTO_REFRESH_SECONDS = 30 * 60  # background re-check cadence; installing is still always manual


def _clear(flowbox: Gtk.FlowBox) -> None:
    if hasattr(flowbox, "remove_all"):  # GTK >= 4.12
        flowbox.remove_all()
        return
    while (child := flowbox.get_child_at_index(0)) is not None:
        flowbox.remove(child)


def _dropdown(choices: list[tuple[str, str | None]], tip: str) -> Gtk.DropDown:
    dd = Gtk.DropDown.new_from_strings([t for t, _ in choices])
    dd.set_tooltip_text(tip)
    dd.add_css_class("fs-select")
    return dd


class FlatsonarWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, api: FlatsonarAPI):
        super().__init__(application=app, title="Flatsonar", default_width=1280, default_height=820,
                         width_request=360, height_request=480)
        self.api = api
        self.decisions = Decisions()
        self.confirmer = DialogConfirmer(self)
        self.installed: dict[str, fp.InstalledApp] = {}
        self.updates: set[str] = set()  # app ids with an update: remotes plus index versions (see _find_updates)
        self._query = ""
        self._category: str | None = None
        self._risk: str | None = None
        self._trust: str | None = None
        self._maintenance: str | None = None
        self._sort = "name"
        self._page = 1
        self._pages_total = 1
        self._search_timer: int | None = None
        self._quiet = False  # changing several filters at once: one reload at the end, not one each
        self._pages: dict[str, AppPage] = {}
        self.installed_page: InstalledPage | None = None

        install_style(Gdk.Display.get_default())

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.nav.add(self._build_browse_page())

        self.refresh_installed()
        self.load_categories()
        self.reload()
        GLib.timeout_add_seconds(AUTO_REFRESH_SECONDS, self._auto_check_updates)

    # --- layout -------------------------------------------------------------------

    def _build_browse_page(self) -> Adw.NavigationPage:
        # show_content: when collapsed (narrow window), open on the apps, not the categories.
        self.split = Adw.NavigationSplitView(min_sidebar_width=220, max_sidebar_width=270, show_content=True)

        # Sidebar: the brand, then categories.
        side_tb = Adw.ToolbarView()
        side_header = Adw.HeaderBar(show_title=False)
        side_header.pack_start(brand(28))
        side_tb.add_top_bar(side_header)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=14, margin_bottom=24)
        side.append(label("CATEGORIES", "fs-kicker", margin_start=20))
        self.category_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.category_list.add_css_class("fs-cats")
        self.category_list.connect("row-selected", self._on_category_selected)
        # Collapsed (narrow window): picking a category goes back to the results.
        self.category_list.connect("row-activated", lambda *_: self.split.set_show_content(True))
        side.append(self.category_list)
        side_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        side_scroller.set_child(side)
        side_tb.set_content(side_scroller)
        self.split.set_sidebar(Adw.NavigationPage(title="Categories", child=side_tb))

        # Content: header, then search + filters, count, grid, pager.
        content_tb = Adw.ToolbarView()
        header = Adw.HeaderBar(show_title=False)
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Menu")
        menu_btn.set_menu_model(self._app_menu())
        header.pack_end(menu_btn)
        self.installed_btn = Gtk.Button(label="Installed", tooltip_text="Installed apps and updates (Ctrl+I)")
        self.installed_btn.add_css_class("fs-nav")
        self.installed_btn.connect("clicked", lambda _b: self.show_installed())
        header.pack_end(self.installed_btn)
        content_tb.add_top_bar(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=8, margin_bottom=40,
                       margin_start=28, margin_end=28)

        self.toolbar = Gtk.Box(spacing=10)
        self.search = Gtk.SearchEntry(placeholder_text="Search name, summary, id, developer…", hexpand=True)
        self.search.add_css_class("fs-search")
        self.search.connect("search-changed", self._on_search_changed)
        self.toolbar.append(self.search)
        self.risk_dd = _dropdown(RISK_FILTERS, "Sandbox risk")
        self.trust_dd = _dropdown(TRUST_FILTERS, "Publisher trust")
        self.maint_dd = _dropdown(MAINTENANCE_FILTERS, "Maintenance")
        self.sort_dd = _dropdown(SORTS, "Sort")
        filters = wrap_box(10)
        for dd in (self.risk_dd, self.trust_dd, self.maint_dd, self.sort_dd):
            dd.connect("notify::selected", self._on_filter_changed)
            filters.append(dd)
        self.toolbar.append(filters)
        body.append(self.toolbar)

        self.searching = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, margin_top=8, visible=False)
        self.searching.append(RadarMark(28, spinning=True))
        self.searching.append(label("Searching…", "fs-muted"))
        body.append(self.searching)

        count_row = Gtk.Box(spacing=6)
        self.count = label("", "fs-count", ellipsize=Pango.EllipsizeMode.END)
        count_row.append(self.count)
        self.clear_btn = Gtk.Button(label="clear filters", has_frame=False, visible=False)
        self.clear_btn.add_css_class("fs-link")
        self.clear_btn.connect("clicked", lambda _b: self.clear_filters())
        count_row.append(self.clear_btn)
        body.append(count_row)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vhomogeneous=False)
        self.grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=20,
                                row_spacing=20, valign=Gtk.Align.START, min_children_per_line=1,
                                max_children_per_line=3)
        self.grid.add_css_class("fs-grid")
        self.stack.add_named(self.grid, "grid")
        self.stack.add_named(self._notice("No apps found",
                                          "Try another search or loosen the filters. The hunt is ongoing: new "
                                          "finds show up here as they are crawled."), "empty")
        self.offline_notice = self._notice("Can't reach the Flatsonar server", "", retry=True)
        self.stack.add_named(self.offline_notice, "offline")
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=80)
        loading.append(RadarMark(64, spinning=True))
        loading.append(label("Hunting for apps…", "fs-h3", xalign=0.5))
        hint = self._cold_start_hint()
        if hint:
            loading.append(label(hint, "fs-muted", xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER))
        self.stack.add_named(loading, "loading")
        body.append(self.stack)

        self.pager = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, margin_top=22, visible=False)
        self.prev_btn = Gtk.Button(label="← Previous")
        self.next_btn = Gtk.Button(label="Next →")
        self.where = label("", "fs-muted", xalign=0.5)
        for w in (self.prev_btn, self.where, self.next_btn):
            self.pager.append(w)
        for b, step in ((self.prev_btn, -1), (self.next_btn, 1)):
            b.add_css_class("fs-pager")
            b.connect("clicked", lambda _b, s=step: self.goto_page(self._page + s))
        body.append(self.pager)

        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.scroller.set_child(body)
        content_tb.set_content(self.scroller)
        self.search.set_key_capture_widget(content_tb)
        self.split.set_content(Adw.NavigationPage(title="Apps", child=content_tb))

        # Narrower windows: filters under the search box, then categories behind a back button.
        for condition, collapse in (("max-width: 1180sp", False), ("max-width: 720sp", True)):
            bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(condition))
            bp.add_setter(self.toolbar, "orientation", Gtk.Orientation.VERTICAL)
            if collapse:
                bp.add_setter(self.split, "collapsed", GObject.Value(GObject.TYPE_BOOLEAN, True))
            self.add_breakpoint(bp)
        return Adw.NavigationPage(title="Browse", tag="browse", child=self.split)

    def _notice(self, title: str, text: str, retry: bool = False) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("fs-empty")
        box.append(label(title, "fs-h3", xalign=0.5))
        box.text = label(text, "fs-muted", xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER)  # type: ignore[attr-defined]
        box.append(box.text)  # type: ignore[attr-defined]
        if retry:
            again = Gtk.Button(label="Try again", halign=Gtk.Align.CENTER, margin_top=8)
            again.add_css_class("pill")
            again.connect("clicked", lambda _b: (self.load_categories(), self.reload()))
            box.append(again)
        return box

    def _app_menu(self):
        menu = Gio.Menu()
        menu.append("Installed apps", "app.installed")
        menu.append("Refresh", "app.refresh")
        menu.append("Forget accepted warnings", "app.forget")
        menu.append("About Flatsonar", "app.about")
        return menu

    # --- data -------------------------------------------------------------------------

    def load_categories(self) -> None:
        def _done(cats):
            self._quiet = True  # removing the selected row reports "no category" on its way out
            while (row := self.category_list.get_row_at_index(0)) is not None:
                self.category_list.remove(row)
            for text, value, count in [("All apps", None, None), *((n, n, c) for n, c in cats)]:
                row = self._cat_row(text, value, count)
                self.category_list.append(row)
                if value == self._category:
                    self.category_list.select_row(row)
            self._quiet = False

        run_async(self.api.categories, _done, lambda e: None)

    def _cat_row(self, text: str, value: str | None, count: int | None) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.category = value  # type: ignore[attr-defined]
        box = Gtk.Box(spacing=10)
        box.append(label(text, "fs-cat-name", hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        if count is not None:
            box.append(label(str(count), "fs-muted"))
        row.set_child(box)
        return row

    def _on_default_server(self) -> bool:
        return self.api.base.rstrip("/") == DEFAULT_API.rstrip("/")

    def _cold_start_hint(self) -> str:
        if self._on_default_server():
            return "The hosted server spins down when idle, so the first load can take up to a minute."
        return ""

    def _offline_description(self) -> str:
        if self._on_default_server():
            return ("The hosted server didn't respond in time. It runs on a free tier and can "
                    "take a minute to wake up after being idle - try again shortly.")
        return f"Is it running at {self.api.base}?"

    def _filters(self) -> tuple:
        return self._query, self._category, self._risk, self._trust, self._maintenance, self._sort

    def goto_page(self, page: int) -> None:
        if 1 <= page <= self._pages_total and page != self._page:
            self._page = page
            self.scroller.get_vadjustment().set_value(0)  # to the top now, where the loader shows
            self.reload(keep_page=True)

    def clear_filters(self) -> None:
        self._quiet = True
        self._query = ""
        self.search.set_text("")
        for dd in (self.risk_dd, self.trust_dd, self.maint_dd):
            dd.set_selected(0)
        self._category = None
        self.category_list.select_row(self.category_list.get_row_at_index(0))
        self._quiet = False
        self._on_filter_changed()

    def reload(self, keep_page: bool = False) -> None:
        if not keep_page:
            self._page = 1
        if self.grid.get_first_child() is None or self.stack.get_visible_child_name() != "grid":
            self.stack.set_visible_child_name("loading")
        else:  # the site's "is-loading": keep the old results, dimmed, under the radar
            self.grid.add_css_class("fs-dim")
            self.searching.set_visible(True)
        self.prev_btn.set_sensitive(False)
        self.next_btn.set_sensitive(False)

        page_no = self._page
        filters = self._filters()
        q, cat, risk, trust, maintenance, sort = filters

        def _done(page: Page):
            if filters != self._filters() or page_no != self._page:
                return  # stale
            self.grid.remove_css_class("fs-dim")
            self.searching.set_visible(False)
            _clear(self.grid)
            for app in page.items:
                card = AppCard(app)
                card.connect("clicked", lambda b: self.open_app(b.app.app_id, b.app))
                self.grid.append(card)
            self.stack.set_visible_child_name("grid" if page.total else "empty")
            self._pages_total = max(1, -(-page.total // page.per_page))
            text = f"{grouped(page.total)} app{'' if page.total == 1 else 's'}"
            if q:
                text += f" matching “{q}”"
            if cat:
                text += f" in {cat}"
            self.count.set_text(text)
            self.clear_btn.set_visible(any((q, cat, risk, trust, maintenance)))
            self.pager.set_visible(self._pages_total > 1)
            self.where.set_text(f"Page {page_no} of {grouped(self._pages_total)}")
            self.prev_btn.set_sensitive(page_no > 1)
            self.next_btn.set_sensitive(page_no < self._pages_total)
            self.scroller.get_vadjustment().set_value(0)

        def _err(exc):
            log.warning("list failed: %s", exc)
            if filters != self._filters() or page_no != self._page:
                return
            self.grid.remove_css_class("fs-dim")
            self.searching.set_visible(False)
            self.offline_notice.text.set_text(self._offline_description())
            self.stack.set_visible_child_name("offline")
            self.pager.set_visible(False)
            self.count.set_text("")

        run_async(lambda: self.api.list_apps(q=q, category=cat, risk=risk, trust=trust, maintenance=maintenance,
                                             sort=sort, page=page_no, per_page=PER_PAGE), _done, _err)

    def refresh_installed(self, announce: bool = False) -> None:
        """Two steps: the local list is instant, asking the remotes for updates is not.
        ``announce``: toast about updates this call newly finds (the periodic background
        check uses this; the initial load and manual "check for updates" button don't need
        to announce what the badge already shows as soon as it appears)."""

        def _have_list(apps: list[fp.InstalledApp]):
            self.installed = {a.app_id: a for a in apps}
            self.updates &= set(self.installed)
            self._apply_installed()
            snapshot = dict(self.installed)
            run_async(lambda: self._find_updates(snapshot), _have_updates, lambda e: None)

        def _have_updates(ids: set[str]):
            new = ids - self.updates
            self.updates = ids & set(self.installed)
            self._apply_installed()
            if announce and new:
                names = [self.installed[i].name or i for i in new if i in self.installed]
                extra = f" and {len(names) - 3} more" if len(names) > 3 else ""
                self.toast(f"Update available for {', '.join(names[:3])}{extra}.")

        run_async(fp.installed_apps, _have_list, lambda e: None)

    def _auto_check_updates(self) -> bool:
        self.refresh_installed(announce=True)
        return True  # GLib.timeout_add_seconds: keep repeating

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
        self.installed_btn.set_label(f"{n} update{'s' if n != 1 else ''}" if n else "Installed")
        (self.installed_btn.add_css_class if n else self.installed_btn.remove_css_class)("fs-has-updates")
        if self.installed_page is not None:
            self.installed_page.refresh()

    # --- handlers -----------------------------------------------------------------------

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        if self._search_timer:
            GLib.source_remove(self._search_timer)

        def fire():
            self._search_timer = None
            query = entry.get_text().strip()
            if query != self._query:
                self._query = query
                self.reload()
            return False

        self._search_timer = GLib.timeout_add(250, fire)

    def _on_category_selected(self, _list, row) -> None:
        if self._quiet:
            return
        cat = getattr(row, "category", None) if row else None
        if cat != self._category:
            self._category = cat
            self.reload()

    def _on_filter_changed(self, *_a) -> None:
        if self._quiet:
            return
        self._risk = RISK_FILTERS[self.risk_dd.get_selected()][1]
        self._trust = TRUST_FILTERS[self.trust_dd.get_selected()][1]
        self._maintenance = MAINTENANCE_FILTERS[self.maint_dd.get_selected()][1]
        self._sort = SORTS[self.sort_dd.get_selected()][1]
        self.reload()

    def open_app(self, app_id: str, summary: AppInfo | None = None) -> None:
        """Push the app's page right away (from ``summary`` when there is one) and fill
        in the full record when it arrives."""
        if app_id in self._pages:
            page = self._pages[app_id]
            if page.get_parent() is not None:  # already somewhere in the stack
                self.nav.pop_to_page(page)
            else:
                self.nav.push(page)
            return

        page = None
        if summary is not None:
            page = self._new_page(summary, complete=False)
            self.nav.push(page)

        def _done(app: AppInfo):
            nonlocal page
            if page is None:
                page = self._new_page(app, complete=True)
                self.nav.push(page)
            else:
                page.show_details(app)

        def _err(exc):
            if page is None:
                self.toast(f"Could not load {app_id}: {exc}")
            else:
                page.show_error(str(exc).splitlines()[-1][:200], lambda: self._retry_details(page))

        run_async(lambda: self.api.get_app(app_id), _done, _err)

    def _retry_details(self, page: AppPage) -> None:
        page.show_loading()
        run_async(lambda: self.api.get_app(page.app.app_id), page.show_details,
                  lambda e: page.show_error(str(e).splitlines()[-1][:200], lambda: self._retry_details(page)))

    def _new_page(self, app: AppInfo, complete: bool) -> AppPage:
        page = AppPage(app, self.installed.get(app.app_id), self.update_available(app),
                       self.install_app, self.uninstall_app, self.launch_app, self.update_app, complete=complete)
        self._pages[app.app_id] = page
        return page

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

    def _status_cb(self, app_id: str):
        """Worker-thread status for one app, shown on its page and its Installed row."""

        def _show(text: str, fraction: float | None) -> bool:
            page = self._pages.get(app_id)
            if page:
                page.set_progress(text, fraction)
            if self.installed_page is not None:
                self.installed_page.set_progress(app_id, text)
            return False

        return lambda text, fraction=None: GLib.idle_add(_show, text, fraction)

    def _run_pipeline(self, app: AppInfo, job, verb: str) -> None:
        """Run an install/update job on a worker thread and report the outcome."""
        self._set_busy(app.app_id, True)

        def _done(outcome: pipeline.Outcome):
            self._set_busy(app.app_id, False)
            if outcome.installed:
                self.toast(outcome.message)
                self.refresh_installed()
            elif outcome.cancelled:
                self.toast(f"Did not {verb} {app.name}.")
            else:
                self.toast(outcome.message or f"{verb.capitalize()} of {app.name} failed.", timeout=8)

        def _err(exc: Exception):
            self._set_busy(app.app_id, False)
            self.toast(f"{app.name}: {str(exc).splitlines()[-1][:160]}", timeout=8)

        run_async(job, _done, _err)

    def install_app(self, app: AppInfo) -> None:
        status = self._status_cb(app.app_id)
        self._run_pipeline(app, lambda: pipeline.install(app, self.api, self.confirmer, self.decisions, status),
                           "install")

    def update_app(self, app: AppInfo, installed: fp.InstalledApp | None = None) -> None:
        installed = installed or self.installed.get(app.app_id)
        if installed is None:
            self.toast(f"{app.name} is not installed.")
            return
        status = self._status_cb(app.app_id)
        self._run_pipeline(app, lambda: pipeline.update(app, installed, self.api, self.confirmer, self.decisions,
                                                        status), "update")

    def update_all(self, pairs: list[tuple[AppInfo, fp.InstalledApp]]) -> None:
        """One worker, apps in sequence; each still gets its own dialogs if it needs them."""
        if not pairs:
            return
        for app, _ in pairs:
            self._set_busy(app.app_id, True)

        def _job():
            results = []
            for app, inst in pairs:
                try:
                    results.append((app, pipeline.update(app, inst, self.api, self.confirmer, self.decisions,
                                                         self._status_cb(app.app_id))))
                except Exception as exc:  # keep going with the rest
                    log.exception("update of %s failed", app.app_id)
                    results.append((app, pipeline.Outcome(installed=False, message=str(exc).splitlines()[-1][:160])))
            return results

        def _done(results):
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
        self._status_cb(app.app_id)(f"Removing {app.name}…")

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
