#!/usr/bin/env python3
"""Shared Shopify API configuration and helpers.
All pipeline scripts import from this file via their local config.py re-exporter.
"""

import os as _os_env

# Secrets resolve from the environment first, falling back to the literals below
# so existing runs keep working unchanged. Set them in the environment (or a
# .env loaded by your shell) and the literals become dead — at which point they
# should be rotated and deleted, since anything committed here is in git history.
# See PRODUCTION_READINESS.md.
def _secret(name, fallback):
    return _os_env.environ.get(name) or fallback


# ── Shopify ───────────────────────────────────────────────────────────────────
SHOP        = _secret("SBOT_SHOPIFY_SHOP", "database-testing-2.myshopify.com")
TOKEN       = _secret("SBOT_SHOPIFY_TOKEN", "shpua_d032a667ceb661c96c27dcfbd11fa33f")
API_VERSION = _secret("SBOT_SHOPIFY_API_VERSION", "2026-01")

# ── SerpAPI ───────────────────────────────────────────────────────────────────
# Dev/testing key — used by the Streamlit dashboard. NOT the client's paid key.
SERPAPI_KEY = _secret(
    "SBOT_SERPAPI_KEY_DEV",
    "48085de7f5e2e78afae54edad4bc9c9da80d9acd4cf246845a7ccc3acaf12c95")

# Client-paid key. Every search on this key costs the client money and must be
# explicitly approved before a live run — see monitoring/SERP_LAYER_DESIGN.md.
SERPAPI_KEY_CLIENT = _secret(
    "SBOT_SERPAPI_KEY_CLIENT",
    "d64a4db1926cf1a1a7f7c6796403bf0a22dc78b2519bdaad48e41514b2861e59")

# ── Per-supplier markup ───────────────────────────────────────────────────────
# The table is no longer hard-coded here: the client edits it before each run
# (dashboard → "Markup per supplier"), and markup.py is the single definition
# shared with production/config.py. Each supplier is either dollar or percent.
#
# Loaded by path, not by `import markup`: the per-supplier config.py shims pull
# this file in via spec_from_file_location, so pipelines/shared/ is never on
# sys.path and a plain import would fail there.
import importlib.util as _ilu

_markup_spec = _ilu.spec_from_file_location(
    "_sbot_markup",
    _os_env.path.join(_os_env.path.dirname(_os_env.path.abspath(__file__)), "markup.py"),
)
markup = _ilu.module_from_spec(_markup_spec)
_markup_spec.loader.exec_module(markup)

DEFAULT_RULES     = markup.DEFAULT_RULES
MarkupConfigError = markup.MarkupConfigError
describe_markup   = markup.describe
load_markups      = markup.load
save_markups      = markup.save
price_floor       = markup.price_floor
price_with_markup = markup.price_with_markup
markup_rule_for   = markup.rule_for


def markup_for(supplier):
    """Back-compat shim: the flat dollar markup for a supplier.

    Only meaningful while that supplier is on dollar mode. Under percent mode
    the markup depends on the item's cost, so there is no single number to
    return and this raises — callers must use price_with_markup(cost, supplier).
    Failing here is the point: a caller that kept doing `cost + markup_for(s)`
    after the client switched s to a percentage would misprice that whole feed.
    """
    rule = markup_rule_for(supplier)
    if rule["mode"] != "dollar":
        raise MarkupConfigError(
            f"Supplier {supplier!r} uses a {rule['mode']} markup "
            f"({describe_markup(supplier)}), which has no fixed dollar value. "
            f"Use price_with_markup(cost, {supplier!r}) instead of markup_for().")
    return rule["value"]

# ── Shared SKU/handle map paths (absolute — always written/read from shared/Data/) ──
import os as _os
_HERE           = _os.path.dirname(_os.path.abspath(__file__))
HANDLE_MAP_FILE = _os.path.join(_HERE, "Data", "handle_map.json")
SKU_MAP_FILE    = _os.path.join(_HERE, "Data", "sku_map.json")

# ── Shopify API helpers ───────────────────────────────────────────────────────
import glob as _glob
import requests
import time
import logging
from datetime import datetime

HEADERS = {
    "X-Shopify-Access-Token": TOKEN,
    "Content-Type": "application/json",
}


def find_latest(pattern):
    """Return the most recently modified file matching glob pattern, or None."""
    matches = sorted(_glob.glob(pattern), key=_os.path.getmtime, reverse=True)
    return matches[0] if matches else None


def api_get(url, retries=5):
    errors = 0
    while True:
        r = requests.get(url, headers=HEADERS)
        if r.status_code == 429:
            wait = max(2, int(float(r.headers.get("Retry-After", 2))))
            time.sleep(wait)
            continue  # 429s don't count against retries
        if not r.ok:
            errors += 1
            if errors >= retries:
                raise Exception(f"HTTP {r.status_code} on GET {url}: {r.text[:200]}")
            time.sleep(1)
            continue
        return r


def api_put(url, payload, retries=5):
    errors = 0
    while True:
        r = requests.put(url, headers=HEADERS, json=payload)
        if r.status_code == 429:
            wait = max(2, int(float(r.headers.get("Retry-After", 2))))
            time.sleep(wait)
            continue
        if not r.ok:
            errors += 1
            if errors >= retries:
                raise Exception(f"HTTP {r.status_code} on PUT {url}: {r.text[:200]}")
            time.sleep(1)
            continue
        return r


def api_post(url, payload, retries=5):
    errors = 0
    while True:
        r = requests.post(url, headers=HEADERS, json=payload)
        if r.status_code == 429:
            wait = max(2, int(float(r.headers.get("Retry-After", 2))))
            time.sleep(wait)
            continue
        if not r.ok:
            errors += 1
            if errors >= retries:
                raise Exception(f"HTTP {r.status_code} on POST {url}: {r.text[:200]}")
            time.sleep(1)
            continue
        return r


def api_delete(url, retries=5):
    errors = 0
    while True:
        r = requests.delete(url, headers=HEADERS)
        if r.status_code == 429:
            wait = max(2, int(float(r.headers.get("Retry-After", 2))))
            time.sleep(wait)
            continue
        if not r.ok:
            errors += 1
            if errors >= retries:
                raise Exception(f"HTTP {r.status_code} on DELETE {url}: {r.text[:200]}")
            time.sleep(1)
            continue
        return r


def get_logger(name, log_dir="logs"):
    """Logger that writes to both console and logs/<name>_TIMESTAMP.log.
    log_dir is relative to the caller's working directory.
    """
    _os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = _os.path.join(log_dir, f"{name}_{timestamp}.log")

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        fh.setFormatter(fmt)
        ch.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(ch)

    logger.info(f"Log file: {log_file}")
    return logger
