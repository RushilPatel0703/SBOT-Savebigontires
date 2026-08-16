#!/usr/bin/env python3
"""Formula Distributors Wheel Scraper
Scrapes all wheel/rim data from weborder.formuladistributors.com and saves to CSV.
"""

import requests
import json
import csv
import time
import sys
import uuid
import hashlib
import base64
from datetime import datetime, timezone

BASE_URL = "https://weborder.formuladistributors.com"
API_BASE = "/WebOrder/CostarApi"
BATCH_SIZE = 50
REQUEST_DELAY = 0.5
OUTPUT_FILE = f"Data/raw/formula_wheels_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

# ── Cookies ──────────────────────────────────────────────────────────────────
# Paste the full cookie string from the browser (copy as cURL → -b "...") here.
# At minimum you need ASP.NET_SessionId + the CostarWeb* cookies.
COOKIE_STRING = "CostarWebUserData=jRa4aGghLZiK4h9tf9KS2w%3d%3d=&CultureCode=en-US&IsRememberMe=True&UserRequestedUx=&wO3%2fdZCItViuMG66VHQitQ%3d%3d=j2tgG82DEmJzPxfO1ysuHg%3d%3d&UserID=2bcc6f00-2606-4444-9cf5-7a35065ed1be; CostarWebAppData=M1dloT0oGu5oPHZUHVYKjw%3d%3d=&CwsVersion=5.7.138.0&DbVersion=0005.0007.0138.0000&5lG0d7HPQ%2fkTy%2fCvDKdPsQ%3d%3d=sbltdMHIefA6i17%2faiycUJKIVC782Qi2tygHwXqKQNvzgsQs4IN74y9r9IcGoxN3&UnifiedBuildNumber=5.7.138.0&Ux=R64Q46VF&CostarLanguage=&SessionID=0e18c19a-6a4b-48e7-8191-2060deb39551; ASP.NET_SessionId=qxysyqvjeef4rdiuudn23eum"

# ── Signing key (extracted from costar.min.js) ───────────────────────────────
_SIGNING_KEY = "5318f4ce4b9bab05c09df20c3d5d8d4c26570542501b1d58231159cd42e65505"

SPEC_CATEGORY_GUIDS = [
    "18ffb53d-f880-436f-a569-f0eacca8782e",
]

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
import string as _string
SEARCH_TERMS = (
    list(_string.ascii_uppercase)
    + [f"{d} {bp}" for d in _DIAMETERS for bp in _BOLT_PATTERNS]
)

SPEC_FIELDS = [
    ("DIAMETER",        "diameter"),
    ("WIDTH",           "width"),
    ("BOLTPATTERN",     "bolt_pattern"),
    ("OFFSET",          "offset"),
    ("BACKSPACING",     "backspacing"),
    ("HUBSIZE",         "hub_bore"),
    ("FINISH",          "finish"),
    ("MATERIAL",        "material"),
    ("LOADRATING",      "load_rating"),
    ("WEIGHTLBS",       "weight_lbs"),
    ("WEIGHTKG",        "weight_kg"),
    ("CENTERCAP",       "center_cap_included"),
]

HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/WebOrder/Product/Search",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "priority": "u=1, i",
    "encryptionalgorithm": "aes-cbc",
    "signaturealgorithm": "sha256",
}


def _utc_timestamp():
    """ISO timestamp without trailing Z, matching the browser's format."""
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', '')


def _sign(body_str):
    """SHA256(base64(utf8(JSON.stringify({utcTimestamp, data, signingKey}))))"""
    ts = _utc_timestamp()
    payload = json.dumps(
        {"utcTimestamp": ts, "data": body_str, "signingKey": _SIGNING_KEY},
        separators=(',', ':')
    )
    b64 = base64.b64encode(payload.encode('utf-8')).decode('ascii')
    sig = hashlib.sha256(b64.encode('utf-8')).hexdigest()
    return ts, sig


def build_search_payload(search_text=""):
    return {
        "criteriaList": [
            {
                "dataField": "FullText",
                "string": search_text,
            },
            {
                "dataField": "SpecificationCategoryGuidList",
                "criteriaList": [
                    {"dataField": "SpecificationCategoryGuid", "guid": g}
                    for g in SPEC_CATEGORY_GUIDS
                ],
            },
            {
                "dataField": "IsStaggeredSearch",
                "boolean": False,
            },
        ],
        "requestId": str(uuid.uuid4()).upper(),
    }


