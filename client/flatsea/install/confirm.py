"""The warning dialogs. Implements ``pipeline.Confirmer`` on top of Adw.AlertDialog,
called from the worker thread via ``ask_on_main``."""

from __future__ import annotations

from gi.repository import Adw, Gtk

from flatsea_core import RiskLevel, RiskReport

from ..api import AppInfo
from ..asyncjob import ask_on_main
from .scan import ScanResult

_HEADINGS = {
    RiskLevel.YELLOW: "This app asks for a lot",
    RiskLevel.RED: "This app looks dangerous",
}


def _reasons_box(report: RiskReport) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    for finding in report.findings:
        if finding.level == RiskLevel.GREEN:
            continue
        row = Gtk.Box(spacing=8)
        dot = Gtk.Image.new_from_icon_name(
            "dialog-error-symbolic" if finding.level == RiskLevel.RED else "dialog-warning-symbolic"
        )
        dot.add_css_class("error" if finding.level == RiskLevel.RED else "warning")
        lbl = Gtk.Label(label=f"{finding.arg}\n{finding.reason}", xalign=0, wrap=True, selectable=False)
        lbl.add_css_class("caption")
        row.append(dot)
        row.append(lbl)
        box.append(row)
    scroller = Gtk.ScrolledWindow(propagate_natural_height=True, max_content_height=280,
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)
    scroller.set_child(box)
    return scroller


class DialogConfirmer:
    def __init__(self, parent: Gtk.Window):
        self.parent = parent

    def _ask(self, dialog: Adw.AlertDialog, yes_id: str) -> bool:
        def present(reply):
            dialog.connect("response", lambda _d, rid: reply(rid == yes_id))
            dialog.present(self.parent)

        return bool(ask_on_main(present))

    def warn_and_confirm(self, app: AppInfo, report: RiskReport, scan_result: ScanResult) -> bool:
        heading = _HEADINGS.get(report.level, "Heads up")
        if scan_result.infected:
            heading = "ClamAV found malware"
        body_bits = []
        if scan_result.infected:
            body_bits.append(f"ClamAV flagged {len(scan_result.infected)} file(s) in {app.name}.")
        elif scan_result.ran:
            body_bits.append("ClamAV found nothing, but the sandbox permissions below are broad.")
        else:
            body_bits.append("ClamAV is not installed, so only the sandbox permissions were checked.")
        body_bits.append("Flatsea recommends not installing this. You can, but it's your call.")
        dialog = Adw.AlertDialog(heading=heading, body=" ".join(body_bits))
        dialog.set_extra_child(_reasons_box(report))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("sure", "Sure")
        dialog.set_response_appearance("sure", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        return self._ask(dialog, "sure")

    def confirm_again(self, app: AppInfo) -> bool:
        dialog = Adw.AlertDialog(
            heading="Really?",
            body=(f"You were warned about {app.name}. Installing it is your decision and your "
                  "responsibility, not Flatsea's and not the author's. Still sure?"),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("sure", "Sure, install it")
        dialog.set_response_appearance("sure", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        return self._ask(dialog, "sure")

    def confirm_plain(self, app: AppInfo, report: RiskReport) -> bool:
        perms = ", ".join(f.reason for f in report.findings[:6]) or "no special permissions"
        dialog = Adw.AlertDialog(heading=f"Install {app.name}?", body=f"Permissions: {perms}.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("install", "Install")
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("install")
        dialog.set_close_response("cancel")
        return self._ask(dialog, "install")
