#!/usr/bin/env python3
"""Emerge Tires — Tire Scraper (Costar API, signed HMAC requests).
Recursive coverage-refinement loop — see SCRAPER_SPEC.md.

Phase 0 completed 2026-06-16:
  - Signing key: CONFIRMED same as Formula/NC (verified against costar.min.js 5.6.15.0).
  - Spec GUIDs: only fba9310f... is active ("Passenger / LT"). Other 3 return 0 results.
  - SEARCH_CAP: 75 confirmed (broad term '225' returned exactly 75).
  - TOTAL_COUNT_KEY: None — no total-count field in Product/Search response.
  - brandName: empty for ~37% of products; use manufacturerCode as fallback.
  - productTypeCode: '2', '9', '2W', '3' — all tires (numeric codes, no accessories found).
  - availableQuantityAtRelatedBranches: always 0.0 — use availableQuantity only.
  - Branches: Delta (BC) and Calgary (AB) — apply all 6 Location_ tags.

Only refresh COOKIE_STRING when session expires (~2 weeks).
"""

import hashlib
import base64
import json
import re
import requests
import csv
import os
import sys
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")  # avoid UnicodeEncodeError on Windows

BASE_URL = "https://order.emergetires.com/WebOrder/CostarApi"

# Phase 0.3 confirmed: same key as Formula/NC despite different CwsVersion (5.6.15.0).
_SIGNING_KEY = "5318f4ce4b9bab05c09df20c3d5d8d4c26570542501b1d58231159cd42e65505"

# >>> Update when session expires (~2 weeks). Captured 2026-06 — verify in Phase 0.1.
COOKIE_STRING = "CostarWebUserData=jRa4aGghLZiK4h9tf9KS2w%3d%3d=&CultureCode=en-US&IsRememberMe=True&UserRequestedUx=&wO3%2fdZCItViuMG66VHQitQ%3d%3d=Gjg2uck6Y7pl%2fZtRFx567g%3d%3d&UserID=b72c4c92-5b4b-4e5b-9109-60bf900446e1; CostarWebAppData=M1dloT0oGu5oPHZUHVYKjw%3d%3d=&CwsVersion=5.6.15.0&DbVersion=0005.0006.0015.0000&5lG0d7HPQ%2fkTy%2fCvDKdPsQ%3d%3d=%2bXR%2fYsREZu%2bRNi08ng5v11P0GNu%2fD5EaRKKcYuCrJnlGPT27IpdiENF%2bo2HSLOBJ&UnifiedBuildNumber=5.6.15.0&Ux=I34R06XR&CostarLanguage=&SessionID=099b4b2d-1f81-4971-aaeb-aa8d3842c8d3; ASP.NET_SessionId=22esgtlqj4bit2serqy14y31"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://order.emergetires.com",
    "Referer": "https://order.emergetires.com/WebOrder/Product/Search",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "encryptionAlgorithm": "aes-cbc",
    "signatureAlgorithm": "sha256",
}

# Phase 0.4 confirmed: only this one GUID is active for Emerge ("Passenger / LT").
# The other 3 Formula/NC GUIDs return 0 results on Emerge's catalogue.
SPEC_CATEGORY_GUIDS = [
    "fba9310f-8f95-42ab-986b-44809d666571",   # "Passenger / LT" — only active category
]

PRODUCT_INFO_BATCH_SIZE = 50
SEARCH_CAP      = 75    # Phase 0.4 confirmed: broad term '225' returned exactly 75.
MAX_WORKERS     = 4
GAIN_STOP       = 0.01  # terminate refinement when a pass adds < 1% new rows
TOTAL_COUNT_KEY = None  # Phase 0.4 confirmed: no total-count field in response.

# ── Search term grid ──────────────────────────────────────────────────────────
_WIDTHS    = [str(w) for w in range(105, 400, 5)]
_RATIOS    = [str(r) for r in range(25, 105, 5)]
_FLOTATION = [f"{d}X" for d in range(28, 46)]

_LT_WIDTHS = [str(w) for w in range(185, 360, 5)]
_LT_RATIOS = [str(r) for r in range(55, 100, 5)]
_LT_TERMS  = [f"LT{w}/{r}" for w in _LT_WIDTHS for r in _LT_RATIOS]

