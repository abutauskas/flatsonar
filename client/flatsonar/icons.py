"""Fetch app icons and screenshots off the main thread and cache them on disk.

A page of the catalogue is dozens of icons from as many hosts. They go through one
small pool (so a page does not start 36 threads at once, and the first rows win the
race instead of the whole page finishing together), requests for a URL already on its
way are coalesced, and decoded textures are kept for the session."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx
from gi.repository import Gdk, GLib, Gtk

from .paths import cache_dir

log = logging.getLogger("flatsonar.icons")

ICON_CACHE = cache_dir() / "flatsonar" / "icons"
FALLBACK = "flatsonar-app-fallback"
_textures: dict[str, Gdk.Texture] = {}
_waiting: dict[str, list[Callable[[Gdk.Texture | None], None]]] = {}
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="icons")
_client = httpx.Client(timeout=15.0, follow_redirects=True, headers={"User-Agent": "Flatsonar-client/1.0"},
                       limits=httpx.Limits(max_connections=16, max_keepalive_connections=16))


def _fetch(url: str) -> Gdk.Texture | None:
    ICON_CACHE.mkdir(parents=True, exist_ok=True)
    p = ICON_CACHE / hashlib.sha256(url.encode()).hexdigest()[:24]
    if not p.exists():
        r = _client.get(url)
        if r.status_code != 200 or not r.content:
            return None
        tmp = p.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(p)
    try:
        return Gdk.Texture.new_from_filename(str(p))
    except GLib.Error as exc:
        log.debug("bad image %s: %s", url, exc)
        p.unlink(missing_ok=True)
        return None


def _deliver(url: str, tex: Gdk.Texture | None) -> bool:
    if tex is not None:
        _textures[url] = tex
    for cb in _waiting.pop(url, []):
        cb(tex)
    return False


def _work(url: str) -> None:
    try:
        tex = _fetch(url)
    except (httpx.HTTPError, OSError) as exc:  # keep the fallback
        log.debug("image fetch failed %s: %s", url, exc)
        tex = None
    GLib.idle_add(_deliver, url, tex)


def load_texture(url: str | None, callback: Callable[[Gdk.Texture | None], None]) -> None:
    """``callback(texture or None)`` on the main loop; immediately if already loaded."""
    if not url:
        callback(None)
        return
    if url in _textures:
        callback(_textures[url])
        return
    first = url not in _waiting
    _waiting.setdefault(url, []).append(callback)
    if first:
        _pool.submit(_work, url)


def load_into(image: Gtk.Image, url: str | None, fallback: str = FALLBACK) -> None:
    image.set_from_icon_name(fallback)
    if url in _textures:
        image.set_from_paintable(_textures[url])
        return

    def _done(tex: Gdk.Texture | None) -> None:
        if tex is not None:
            image.set_from_paintable(tex)

    load_texture(url, _done)
