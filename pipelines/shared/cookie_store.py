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
import time
from pathlib import Path

STORE = Path(__file__).resolve().parent / "Data" / "session_cookies.json"

_LOCK = threading.Lock()

# Windows refuses to open or replace a file another process has open
# (PermissionError). It clears in milliseconds, so retry before giving up.
_RETRIES = 20
_RETRY_SLEEP = 0.05


def _retry(fn):
    for attempt in range(_RETRIES):
        try:
            return fn()
        except PermissionError:
            if attempt == _RETRIES - 1:
                raise
            time.sleep(_RETRY_SLEEP)


def _read(strict=False):
    """The store as a dict, {} when it doesn't exist yet.

    A store that exists but can't be read is {} for a reader, which then uses
    its scraper's literal, but an error for a writer (strict): writing {key}
    over it would erase every other supplier's cookie.
    """
    try:
        doc = json.loads(_retry(lambda: STORE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise
        return {}
    if not isinstance(doc, dict):
        if strict:
            raise ValueError(f"{STORE} does not hold a JSON object")
        return {}
    return doc


def get(key, default=None):
    """The stored cookie for `key`, or `default` when none has been saved."""
    value = _read().get(key)
    return value if value else default


def put(key, value):
    """Save the cookie for `key`. Written via a temp file so a scraper reading
    concurrently never sees a half-written store."""
    with _LOCK:
        doc = _read(strict=True)
        doc[key] = value
        STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE.with_name(STORE.name + f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            _retry(lambda: os.replace(tmp, STORE))
        finally:
            tmp.unlink(missing_ok=True)
