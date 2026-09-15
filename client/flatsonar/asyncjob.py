"""Run blocking work off the GTK main loop and deliver the result back on it."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from gi.repository import GLib

log = logging.getLogger("flatsonar.job")


def run_async(fn: Callable[[], Any], on_done: Callable[[Any], None] | None = None,
              on_error: Callable[[Exception], None] | None = None) -> threading.Thread:
    def _worker():
        try:
            result = fn()
        except Exception as exc:  # deliver to UI, never kill the thread silently
            log.exception("background job failed")
            if on_error:
                GLib.idle_add(on_error, exc)
            return
        if on_done:
            GLib.idle_add(on_done, result)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


def call_on_main(fn: Callable[..., Any], *args) -> None:
    GLib.idle_add(lambda: (fn(*args), False)[1])


def ask_on_main(present: Callable[[Callable[[Any], None]], None]) -> Any:
    """Block a worker thread until the main loop answers.

    ``present(reply)`` is invoked on the main thread; it must eventually call
    ``reply(value)`` (for example from a dialog's response handler).
    """
    done = threading.Event()
    box: list[Any] = [None]

    def _reply(value: Any) -> None:
        box[0] = value
        done.set()

    GLib.idle_add(lambda: (present(_reply), False)[1])
    done.wait()
    return box[0]
