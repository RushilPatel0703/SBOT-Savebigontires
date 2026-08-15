#!/usr/bin/env python3
"""North Country Tire — Wheel Scraper
Fetches wheel products from the Costar API using signed HMAC requests.

IMPORTANT: Update COOKIE_STRING below when your session expires.
"""

import hashlib
import base64
import json
import random
import requests
import csv
import os
import time
import string
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BASE_URL     = "https://online.northcountrytire.ca/WebOrder/CostarApi"
_SIGNING_KEY = "5318f4ce4b9bab05c09df20c3d5d8d4c26570542501b1d58231159cd42e65505"

# ── Update this when session expires ─────────────────────────────────────────
COOKIE_STRING = "CostarWebAppData=M1dloT0oGu5oPHZUHVYKjw%3d%3d=&CwsVersion=5.7.151.0&DbVersion=0005.0007.0151.0000&5lG0d7HPQ%2fkTy%2fCvDKdPsQ%3d%3d=jL6ZMc7YXSYbvy0fZRX0SOZYf%2bgvl4iTn62P8LwabBgZdoLVCVTtGb23RTHbf4VN&UnifiedBuildNumber=5.7.151.0&Ux=M48B07OD&CostarLanguage=&SessionID=2e91d014-47a1-48f2-8b77-7684e3d748c3; CostarWebUserData=jRa4aGghLZiK4h9tf9KS2w%3d%3d=&CultureCode=en-US&IsRememberMe=True&UserRequestedUx=&wO3%2fdZCItViuMG66VHQitQ%3d%3d=&UserID=62753762-445e-44ac-afdc-0606628d7cbf; ASP.NET_SessionId=0u4djskt1a4bzh20xit3xhau"
# ─────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://online.northcountrytire.ca",
    "Referer": "https://online.northcountrytire.ca/WebOrder/Product/Search",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "encryptionAlgorithm": "aes-cbc",
    "signatureAlgorithm": "sha256",
}

# Wheel spec category GUID (same across NC and Formula)
SPEC_CATEGORY_GUID = "18ffb53d-f880-436f-a569-f0eacca8782e"

# A-Z brand search + diameter×bolt-pattern grid (avoids per-query result cap of 75)
_DIAMETERS = [str(d) for d in [10, 12, 13, 14, 15, 16, 17, 17.5, 18, 19, 19.5, 20, 21, 22, 22.5, 24, 26, 28, 30]]
_BOLT_PATTERNS = [
    '4-98', '4-100', '4-108', '4-110', '4-114.3', '4-115', '4-156', '4on4',
    '5-98', '5-100', '5-105', '5-108', '5-110', '5-112', '5-114.3', '5-115',
    '5-120', '5-127', '5-130', '5-139.7', '5-150', '5-160', '5on4.5', '5-4.50',
    '6-114.3', '6-120', '6-127', '6-130', '6-132', '6-135', '6-139.7', '6-180', '6-205',
    '7-150',
    '8-165.1', '8-170', '8-180', '8-200', '8-210', '8-225', '8-275',
    '10-184.1', '10-225', '10-285',
]
SEARCH_TERMS = (
    list(string.ascii_uppercase)
    + [f"{d} {bp}" for d in _DIAMETERS for bp in _BOLT_PATTERNS]
)

PRODUCT_INFO_BATCH_SIZE = 50
MAX_WORKERS = 1   # sequential — server can't handle concurrent load without timeouts


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
                    {"dataField": "SpecificationCategoryGuid", "guid": SPEC_CATEGORY_GUID}
                ],
            },
            {"dataField": "IsStaggeredSearch", "boolean": False},
        ],
        "requestId": str(uuid.uuid4()).upper(),
    }
    data = api_post("Product/Search", body)
    return data.get("searchResultList") or []


_info_logged = False
_first_result_logged = False