_ST_WIDTHS = [str(w) for w in range(145, 270, 5)]
_ST_RATIOS = ["55", "60", "65", "70", "75", "80", "85"]
_ST_TERMS  = [f"ST{w}/{r}" for w in _ST_WIDTHS for r in _ST_RATIOS]

SEARCH_TERMS = _FLOTATION + [f"{w}/{r}" for w in _WIDTHS for r in _RATIOS] + _LT_TERMS + _ST_TERMS

_RIM_SIZES = [
    "12", "13", "14", "15",
    "16", "16.5",
    "17", "17.5",
    "18", "19", "19.5",
    "20", "21", "22", "22.5",
    "23", "24", "24.5",
    "26", "26.5",
    "28", "30",
]

_PLAIN_METRIC_RE = re.compile(r'^\d+/\d+$')
_ST_TERM_RE      = re.compile(r'^ST\d+/\d+$')


def _sign(body_str: str) -> dict:
    now = datetime.now(timezone.utc)
    ts  = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}"
    payload = json.dumps(
        {"utcTimestamp": ts, "data": body_str, "signingKey": _SIGNING_KEY},
        separators=(",", ":"),
    )
    sig = hashlib.sha256(base64.b64encode(payload.encode())).hexdigest()
    return {"utcTimestamp": ts, "signature": sig}


