#!/usr/bin/env python3
"""North Country Tire — Tire Scraper
Fetches tire products from the Costar API using signed HMAC requests.

IMPORTANT: Update COOKIE_STRING below when your session expires.
"""

import hashlib
import base64
import json
import re
import requests
import csv
import os
import random
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BASE_URL     = "https://online.northcountrytire.ca/WebOrder/CostarApi"
_SIGNING_KEY = "5318f4ce4b9bab05c09df20c3d5d8d4c26570542501b1d58231159cd42e65505"

# ── Update this when session expires ─────────────────────────────────────────
COOKIE_STRING = "CostarWebAppData=M1dloT0oGu5oPHZUHVYKjw%3d%3d=&CwsVersion=5.7.151.0&DbVersion=0005.0007.0151.0000&5lG0d7HPQ%2fkTy%2fCvDKdPsQ%3d%3d=3w%2bczgMX6A7n3RHKeBte4NRwZMbXBiom1HbS2aNXu3MudpUukoAAvqK8Ff0xxPsn&UnifiedBuildNumber=5.7.151.0&Ux=M48B07OD&CostarLanguage=&SessionID=9ec756ef-07dc-49ac-8a90-e7ec881ec1cb; CostarWebUserData=jRa4aGghLZiK4h9tf9KS2w%3d%3d=&CultureCode=en-US&IsRememberMe=True&UserRequestedUx=&wO3%2fdZCItViuMG66VHQitQ%3d%3d=&UserID=3a743520-dce4-4a69-b4f0-ee2960e625d7; ASP.NET_SessionId=gi0rolmr5in13jmb113q41pn"
# ─────────────────────────────────────────────────────────────────────────────

PRODUCT_INFO_BATCH_SIZE = 50

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://online.northcountrytire.ca",
    "Referer": "https://online.northcountrytire.ca/WebOrder/Product/Search",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "encryptionAlgorithm": "aes-cbc",
    "signatureAlgorithm": "sha256",
}

SPEC_CATEGORY_GUIDS = [
    "fba9310f-8f95-42ab-986b-44809d666571",
    "077f1e3d-ff2a-4fae-8521-7ebcb4ceeef9",
    "1589eae7-c16f-4849-bb57-7fc9a33ef406",
    "bfaa8bcf-ec8f-4e5b-aed8-e68272657098",
]

# ── Search term grid ──────────────────────────────────────────────────────────
_WIDTHS    = [str(w) for w in range(105, 400, 5)]     # 105, 110, ..., 395
_RATIOS    = [str(r) for r in range(25, 105, 5)]      # 25, 30, ..., 100
_FLOTATION = [f"{d}X" for d in range(28, 46)]         # 28X … 45X

# Explicit LT-prefix search terms.
# Root cause: when a metric search like "315/75" hits the 75-result cap, the
# cap is filled by 315/75R22.5 commercial truck tires. The rim-level drilldown
# then searches "315/75R16" but NC stores the tire as "LT315/75R16 E", and
# the API may not substring-match these as distinct results from the capped set.
# Adding "LT315/75" as a dedicated search term finds these sizes directly.
_LT_WIDTHS = [str(w) for w in range(185, 360, 5)]    # 185, 190, ..., 355
_LT_RATIOS = [str(r) for r in range(55, 100, 5)]     # 55, 60, ..., 95
_LT_TERMS  = [f"LT{w}/{r}" for w in _LT_WIDTHS for r in _LT_RATIOS]

# ST (Special Trailer) tire search terms.
# ST tires are stored with the ST prefix in NC's system and are not reliably
# returned by plain metric searches (e.g. "205/75" misses "ST205/75R14").
# ST tires also use both R (radial) and D (diagonal/bias-ply) designations;
# the D-ply drilldown is handled in search_with_drilldown when cap is hit.
_ST_WIDTHS = [str(w) for w in range(145, 270, 5)]    # 145, 150, ..., 265
_ST_RATIOS = ["55", "60", "65", "70", "75", "80", "85"]
_ST_TERMS  = [f"ST{w}/{r}" for w in _ST_WIDTHS for r in _ST_RATIOS]

SEARCH_TERMS = _FLOTATION + [f"{w}/{r}" for w in _WIDTHS for r in _RATIOS] + _LT_TERMS + _ST_TERMS

SEARCH_CAP = 75   # API hard cap — any result set this size may be truncated

