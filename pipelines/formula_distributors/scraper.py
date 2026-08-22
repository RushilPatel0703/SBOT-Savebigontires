#!/usr/bin/env python3
"""Formula Distributors Tire Scraper
Scrapes all tire data from weborder.formuladistributors.com and saves to CSV.
"""

import os
import requests
import json
import csv
import time
import sys
import uuid
import hashlib
import base64
from datetime import datetime, timezone

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared')))
from costar_session import CostarSessionGuard, SessionDeathError, all_zero_batch
from costar_images import measure_and_build_image_map

BASE_URL = "https://weborder.formuladistributors.com"
API_BASE = "/WebOrder/CostarApi"
BATCH_SIZE = 50
REQUEST_DELAY = 0.5
OUTPUT_FILE = f"Data/raw/formula_tires_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

# ── Cookies ──────────────────────────────────────────────────────────────────
# Paste the full cookie string from the browser (copy as cURL → -b "...") here.
# At minimum you need ASP.NET_SessionId + the CostarWeb* cookies.
COOKIE_STRING = "CostarWebUserData=jRa4aGghLZiK4h9tf9KS2w%3d%3d=&CultureCode=en-US&IsRememberMe=True&UserRequestedUx=&wO3%2fdZCItViuMG66VHQitQ%3d%3d=j2tgG82DEmJzPxfO1ysuHg%3d%3d&UserID=2bcc6f00-2606-4444-9cf5-7a35065ed1be; CostarWebAppData=M1dloT0oGu5oPHZUHVYKjw%3d%3d=&CwsVersion=5.7.138.0&DbVersion=0005.0007.0138.0000&5lG0d7HPQ%2fkTy%2fCvDKdPsQ%3d%3d=sbltdMHIefA6i17%2faiycUJKIVC782Qi2tygHwXqKQNvzgsQs4IN74y9r9IcGoxN3&UnifiedBuildNumber=5.7.138.0&Ux=R64Q46VF&CostarLanguage=&SessionID=0e18c19a-6a4b-48e7-8191-2060deb39551; ASP.NET_SessionId=qxysyqvjeef4rdiuudn23eum"

# Live, refreshable session state — see costar_session.py / northcountry/scraper_tires.py.
_SESSION = CostarSessionGuard("formula", COOKIE_STRING, __file__)

# ── Signing key (extracted from costar.min.js) ───────────────────────────────
_SIGNING_KEY = "5318f4ce4b9bab05c09df20c3d5d8d4c26570542501b1d58231159cd42e65505"

SPEC_CATEGORY_GUIDS = [
    "fba9310f-8f95-42ab-986b-44809d666571",
    "077f1e3d-ff2a-4fae-8521-7ebcb4ceeef9",
    "1589eae7-c16f-4849-bb57-7fc9a33ef406",
    "bfaa8bcf-ec8f-4e5b-aed8-e68272657098",
]

# Search terms cover both brand names (A-Z) and tire sizes (section widths 105-395).
# The site's FullText search routes text → brand fields and numbers → size fields,
# so both sets are needed to get the full product catalogue.
SEARCH_TERMS = (
    list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + [str(w) for w in range(105, 400, 10)]   # 105, 115, 125 … 395
)

