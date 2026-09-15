"""Entry point: ``python -m flatsea`` or the ``flatsea`` script."""

from __future__ import annotations

import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.DEBUG if os.environ.get("FLATSEA_DEBUG") else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gio, Gtk
    except (ImportError, ValueError) as exc:
        print(f"Flatsea needs GTK 4 and libadwaita with Python bindings: {exc}\n"
              "On Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1", file=sys.stderr)
        return 2

    from . import APP_ID, VERSION
    from .api import DEFAULT_API, FlatseaAPI
    from .window import FlatseaWindow

    class FlatseaApp(Adw.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
            self.window: FlatseaWindow | None = None
            for name, cb in (("refresh", self._refresh), ("forget", self._forget), ("about", self._about),
                             ("quit", lambda *_: self.quit())):
                action = Gio.SimpleAction.new(name, None)
                action.connect("activate", cb)
                self.add_action(action)
            self.set_accels_for_action("app.quit", ["<primary>q"])
            self.set_accels_for_action("app.refresh", ["<primary>r"])

        def do_activate(self):
            if self.window is None:
                self.window = FlatseaWindow(self, FlatseaAPI(DEFAULT_API))
            self.window.present()

        def _refresh(self, *_):
            if self.window:
                self.window.load_categories()
                self.window.reload()
                self.window.refresh_installed()

        def _forget(self, *_):
            if self.window:
                self.window.decisions.path.unlink(missing_ok=True)
                self.window.decisions = type(self.window.decisions)()
                self.window.toast("Accepted warnings forgotten. You'll be asked again next time.")

        def _about(self, *_):
            about = Adw.AboutDialog(
                application_name="Flatsea",
                application_icon=APP_ID,
                developer_name="abutauskas",
                version=VERSION,
                license_type=Gtk.License.GPL_3_0,
                comments="F-Droid for Linux: open-source Flatpak apps hunted from everywhere, with credit "
                         "to their creators and a warning before anything risky.",
                website="https://github.com/abutauskas/flatsea",
                issue_url="https://github.com/abutauskas/flatsea/issues",
            )
            about.present(self.window)

    return FlatseaApp().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
