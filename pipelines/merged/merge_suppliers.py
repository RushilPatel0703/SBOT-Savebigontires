#!/usr/bin/env python3
"""Merge cleaned supplier CSVs into a single source of truth for price/inventory sync.

Rules:
  - Each supplier's cleaned CSV already has the qty >= 4 filter applied (per-supplier threshold).
  - Matching key: SKU (formula uses 'item' column, NC uses 'sku' column).
  - Each supplier's price has its per-supplier markup applied at load, so merged_price
    is the FINAL selling price. Downstream must not add markup again.
  - For SKUs present in multiple suppliers: price = min, qty = sum of qualifying supplier quantities.
  - Only quantities from suppliers that individually pass the threshold contribute (handled
    upstream by clean.py — if a supplier's cleaned CSV has a SKU, it passed their filter).
  - Outputs Data/merged_tires_<ts>.csv consumed by update_prices.py and update_inventory.py.

Usage:
    python merge_suppliers.py           # merge tires (default)
    python merge_suppliers.py --type wheels
"""

import argparse
import glob
import os
import sys
import pandas as pd
from datetime import datetime

from config import markup_for

_HERE = os.path.dirname(os.path.abspath(__file__))

SUPPLIER_CONFIGS = {
    "formula": {
        "pattern": os.path.join(_HERE, "..", "formula_distributors", "Data", "cleaned", "cleaned_{type}_*.csv"),
        "sku_col":   "item",
        "price_cols": ("cost_price", "retail_price"),
        "qty_col":   "available_quantity",
    },
    "northcountry": {
        "pattern": os.path.join(_HERE, "..", "northcountry", "Data", "cleaned", "cleaned_{type}_*.csv"),
        "sku_col":   "sku",
        "price_cols": ("cost_price", "retail_price"),
        "qty_col":   "available_quantity_related_branches",
    },
    "dttire": {
        "pattern": os.path.join(_HERE, "..", "dttire", "Data", "cleaned", "cleaned_{type}_*.csv"),
        "sku_col":   "sku",
        "price_cols": ("cost_price",),
        "qty_col":   "qty_total",
    },
}


def find_latest(pattern):
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return matches[0] if matches else None


def load_supplier(name, config, product_type):
    pattern = config["pattern"].replace("{type}", product_type)
    path    = find_latest(pattern)
    if not path:
        print(f"  [{name}] No cleaned_{product_type}_*.csv found — skipping.")
        return None

    df = pd.read_csv(path, dtype=str, low_memory=False).fillna("")
    print(f"  [{name}] Loaded {len(df)} rows from {os.path.basename(path)}")

    sku_col   = config["sku_col"]
    qty_col   = config["qty_col"]

    if sku_col not in df.columns:
        print(f"  [{name}] WARNING: expected SKU column '{sku_col}' not found — skipping.")
        return None

    # price_cols is ordered cost-first, retail last. A feed scraped before the
    # cost-price migration has no cost column and falls back to retail with a
    # warning rather than failing the whole merge.
    price_col = next((c for c in config["price_cols"] if c in df.columns), None)
    if price_col is None:
        print(f"  [{name}] WARNING: none of price columns {config['price_cols']} found — skipping.")
        return None
    if price_col != config["price_cols"][0]:
        print(f"  [{name}] WARNING: cost column '{config['price_cols'][0]}' absent — "
              f"falling back to '{price_col}' (re-scrape to price off cost).")

    df["_sku"]   = df[sku_col].apply(lambda v: str(int(float(v))) if _is_numeric(v) else str(v).strip())

    # Markup is per supplier, so it must be applied HERE — before merge() takes
    # the min — because min(cost) and min(cost + markup) no longer select the
    # same supplier once markups differ (cost 100 + 46.50 = 146.50 loses to
    # cost 105 + 36.50 = 141.50). Adding markup after the min would pick the
    # cheapest cost rather than the cheapest price and overcharge on every
    # multi-supplier SKU. _price is therefore the FINAL selling price, and
    # update_prices.py must not add markup again.
    # Zero/unparseable costs stay 0 so they aren't turned into a bare markup
    # value that could win the min as a fake price.
    cost         = pd.to_numeric(df[price_col], errors="coerce").fillna(0)
    df["_price"] = (cost + markup_for(name)).where(cost > 0, 0.0).round(2)
    df["_qty"]   = pd.to_numeric(df[qty_col],   errors="coerce").fillna(0).astype(int)
    df["_source"] = name

    return df[["_sku", "_price", "_qty", "_source"]].copy()


def _is_numeric(val):
    try:
        float(val)
        return True
    except (ValueError, TypeError):
        return False


def merge(frames):
    """Merge supplier frames: price = min, qty = sum per SKU."""
    combined = pd.concat(frames, ignore_index=True)
    combined = combined[combined["_sku"] != ""]

    grouped = combined.groupby("_sku", as_index=False).agg(
        merged_price=("_price", "min"),
        merged_qty=("_qty",   "sum"),
        sources=("_source",  lambda s: "|".join(sorted(set(s)))),
    )
    return grouped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["tires", "wheels"], default="tires")
    args = parser.parse_args()
    product_type = args.type

    print(f"Merging {product_type} from all suppliers...")

    frames = []
    for name, config in SUPPLIER_CONFIGS.items():
        df = load_supplier(name, config, product_type)
        if df is not None:
            frames.append(df)

    if not frames:
        print("ERROR: No supplier data found. Run supplier pipelines first.")
        sys.exit(1)

    merged = merge(frames)
    print(f"\nMerged rows : {len(merged)}")

    # Summary
    has_both   = merged["sources"].str.contains("|", regex=False).sum()
    has_single = len(merged) - has_both
    print(f"Both suppliers: {has_both} | Single supplier: {has_single}")
    if has_both:
        shared = merged[merged["sources"].str.contains("|", regex=False)]["_sku"].tolist()
        print(f"Shared SKUs: {shared}")

    # Detect SKUs present in one run but not another (early warning for SKU drift)
    all_skus = set()
    per_supplier = {}
    for name, config in SUPPLIER_CONFIGS.items():
        pattern = config["pattern"].replace("{type}", product_type)
        path    = find_latest(pattern)
        if path:
            df = pd.read_csv(path, dtype=str, low_memory=False).fillna("")
            sku_col = config["sku_col"]
            if sku_col in df.columns:
                skus = set(df[sku_col].dropna().astype(str).str.strip())
                per_supplier[name] = skus
                all_skus |= skus

    if len(per_supplier) > 1:
        names = list(per_supplier.keys())
        only_in_a = per_supplier[names[0]] - per_supplier[names[1]]
        only_in_b = per_supplier[names[1]] - per_supplier[names[0]]
        if only_in_a:
            print(f"  SKUs only in {names[0]}: {len(only_in_a)}")
        if only_in_b:
            print(f"  SKUs only in {names[1]}: {len(only_in_b)}")

    os.makedirs("Data", exist_ok=True)
    ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = f"Data/merged_{product_type}_{ts}.csv"
    merged.to_csv(output_file, index=False)
    print(f"\nOutput: {output_file}")
    print("Done.")


if __name__ == "__main__":
    main()
