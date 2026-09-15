"""Fetch app icons off the main thread and cache them on disk."""

from __future__ import annotations

import hashlib
import logging

import httpx
from gi.repository import Gdk, GLib, Gtk

from .paths import cache_dir

log = logging.getLogger("flatsea.icons")

ICON_CACHE = cache_dir() / "flatsea" / "icons"
_textures: dict[str, Gdk.Texture] = {}
_client = httpx.Client(timeout=15.0, follow_redirects=True, headers={"User-Agent": "Flatsea-client/0.1"})


def _fetch(url: str) -> Gdk.Texture | None:
    ICON_CACHE.mkdir(parents=True, exist_ok=True)
    p = ICON_CACHE / hashlib.sha256(url.encode()).hexdigest()[:24]
    if not p.exists():
        r = _client.get(url)
        if r.status_code != 200 or not r.content:
            return None
        p.write_bytes(r.content)
    try:
        return Gdk.Texture.new_from_filename(str(p))
    except GLib.Error as exc:
        log.debug("bad icon %s: %s", url, exc)
        p.unlink(missing_ok=True)
        return None


def load_into(image: Gtk.Image, url: str | None, fallback: str = "application-x-executable-symbolic") -> None:
    image.set_from_icon_name(fallback)
    if not url:
        return
    if url in _textures:
        image.set_from_paintable(_textures[url])
        return

    from .asyncjob import run_async

    def _done(tex):
        if tex is not None:
            _textures[url] = tex
            image.set_from_paintable(tex)

    run_async(lambda: _fetch(url), _done, lambda e: None)
