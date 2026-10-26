#!/usr/bin/env python3
"""NC Brand Sweep — Second-Pass Gap Checker

Searches the NC Costar API by BRAND NAME instead of by size, then compares
the resulting GUIDs to the most recent size-based raw CSV. Any product found
by brand search but absent from the size-based scrape is a coverage gap.

Why this works as a completeness check:
  - Size search can miss products when the 75-result cap is hit by other sizes
  - Brand search hits a different search axis — it can't be blocked by the same cap
  - Union of both strategies catches what either alone misses

Run AFTER scraper_tires.py has produced a raw CSV.

Usage:
    python brand_sweep.py                        # compare vs latest raw CSV
    python brand_sweep.py --merge                # merge gaps into latest raw CSV
    python brand_sweep.py --file Data/raw/northcountry_tires_TIMESTAMP.csv
"""

import argparse
import csv
import glob
import hashlib
import base64
import json
import os
import re
import sys
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BASE_URL     = "https://online.northcountrytire.ca/WebOrder/CostarApi"
_SIGNING_KEY = "5318f4ce4b9bab05c09df20c3d5d8d4c26570542501b1d58231159cd42e65505"

try:
    from scraper_tires import (
        COOKIE_STRING, HEADERS, SPEC_CATEGORY_GUIDS, SEARCH_CAP,
        _RIM_SIZES, _sign, api_post, flatten_product, get_product_info,
        get_cost_prices, PRODUCT_INFO_BATCH_SIZE,
    )
except ImportError as e:
    print(f"ERROR importing scraper_tires: {e}")
    sys.exit(1)
from scrape_journal import Halt, Journal, state_dir   # shared/ is on sys.path via scraper_tires

MAX_WORKERS = 1   # sequential — server can't handle concurrent load without timeouts
_HERE        = os.path.dirname(os.path.abspath(__file__))
JOURNAL_FILE = state_dir(_HERE) / "northcountry_brand_sweep_progress.jsonl"


# ── Core API helpers (re-uses scraper_tires internals) ───────────────────────