def api_post(endpoint: str, body: dict, _retries: int = 4) -> dict:
    body_str = json.dumps(body, separators=(",", ":"))
    for attempt in range(_retries):
        signed  = _sign(body_str)
        headers = {**HEADERS, **signed, "Cookie": COOKIE_STRING}
        resp = requests.post(f"{BASE_URL}/{endpoint}", headers=headers, data=body_str, timeout=30)
        if resp.ok:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < _retries - 1:
            wait = 2 ** attempt
            print(f"  HTTP {resp.status_code} (attempt {attempt+1}/{_retries}) — retry in {wait}s")
            time.sleep(wait)
            continue
        print(f"  HTTP {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
    resp.raise_for_status()


def _search_one(term: str) -> tuple:
    """Issue a single Product/Search. Returns (searchResultList, total_count | None)."""
    body = {
        "criteriaList": [
            {"dataField": "FullText", "string": term},
            {
                "dataField": "SpecificationCategoryGuidList",
                "criteriaList": [
                    {"dataField": "SpecificationCategoryGuid", "guid": g}
                    for g in SPEC_CATEGORY_GUIDS
                ],
            },
            {"dataField": "IsStaggeredSearch", "boolean": False},
        ],
        "requestId": str(uuid.uuid4()).upper(),
    }
    data = api_post("Product/Search", body)
    results = data.get("searchResultList") or []
    total   = data.get(TOTAL_COUNT_KEY) if TOTAL_COUNT_KEY else None
    return results, total


def _is_capped(results: list, total) -> bool:
    """Return True if this result set is at or over the cap (may have missed products)."""
    if total is not None:
        return total > len(results)
    return len(results) >= SEARCH_CAP


def search_with_drilldown(term: str) -> tuple:
    """Search term + first-level rim drilldown if capped.

    Returns (results_list, still_capped_bool).
    still_capped=True means at least one rim-sub-search also hit the cap,
    which triggers a deeper refinement pass.

    Threading: all work is local (no shared state written). Caller merges
    results into the global pool under lock.
    """
    try:
        results, total = _search_one(term)
    except Exception as e:
        print(f"  ERROR '{term}': {e}")
        return [], False

    if not _is_capped(results, total):
        return results, False

    # Cap hit — drilldown by rim to recover all products
    local: dict = {r["productGuid"]: r for r in results if r.get("productGuid")}
    is_plain = bool(_PLAIN_METRIC_RE.match(term))
    is_st    = bool(_ST_TERM_RE.match(term))
    still_capped = False

    for rim in _RIM_SIZES:
        # Standard drilldown: "225/65R17"
        try:
            sub, sub_total = _search_one(f"{term}R{rim}")
            for r in sub:
                g = r.get("productGuid")
                if g:
                    local[g] = r
            if _is_capped(sub, sub_total):
                still_capped = True
        except Exception as e:
            print(f"  WARN {term}R{rim}: {e}")

        # LT-prefix drilldown for plain metric: "LT225/65R17"
        if is_plain:
            try:
                lt_sub, _ = _search_one(f"LT{term}R{rim}")
                for r in lt_sub:
                    g = r.get("productGuid")
                    if g:
                        local[g] = r
            except Exception:
                pass

        # D-ply drilldown for ST terms: "ST205/75D15"
        if is_st:
            try:
                d_sub, _ = _search_one(f"{term}D{rim}")
                for r in d_sub:
                    g = r.get("productGuid")
                    if g:
                        local[g] = r
            except Exception:
                pass

    return list(local.values()), still_capped


def flatten_product(p: dict) -> dict:
    # Phase 0 finding: brandName is empty for ~37% of Emerge products.
    # manufacturerCode is always populated and holds the brand name (e.g. "GRENLANDER").
    brand = (p.get("brandName", "") or "").strip() or \
            (p.get("manufacturerCode", "") or "").strip() or \
            (p.get("manufacturerDescription", "") or "").strip()
    return {
        "product_guid":                        p.get("productGuid", ""),
        "sku":                                 p.get("brandSku", "") or p.get("item", ""),
        "brand":                               brand,
        "model":                               p.get("model", ""),
        "description":                         p.get("description", ""),
        "size":                                p.get("size", ""),
        "product_type_code":                   p.get("productTypeCode", ""),
        "specification_category":              p.get("specificationCategoryCaption", ""),
        "specification_category_code":         p.get("specificationCategoryCode", ""),
        "available_quantity":                  p.get("availableQuantity", ""),
        "available_quantity_related_branches": p.get("availableQuantityAtRelatedBranches", ""),
        "retail_price":                        p.get("retailPrice", ""),
        "suggested_price":                     p.get("suggestedPrice", ""),
        "is_winter_tire":                      p.get("isWinterTire", ""),
        "is_all_weather":                      p.get("isAllWeather", ""),
        "is_on_sale":                          p.get("isOnSale", ""),
        "is_active":                           p.get("isActive", ""),
        "is_discontinued":                     p.get("isDiscontinued", ""),
        "status":                              p.get("status", ""),
        "manufacturer_code":                   p.get("manufacturerCode", ""),
        "image_url":                           "",
    }


_image_keys_logged = False


def get_product_info(guids: list) -> tuple:
    """Fetch ProductInfo for a batch of GUIDs.
    Returns ({guid: image_url}, {guid: {spec_code: value}}).
    """
    global _image_keys_logged
    body = {
        "criteriaList": [{
            "dataField": "ProductGuidList",
            "criteriaList": [
                {"dataField": "ProductGuid", "guid": g} for g in guids
            ],
        }],
        "requestId": str(uuid.uuid4()).upper(),
    }
    data = api_post("Product/ProductInfo", body)

    if not _image_keys_logged:
        print(f"\n[ProductInfo] Top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
        _image_keys_logged = True

    guid_to_image = {}
    for img in data.get("productImageList") or []:
        guid = img.get("productGuid", "")
        url  = img.get("url", "")
        if not guid or not url:
            continue
        if guid not in guid_to_image or img.get("imageType", "") == "LARGE":
            guid_to_image[guid] = url

    guid_to_specs = {}
    for entry in data.get("productSpecificationList") or []:
        guid  = entry.get("productGuid", "")
        code  = entry.get("code", "").upper()
        value = entry.get("value", "")
        if not guid:
            continue
        if guid not in guid_to_specs:
            guid_to_specs[guid] = {}
        guid_to_specs[guid][code] = value

    return guid_to_image, guid_to_specs


_CHECKPOINT_FILE = "Data/raw/emergetires_tires_checkpoint.json"


def _save_checkpoint(output_file: str, pool: dict, stage: str) -> None:
    """Write pool to JSON checkpoint so a crash/session-expiry never loses the search pass.

    Stage values: 'pass1' (breadth done), 'refine_done' (all refine passes done).
    Resume reads this file, restores pool + output_file, and skips to ProductInfo.
    Uses atomic replace so a partial write never corrupts an existing checkpoint.
    """
    tmp = _CHECKPOINT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"output_file": output_file, "stage": stage, "pool": pool}, f)
    os.replace(tmp, _CHECKPOINT_FILE)
    print(f"  [checkpoint] saved {len(pool)} products at stage={stage!r} → {_CHECKPOINT_FILE}")


