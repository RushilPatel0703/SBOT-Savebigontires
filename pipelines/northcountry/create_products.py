#!/usr/bin/env python3
"""North Country Tire — Create new products on Shopify.

Reads the latest Shopify SEO CSV, checks each SKU against sku_map.json,
and creates any SKUs that are not yet on Shopify.

New products start at qty=0 — inventory is set by update_inventory.py in run_sync.py.

Usage:
    python create_products.py --type tires
    python create_products.py --type wheels
    python create_products.py --type tires --dry-run
"""

import json
import os
import sys
import argparse
import glob
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
from config import SHOP, API_VERSION, SKU_MAP_FILE, api_post, get_logger

log = get_logger("create_products")


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


def load_sku_map():
    if not os.path.exists(SKU_MAP_FILE):
        log.warning("sku_map.json not found — treating all SKUs as new.")
        return {}
    with open(SKU_MAP_FILE, encoding="utf-8") as f:
        return json.load(f)


def build_payload(row):
    sku       = str(row.get("Variant SKU", "")).strip()
    image_src = str(row.get("Image Src", "")).strip()

    variant = {
        "sku":                  sku,
        "price":                str(row.get("Variant Price", "0")).strip(),
        "requires_shipping":    True,
        "taxable":              True,
        "fulfillment_service":  "manual",
        "inventory_management": "shopify",
        "inventory_policy":     "continue",
    }

    product = {
        "title":        str(row.get("Title", "")).strip(),
        "body_html":    str(row.get("Body (HTML)", "")).strip(),
        "vendor":       str(row.get("Vendor", "")).strip(),
        "product_type": str(row.get("Product Type", "Vehicle Tires")).strip(),
        "tags":         str(row.get("Tags", "")).strip(),
        "published":    True,
        "variants":     [variant],
    }

    if image_src:
        product["images"] = [{"src": image_src}]

    return {"product": product}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["tires", "wheels"], default="tires",
                        help="Product type to create (default: tires)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to Shopify")
    args = parser.parse_args()

    csv_file = find_latest_shopify_csv(args.type)
    log.info(f"Reading: {csv_file}")
    df = pd.read_csv(csv_file, dtype=str, low_memory=False).fillna("")
    log.info(f"Rows loaded: {len(df)}")

    sku_map = load_sku_map()
    log.info(f"SKU map: {len(sku_map)} existing products on Shopify")

    if args.dry_run:
        log.info("DRY RUN — no products will be created.")

    url = f"https://{SHOP}/admin/api/{API_VERSION}/products.json"

    counters = {"created": 0, "skipped": 0, "errors": 0}
    lock     = threading.Lock()

    def process_row(row):
        sku = str(row.get("Variant SKU", "")).strip()
        if not sku or sku in sku_map:
            with lock:
                counters["skipped"] += 1
            return

        title = str(row.get("Title", ""))[:60]
        if args.dry_run:
            log.info(f"[DRY RUN] Would create SKU {sku} — {title}")
            with lock:
                counters["created"] += 1
            return

        try:
            r          = api_post(url, build_payload(row))
            product_id = r.json()["product"]["id"]
            log.info(f"Created SKU {sku} → product_id={product_id}")
            with lock:
                counters["created"] += 1
        except Exception as e:
            log.error(f"ERROR SKU {sku}: {e}")
            with lock:
                counters["errors"] += 1

    rows = [row for _, row in df.iterrows()]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(process_row, row): row for row in rows}
        for future in as_completed(futures):
            future.result()

    log.info(f"Done. Created: {counters['created']} | Skipped (already on Shopify): {counters['skipped']} | Errors: {counters['errors']}")
    if counters["created"] > 0 and not args.dry_run:
        log.info("Run build_sku_map.py (via run_sync.py) to register newly created products.")


if __name__ == "__main__":
    main()
