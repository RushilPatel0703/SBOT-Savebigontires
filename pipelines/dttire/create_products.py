#!/usr/bin/env python3
"""DT Tire — Create new wheel products on Shopify.

Reads the latest Shopify SEO CSV, checks each SKU against sku_map.json,
and creates any SKUs not yet on Shopify.  Also sets inventory from total_qty.

Usage:
    python create_products.py --type wheels
    python create_products.py --type wheels --dry-run
"""

import csv
import os
import sys
import time
import argparse
import json

from config import SHOP, API_VERSION, find_latest, SKU_MAP_FILE, api_get, api_post, get_logger

log = get_logger("create_products")


def get_location_id():
    url = f"https://{SHOP}/admin/api/{API_VERSION}/locations.json"
    r = api_get(url)
    return r.json()["locations"][0]["id"]


def load_sku_map():
    if not os.path.exists(SKU_MAP_FILE):
        log.warning("sku_map.json not found — treating all SKUs as new.")
        return {}
    with open(SKU_MAP_FILE, encoding="utf-8") as f:
        return json.load(f)


def create_product(row):
    sku       = row.get("Variant SKU", "").strip()
    price     = row.get("Variant Price", "0").strip() or "0"
    image_src = row.get("Image Src", "").strip()

    payload = {
        "product": {
            "title":        row.get("Title", "").strip(),
            "handle":       row.get("Handle", "").strip(),
            "body_html":    row.get("Body (HTML)", ""),
            "vendor":       row.get("Vendor", ""),
            "product_type": row.get("Product Type", ""),
            "tags":         row.get("Tags", ""),
            "status":       "active" if row.get("Published", "TRUE").upper() == "TRUE" else "draft",
            "options": [{"name": row.get("Option1 Name", "Title")}],
            "variants": [{
                "price":                price,
                "sku":                  sku,
                "option1":              row.get("Option1 Value", "Default Title"),
                "inventory_management": "shopify",
                "inventory_policy":     "continue",
                "fulfillment_service":  "manual",
                "requires_shipping":    True,
                "taxable":              True,
            }],
        }
    }

    if image_src:
        payload["product"]["images"] = [{"src": image_src}]

    url = f"https://{SHOP}/admin/api/{API_VERSION}/products.json"
    r = api_post(url, payload)
    return r.json()["product"]


def set_inventory(inventory_item_id, location_id, qty):
    url = f"https://{SHOP}/admin/api/{API_VERSION}/inventory_levels/set.json"
    api_post(url, {
        "inventory_item_id": inventory_item_id,
        "location_id":       location_id,
        "available":         qty,
    })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["tires", "wheels"], default="wheels",
                        help="Product type to create (default: wheels)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to Shopify")
    args = parser.parse_args()

    pattern = (
        "Data/shopify/Shopify_SEO_dttire_wheels_*.csv" if args.type == "wheels"
        else "Data/shopify/Shopify_SEO_dttire_tires_*.csv"
    )
    csv_file = find_latest(pattern)
    if not csv_file:
        log.error(f"No CSV found for type '{args.type}'. Run augment_wheels.py first.")
        sys.exit(1)

    log.info(f"Reading: {csv_file}")
    with open(csv_file, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("Title", "").strip()]
    log.info(f"Products in CSV: {len(rows)}")

    sku_map = load_sku_map()
    log.info(f"SKU map: {len(sku_map)} existing products on Shopify")

    if args.dry_run:
        log.info("DRY RUN — no products will be created.")
        new_rows = [r for r in rows if r.get("Variant SKU", "").strip() not in sku_map]
        log.info(f"Would create: {len(new_rows)} | Would skip (already on Shopify): {len(rows) - len(new_rows)}")
        for r in new_rows[:5]:
            log.info(f"  [DRY RUN] {r.get('Variant SKU','')} — {r.get('Title','')[:60]}")
        return

    log.info("Fetching Shopify location ID...")
    location_id = get_location_id()
    log.info(f"Location ID: {location_id}")

    log_csv = f"Data/shopify/create_{args.type}_log.csv"
    os.makedirs(os.path.dirname(log_csv), exist_ok=True)

    log_rows = []
    created = skipped = errors = 0

    for i, row in enumerate(rows, 1):
        sku   = row.get("Variant SKU", "").strip()
        title = row.get("Title", "").strip()

        if sku in sku_map:
            log.debug(f"[{i}] SKIP {sku} — already on Shopify")
            skipped += 1
            continue

        try:
            qty = int(float(row.get("total_qty", "0") or "0"))
        except ValueError:
            qty = 0

        try:
            product           = create_product(row)
            product_id        = product["id"]
            inventory_item_id = product["variants"][0]["inventory_item_id"]

            if qty > 0:
                set_inventory(inventory_item_id, location_id, qty)
                time.sleep(0.3)

            log.info(f"[{i}] CREATED {title[:60]} | sku={sku} | qty={qty} | id={product_id}")
            log_rows.append({"title": title, "sku": sku, "product_id": product_id,
                             "qty": qty, "status": "created"})
            created += 1

        except Exception as e:
            log.error(f"[{i}] ERROR {title[:60]} — {e}")
            log_rows.append({"title": title, "sku": sku, "product_id": "",
                             "qty": qty, "status": f"error: {e}"})
            errors += 1

        time.sleep(0.8)

    with open(log_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["title", "sku", "product_id", "qty", "status"])
        writer.writeheader()
        writer.writerows(log_rows)

    log.info(f"Done. Created: {created} | Skipped: {skipped} | Errors: {errors}")
    log.info(f"Log: {log_csv}")
    if created > 0:
        log.info("Run pipelines/shared/build_sku_map.py to register newly created products.")


if __name__ == "__main__":
    main()
