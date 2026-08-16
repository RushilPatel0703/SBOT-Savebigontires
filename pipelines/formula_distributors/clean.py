#!/usr/bin/env python3
"""Formula Distributors Data Cleaner
- Splits out low-quantity rows (available_quantity < 4) into low_quantity_<type>.csv
- Deduplicates by SKU (item column), confirming true duplicates before dropping
- Works for both tires (formula_tires_*.csv) and wheels (formula_wheels_*.csv)
"""

import sys
import glob
import os
import time
import argparse
import pandas as pd
from tire_designation_inference import infer_designation

LOW_QTY_THRESHOLD = 4

# See dttire/clean.py for the rationale: a "latest" raw file this old is more
# likely a stale leftover (scraper step silently produced nothing this run)
# than genuinely fresh input, and must be flagged rather than used silently.
STALE_INPUT_WARN_HOURS = 24

# Columns that must all match for a duplicate to be considered truly identical.
# product_guid is the strongest confirming field — if two rows share an item
# (SKU) and product_guid they are the same product entry.
CONFIRM_COLS = ["description", "size", "brand_name", "product_guid"]


def find_input_file(product_type):
    """Use the most recently modified raw CSV for the given product type."""
    pattern = f"Data/raw/formula_{product_type}_*.csv"
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not matches:
        print(f"ERROR: No formula_{product_type}_*.csv found in Data/raw/.")
        print(f"Run scraper{'_wheels' if product_type == 'wheels' else ''}.py first.")
        sys.exit(1)
    return matches[0]


