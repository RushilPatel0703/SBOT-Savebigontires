#!/usr/bin/env python3
"""Shared Shopify API configuration and helpers.
All pipeline scripts import from this file via their local config.py re-exporter.
"""

# ── Shopify ───────────────────────────────────────────────────────────────────
SHOP        = "database-testing-2.myshopify.com"
TOKEN       = "shpua_d032a667ceb661c96c27dcfbd11fa33f"
API_VERSION = "2026-01"

# ── SerpAPI ───────────────────────────────────────────────────────────────────
SERPAPI_KEY = "48085de7f5e2e78afae54edad4bc9c9da80d9acd4cf246845a7ccc3acaf12c95"

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