def build_product_info_payload(product_guids):
    return {
        "criteriaList": [
            {
                "dataField": "ProductGuidList",
                "criteriaList": [
                    {"dataField": "ProductGuid", "guid": g}
                    for g in product_guids
                ],
            }
        ],
        "requestId": str(uuid.uuid4()).upper(),
    }


def post(session, path, payload):
    url = BASE_URL + path
    body_str = json.dumps(payload, separators=(',', ':'))
    ts, sig = _sign(body_str)
    headers = {"utctimestamp": ts, "signature": sig}
    try:
        resp = session.post(url, data=body_str, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        print(f"  HTTP error {e.response.status_code} for {path}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"  Request error: {e}")
        return None
    except json.JSONDecodeError:
        print(f"  Invalid JSON response from {path}")
        return None


def fetch_all_products(session):
    """
    Returns (products_list, qty_by_branch_dict).
    products_list: list of dicts from searchResultList
    qty_by_branch_dict: {productGuid: {branchName: {qty, ordered_qty, status}}}
    """
    print("Attempting empty-string search to fetch all products...")
    data = post(session, f"{API_BASE}/Product/Search", build_search_payload(""))

    all_products = []
    qty_by_branch = {}

    if data and data.get("searchResultList"):
        all_products = data["searchResultList"]
        print(f"  Empty search returned {len(all_products)} products.")
        _collect_branch_qty(data, qty_by_branch)
    else:
        print("  Empty search returned no results. Falling back to A-Z + size search...")
        seen_guids = set()
        for term in SEARCH_TERMS:
            print(f"  Searching: '{term}'")
            data = post(session, f"{API_BASE}/Product/Search", build_search_payload(term))
            if data and data.get("searchResultList"):
                new = 0
                for p in data["searchResultList"]:
                    guid = p.get("productGuid")
                    if guid and guid not in seen_guids:
                        seen_guids.add(guid)
                        all_products.append(p)
                        new += 1
                print(f"    +{new} new products (total: {len(all_products)})")
                if len(data["searchResultList"]) >= 75:
                    print(f"  [CAPPED] term '{term}' hit 75-result limit")
                _collect_branch_qty(data, qty_by_branch)
            time.sleep(REQUEST_DELAY)

    print(f"\nTotal unique products found: {len(all_products)}")
    return all_products, qty_by_branch


def _collect_branch_qty(data, qty_by_branch):
    for entry in data.get("productQuantityForAccessibleBranchList", []):
        guid = entry.get("productGuid")
        branch = entry.get("branchName", f"Branch_{entry.get('branchCode','?')}")
        if not guid:
            continue
        if guid not in qty_by_branch:
            qty_by_branch[guid] = {}
        qty_by_branch[guid][branch] = {
            "qty": entry.get("availableQuantity", 0),
            "ordered_qty": entry.get("orderedQuantity", 0),
            "status": entry.get("quantityStatus", ""),
        }


def fetch_product_specs(session, product_guids):
    """
    Returns (specs_dict, images_dict).
    specs_dict: {productGuid: {code: value}}
    images_dict: {productGuid: url_string}
    """
    specs = {}
    images = {}
    total = len(product_guids)
    batches = [product_guids[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    print(f"\nFetching specs for {total} products in {len(batches)} batches...")

    for idx, batch in enumerate(batches, 1):
        print(f"  Batch {idx}/{len(batches)} ({len(batch)} products)...")
        data = post(session, f"{API_BASE}/Product/ProductInfo", build_product_info_payload(batch))
        if data:
            for entry in data.get("productSpecificationList", []):
                guid = entry.get("productGuid")
                code = entry.get("code", "").upper()
                value = entry.get("value", "")
                if guid:
                    if guid not in specs:
                        specs[guid] = {}
                    specs[guid][code] = value

            for entry in data.get("productImageList", []):
                guid = entry.get("productGuid")
                url = entry.get("url") or entry.get("imageUrl") or ""
                if guid and url:
                    images[guid] = url

        time.sleep(REQUEST_DELAY)

    print(f"  Specs collected for {len(specs)} products.")
    return specs, images


def fetch_cost_prices(session, product_guids):
    """
    Returns {productGuid: cost_price}.
    Cost price comes from ShoppingCart/GetShoppingCartCustomerItemPrice —
    NOT Product/Search or Product/ProductInfo, which only expose retailPrice
    (the supplier's suggested retail, not our actual cost). Uses the same
    signed post() helper and ProductGuidList payload shape as ProductInfo.
    Verified idempotent/side-effect-free: repeated calls return the same
    shoppingCartDetailGuid with orderQuantity 0.0 and don't touch the cart.
    """
    costs = {}
    total = len(product_guids)
    batches = [product_guids[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    print(f"\nFetching cost prices for {total} products in {len(batches)} batches...")

    for idx, batch in enumerate(batches, 1):
        print(f"  Batch {idx}/{len(batches)} ({len(batch)} products)...")
        data = post(session, f"{API_BASE}/ShoppingCart/GetShoppingCartCustomerItemPrice",
                    build_product_info_payload(batch))
        if data:
            for entry in data.get("shoppingCartDetailList", []):
                guid  = entry.get("productGuid")
                price = entry.get("priceToDisplay", "")
                if guid:
                    costs[guid] = price

        time.sleep(REQUEST_DELAY)

    print(f"  Cost prices collected for {len(costs)} products.")
    return costs


def save_to_csv(products, qty_by_branch, specs, images, costs, output_file):
    # Discover all branch names dynamically
    branch_names = sorted({
        branch
        for guid_branches in qty_by_branch.values()
        for branch in guid_branches
    })
    print(f"\nBranches found: {branch_names}")

    # Build header row
    base_fields = [
        "item", "description", "size", "model", "brand_name",
        "retail_price", "cost_price", "available_quantity",
        "product_guid", "model_code", "category", "sub_category",
        "unit_of_measure", "weight", "image_url",
    ]

    branch_qty_fields = []
    for branch in branch_names:
        safe = branch.replace(" ", "_").replace(".", "").replace("/", "_").upper()
        branch_qty_fields += [
            f"qty_{safe}",
            f"ordered_qty_{safe}",
            f"status_{safe}",
        ]

    spec_csv_fields = [csv_name for _, csv_name in SPEC_FIELDS]

    all_fields = base_fields + branch_qty_fields + spec_csv_fields

    import os
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    written = 0
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        writer.writeheader()

        for p in products:
            guid = p.get("productGuid", "")
            row = {
                "item":               p.get("item", ""),
                "description":        p.get("description", ""),
                "size":               p.get("size", ""),
                "model":              p.get("model", ""),
                "brand_name":         p.get("brandName", ""),
                "retail_price":       p.get("retailPrice", ""),
                "cost_price":         costs.get(guid, ""),
                "available_quantity": p.get("availableQuantity", ""),
                "product_guid":       guid,
                "model_code":         p.get("modelCode", ""),
                "category":           p.get("category", ""),
                "sub_category":       p.get("subCategory", ""),
                "unit_of_measure":    p.get("unitOfMeasure", ""),
                "weight":             p.get("weight", ""),
                "image_url":          images.get(guid, ""),
            }

            # Per-branch quantities
            for branch in branch_names:
                safe = branch.replace(" ", "_").replace(".", "").replace("/", "_").upper()
                branch_data = qty_by_branch.get(guid, {}).get(branch, {})
                row[f"qty_{safe}"]         = branch_data.get("qty", 0)
                row[f"ordered_qty_{safe}"] = branch_data.get("ordered_qty", 0)
                row[f"status_{safe}"]      = branch_data.get("status", "")

            # Spec fields
            product_specs = specs.get(guid, {})
            for code, csv_name in SPEC_FIELDS:
                row[csv_name] = product_specs.get(code, "")

            writer.writerow(row)
            written += 1

    print(f"Saved {written} products to: {output_file}")


def main():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Cookie"] = COOKIE_STRING

    print("=" * 60)
    print("Formula Distributors Wheel Scraper")
    print("=" * 60)

    # Step 1: Get all products and branch quantities
    products, qty_by_branch = fetch_all_products(session)

    if not products:
        print("No products found. Check your cookies/authentication and try again.")
        sys.exit(1)

    # Step 2: Fetch detailed specs in batches
    product_guids = [p["productGuid"] for p in products if p.get("productGuid")]
    specs, images = fetch_product_specs(session, product_guids)

    # Step 3: Fetch true cost prices in batches
    costs = fetch_cost_prices(session, product_guids)

    # Step 4: Write CSV
    save_to_csv(products, qty_by_branch, specs, images, costs, OUTPUT_FILE)
    print("\nDone!")


if __name__ == "__main__":
    main()