def _load_checkpoint() -> tuple:
    """Return (output_file, pool, stage) from checkpoint, or (None, {}, None) if none exists."""
    if not os.path.exists(_CHECKPOINT_FILE):
        return None, {}, None
    try:
        with open(_CHECKPOINT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        output_file = data.get("output_file")
        pool        = data.get("pool", {})
        stage       = data.get("stage")
        if not output_file or not pool:
            return None, {}, None
        age_h = (datetime.now().timestamp() - os.path.getmtime(_CHECKPOINT_FILE)) / 3600
        print(f"  [checkpoint] found {len(pool)} products, stage={stage!r}, age={age_h:.1f}h → {output_file}")
        return output_file, pool, stage
    except Exception as e:
        print(f"  [checkpoint] load failed ({e}), starting fresh")
        return None, {}, None


def main():
    os.makedirs("Data/raw", exist_ok=True)

    # ── Resume detection ─────────────────────────────────────────────────────
    ck_output_file, ck_pool, ck_stage = _load_checkpoint()
    resume = False
    if ck_pool:
        answer = input(
            f"\nCheckpoint found: {len(ck_pool)} products at stage={ck_stage!r}.\n"
            f"  Resume (skip Pass 1 + refine, go straight to ProductInfo)? [y/N]: "
        ).strip().lower()
        resume = answer == "y"

    if resume:
        output_file = ck_output_file
        pool        = ck_pool
        seen        = set(pool.keys())
        capped      = []
        print(f"Resuming from checkpoint — {len(pool)} products loaded, skipping to ProductInfo.")
    else:
        output_file = f"Data/raw/emergetires_tires_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        seen: set   = set()
        pool: dict  = {}
        capped: list = []

    if not resume:
        lock      = threading.Lock()
        completed = [0]
        total_terms = len(SEARCH_TERMS)

        print(f"Starting scrape — {total_terms} search terms ({MAX_WORKERS} workers)")
        print(f"  Metric terms  : {len(_WIDTHS) * len(_RATIOS)}")
        print(f"  Flotation     : {len(_FLOTATION)}")
        print(f"  LT-prefix     : {len(_LT_TERMS)}")
        print(f"  ST-prefix     : {len(_ST_TERMS)}")
        print(f"  Signing key   : {_SIGNING_KEY[:8]}... (confirmed Phase 0.3)")
        print(f"  SEARCH_CAP    : {SEARCH_CAP} (confirmed Phase 0.4)")
        print(f"  TOTAL_COUNT_KEY: {TOTAL_COUNT_KEY!r} (no total-count field — confirmed Phase 0.4)")
        print(f"  Active GUIDs  : {len(SPEC_CATEGORY_GUIDS)} ('Passenger / LT' only)")

    # ── PASS 1: Breadth (parallel) ───────────────────────────────────────────
    def process(term: str):
        """Worker: search + drilldown locally, merge into global pool under lock."""
        results, is_still_capped = search_with_drilldown(term)
        with lock:
            new = [r for r in results if r.get("productGuid") not in seen]
            for r in new:
                g = r["productGuid"]
                seen.add(g)
                pool[g] = r
            completed[0] += 1
            if is_still_capped:
                capped.append(term)
            print(
                f"  [{completed[0]}/{total_terms}] '{term}': {len(results)} results, "
                f"{len(new)} new (total: {len(pool)})"
                + (" [STILL-CAPPED]" if is_still_capped else "")
            )

    if not resume:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(process, term) for term in SEARCH_TERMS]
            for f in as_completed(futures):
                f.result()
        _save_checkpoint(output_file, pool, "pass1")

    # ── REFINE passes: depth, with <1% gain termination ─────────────────────
    # `capped` contains terms where at least one rim-sub also hit the cap,
    # meaning we may have missed products in that sub-family. Refine by searching
    # {term}R{rim} explicitly and checking those for cap too.
    prev_total   = len(pool)
    refine_pass  = 0

    while capped:
        refine_pass += 1
        next_capped: list = []
        print(f"\n── Refine pass {refine_pass}: {len(capped)} still-capped terms ──")

        for term in list(capped):
            for rim in _RIM_SIZES:
                sub_term = f"{term}R{rim}"
                try:
                    sub, sub_total = _search_one(sub_term)
                except Exception as e:
                    print(f"  WARN refine {sub_term}: {e}")
                    continue

                # Merge sub results (no threading in refine passes — sequential)
                for r in sub:
                    g = r.get("productGuid")
                    if g and g not in seen:
                        seen.add(g)
                        pool[g] = r

                if _is_capped(sub, sub_total):
                    next_capped.append(sub_term)

        gain = len(pool) - prev_total
        print(f"  Refine pass {refine_pass}: +{gain} rows (total {len(pool)}), "
              f"still-capped {len(next_capped)}")

        if gain < GAIN_STOP * max(len(pool), 1):
            print("  Termination: <1% gain — stopping refinement.")
            capped = next_capped
            break

        prev_total = len(pool)
        capped     = next_capped

    if capped:
        print(f"\n{'='*60}")
        print(f"COVERAGE WARNING — {len(capped)} still-capped terms after refinement:")
        for t in sorted(capped):
            print(f"  {t}")
        print("These size families may have incomplete coverage.")
        print(f"{'='*60}")

    if not resume:
        _save_checkpoint(output_file, pool, "refine_done")

    if not pool:
        print("\nNo products scraped — check cookies (Phase 0.1) and signing key (Phase 0.3).")
        return

    # ── Flatten pool to list ─────────────────────────────────────────────────
    products = [flatten_product(p) for p in pool.values()]

    # ── SECOND PASS: ProductInfo (images + specs) ────────────────────────────
    print(f"\nFetching images + specs via ProductInfo ({len(products)} products, "
          f"batch size {PRODUCT_INFO_BATCH_SIZE})...")

    guid_map       = {p["product_guid"]: i for i, p in enumerate(products) if p["product_guid"]}
    guids          = list(guid_map.keys())
    images_found   = 0
    specs_found    = 0
    all_spec_codes = set()
    all_guid_specs = {}

    for batch_start in range(0, len(guids), PRODUCT_INFO_BATCH_SIZE):
        batch = guids[batch_start:batch_start + PRODUCT_INFO_BATCH_SIZE]
        try:
            guid_to_image, guid_to_specs = get_product_info(batch)

            for guid, image_url in guid_to_image.items():
                if guid in guid_map and image_url:
                    products[guid_map[guid]]["image_url"] = image_url
                    images_found += 1

            for guid, specs in guid_to_specs.items():
                all_spec_codes.update(specs.keys())
                all_guid_specs[guid] = specs
                specs_found += 1

        except Exception as e:
            print(f"  WARNING: ProductInfo batch {batch_start}-{batch_start+len(batch)} failed: {e}")
        time.sleep(0.5)

    print(f"Images found : {images_found} / {len(products)}")
    print(f"Specs found  : {specs_found} / {len(products)}")

    sorted_codes = sorted(all_spec_codes)
    if sorted_codes:
        print(f"Spec codes   : {sorted_codes}")
        for product in products:
            guid  = product["product_guid"]
            specs = all_guid_specs.get(guid, {})
            for code in sorted_codes:
                product[f"spec_{code.lower()}"] = specs.get(code, "")

    # ── Category breakdown ───────────────────────────────────────────────────
    from collections import Counter
    cat_counts = Counter(p.get("specification_category", "") for p in products)
    print(f"\nPer-category counts:")
    for cat, cnt in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat or '(blank)':40s}: {cnt}")

    # ── Write CSV ────────────────────────────────────────────────────────────
    fieldnames = list(products[0].keys())
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(products)

    # Remove checkpoint on clean completion so next run starts fresh
    if os.path.exists(_CHECKPOINT_FILE):
        os.remove(_CHECKPOINT_FILE)
        print("  [checkpoint] cleared after successful write.")

    print(f"\n{'='*60}")
    print(f"Done. {len(products)} products -> {output_file}")
    print(f"Capped terms remaining: {len(capped)} (target: 0)")
    print(f"Next steps: python clean.py --type tires, then python augment_tires.py, then python qa_check.py")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