SPEC_FIELDS = [
    ("SPEEDRATING",         "speed_rating"),
    ("LOADINDEX",           "load_index"),
    ("LOADINDEXDUAL",       "load_index_dual"),
    ("ASPECTRATIO",         "aspect_ratio"),
    ("RIMSIZE",             "rim_size"),
    ("SECTIONWIDTH",        "section_width"),
    ("SECTIONWIDTHINCHES",  "section_width_inches"),
    ("OVERALLDIAMETER",     "overall_diameter"),
    ("LOADRANGE",           "load_range"),
    ("LOADINDICATOR",       "load_indicator"),
    ("TREADDEPTH32NDS",     "tread_depth_32nds"),
    ("TREADDEPTHMM",        "tread_depth_mm"),
    ("TREADWIDTH",          "tread_width"),
    ("SINGLEMAXPSI",        "single_max_psi"),
    ("DUALMAXPSI",          "dual_max_psi"),
    ("SINGLEMAXLOAD",       "single_max_load"),
    ("DUALMAXLOAD",         "dual_max_load"),
    ("RIMWIDTHMIN",         "rim_width_min"),
    ("RIMWIDTHMAX",         "rim_width_max"),
    ("RIMWIDTHMEASURED",    "rim_width_measured"),
    ("SIDEWALL",            "sidewall"),
    ("REVOLUTIONSPERKM",    "revolutions_per_km"),
    ("REVOLUTIONSPERMILE",  "revolutions_per_mile"),
    ("WEIGHTLBS",           "weight_lbs"),
    ("WEIGHTKG",            "weight_kg"),
    ("RUNFLAT",             "run_flat"),
    ("UTQGTREADWEAR",       "utqg_treadwear"),
    ("UTQGTRACTION",        "utqg_traction"),
    ("UTQGTEMPERATURE",     "utqg_temperature"),
    ("WARRANTYM",           "warranty_miles"),
    ("WARRANTYKM",          "warranty_km"),
    ("WARRANTYYEARS",       "warranty_years"),
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
    # Proactive keepalive — see pipelines/shared/costar_session.py.
    if _SESSION.keepalive_due():
        _SESSION.reauth(_SESSION.cookie, "proactive keepalive interval elapsed")
    session.headers["Cookie"] = _SESSION.cookie

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


def _do_search(session, term):
    """Bare Product/Search call — no session-health bookkeeping. Used by
    the fallback per-term loop and, directly, for canary re-probes."""
    return post(session, f"{API_BASE}/Product/Search", build_search_payload(term))


def _check_session_health(session, term, count):
    """See northcountry/scraper_tires.py / costar_session.py docstring."""
    if not _SESSION.note_search_result(term, count):
        return
    canary = _SESSION.canary_term()
    if canary is None:
        print(f"  [session] zero-streak of {_SESSION.zero_streak_threshold} at '{term}' but "
              f"no healthy baseline yet this run — cannot canary-verify, continuing")
        _SESSION.reset_streak()
        return
    try:
        canary_data = _do_search(session, canary)
        canary_count = len(canary_data.get("searchResultList") or []) if canary_data else 0
    except SessionDeathError:
        raise
    except Exception as e:
        print(f"  [session] canary probe '{canary}' raised: {e} — treating as dead")
        canary_count = 0
    if canary_count > 0:
        print(f"  [session] zero-streak of {_SESSION.zero_streak_threshold} at '{term}' but "
              f"canary '{canary}' still returns {canary_count} results — legitimate empty "
              f"region, resetting streak")
        _SESSION.reset_streak()
        return
    print(f"  [session] zero-streak of {_SESSION.zero_streak_threshold} at '{term}' AND canary "
          f"'{canary}' also returns 0 — suspected session death")
    _SESSION.reauth(_SESSION.cookie,
                     f"search-phase zero-streak confirmed by dead canary '{canary}'")


def fetch_all_products(session):
    """
    Returns (products_list, qty_by_branch_dict).
    products_list: list of dicts from searchResultList
    qty_by_branch_dict: {productGuid: {branchName: {qty, ordered_qty, status}}}
    """
    print("Attempting empty-string search to fetch all products...")
    data = _do_search(session, "")

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
            data = _do_search(session, term)
            count = len(data.get("searchResultList") or []) if data else 0
            _check_session_health(session, term, count)
            if data and data.get("searchResultList"):
                new = 0
                for p in data["searchResultList"]:
                    guid = p.get("productGuid")
                    if guid and guid not in seen_guids:
                        seen_guids.add(guid)
                        all_products.append(p)
                        new += 1
                print(f"    +{new} new products (total: {len(all_products)})")
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
        batch_specs, batch_images = 0, 0
        if data:
            for entry in data.get("productSpecificationList", []):
                guid = entry.get("productGuid")
                code = entry.get("code", "").upper()
                value = entry.get("value", "")
                if guid:
                    if guid not in specs:
                        specs[guid] = {}
                    specs[guid][code] = value
                    batch_specs += 1

            # Was `images[guid] = url` — last entry wins, with no regard for
            # imageType. Costar returns several images for 26% of products and
            # SMALL runs to a 54px median, so that rule picked a worse image
            # than the best available LARGE on 63% of multi-image products
            # (33 of 52 measured) and a better one on none.
            # Picking is by MEASURED pixels, not by imageType — the label is
            # wrong on 16% of products. See pipelines/shared/costar_images.py.
            batch_image_map = measure_and_build_image_map(data.get("productImageList"))
            images.update(batch_image_map)
            batch_images += len(batch_image_map)

        # A whole batch with NEITHER a spec NOR an image is a much stronger
        # dead-session signal than any single empty search term — see
        # pipelines/shared/costar_session.py module docstring.
        if batch and all_zero_batch(batch_specs, batch_images):
            _SESSION.reauth(_SESSION.cookie,
                             f"ProductInfo batch of {len(batch)} guids returned 0 specs AND 0 images")
            data = post(session, f"{API_BASE}/Product/ProductInfo", build_product_info_payload(batch))
            if data:
                for entry in data.get("productSpecificationList", []):
                    guid = entry.get("productGuid")
                    code = entry.get("code", "").upper()
                    value = entry.get("value", "")
                    if guid:
                        specs.setdefault(guid, {})[code] = value
                images.update(measure_and_build_image_map(data.get("productImageList")))

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
        batch_costs = 0
        if data:
            for entry in data.get("shoppingCartDetailList", []):
                guid  = entry.get("productGuid")
                price = entry.get("priceToDisplay", "")
                if guid:
                    costs[guid] = price
                    batch_costs += 1

        if batch and all_zero_batch(batch_costs):
            _SESSION.reauth(_SESSION.cookie,
                             f"Cost-price batch of {len(batch)} guids returned 0 prices")
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
        "retail_price", "cost_price", "available_quantity", "is_winter_tire",
        "is_all_weather", "is_all_season", "is_mud_snow",
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
                "item":             p.get("item", ""),
                "description":      p.get("description", ""),
                "size":             p.get("size", ""),
                "model":            p.get("model", ""),
                "brand_name":       p.get("brandName", ""),
                "retail_price":     p.get("retailPrice", ""),
                "cost_price":       costs.get(guid, ""),
                "available_quantity": p.get("availableQuantity", ""),
                "is_winter_tire":   p.get("isWinterTire", ""),
                "is_all_weather":   p.get("isAllWeather", ""),
                "is_all_season":    p.get("isAllSeason", ""),
                "is_mud_snow":      p.get("isMudSnow", ""),
                "product_guid":     guid,
                "model_code":       p.get("modelCode", ""),
                "category":         p.get("category", ""),
                "sub_category":     p.get("subCategory", ""),
                "unit_of_measure":  p.get("unitOfMeasure", ""),
                "weight":           p.get("weight", ""),
                "image_url":        images.get(guid, ""),
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
    print("Formula Distributors Tire Scraper")
    print("=" * 60)

    # Step 1: Get all products and branch quantities
    products, qty_by_branch = fetch_all_products(session)

    if not products:
        print("No products found. Check your cookies/authentication and try again.")
        sys.exit(1)

    # Step 2: Fetch detailed specs in batches
    product_guids = [p["productGuid"] for p in products if p.get("productGuid")]
    specs, images = fetch_product_specs(session, product_guids)

    if all_zero_batch(len(specs), len(images)):
        raise SessionDeathError(
            f"Specs/images phase yielded 0 specs AND 0 images across all {len(product_guids)} "
            f"products, even after per-batch re-auth retries. Refusing to write an "
            f"empty-but-'OK' result."
        )

    # Step 3: Fetch true cost prices in batches
    costs = fetch_cost_prices(session, product_guids)

    if all_zero_batch(len(costs)):
        raise SessionDeathError(
            f"Cost-price phase yielded 0 prices across all {len(product_guids)} products, "
            f"even after per-batch re-auth retries."
        )

    # Step 4: Write CSV
    save_to_csv(products, qty_by_branch, specs, images, costs, OUTPUT_FILE)
    print("\nDone!")


if __name__ == "__main__":
    try:
        main()
    except SessionDeathError as e:
        print(f"\nFATAL: {e}")
        sys.exit(1)
