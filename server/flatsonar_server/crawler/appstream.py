"""Parse AppStream metainfo XML (``*.metainfo.xml`` / ``*.appdata.xml``) found in
upstream repositories, so off-Flathub apps get a proper name, summary, license,
screenshots and donation link."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

_WS = re.compile(r"\s+")
_CAMO = re.compile(r"^https?://camo\.githubusercontent\.com/[0-9a-f]{40}/([0-9a-f]+)$", re.IGNORECASE)


def _decamo(url: str) -> str:
    """GitHub's Camo image proxy rewrites embedded markdown images to
    ``camo.githubusercontent.com/<hmac>/<hex-encoded-original-url>`` when it renders a
    README. Some projects paste that rendered URL straight into their metainfo instead
    of the real image link; Camo then 403s anyone who isn't github.com itself; it was
    never meant for third-party embedding. Recover the original URL when the pattern
    matches - it usually still works fine hotlinked."""
    m = _CAMO.match(url)
    if not m:
        return url
    try:
        return bytes.fromhex(m.group(1)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return url


@dataclass
class MetaInfo:
    app_id: str | None = None
    name: str | None = None
    summary: str | None = None
    description: str | None = None
    license: str | None = None
    developer_name: str | None = None
    homepage: str | None = None
    donation: str | None = None
    vcs: str | None = None
    screenshots: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    latest_version: str | None = None


def _untranslated(root: ET.Element, tag: str) -> str | None:
    """First <tag> without an xml:lang attribute (the source language)."""
    for el in root.findall(tag):
        if "{http://www.w3.org/XML/1998/namespace}lang" not in el.attrib:
            return (el.text or "").strip() or None
    return None


def _description_text(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    parts: list[str] = []
    for child in el:
        if "{http://www.w3.org/XML/1998/namespace}lang" in child.attrib:
            continue
        if child.tag == "p":
            parts.append(_WS.sub(" ", "".join(child.itertext())).strip())
        elif child.tag in ("ul", "ol"):
            for li in child.findall("li"):
                parts.append("- " + _WS.sub(" ", "".join(li.itertext())).strip())
    return "\n".join(p for p in parts if p) or None


def parse_metainfo(text: str) -> MetaInfo | None:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if root.tag not in ("component", "application"):
        return None
    info = MetaInfo()
    info.app_id = (_untranslated(root, "id") or "").removesuffix(".desktop") or None
    info.name = _untranslated(root, "name")
    info.summary = _untranslated(root, "summary")
    info.description = _description_text(root.find("description"))
    info.license = _untranslated(root, "project_license")
    dev = root.find("developer")
    if dev is not None:
        info.developer_name = _untranslated(dev, "name")
    if not info.developer_name:
        info.developer_name = _untranslated(root, "developer_name")
    for url in root.findall("url"):
        kind = url.get("type")
        val = (url.text or "").strip()
        if not val:
            continue
        if kind == "homepage":
            info.homepage = val
        elif kind == "donation":
            info.donation = val
        elif kind == "vcs-browser":
            info.vcs = val
    for shot in root.findall("screenshots/screenshot"):
        img = shot.find("image")
        if img is not None and (img.text or "").strip():
            info.screenshots.append(_decamo(img.text.strip()))
    info.categories = [c.text.strip() for c in root.findall("categories/category") if c.text and c.text.strip()]
    rel = root.find("releases/release")
    if rel is not None and rel.get("version"):
        info.latest_version = rel.get("version")
    return info