# Expanded rim list vs original — added 16.5 (LT duallies/trailers), 23, 24.5, 26.5
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
MAX_WORKERS = 1   # sequential — server can't handle concurrent load without timeouts

# Detects a plain metric size like "315/75" (no prefix) — used to trigger LT drilldown
_PLAIN_METRIC_RE = re.compile(r'^\d+/\d+$')
# Detects an ST term like "ST205/75" — used to trigger D-ply drilldown
_ST_TERM_RE = re.compile(r'^ST\d+/\d+$')
# Detects a flotation term like "35X" — used to trigger width-aware drilldown
_FLOTATION_TERM_RE = re.compile(r'^\d+X$')

# Decimal widths NC appends to flotation sizes (e.g. "35X12.50R20"). The plain
# rim drilldown ("35XR20") has no decimal-width component and never matches
# this format, so flotation terms that hit the cap recovered nothing. Drill
# by width first; values below are the widths observed in scraped data plus
# common flotation widths not yet seen.
_FLOTATION_WIDTHS = [
    "8.50", "9.50", "10.00", "10.50", "11.50",
    "12.50", "13.50", "14.50", "15.50", "16.50",
]

# Speed-rating letters NC sometimes embeds directly before "R" in the size
# string (e.g. "215/50ZR17"). A plain "{term}R{rim}" drilldown query never
# matches these — there's a letter between the size and "R" — so high-speed
# variants silently vanish from the rim drilldown. Add more letters here if
# verify_coverage.py / brand_sweep.py turn up others.
_EMBEDDED_SPEED_LETTERS = ["Z", "Y"]

# Sub-queries that still hit the cap after drilldown — these need a third
# search axis (brand_sweep.py) since rim/width splitting alone couldn't
# separate the result set below SEARCH_CAP. Thread-safe: appended to from
# multiple search_with_drilldown() calls running in parallel worker threads.
STILL_CAPPED: list[str] = []
_still_capped_lock = threading.Lock()


