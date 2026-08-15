#!/usr/bin/env python3
"""Create Shopify products from the augmented Formula Distributors CSV.
Reads Data/Shopify_SEO_tires.csv (output of augment.py).
For each row:
  1. POST /products.json  — creates the product
  2. POST /inventory_levels/set.json — sets stock qty
Variant SKU is populated from the Formula SKU column.
"""

import csv
import json
import os
import sys
import time
import argparse
from config import SHOP, API_VERSION, SKU_MAP_FILE, find_latest, api_get, api_post, get_logger

log = get_logger("create_products")


def load_sku_map():
    if not os.path.exists(SKU_MAP_FILE):
        log.warning("sku_map.json not found — treating all SKUs as new.")
        return {}
    with open(SKU_MAP_FILE, encoding="utf-8") as f:
        return json.load(f)


def get_location_id():
    url = f"https://{SHOP}/admin/api/{API_VERSION}/locations.json"
    r = api_get(url)
    return r.json()["locations"][0]["id"]


def create_product(row, sku_col="SKU"):
    sku       = row.get(sku_col, "").strip()
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
    parser.add_argument("--type", choices=["tires", "wheels"], default="tires",
                        help="Which product type to create (default: tires)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to Shopify")
    args = parser.parse_args()

    pattern  = "Data/shopify/Shopify_SEO_wheels_*.csv" if args.type == "wheels" else "Data/shopify/Shopify_SEO_tires_*.csv"
    csv_file = find_latest(pattern)
    if not csv_file:
        log.error(f"No Shopify SEO CSV found for type '{args.type}'. Run augment{('_wheels' if args.type == 'wheels' else '')}.py first.")
        raise SystemExit(1)
    log_csv  = f"Data/shopify/create_{args.type}_log.csv"

    log.info("Fetching Shopify location ID...")
    location_id = get_location_id()
    log.info(f"Location ID: {location_id}")

    log.info(f"Loading {csv_file}...")
    with open(csv_file, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("Title", "").strip()]
    log.info(f"Products to create: {len(rows)}")

    sku_map = load_sku_map()
    log.info(f"SKU map: {len(sku_map)} existing products on Shopify")

    if args.dry_run:
        log.info("DRY RUN — no products will be created.")

    log_rows = []
    created = skipped = errors = 0

    sku_col = "Variant SKU" if args.type == "wheels" else "SKU"

    for i, row in enumerate(rows, 1):
        title = row.get("Title", "").strip()
        sku   = row.get(sku_col, "").strip()

        if not sku or sku in sku_map:
            skipped += 1
            continue

        try:
            qty = int(float(row.get("total_qty", "0") or "0"))
        except ValueError:
            qty = 0

        if args.dry_run:
            log.info(f"[DRY RUN] Would create SKU {sku} — {title[:60]}")
            created += 1
            continue

        try:
            product           = create_product(row, sku_col)
            product_id        = product["id"]
            inventory_item_id = product["variants"][0]["inventory_item_id"]

            if qty > 0:
                set_inventory(inventory_item_id, location_id, qty)
                time.sleep(0.5)

            log.info(f"[{i}] CREATED {title} | sku={sku} | qty={qty} | id={product_id}")
            log_rows.append({"title": title, "sku": sku, "product_id": product_id,
                             "qty": qty, "status": "created"})
            created += 1

        except Exception as e:
            log.error(f"[{i}] ERROR {title} — {e}")
            log_rows.append({"title": title, "sku": sku, "product_id": "",
                             "qty": qty, "status": f"error: {e}"})
            errors += 1

        time.sleep(1)

    import os
    os.makedirs(os.path.dirname(log_csv), exist_ok=True)
    with open(log_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["title", "sku", "product_id", "qty", "status"])
        writer.writeheader()
        writer.writerows(log_rows)

    log.info(f"Done. Created: {created} | Skipped (already on Shopify): {skipped} | Errors: {errors}")
    log.info(f"Log: {log_csv}")


if __name__ == "__main__":
    main()
