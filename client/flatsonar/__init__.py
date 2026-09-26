"""Flatsonar GTK4 / libadwaita client."""

try:  # declare GTK/Adw versions once, before any submodule touches gi.repository
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except (ImportError, ValueError):  # no PyGObject here (server-side tests); UI modules will fail loudly later
    pass

APP_ID = "io.github.abutauskas.Flatsonar"
VERSION = "1.1.0"
