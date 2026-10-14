#!/usr/bin/env python3
"""Supplier session cookies, kept in one JSON file instead of inside the scrapers.

Cookies used to be refreshed by regex-patching the COOKIE_STRING / COOKIES
literals in each scraper's own source. That made every refresh a diff on a .py
file, and it cannot work at all in the packaged Mac app, which ships compiled
bytecode and no source to patch.

Scrapers keep their literal as a fallback and read through here:

    COOKIE_STRING = cookie_store.get("nc", COOKIE_STRING)

refresh_cookies.py, emergetires/refresh_session.py and the in-run re-auth in
costar_session.py all write here, so the next run starts from the fresh cookie.
Keys match refresh_cookies.SUPPLIERS: formula, nc, emerge (Costar cookie
strings) and wheelsmart (a dict of cookie names to values).
"""

import json
import os
import threading
from pathlib import Path

STORE = Path(__file__).resolve().parent / "Data" / "session_cookies.json"

_LOCK = threading.Lock()


def _read():
    try:
        doc = json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def get(key, default=None):
    """The stored cookie for `key`, or `default` when none has been saved."""
    value = _read().get(key)
    return value if value else default


def put(key, value):
    """Save the cookie for `key`. Written via a temp file so a scraper reading
    concurrently never sees a half-written store."""
    with _LOCK:
        doc = _read()
        doc[key] = value
        STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE.with_name(STORE.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        os.replace(tmp, STORE)