def get_product_info(guids: list) -> tuple:
    """Fetch ProductInfo for a batch of GUIDs.
    Returns ({guid: image_url}, {guid: {spec_code: value}}).
    """
    global _info_logged
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

    if not _info_logged:
        print(f"\n[ProductInfo] Top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
        spec_sample = (data.get("productSpecificationList") or [{}])[:1]
        if spec_sample[0]:
            print(f"[ProductInfo] Spec item keys: {list(spec_sample[0].keys())}")
        _info_logged = True

    # ── Images ────────────────────────────────────────────────────────────────
    guid_to_image = {}
    for img in data.get("productImageList") or []:
        guid = img.get("productGuid", "")
        url  = img.get("url", "")
        if not guid or not url:
            continue
        if guid not in guid_to_image or img.get("imageType", "") == "LARGE":
            guid_to_image[guid] = url

    # ── Specs ─────────────────────────────────────────────────────────────────
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


def flatten_product(p: dict) -> dict:
    global _first_result_logged
    if not _first_result_logged:
        print(f"\n[Search] First result keys: {list(p.keys())}")
        _first_result_logged = True

    row = {
        "product_guid":                p.get("productGuid", ""),
        "sku":                         p.get("brandSku", "") or p.get("item", ""),
        "brand":                       p.get("brandName", ""),
        "model":                       p.get("model", ""),
        "description":                 p.get("description", ""),
        "size":                        p.get("size", ""),
        "product_type_code":           p.get("productTypeCode", ""),
        "specification_category":      p.get("specificationCategoryCaption", ""),
        "specification_category_code": p.get("specificationCategoryCode", ""),
        "available_quantity":                  p.get("availableQuantity", ""),
        "available_quantity_related_branches": p.get("availableQuantityAtRelatedBranches", ""),
        "retail_price":                p.get("retailPrice", ""),
        "suggested_price":             p.get("suggestedPrice", ""),
        "is_active":                   p.get("isActive", ""),
        "is_discontinued":             p.get("isDiscontinued", ""),
        "status":                      p.get("status", ""),
        "manufacturer_code":           p.get("manufacturerCode", ""),
        "image_url":                   "",
    }
    return row


ZERO_TERMS_CACHE = "Data/raw/zero_terms_wheels_cache.json"


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
    parser = argparse.ArgumentParser(description="NC Wheel Scraper")
    parser.add_argument("--full", action="store_true",
                        help="Search all terms ignoring zero-terms cache (rebuilds cache)")
    args = parser.parse_args()

    os.makedirs("Data/raw", exist_ok=True)
    output_file = f"Data/raw/northcountry_wheels_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    # ── Zero-terms cache ──────────────────────────────────────────────────────
    cached_zero = set()
    if not args.full:
        cached_zero = _load_zero_terms_cache()
        if cached_zero:
            print(f"Zero-terms cache: {len(cached_zero)} terms skipped (--full to rebuild cache)")

    effective_terms = [t for t in SEARCH_TERMS if t not in cached_zero]
    skipped = len(SEARCH_TERMS) - len(effective_terms)

    seen_guids   = set()
    all_products = []
    term_counts  = {}

    lock      = threading.Lock()
    completed = [0]
    total     = len(effective_terms)

    print(f"Starting wheel scrape — {total} search terms ({MAX_WORKERS} workers)"
          + (f" [{skipped} skipped via cache]" if skipped else ""))

    def process(term: str):
        try:
            results = search(term)
        except Exception as e:
            print(f"  ERROR '{term}': {e}")
            results = []
        with lock:
            new = [r for r in results if r.get("productGuid") not in seen_guids]
            for r in new:
                seen_guids.add(r["productGuid"])
                all_products.append(flatten_product(r))
            completed[0] += 1
            term_counts[term] = len(results)
            print(f"  [{completed[0]}/{total}] '{term}': {len(results)} results, "
                  f"{len(new)} new (total: {len(all_products)})")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process, term) for term in effective_terms]
        for f in as_completed(futures):
            f.result()

    # ── Update zero-terms cache ───────────────────────────────────────────────
    new_zero    = {t for t, c in term_counts.items() if c == 0}
    new_nonzero = {t for t, c in term_counts.items() if c > 0}
    _save_zero_terms_cache(cached_zero, new_zero, new_nonzero)

    if not all_products:
        print("No products scraped.")
        return

    # ── Second pass: fetch images + specs via ProductInfo ─────────────────────
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

    print(f"\nDone. {len(all_products)} wheels → {output_file}")


if __name__ == "__main__":
    main()
