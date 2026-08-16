#!/usr/bin/env python3
"""Sync inventory quantities from the merged supplier CSV to Shopify.
Reads the latest merged_tires_*.csv (or merged_wheels_*.csv) and SETs inventory
for all existing products via inventory_item_id from shared sku_map.

Two-pass approach:
  Pass 1 — For every SKU in the merged CSV: SET qty to merged_qty.
  Pass 2 — For every SKU in sku_map NOT in the merged CSV: SET qty to 0.
           (handles discontinued products and those that dropped below all supplier thresholds)

Usage:
    python update_inventory.py --type tires
    python update_inventory.py --type wheels
    python update_inventory.py --type tires --dry-run
"""

import json
import os
import sys
import time
import argparse
import glob
import pandas as pd
from config import SHOP, API_VERSION, SKU_MAP_FILE, api_get, api_post, get_logger

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared")))
from sku_normalize import normalize_sku

# Maps CLI --type to the Shopify product_type field stored in sku_map
PRODUCT_TYPE_MAP = {
    "tires":  "Vehicle Tires",
    "wheels": "Wheels",
}

log = get_logger("update_inventory")


def find_latest(pattern):
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return matches[0] if matches else None


def load_sku_map():
    if not os.path.exists(SKU_MAP_FILE):
        log.error(f"sku_map not found at {SKU_MAP_FILE} — run shared/build_sku_map.py first.")
        raise SystemExit(1)
    with open(SKU_MAP_FILE, encoding="utf-8") as f:
        return json.load(f)


def get_location_id():
    url = f"https://{SHOP}/admin/api/{API_VERSION}/locations.json"
    r   = api_get(url)
    locs = r.json().get("locations", [])
    if not locs:
        raise Exception("No locations found in Shopify store.")
    loc_id = locs[0]["id"]
    log.info(f"Location: {locs[0].get('name', loc_id)} (id={loc_id})")
    return loc_id


def set_inventory(inventory_item_id, location_id, qty, dry_run):
    if dry_run:
        log.info(f"  [DRY RUN] inventory_item {inventory_item_id} → qty={qty}")
        return
    url     = f"https://{SHOP}/admin/api/{API_VERSION}/inventory_levels/set.json"
    payload = {
        "location_id":        location_id,
        "inventory_item_id":  inventory_item_id,
        "available":          qty,
    }
    api_post(url, payload)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type",    choices=["tires", "wheels"], default="tires")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without pushing")
    args = parser.parse_args()

    csv_file = find_latest(f"Data/merged_{args.type}_*.csv")
    if not csv_file:
        log.error(f"No merged_{args.type}_*.csv found — run merge_suppliers.py first.")
        raise SystemExit(1)

    log.info(f"Reading merged data: {csv_file}")
    df = pd.read_csv(csv_file, dtype=str, low_memory=False).fillna("")

    sku_map = load_sku_map()
    log.info(f"SKU map: {len(sku_map)} entries")

    if not args.dry_run:
        location_id = get_location_id()
    else:
        location_id = 0
        log.info("DRY RUN mode — no changes will be pushed.")

    # Build set of SKUs seen in the merged CSV
    merged_skus = {}
    for _, row in df.iterrows():
        sku = normalize_sku(row.get("_sku", "") or row.get("sku", ""))
        if not sku:
            continue
        try:
            qty = int(float(row.get("merged_qty", 0) or 0))
        except ValueError:
            qty = 0
        merged_skus[sku] = qty

    updated = zeroed = skipped = errors = 0

    # Pass 1: update inventory for all SKUs in merged CSV
    log.info(f"Pass 1: updating {len(merged_skus)} SKUs from merged CSV...")
    for sku, qty in merged_skus.items():
        entry = sku_map.get(sku)
        if not entry:
            skipped += 1
            continue

        inventory_item_id = entry["inventory_item_id"]
        try:
            set_inventory(inventory_item_id, location_id, qty, args.dry_run)
            log.info(f"SKU {sku}: qty={qty}")
            updated += 1
        except Exception as e:
            log.error(f"ERROR SKU {sku}: {e}")
            errors += 1

        time.sleep(0.5)

    # Pass 2: zero out SKUs of this product type that are absent from merged CSV.
    # Filter by product_type so tires run doesn't zero wheel inventory and vice versa.
    # sku_map entries without product_type (built before this fix) fall back to included.
    expected_type = PRODUCT_TYPE_MAP.get(args.type, "")
    absent_skus = [
        sku for sku in sku_map
        if sku not in merged_skus
        and sku_map[sku].get("product_type", expected_type) == expected_type
    ]
    log.info(f"Pass 2: zeroing {len(absent_skus)} {args.type} SKUs absent from merged CSV...")

    for sku in absent_skus:
        entry             = sku_map[sku]
        inventory_item_id = entry["inventory_item_id"]
        try:
            set_inventory(inventory_item_id, location_id, 0, args.dry_run)
            log.info(f"SKU {sku}: qty=0 (absent from supplier feeds)")
            zeroed += 1
        except Exception as e:
            log.error(f"ERROR zeroing SKU {sku}: {e}")
            errors += 1

        time.sleep(0.5)

    log.info(
        f"Done. Updated: {updated} | Zeroed (absent): {zeroed} | "
        f"Skipped (not in store): {skipped} | Errors: {errors}"
    )


if __name__ == "__main__":
    main()
