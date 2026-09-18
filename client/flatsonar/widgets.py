"""Small reusable widgets: app cards, risk badges, permission rows."""

from __future__ import annotations

from gi.repository import Adw, Gtk, Pango

from .api import AppInfo
from .icons import load_into

RISK_ICON = {"green": "security-high-symbolic", "yellow": "security-medium-symbolic", "red": "security-low-symbolic"}
RISK_CSS = {"green": "success", "yellow": "warning", "red": "error"}
RISK_TEXT = {"green": "Sandboxed", "yellow": "Broad permissions", "red": "Dangerous permissions"}

# Publisher trust, from the server's provenance checks. Never let unverified look verified.
TRUST_TEXT = {"verified": "Verified creator", "reviewed": "Flathub reviewed", "unverified": "Unverified publisher",
              "suspicious": "Suspicious publisher"}
TRUST_STYLE = {"verified": "green", "reviewed": "accent", "unverified": "yellow", "suspicious": "red"}
TRUST_ICON = {"verified": "emblem-ok-symbolic", "reviewed": "emblem-documents-symbolic",
              "unverified": "dialog-question-symbolic", "suspicious": "dialog-warning-symbolic"}
TRUST_TIP = {
    "verified": "The creator demonstrably controls this app id (Flathub verification, the hosting account owns "
                "the namespace, or a well-known file on their domain).",
    "reviewed": "On Flathub: the manifest was reviewed and the build ran on Flathub's infrastructure, "
                "but the developer has not verified ownership of the app id.",
    "unverified": "Nobody has confirmed that the publisher controls this app id. You'll be warned before installing.",
    "suspicious": "Something concrete is wrong: the app id claims a namespace this repository does not own, "
                  "or the build does things a build should not. You'll be warned twice.",
}

CSS = """
.app-card { border-radius: 12px; padding: 12px; }
.app-card:hover { background: alpha(currentColor, 0.06); }
.app-icon { border-radius: 16px; }
.risk-pill { border-radius: 999px; padding: 2px 8px; font-size: 0.8em; font-weight: 600; }
.risk-pill.green { background: alpha(@success_color, 0.18); color: @success_color; }
.risk-pill.yellow { background: alpha(@warning_color, 0.18); color: @warning_color; }
.risk-pill.red { background: alpha(@error_color, 0.18); color: @error_color; }
.risk-pill.accent { background: alpha(@accent_bg_color, 0.18); color: @accent_color; }
.hero-icon { border-radius: 24px; }
.support { background: alpha(@accent_bg_color, 0.15); color: @accent_color; }
.status-bar { padding: 6px 12px; }
"""


def risk_pill(level: str) -> Gtk.Widget:
    pill = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
    pill.add_css_class("risk-pill")
    pill.add_css_class(level)
    pill.append(Gtk.Image.new_from_icon_name(RISK_ICON.get(level, "security-medium-symbolic")))
    pill.append(Gtk.Label(label=RISK_TEXT.get(level, level)))
    pill.set_tooltip_text({
        "green": "Only ordinary sandbox permissions.",
        "yellow": "Asks for permissions that weaken the sandbox. You'll be warned before installing.",
        "red": "Asks for permissions that effectively escape the sandbox. You'll be warned twice.",
    }.get(level, ""))
    return pill


def trust_pill(level: str) -> Gtk.Widget:
    pill = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
    pill.add_css_class("risk-pill")
    pill.add_css_class(TRUST_STYLE.get(level, "yellow"))
    pill.append(Gtk.Image.new_from_icon_name(TRUST_ICON.get(level, "dialog-question-symbolic")))
    pill.append(Gtk.Label(label=TRUST_TEXT.get(level, level)))
    pill.set_tooltip_text(TRUST_TIP.get(level, ""))
    return pill


class AppCard(Gtk.Button):
    """Grid tile for the browse page."""

    def __init__(self, app: AppInfo):
        super().__init__(has_frame=False)
        self.app = app
        self.add_css_class("app-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, width_request=150)

        icon = Gtk.Image(pixel_size=64, halign=Gtk.Align.CENTER)
        icon.add_css_class("app-icon")
        load_into(icon, app.icon_url)
        box.append(icon)

        name = Gtk.Label(label=app.name, ellipsize=Pango.EllipsizeMode.END, max_width_chars=18, justify=Gtk.Justification.CENTER)
        name.add_css_class("heading")
        box.append(name)

        by = Gtk.Label(label=app.developer_name or "", ellipsize=Pango.EllipsizeMode.END, max_width_chars=20)
        by.add_css_class("dim-label")
        by.add_css_class("caption")
        box.append(by)

        summary = Gtk.Label(label=app.summary, wrap=True, lines=2, ellipsize=Pango.EllipsizeMode.END, max_width_chars=22,
                            justify=Gtk.Justification.CENTER)
        summary.add_css_class("caption")
        box.append(summary)

        foot = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        foot.append(risk_pill(app.risk_level))
        if app.has_funding:
            heart = Gtk.Image.new_from_icon_name("emblem-favorite-symbolic")
            heart.set_tooltip_text("Has a way to support the creator")
            heart.add_css_class("accent")
            foot.append(heart)
        if app.trust != "reviewed":  # the common Flathub case stays quiet; everything else gets a mark
            mark = Gtk.Image.new_from_icon_name(TRUST_ICON.get(app.trust, "dialog-question-symbolic"))
            mark.set_tooltip_text(TRUST_TEXT.get(app.trust, app.trust))
            mark.add_css_class({"green": "success", "yellow": "warning", "red": "error"}.get(
                TRUST_STYLE.get(app.trust, "yellow"), "warning"))
            foot.append(mark)
        box.append(foot)
        self.set_child(box)


def finding_row(reason: str, detail: str, level: str) -> Adw.ActionRow:
    row = Adw.ActionRow(title=reason, subtitle=detail)
    row.set_title_lines(3)
    icon = Gtk.Image.new_from_icon_name({
        "green": "emblem-ok-symbolic", "yellow": "dialog-warning-symbolic", "red": "dialog-error-symbolic",
    }.get(level, "dialog-warning-symbolic"))
    icon.add_css_class(RISK_CSS.get(level, "warning"))
    row.add_prefix(icon)
    return row


def permission_row(perm: dict[str, str]) -> Adw.ActionRow:
    return finding_row(perm["reason"], perm["arg"], perm["level"])


def trust_row(finding: dict[str, str]) -> Adw.ActionRow:
    return finding_row(finding["reason"], finding["check"], finding["level"])


def funding_button(link: dict[str, str]) -> Gtk.Button:
    label = {
        "github": "GitHub Sponsors", "patreon": "Patreon", "ko_fi": "Ko-fi", "liberapay": "Liberapay",
        "open_collective": "Open Collective", "buy_me_a_coffee": "Buy Me a Coffee", "custom": "Donate",
    }.get(link["platform"], link["platform"].replace("_", " ").title())
    btn = Gtk.Button()
    content = Adw.ButtonContent(icon_name="emblem-favorite-symbolic", label=label)
    btn.set_child(content)
    btn.add_css_class("support")
    btn.add_css_class("pill")
    btn.set_tooltip_text(link["url"])
    btn.connect("clicked", lambda _b: Gtk.UriLauncher(uri=link["url"]).launch(None, None, None))
    return btn