def search_brand(brand: str) -> list:
    body = {
        "criteriaList": [
            {"dataField": "FullText", "string": brand},
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


def _merge(pool: dict, results: list) -> int:
    added = 0
    for r in results:
        g = r.get("productGuid")
        if g and g not in pool:
            pool[g] = r
            added += 1
    return added


def brand_search_with_drilldown(brand: str) -> list:
    """Search brand; if cap hit, drilldown by rim diameter."""
    try:
        results = search_brand(brand)
    except Exception as e:
        print(f"  ERROR brand='{brand}': {e}")
        return []

    if len(results) < SEARCH_CAP:
        return results

    # Cap hit — drilldown by rim size: "Michelin R16", "Michelin R17", etc.
    pool = {r["productGuid"]: r for r in results if r.get("productGuid")}
    for rim in _RIM_SIZES:
        try:
            sub = search_brand(f"{brand} R{rim}")
            added = _merge(pool, sub)
            if len(sub) == SEARCH_CAP:
                print(f"  WARN: '{brand} R{rim}' also hit cap")
        except Exception as e:
            print(f"  WARN brand-rim drilldown {brand} R{rim}: {e}")

    return list(pool.values())


# ── File helpers ─────────────────────────────────────────────────────────────

def find_latest_raw() -> str:
    matches = sorted(glob.glob("Data/raw/northcountry_tires_*.csv"),
                     key=os.path.getmtime, reverse=True)
    if not matches:
        print("ERROR: No northcountry_tires_*.csv found in Data/raw/")
        sys.exit(1)
    return matches[0]


def load_raw(path: str) -> tuple[list[dict], set[str], set[str]]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    guids  = {r["product_guid"] for r in rows if r.get("product_guid")}
    brands = {r["brand"].strip() for r in rows if r.get("brand", "").strip()}
    return rows, guids, brands


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file",  default=None, help="Raw CSV to compare against")
    parser.add_argument("--merge", action="store_true",
                        help="Merge gap products into the raw CSV in-place")
    parser.add_argument("--resume", action="store_true",
                        help="Carry on from the brands a paused or interrupted sweep of the "
                             "same base file already searched")
    args = parser.parse_args()

    csv_path = args.file or find_latest_raw()
    print(f"Base file : {csv_path}")

    rows, known_guids, brands = load_raw(csv_path)
    print(f"Known products : {len(rows)}")
    print(f"Unique brands  : {len(brands)}")
    print(f"Brand sweep    : {sorted(brands)[:10]}{'...' if len(brands) > 10 else ''}")

    # ── Brand sweep ───────────────────────────────────────────────────────────
    # Journaled per brand (see shared/scrape_journal.py). A resume only replays
    # a sweep of this same base file; a newer scrape starts the sweep over.
    journal = Journal.begin(JOURNAL_FILE, resume=args.resume, match=("base_file",),
                            meta={"base_file": os.path.abspath(csv_path)})
    brand_pool: dict[str, dict] = {}  # guid → product from brand search
    swept = set()
    for e in journal.entries("brand"):
        _merge(brand_pool, e["results"])
        swept.add(e["brand"])
    halt      = Halt(_HERE)
    lock      = threading.Lock()
    completed = [len(swept)]
    total     = len(brands)

    def sweep(brand: str):
        if halt():
            return
        try:
            results = brand_search_with_drilldown(brand)
        except BaseException:
            halt.stop()
            raise
        with lock:
            added = _merge(brand_pool, results)
            completed[0] += 1
            journal.add("brand", brand=brand, results=results)
            print(f"  [{completed[0]}/{total}] brand='{brand}': "
                  f"{len(results)} results, {added} new to brand pool "
                  f"(pool size: {len(brand_pool)})")

    print(f"\nStarting brand sweep ({total} brands, {MAX_WORKERS} workers)..."
          + (f" [{len(swept)} already swept]" if swept else ""))
    journal.phase("brand", "Brand sweep", total, "brands")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(sweep, b) for b in sorted(brands) if b not in swept]
        for f in as_completed(futures):
            f.result()
    halt.exit_if_paused(f"{completed[0]} of {total} brands")

    # ── Gap analysis ──────────────────────────────────────────────────────────
    gap_guids    = set(brand_pool.keys()) - known_guids
    in_size_only = known_guids - set(brand_pool.keys())

    print(f"\n{'='*60}")
    print(f"BRAND SWEEP RESULTS")
    print(f"  Products via brand search  : {len(brand_pool)}")
    print(f"  Products in base CSV       : {len(rows)}")
    print(f"  In brand-search ONLY (gaps): {len(gap_guids)}")
    print(f"  In base CSV only           : {len(in_size_only)}")
    print(f"    (size-only products are normal — brands NC doesn't list prominently)")

    if not gap_guids:
        print("\nRESULT: No gaps found. Brand sweep confirms size-based scrape is complete.")
        journal.finish()
        return

    gap_products = [brand_pool[g] for g in gap_guids]
    gap_sizes    = sorted({p.get("size", "") for p in gap_products})
    gap_br_list  = sorted({p.get("brandName", "") for p in gap_products})

    print(f"\n  GAP PRODUCTS — missed by size-based scrape:")
    print(f"    Brands  : {gap_br_list[:20]}")
    print(f"    Sizes   : {gap_sizes[:30]}")

    # ── Optional ProductInfo enrichment for gap products ─────────────────────
    gap_flat = [flatten_product(p) for p in gap_products]

    print(f"\nFetching images + specs for {len(gap_flat)} gap products...")
    guid_map       = {p["product_guid"]: i for i, p in enumerate(gap_flat) if p["product_guid"]}
    guids          = list(guid_map.keys())
    all_spec_codes = set()
    all_guid_specs = {}

    for batch_start in range(0, len(guids), PRODUCT_INFO_BATCH_SIZE):
        batch = guids[batch_start:batch_start + PRODUCT_INFO_BATCH_SIZE]
        try:
            guid_to_image, guid_to_specs = get_product_info(batch)
            for guid, url in guid_to_image.items():
                if guid in guid_map and url:
                    gap_flat[guid_map[guid]]["image_url"] = url
            for guid, specs in guid_to_specs.items():
                all_spec_codes.update(specs.keys())
                all_guid_specs[guid] = specs
        except Exception as e:
            print(f"  WARNING: ProductInfo batch failed: {e}")
        time.sleep(0.5)

    # clean.py quarantines every row without a cost price. Without this pass all
    # 772 products the 10 October sweep found were merged and then dropped.
    print(f"Fetching cost prices for {len(gap_flat)} gap products...")
    cost_found = 0
    for batch_start in range(0, len(guids), PRODUCT_INFO_BATCH_SIZE):
        batch = guids[batch_start:batch_start + PRODUCT_INFO_BATCH_SIZE]
        try:
            for guid, cost in get_cost_prices(batch).items():
                if guid in guid_map and cost not in (None, ""):
                    gap_flat[guid_map[guid]]["cost_price"] = cost
                    cost_found += 1
        except Exception as e:
            print(f"  WARNING: cost price batch failed: {e}")
        time.sleep(0.5)
    print(f"  Cost prices found: {cost_found} / {len(gap_flat)}")

    sorted_codes = sorted(all_spec_codes)
    for product in gap_flat:
        guid  = product["product_guid"]
        specs = all_guid_specs.get(guid, {})
        for code in sorted_codes:
            product[f"spec_{code.lower()}"] = specs.get(code, "")

    # ── Save gap CSV ──────────────────────────────────────────────────────────
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    gap_file = f"Data/raw/northcountry_tires_gaps_{ts}.csv"
    os.makedirs("Data/raw", exist_ok=True)

    if gap_flat:
        all_fields = list(rows[0].keys()) if rows else list(gap_flat[0].keys())
        # Add any new spec columns from gaps not in base CSV
        for p in gap_flat:
            for k in p.keys():
                if k not in all_fields:
                    all_fields.append(k)

        with open(gap_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(gap_flat)
        print(f"\nGap products saved → {gap_file}")

    # ── Optional merge ────────────────────────────────────────────────────────
    if args.merge and gap_flat:
        print(f"\nMerging {len(gap_flat)} gap products into {csv_path} ...")

        # Align columns: base CSV fieldnames + any new spec columns from gaps
        base_fields = list(rows[0].keys()) if rows else []
        extra_fields = [k for p in gap_flat for k in p.keys() if k not in base_fields]
        seen_extra: list = []
        for k in extra_fields:
            if k not in seen_extra:
                seen_extra.append(k)
        all_fields = base_fields + seen_extra

        # Pad existing rows with empty values for new columns
        for r in rows:
            for k in seen_extra:
                r.setdefault(k, "")

        # Written beside and swapped in: this file is the whole size scrape, and
        # a crash halfway through rewriting it in place would truncate it.
        merged_rows = rows + gap_flat
        with open(csv_path + ".tmp", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(merged_rows)
        os.replace(csv_path + ".tmp", csv_path)

        print(f"Merged. Total rows now: {len(merged_rows)} (was {len(rows)})")
        print(f"Run verify_coverage.py to confirm gaps are resolved.")
    elif gap_flat and not args.merge:
        print(f"\nTo merge gaps into the base CSV, re-run with --merge flag:")
        print(f"  python brand_sweep.py --merge")

    journal.finish()
    print(f"\nVERDICT: {'COMPLETE' if not gap_guids else f'{len(gap_guids)} GAP PRODUCTS FOUND'}")


if __name__ == "__main__":
    main()