def _flag_still_capped(sub_term: str) -> None:
    with _still_capped_lock:
        STILL_CAPPED.append(sub_term)


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
        try:
            resp = requests.post(f"{BASE_URL}/{endpoint}", headers=headers,
                                 data=body_str, timeout=(10, 90))
        except requests.exceptions.Timeout:
            if attempt < _retries - 1:
                wait = 2 ** attempt + random.uniform(0, 1.0)
                print(f"  Timeout (attempt {attempt+1}/{_retries}) — retry in {wait:.1f}s")
                time.sleep(wait)
                continue
            raise
        if resp.ok:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < _retries - 1:
            wait = 2 ** attempt + random.uniform(0, 1.0)
            print(f"  HTTP {resp.status_code} (attempt {attempt+1}/{_retries}) — retry in {wait:.1f}s")
            time.sleep(wait)
            continue
        print(f"  HTTP {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
    resp.raise_for_status()


def search(term: str) -> list:
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
    return data.get("searchResultList") or []


def _merge(pool: dict[str, dict], results: list) -> int:
    """Add new-GUID entries from results into pool. Returns count added."""
    added = 0
    for r in results:
        g = r.get("productGuid")
        if g and g not in pool:
            pool[g] = r
            added += 1
    return added


def search_with_drilldown(term: str) -> list:
    """Search term; if API cap hit, re-search per rim diameter to recover all results.

    For plain metric terms (e.g. '315/75') also issues LT-prefixed drilldown
    searches (e.g. 'LT315/75R16') since commercial truck tires can saturate the
    75-result cap, displacing LT-only sizes from the initial result set.

    Flotation terms (e.g. '35X') are drilled down by decimal width first
    ('35X12.50'), not by rim — NC stores these with a width component that a
    plain rim suffix never matches. See _FLOTATION_WIDTHS.

    Plain metric terms also get an embedded-speed-letter drilldown (e.g.
    '215/50ZR17') to catch sizes where NC embeds the speed rating directly
    before "R" — these never match a plain "{term}R{rim}" query either.
    """
    try:
        results = search(term)
    except Exception as e:
        print(f"  ERROR '{term}': {e}")
        return []

    if len(results) < SEARCH_CAP:
        return results

    # Cap hit — start with the initial 75 results, then drilldown
    pool = {r["productGuid"]: r for r in results if r.get("productGuid")}
    is_plain_metric = bool(_PLAIN_METRIC_RE.match(term))
    is_st_term      = bool(_ST_TERM_RE.match(term))
    is_flotation    = bool(_FLOTATION_TERM_RE.match(term))

    if is_flotation:
        for width in _FLOTATION_WIDTHS:
            width_term = f"{term}{width}"
            try:
                wsub = search(width_term)
                _merge(pool, wsub)
                if len(wsub) == SEARCH_CAP:
                    print(f"  WARN: '{width_term}' also hit cap — drilling by rim too")
                    for rim in _RIM_SIZES:
                        try:
                            sub = search(f"{width_term}R{rim}")
                            _merge(pool, sub)
                            if len(sub) == SEARCH_CAP:
                                _flag_still_capped(f"{width_term}R{rim}")
                        except Exception as e:
                            print(f"  WARN flotation width-rim drilldown {width_term}R{rim}: {e}")
            except Exception as e:
                print(f"  WARN flotation width drilldown {width_term}: {e}")
        return list(pool.values())

    for rim in _RIM_SIZES:
        # Standard drilldown: "315/75R16"
        try:
            sub = search(f"{term}R{rim}")
            _merge(pool, sub)
            if len(sub) == SEARCH_CAP:
                print(f"  WARN: '{term}R{rim}' also hit cap — some products may still be missed")
                _flag_still_capped(f"{term}R{rim}")
        except Exception as e:
            print(f"  WARN drilldown {term}R{rim}: {e}")

        # LT-prefix drilldown for plain metric terms: "LT315/75R16"
        # Catches LT-only sizes (e.g. LT315/75R16 E) hidden by the cap.
        if is_plain_metric:
            try:
                lt_sub = search(f"LT{term}R{rim}")
                added = _merge(pool, lt_sub)
                if added:
                    print(f"  LT-drilldown 'LT{term}R{rim}': {added} new LT products found")
            except Exception as e:
                print(f"  WARN LT-drilldown LT{term}R{rim}: {e}")

            # Embedded speed-letter drilldown: "215/50ZR17"
            for letter in _EMBEDDED_SPEED_LETTERS:
                try:
                    l_sub = search(f"{term}{letter}R{rim}")
                    added = _merge(pool, l_sub)
                    if added:
                        print(f"  Speed-letter drilldown '{term}{letter}R{rim}': {added} new products found")
                except Exception as e:
                    print(f"  WARN speed-letter drilldown {term}{letter}R{rim}: {e}")

        # D-ply drilldown for ST terms: "ST205/75D15"
        # ST trailer tires use both R (radial) and D (diagonal/bias-ply) — the
        # standard R-drilldown misses D-ply variants when cap is hit.
        if is_st_term:
            try:
                d_sub = search(f"{term}D{rim}")
                added = _merge(pool, d_sub)
                if added:
                    print(f"  D-drilldown '{term}D{rim}': {added} new D-ply products found")
            except Exception as e:
                print(f"  WARN D-drilldown {term}D{rim}: {e}")

    return list(pool.values())


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


def get_cost_prices(guids: list) -> dict:
    """Fetch true cost price for a batch of GUIDs via the ShoppingCart pricing
    endpoint — NOT Product/Search or Product/ProductInfo, which only expose
    retailPrice/suggestedPrice (the supplier's suggested retail, not our cost).
    Returns {guid: cost_price}. Verified idempotent/side-effect-free: repeated
    calls return the same shoppingCartDetailGuid with orderQuantity 0.0 and do
    not add anything to the supplier's actual cart.
    """
    body = {
        "criteriaList": [{
            "dataField": "ProductGuidList",
            "criteriaList": [
                {"dataField": "ProductGuid", "guid": g} for g in guids
            ],
        }],
        "requestId": str(uuid.uuid4()).upper(),
    }
    data = api_post("ShoppingCart/GetShoppingCartCustomerItemPrice", body)

    guid_to_cost = {}
    for entry in data.get("shoppingCartDetailList") or []:
        guid  = entry.get("productGuid", "")
        price = entry.get("priceToDisplay", "")
        if guid:
            guid_to_cost[guid] = price
    return guid_to_cost


def flatten_product(p: dict) -> dict:
    return {
        "product_guid":                        p.get("productGuid", ""),
        "sku":                                 p.get("brandSku", "") or p.get("item", ""),
        "brand":                               p.get("brandName", ""),
        "model":                               p.get("model", ""),
        "description":                         p.get("description", ""),
        "size":                                p.get("size", ""),
        "product_type_code":                   p.get("productTypeCode", ""),
        "specification_category":              p.get("specificationCategoryCaption", ""),
        "specification_category_code":         p.get("specificationCategoryCode", ""),
        "available_quantity":                  p.get("availableQuantity", ""),
        "available_quantity_related_branches": p.get("availableQuantityAtRelatedBranches", ""),
        "retail_price":                        p.get("retailPrice", ""),
        "cost_price":                          "",
        "suggested_price":                     p.get("suggestedPrice", ""),
        "is_winter_tire":                      p.get("isWinterTire", ""),
        "is_all_weather":                      p.get("isAllWeather", ""),
        "is_on_sale":                          p.get("isOnSale", ""),
        "sale_begins":                         p.get("saleBegins", ""),
        "sale_ends":                           p.get("saleEnds", ""),
        "is_active":                           p.get("isActive", ""),
        "is_discontinued":                     p.get("isDiscontinued", ""),
        "status":                              p.get("status", ""),
        "manufacturer_code":                   p.get("manufacturerCode", ""),
        "rebate_description":                  p.get("rebateDescription", ""),
        "rebate_url":                          p.get("rebateUrl", ""),
        "image_url":                           "",
    }


CHECKPOINT_EVERY = 25   # dump all_products to the checkpoint CSV every N terms
ZERO_TERMS_CACHE = "Data/raw/zero_terms_tires_cache.json"


def _write_checkpoint(products: list, path: str) -> None:
    """Overwrite the checkpoint CSV so a crash/hang never loses the whole run.
    Lacks the spec_* columns added in the ProductInfo pass — recovery data only.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(products[0].keys()))
        writer.writeheader()
        writer.writerows(products)
    os.replace(tmp, path)


def _load_zero_terms_cache() -> set:
    if not os.path.exists(ZERO_TERMS_CACHE):
        return set()
    try:
        with open(ZERO_TERMS_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("zero_terms", []))
    except Exception as e:
        print(f"  WARNING: could not load zero-terms cache: {e}")
        return set()


def _save_zero_terms_cache(cached: set, new_zero: set, new_nonzero: set) -> None:
    # Keep all cached zeros, add newly-discovered zeros, drop any that now have results.
    # Coverage safety: brand_sweep.py always runs unconditionally as a gap-fill net.
    updated = (cached | new_zero) - new_nonzero
    os.makedirs("Data/raw", exist_ok=True)
    with open(ZERO_TERMS_CACHE, "w", encoding="utf-8") as f:
        json.dump({
            "generated": datetime.now().isoformat(),
            "total": len(updated),
            "zero_terms": sorted(updated),
        }, f, indent=2)
    print(f"Zero-terms cache updated: {len(updated)} terms → {ZERO_TERMS_CACHE}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="NC Tire Scraper")
    parser.add_argument("--full", action="store_true",
                        help="Search all terms ignoring zero-terms cache (rebuilds cache)")
    args = parser.parse_args()

    os.makedirs("Data/raw", exist_ok=True)
    output_file     = f"Data/raw/northcountry_tires_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    checkpoint_file = "Data/raw/northcountry_tires_checkpoint.csv"

    # ── Zero-terms cache ───────────────────────────────────────────────────────
    cached_zero = set()
    if not args.full:
        cached_zero = _load_zero_terms_cache()
        if cached_zero:
            print(f"Zero-terms cache: {len(cached_zero)} terms skipped (--full to rebuild cache)")

    effective_terms = [t for t in SEARCH_TERMS if t not in cached_zero]
    skipped = len(SEARCH_TERMS) - len(effective_terms)

    seen_guids   = set()
    all_products = []
    cap_hits     = []
    term_counts  = {}   # term → result count, used to update cache

    total = len(effective_terms)
    print(f"Starting scrape — {total} search terms ({MAX_WORKERS} workers)"
          + (f" [{skipped} skipped via cache]" if skipped else ""))
    print(f"  Metric terms  : {len(_WIDTHS) * len(_RATIOS)}")
    print(f"  Flotation     : {len(_FLOTATION)}")
    print(f"  LT-prefix     : {len(_LT_TERMS)}")

    lock      = threading.Lock()
    completed = [0]

    def process(term: str):
        results = search_with_drilldown(term)
        with lock:
            new = [r for r in results if r.get("productGuid") not in seen_guids]
            for r in new:
                seen_guids.add(r["productGuid"])
                all_products.append(flatten_product(r))
            completed[0] += 1
            term_counts[term] = len(results)
            if len(results) >= SEARCH_CAP:
                cap_hits.append(term)
            print(f"  [{completed[0]}/{total}] '{term}': {len(results)} results, "
                  f"{len(new)} new (total: {len(all_products)})")
            if all_products and completed[0] % CHECKPOINT_EVERY == 0:
                _write_checkpoint(all_products, checkpoint_file)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process, term) for term in effective_terms]
        for f in as_completed(futures):
            f.result()

    if all_products:
        _write_checkpoint(all_products, checkpoint_file)

    # ── Update zero-terms cache ────────────────────────────────────────────────
    new_zero    = {t for t, c in term_counts.items() if c == 0}
    new_nonzero = {t for t, c in term_counts.items() if c > 0}
    _save_zero_terms_cache(cached_zero, new_zero, new_nonzero)

    if cap_hits:
        print(f"\n{'='*55}")
        print(f"COVERAGE WARNING — {len(cap_hits)} search terms hit the {SEARCH_CAP}-result cap:")
        for t in sorted(cap_hits):
            print(f"  {t}")
        print("  These were drilled down by rim (including LT-prefix for metric terms).")
        print(f"{'='*55}")

    if STILL_CAPPED:
        print(f"\n{'='*55}")
        print(f"UNRESOLVED CAPS — {len(set(STILL_CAPPED))} sub-queries still hit the "
              f"{SEARCH_CAP}-result cap even after drilldown:")
        for t in sorted(set(STILL_CAPPED)):
            print(f"  {t}")
        print("  Rim/width splitting alone can't separate these further — they need")
        print("  a third search axis. Run:  python brand_sweep.py --merge")
        print(f"{'='*55}")

    if not all_products:
        print("No products scraped.")
        return

    # ── Second pass: fetch images + specs via ProductInfo ────────────────────
    print(f"\nFetching images + specs via ProductInfo ({len(all_products)} products, "
          f"batch size {PRODUCT_INFO_BATCH_SIZE}, {MAX_WORKERS} workers)...")
    guid_map       = {p["product_guid"]: i for i, p in enumerate(all_products) if p["product_guid"]}
    guids          = list(guid_map.keys())
    batches        = [guids[i:i + PRODUCT_INFO_BATCH_SIZE]
                      for i in range(0, len(guids), PRODUCT_INFO_BATCH_SIZE)]
    images_found   = 0
    specs_found    = 0
    all_spec_codes = set()
    all_guid_specs = {}

    def _fetch_batch(batch):
        try:
            return get_product_info(batch)
        except Exception as e:
            print(f"  WARNING: ProductInfo batch failed: {e}")
            return {}, {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for guid_to_image, guid_to_specs in pool.map(_fetch_batch, batches):
            for guid, image_url in guid_to_image.items():
                if guid in guid_map and image_url:
                    all_products[guid_map[guid]]["image_url"] = image_url
                    images_found += 1
            for guid, specs in guid_to_specs.items():
                all_spec_codes.update(specs.keys())
                all_guid_specs[guid] = specs
                specs_found += 1

    print(f"Images found : {images_found} / {len(all_products)}")
    print(f"Specs found  : {specs_found} / {len(all_products)}")

    # ── Third pass: fetch true cost price via ShoppingCart pricing endpoint ──
    print(f"\nFetching cost prices via ShoppingCart ({len(all_products)} products, "
          f"batch size {PRODUCT_INFO_BATCH_SIZE}, {MAX_WORKERS} workers)...")
    cost_found = 0

    def _fetch_cost_batch(batch):
        try:
            return get_cost_prices(batch)
        except Exception as e:
            print(f"  WARNING: Cost price batch failed: {e}")
            return {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for guid_to_cost in pool.map(_fetch_cost_batch, batches):
            for guid, cost in guid_to_cost.items():
                if guid in guid_map and cost not in (None, ""):
                    all_products[guid_map[guid]]["cost_price"] = cost
                    cost_found += 1

    print(f"Cost prices found: {cost_found} / {len(all_products)}")

    sorted_codes = sorted(all_spec_codes)
    if sorted_codes:
        print(f"Spec codes   : {sorted_codes}")
        for product in all_products:
            guid  = product["product_guid"]
            specs = all_guid_specs.get(guid, {})
            for code in sorted_codes:
                product[f"spec_{code.lower()}"] = specs.get(code, "")

    fieldnames = list(all_products[0].keys())
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_products)

    print(f"\nDone. {len(all_products)} products → {output_file}")
    print(f"Next: run  python verify_coverage.py  to audit scrape completeness.")


if __name__ == "__main__":
    main()
