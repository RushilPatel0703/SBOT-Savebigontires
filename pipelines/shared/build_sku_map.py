#!/usr/bin/env python3
"""Build shared SKU/handle lookup maps from live Shopify product data.
Run this once at the start of every sync cycle — all other scripts depend on it.

Outputs (absolute paths defined in shared/config.py):
  shared/Data/handle_map.json  — {handle: {product_id, variant_id, inventory_item_id, current_sku, title, tags}}
  shared/Data/sku_map.json     — {sku: {variant_id, inventory_item_id, handle}}
"""

import json
import os
import time
from config import SHOP, API_VERSION, HANDLE_MAP_FILE, SKU_MAP_FILE, api_get, get_logger

log = get_logger("build_sku_map")


def fetch_all_products():
    """Paginate through all Shopify products, 250 per page."""
    products = []
    url = (
        f"https://{SHOP}/admin/api/{API_VERSION}/products.json"
        f"?limit=250&fields=id,handle,title,tags,product_type,variants"
    )
    page = 1
    while url:
        log.debug(f"Fetching page {page}...")
        r    = api_get(url)
        data = r.json().get("products", [])
        products.extend(data)
        link = r.headers.get("Link", "")
        url  = None
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
        page += 1
        time.sleep(0.5)
    return products


def main():
    log.info(f"Fetching all products from {SHOP}...")
    products = fetch_all_products()
    log.info(f"Total products fetched: {len(products)}")

    handle_map = {}
    sku_map    = {}
    with_sku   = 0
    no_sku     = 0

    for product in products:
        handle       = product.get("handle", "")
        product_type = product.get("product_type", "")
        for variant in product.get("variants", []):
            variant_id        = variant["id"]
            inventory_item_id = variant.get("inventory_item_id")
            current_sku       = (variant.get("sku") or "").strip()

            handle_map[handle] = {
                "product_id":        product["id"],
                "variant_id":        variant_id,
                "inventory_item_id": inventory_item_id,
                "current_sku":       current_sku,
                "title":             product.get("title", ""),
                "tags":              product.get("tags", ""),
                "product_type":      product_type,
            }

            if current_sku:
                sku_map[current_sku] = {
                    "variant_id":        variant_id,
                    "inventory_item_id": inventory_item_id,
                    "handle":            handle,
                    "product_type":      product_type,
                }
                with_sku += 1
            else:
                no_sku += 1

    os.makedirs(os.path.dirname(HANDLE_MAP_FILE), exist_ok=True)
    with open(HANDLE_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(handle_map, f)

    with open(SKU_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(sku_map, f)

    log.info(f"handle_map : {len(handle_map)} entries → {HANDLE_MAP_FILE}")
    log.info(f"sku_map    : {len(sku_map)} entries → {SKU_MAP_FILE}")
    log.info(f"With SKU   : {with_sku} | Without SKU: {no_sku}")
    log.info("Done.")


if __name__ == "__main__":
    main()
