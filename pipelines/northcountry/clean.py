#!/usr/bin/env python3
"""North Country Tire — Data Cleaner
Reads raw scrape output from Data/raw/ and produces cleaned CSVs in Data/cleaned/.
Always applies the qty >= 4 filter (per-supplier threshold — see merge_suppliers.py
for how quantities are combined across suppliers for the update path).

Usage:
    python clean.py --type tires
    python clean.py --type wheels
"""

import sys
import glob
import os
import argparse
import csv
from datetime import datetime

LOW_QTY_THRESHOLD = 4

TIRE_ACCESSORY_CODES = {
    # Non-tire accessories
    "TUBES", "FLAPS", "VALVES", "TPMS", "CHAINS",
    # Generic accessories
    "ACC",
    # Farm implement / agricultural codes
    "FTIMPFARM", "RBFARM1", "RBFARM2", "RRFARM1", "RRFARM2",
    # Turf / lawn
    "TURF", "TURF2",
    # OTR (Off-the-Road) — mining/construction-scale
    "OTRS", "OTRM",
    # Skid steer
    "SKID", "SKID2",
    # Forklift
    "FORK",
    # Mountain bike
    "MTB",
    # Low-speed vehicle
    "LOSPD",
}
WHEEL_VALID_CODES    = {"WHL", "WHLREP", "WHLT", "WHLW", "WHLWS"}


def find_input_file(product_type):
    pattern = f"Data/raw/northcountry_{product_type}_*.csv"
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not matches:
        print(f"ERROR: No northcountry_{product_type}_*.csv found in Data/raw/")
        print(f"Run scraper_tires.py first.")
        sys.exit(1)
    return matches[0]


def format_price(val):
    try:
        return f"{float(val):.2f}"
    except (ValueError, TypeError):
        return val


def clean_sale_date(val):
    return "" if val in (None, "None", "null", "") else val


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["tires", "wheels"], required=True)
    args         = parser.parse_args()
    product_type = args.type

    input_file = find_input_file(product_type)
    print(f"Input  : {input_file}")
    print(f"Type   : {product_type}")

    with open(input_file, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows   = list(reader)

    print(f"Rows loaded: {len(rows)}")

    all_codes = sorted({r.get("product_type_code", "") for r in rows})
    print(f"product_type_codes found: {all_codes}")

    removed = {
        "accessories":    0,
        "inactive":       0,
        "zero_price":     0,
        "low_qty":        0,
        "duplicate_sku":  0,
    }

    # 1. Remove accessories / non-product rows by product_type_code
    rows_filtered = []
    for r in rows:
        code = r.get("product_type_code", "").strip().upper()
        keep = code not in TIRE_ACCESSORY_CODES if product_type == "tires" else code in WHEEL_VALID_CODES
        if keep:
            rows_filtered.append(r)
        else:
            removed["accessories"] += 1
    rows = rows_filtered

    # 2. Remove inactive products
    rows_filtered = [r for r in rows if str(r.get("is_active", "True")).lower() not in ("false", "0", "")]
    removed["inactive"] = len(rows) - len(rows_filtered)
    rows = rows_filtered

    # 3. Remove zero-price products
    def has_price(r):
        try:
            return float(r.get("retail_price", 0) or 0) > 0
        except (ValueError, TypeError):
            return False

    rows_filtered = [r for r in rows if has_price(r)]
    removed["zero_price"] = len(rows) - len(rows_filtered)
    rows = rows_filtered

    def get_qty(r):
        """Return the higher of available_quantity and available_quantity_related_branches."""
        def _parse(val):
            try:
                return float(val) if str(val or "").strip() else 0.0
            except (ValueError, TypeError):
                return 0.0
        return max(
            _parse(r.get("available_quantity_related_branches")),
            _parse(r.get("available_quantity")),
        )

    # 4. Apply per-supplier qty >= 4 filter
    low_qty_rows  = []
    rows_filtered = []
    for r in rows:
        if get_qty(r) >= LOW_QTY_THRESHOLD:
            rows_filtered.append(r)
        else:
            low_qty_rows.append(r)
            removed["low_qty"] += 1
    rows = rows_filtered

    # 5. Deduplicate by SKU — keep highest quantity per SKU
    seen_skus = {}
    for r in rows:
        sku = r.get("sku", "").strip()
        if not sku:
            continue
        if sku not in seen_skus or get_qty(r) > get_qty(seen_skus[sku]):
            seen_skus[sku] = r

    no_sku_rows        = [r for r in rows if not r.get("sku", "").strip()]
    before_dedup       = len(rows)
    rows               = list(seen_skus.values()) + no_sku_rows
    removed["duplicate_sku"] = before_dedup - len(rows)

    # 6. Format prices
    for r in rows:
        r["retail_price"]    = format_price(r.get("retail_price", ""))
        r["suggested_price"] = format_price(r.get("suggested_price", ""))

    # 7. Normalize sale dates
    for r in rows:
        r["sale_begins"] = clean_sale_date(r.get("sale_begins", ""))
        r["sale_ends"]   = clean_sale_date(r.get("sale_ends", ""))

    # Output
    os.makedirs("Data/cleaned", exist_ok=True)
    ts           = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file  = f"Data/cleaned/cleaned_{product_type}_{ts}.csv"
    low_qty_file = f"Data/cleaned/low_quantity_{product_type}.csv"

    fieldnames = list(rows[0].keys()) if rows else []
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    if low_qty_rows:
        lq_fields = list(low_qty_rows[0].keys())
        with open(low_qty_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=lq_fields)
            writer.writeheader()
            writer.writerows(low_qty_rows)

    print(f"\nRemoved:")
    print(f"  Accessories (product_type_code filter) : {removed['accessories']}")
    print(f"  Inactive products                      : {removed['inactive']}")
    print(f"  Zero-price products                    : {removed['zero_price']}")
    print(f"  Low quantity (qty < {LOW_QTY_THRESHOLD})              : {removed['low_qty']}")
    print(f"  Duplicate SKUs                         : {removed['duplicate_sku']}")
    print(f"\nFinal row count : {len(rows)}")
    print(f"Output          : {output_file}")
    if low_qty_rows:
        print(f"Low qty ref     : {low_qty_file}")
    print("Done.")


if __name__ == "__main__":
    main()
