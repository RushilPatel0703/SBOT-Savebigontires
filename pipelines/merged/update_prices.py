#!/usr/bin/env python3
"""Sync prices from the merged supplier CSV to Shopify.
Reads the latest merged_tires_*.csv (or merged_wheels_*.csv), applies $30 markup,
and pushes to Shopify via SKU → variant_id lookup from shared sku_map.

Only updates SKUs already in the sku_map (existing products).
New products are handled by each supplier's augment + create_products pipeline.

Usage:
    python update_prices.py --type tires
    python update_prices.py --type wheels
"""

import json
import os
import time
import argparse
import glob
import pandas as pd
from config import SHOP, API_VERSION, SKU_MAP_FILE, api_put, get_logger

MARKUP = 30.00

log = get_logger("update_prices")


def find_latest(pattern):
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return matches[0] if matches else None


def load_sku_map():
    if not os.path.exists(SKU_MAP_FILE):
        log.error(f"sku_map not found at {SKU_MAP_FILE} — run shared/build_sku_map.py first.")
        raise SystemExit(1)
    with open(SKU_MAP_FILE, encoding="utf-8") as f:
        return json.load(f)


def normalize_sku(val):
    try:
        return str(int(float(val)))
    except (ValueError, OverflowError):
        return str(val).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["tires", "wheels"], default="tires")
    args = parser.parse_args()

    csv_file = find_latest(f"Data/merged_{args.type}_*.csv")
    if not csv_file:
        log.error(f"No merged_{args.type}_*.csv found — run merge_suppliers.py first.")
        raise SystemExit(1)

    log.info(f"Reading merged data: {csv_file}")
    df = pd.read_csv(csv_file, dtype=str, low_memory=False).fillna("")

    sku_map = load_sku_map()
    log.info(f"SKU map: {len(sku_map)} entries")

    updated = skipped = errors = 0

    for _, row in df.iterrows():
        sku = normalize_sku(row.get("_sku", "") or row.get("sku", ""))
        if not sku:
            skipped += 1
            continue

        try:
            raw_price = float(row.get("merged_price", 0) or 0)
        except ValueError:
            skipped += 1
            continue

        if raw_price <= 0:
            skipped += 1
            continue

        new_price = round(raw_price + MARKUP, 2)

        entry = sku_map.get(sku)
        if not entry:
            skipped += 1
            continue

        variant_id = entry["variant_id"]
        url = f"https://{SHOP}/admin/api/{API_VERSION}/variants/{variant_id}.json"

        try:
            api_put(url, {"variant": {"id": variant_id, "price": str(new_price)}})
            log.info(f"SKU {sku}: price=${new_price:.2f}")
            updated += 1
        except Exception as e:
            log.error(f"ERROR SKU {sku}: {e}")
            errors += 1

        time.sleep(0.5)

    log.info(f"Done. Updated: {updated} | Skipped: {skipped} | Errors: {errors}")


if __name__ == "__main__":
    main()
