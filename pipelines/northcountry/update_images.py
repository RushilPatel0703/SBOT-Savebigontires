#!/usr/bin/env python3
"""Update product images for NC products on Shopify.

Reads the latest Shopify SEO CSV, looks up product_id via sku_map + handle_map,
and PUTs the image URL to each product. Replaces any existing images.

Skips products with no image URL or not found in the sku_map.
New products created by create_products.py already have images set at creation time
and will be skipped here (not yet in sku_map — picked up on next run_sync cycle).

Usage:
    python update_images.py --type tires
    python update_images.py --type wheels
    python update_images.py --type tires --dry-run
"""

import json
import os
import sys
import time
import argparse
import glob
import pandas as pd
from config import SHOP, API_VERSION, SKU_MAP_FILE, HANDLE_MAP_FILE, api_put, get_logger

log = get_logger("update_images")


def find_latest_shopify_csv(product_type):
    matches = sorted(
        glob.glob(f"Data/shopify/Shopify_SEO_northcountry_{product_type}_*.csv"),
        key=os.path.getmtime,
        reverse=True,
    )
    if not matches:
        log.error(f"No Shopify SEO CSV found for type '{product_type}' — run augment_{product_type}.py first.")
        sys.exit(1)
    return matches[0]


def load_maps():
    for path, name in [(SKU_MAP_FILE, "sku_map"), (HANDLE_MAP_FILE, "handle_map")]:
        if not os.path.exists(path):
            log.error(f"{name}.json not found at {path} — run build_sku_map.py first.")
            sys.exit(1)
    with open(SKU_MAP_FILE,    encoding="utf-8") as f:
        sku_map    = json.load(f)
    with open(HANDLE_MAP_FILE, encoding="utf-8") as f:
        handle_map = json.load(f)
    return sku_map, handle_map


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["tires", "wheels"], default="tires",
                        help="Product type to update images for (default: tires)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to Shopify")
    args = parser.parse_args()

    csv_file = find_latest_shopify_csv(args.type)
    log.info(f"Reading: {csv_file}")
    df = pd.read_csv(csv_file, dtype=str, low_memory=False).fillna("")
    log.info(f"Rows loaded: {len(df)}")

    sku_map, handle_map = load_maps()
    log.info(f"SKU map: {len(sku_map)} | Handle map: {len(handle_map)}")

    if args.dry_run:
        log.info("DRY RUN — no changes will be pushed.")

    updated = skipped = errors = 0

    for _, row in df.iterrows():
        sku       = str(row.get("Variant SKU", "")).strip()
        image_url = str(row.get("Image Src",   "")).strip()

        if not sku or not image_url:
            skipped += 1
            continue

        sku_entry = sku_map.get(sku)
        if not sku_entry:
            skipped += 1
            continue

        handle       = sku_entry.get("handle", "")
        handle_entry = handle_map.get(handle)
        if not handle_entry:
            skipped += 1
            continue

        product_id = handle_entry.get("product_id")
        if not product_id:
            skipped += 1
            continue

        if args.dry_run:
            log.info(f"[DRY RUN] SKU {sku} → product_id={product_id}  {image_url[:70]}")
            updated += 1
            continue

        url = f"https://{SHOP}/admin/api/{API_VERSION}/products/{product_id}.json"
        try:
            api_put(url, {"product": {"id": product_id, "images": [{"src": image_url}]}})
            log.info(f"SKU {sku}: image updated")
            updated += 1
        except Exception as e:
            log.error(f"ERROR SKU {sku}: {e}")
            errors += 1

        time.sleep(0.5)

    log.info(f"Done. Updated: {updated} | Skipped (no image / not on store): {skipped} | Errors: {errors}")


if __name__ == "__main__":
    main()