def detect_type(input_file):
    """Returns 'tires' or 'wheels' based on the input filename."""
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["tires", "wheels"], required=True,
                        help="Which product type to clean")
    args = parser.parse_args()
    product_type = args.type

    input_file = find_input_file(product_type)
    print(f"Input file   : {input_file}")
    print(f"Product type : {product_type}")

    age_hours = (time.time() - os.path.getmtime(input_file)) / 3600
    if age_hours > STALE_INPUT_WARN_HOURS:
        print(
            f"  WARNING: input file is {age_hours / 24:.1f} day(s) old "
            f"(> {STALE_INPUT_WARN_HOURS}h threshold). Proceeding, but this "
            f"usually means the scraper step didn't produce a fresh file this "
            f"run — verify before trusting this output."
        )

    df = pd.read_csv(input_file, dtype=str, low_memory=False)
    print(f"Rows loaded  : {len(df)}")
    if len(df) == 0:
        print(f"ERROR: {input_file} has 0 rows — refusing to write an empty cleaned CSV. "
              f"Check the raw scrape before re-running.")
        sys.exit(1)

    # ── Cost price — backfill missing/zero cost_price from retail_price ───────
    # cost_price may be entirely absent from raw scrapes that predate this
    # column (schema migration in progress — never hard-fail on it, warn and
    # fall back to retail per the safety requirement covering in-flight runs).
    # Deliberately do NOT synthesize cost_price when the raw scrape has no such
    # column: writing cost_price = retail_price would make every downstream
    # consumer (augment, merge_suppliers, production ingest) see a cost column
    # that is present but actually holds retail, silently pricing off the wrong
    # basis with no warning anywhere. Leaving it absent is what makes those
    # consumers fall back loudly.
    if "cost_price" not in df.columns:
        print(f"  WARNING: cost_price absent from raw scrape ({len(df)} rows) — "
              f"leaving it unset so downstream pricing falls back to retail_price "
              f"with a warning. Re-scrape to price off true cost.")
    else:
        cost_num = pd.to_numeric(df["cost_price"], errors="coerce").fillna(0)
        fallback_mask = cost_num <= 0
        if fallback_mask.any():
            print(f"  WARNING: {fallback_mask.sum()} rows had missing/zero cost_price — fell back to retail_price.")
        df.loc[fallback_mask, "cost_price"] = df.loc[fallback_mask, "retail_price"]

    os.makedirs("Data/cleaned", exist_ok=True)
    low_qty_file = f"Data/cleaned/low_quantity_{product_type}.csv"

    # ── Normalise available_quantity to numeric ────────────────────────────────
    df["available_quantity"] = pd.to_numeric(df["available_quantity"], errors="coerce").fillna(0)

    # ── Step 1: Split low-quantity rows ───────────────────────────────────────
    low_qty_mask = df["available_quantity"] < LOW_QTY_THRESHOLD
    low_qty_df   = df[low_qty_mask].copy()
    main_df      = df[~low_qty_mask].copy()

    low_qty_df.to_csv(low_qty_file, index=False)
    print(f"\nLow-quantity rows (qty < {LOW_QTY_THRESHOLD}): {len(low_qty_df)}")
    print(f"  -> saved to: {low_qty_file}")
    print(f"Remaining rows after low-qty removal: {len(main_df)}")

    # ── Step 2: Deduplicate by SKU (item) ─────────────────────────────────────
    sku_col = "item"

    # Find all SKUs that appear more than once
    dup_skus = main_df[main_df.duplicated(subset=[sku_col], keep=False)][sku_col].unique()
    print(f"\nSKUs with duplicates: {len(dup_skus)}")

    true_dup_count = 0
    conflict_count = 0

    if len(dup_skus) > 0:
        for sku in dup_skus:
            group = main_df[main_df[sku_col] == sku]

            # Check confirmation columns — only drop if all CONFIRM_COLS match
            confirm_cols_present = [c for c in CONFIRM_COLS if c in group.columns]
            if group[confirm_cols_present].nunique().max() == 1:
                # All confirm fields are identical → true duplicate, keep first
                true_dup_count += len(group) - 1
            else:
                # Same SKU but differing data → flag as conflict, keep all
                conflict_count += 1
                print(f"  CONFLICT — SKU '{sku}' has {len(group)} rows with differing data (kept all):")
                for _, row in group.iterrows():
                    vals = {c: row[c] for c in confirm_cols_present}
                    print(f"    {vals}")

        # Drop true duplicates: keep first occurrence of each SKU where all
        # confirm fields are identical across all rows of that SKU group.
        def is_true_dup_group(group):
            confirm_cols_present = [c for c in CONFIRM_COLS if c in group.columns]
            return group[confirm_cols_present].nunique().max() == 1

        true_dup_skus = [
            sku for sku in dup_skus
            if is_true_dup_group(main_df[main_df[sku_col] == sku])
        ]

        # Remove all but the first row for true-duplicate SKUs
        true_dup_mask = main_df.duplicated(subset=[sku_col], keep="first") & main_df[sku_col].isin(true_dup_skus)
        main_df = main_df[~true_dup_mask].copy()

        print(f"\nTrue duplicate rows dropped : {true_dup_count}")
        print(f"SKU conflicts kept (review) : {conflict_count}")

    # ── Step 3: Designation column (tires only) ──────────────────────────────
    if product_type == "tires":
        results = main_df.apply(infer_designation, axis=1, result_type="expand")
        results.columns = ["designation", "designation_reason"]
        main_df["designation"]        = results["designation"]
        main_df["designation_reason"] = results["designation_reason"]

        print(f"\nDesignation breakdown:\n{main_df['designation'].value_counts(dropna=False).to_string()}")
        unknown_count = (main_df["designation"] == "Unknown").sum()
        if unknown_count:
            print(f"\n  WARNING: {unknown_count} rows with Unknown designation — check designation_reason column")
            unknown_samples = main_df[main_df["designation"] == "Unknown"][
                ["brand_name", "model", "description", "size", "designation_reason"]
            ].head(10)
            print(unknown_samples.to_string())

    if len(main_df) == 0:
        print(f"ERROR: 0 rows survive cleaning (low-qty filter + dedup) — refusing to write "
              f"an empty cleaned_{product_type} CSV. Check the raw scrape before re-running.")
        sys.exit(1)

    # ── Output ─────────────────────────────────────────────────────────────────
    # Restore available_quantity to original numeric string form
    main_df["available_quantity"] = main_df["available_quantity"].apply(
        lambda x: str(int(x)) if x == int(x) else str(x)
    )

    basename = os.path.basename(input_file).replace(f"formula_{product_type}_", f"cleaned_{product_type}_")
    output_file = os.path.join("Data/cleaned", basename)
    main_df.to_csv(output_file, index=False)

    print(f"\nFinal row count : {len(main_df)}")
    print(f"Cleaned file    : {output_file}")
    print("Done!")


if __name__ == "__main__":
    main()
