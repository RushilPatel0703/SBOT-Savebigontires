#!/usr/bin/env python3
"""Emerge Tires — Data Cleaner
Reads raw scrape output from Data/raw/ and produces cleaned CSVs in Data/cleaned/.
Applies qty >= 4 filter (see CLEAN_SPEC.md for full decision log).

Usage:
    python clean.py --type tires
    python clean.py --type wheels
"""

import sys
import glob
import os
import re
import time
import argparse
import csv
from datetime import datetime

# APPEND, never insert(0): every pipeline has its own config.py and so does
# shared/. Putting shared/ first on the path shadows the local one and silently
# swaps in the wrong markup.
sys.path.append(os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "shared")))
import no_price

LOW_QTY_THRESHOLD = 4  # keep if max(qty, related_branches_qty) >= 4

# See dttire/clean.py for the rationale: a "latest" raw file this old is more
# likely a stale leftover (scraper step silently produced nothing this run)
# than genuinely fresh input, and must be flagged rather than used silently.
STALE_INPUT_WARN_HOURS = 24


def check_input_freshness(input_file):
    age_hours = (time.time() - os.path.getmtime(input_file)) / 3600
    if age_hours > STALE_INPUT_WARN_HOURS:
        print(
            f"  WARNING: input file is {age_hours / 24:.1f} day(s) old "
            f"(> {STALE_INPUT_WARN_HOURS}h threshold). Proceeding, but this "
            f"usually means the scraper step didn't produce a fresh file this "
            f"run — verify before trusting this output."
        )

# Phase 0 finding (OQ-6 resolved): Emerge's productTypeCode values are NUMERIC strings:
#   '2'  = standard all-season tire
#   '9'  = all-season tire (different grade/tier)
#   '2W' = winter tire
#   '3'  = LT tire
# All 4 codes observed are tires. No accessories found in the catalogue.
# String-based NC codes below won't match Emerge's numeric codes, so the filter is
# effectively a no-op for Emerge — which is correct (keep everything).
# If future scrapes reveal non-tire codes, add them here as strings.
TIRE_ACCESSORY_CODES = {
    # Non-tire accessories (NC/Formula string codes — not used by Emerge)
    "TUBES", "FLAPS", "VALVES", "TPMS", "CHAINS",
    "ACC",
    "FTIMPFARM", "RBFARM1", "RBFARM2", "RRFARM1", "RRFARM2",
    "TURF", "TURF2",
    "OTRS", "OTRM",
    "SKID", "SKID2",
    "FORK",
    "MTB",
    "LOSPD",
}

# Wheel-specific: keep only rows whose product_type_code is in this set.
# These are the standard Costar wheel codes (same as NC/Formula).
# If Emerge uses different codes, they will be printed at runtime and can be added.
WHEEL_VALID_CODES = {"WHL", "WHLREP", "WHLT", "WHLW", "WHLWS"}


def find_input_file(product_type: str) -> str:
    pattern = f"Data/raw/emergetires_{product_type}_*.csv"
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not matches:
        print(f"ERROR: No emergetires_{product_type}_*.csv found in Data/raw/")
        print("Run scraper_tires.py first.")
        sys.exit(1)
    return matches[0]


def format_price(val) -> str:
    try:
        return f"{float(val):.2f}"
    except (ValueError, TypeError):
        return val


_DESC_SIZE_PATTERNS = [
    re.compile(r'\b((?:LT|ST|P)?\d{3}/\d{2,3}[A-Za-z]*R\d{2,3})', re.IGNORECASE),
    re.compile(r'\b((?:LT|ST)?\d{2,3}[Xx]\d{1,2}(?:\.\d+)?R\d{2,3})', re.IGNORECASE),
    re.compile(r'\b((?:LT|ST|P)?\d{3}/\d{2,3}/\d{2,3})', re.IGNORECASE),
]

# Matches WIDTH/RATIORIM with no R separator (e.g. "245/5019" → 245/50R19)
_DESC_NO_R_PATTERN = re.compile(r'\b((?:LT|ST|P)?\d{3})/(\d{2})(\d{2})\b', re.IGNORECASE)


def extract_size_from_description(description: str) -> str:
    """Recover a human-readable size when the size column has no separators.

    Costar sometimes sends `size` as bare concatenated digits (e.g. "2853522"
    for 285/35R22) with no slash/dash/X. `description` reliably carries the
    properly formatted size, so prefer that over the squashed digit string.
    """
    for pat in _DESC_SIZE_PATTERNS:
        m = pat.search(description)
        if m:
            return m.group(1).upper()
    # Handle WIDTH/RATIORIM where the R is missing (e.g. "245/5019" → "245/50R19")
    m = _DESC_NO_R_PATTERN.search(description)
    if m:
        return f"{m.group(1).upper()}/{m.group(2)}R{m.group(3)}"
    return ""


def normalize_size(size: str, description: str = "") -> str:
    """Convert non-standard size formats to WIDTH/RATIORRIM."""
    size = size.strip()
    # Dash format: LT225-75-16 → LT225/75R16
    m = re.match(r'^(LT|ST|P)?(\d+)-(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$', size, re.IGNORECASE)
    if m:
        prefix = (m.group(1) or "").upper()
        return f"{prefix}{m.group(2)}/{m.group(3)}R{m.group(4)}"
    # Slash-triplet: 255/35/19 → 255/35R19
    m = re.match(r'^(LT|ST|P)?(\d+)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)$', size, re.IGNORECASE)
    if m:
        prefix = (m.group(1) or "").upper()
        return f"{prefix}{m.group(2)}/{m.group(3)}R{m.group(4)}"
    # Slash + concatenated ratio+rim without R: 245/5019 → 245/50R19
    m = re.match(r'^((?:LT|ST|P)?\d{3})/(\d{2})(\d{2})$', size, re.IGNORECASE)
    if m:
        return f"{m.group(1).upper()}/{m.group(2)}R{m.group(3)}"
    if not re.search(r'[/Xx]', size):
        # Try description first (handles "245/5019" type in description text)
        extracted = extract_size_from_description(description)
        if extracted:
            return extracted
        # Last resort: fully concatenated WIDTHRATIORIM (e.g. "2455019" → "245/50R19")
        m = re.match(r'^((?:LT|ST|P)?)(\d{3})(\d{2})(\d{2})$', size, re.IGNORECASE)
        if m:
            prefix = (m.group(1) or "").upper()
            return f"{prefix}{m.group(2)}/{m.group(3)}R{m.group(4)}"
    return size


def strip_snowflake_marker(text: str) -> str:
    """Remove Costar's literal '*SNOWFLAKE*' text marker from model/description.

    This is a raw-feed artifact (not present in other suppliers' data) — the
    3-peak-snowflake designation is already surfaced via the Winter/All-Weather
    label logic in augment_tires.py, so the literal marker is just noise.
    """
    text = re.sub(r'\*\s*SNOWFLAKE\s*\*', '', text, flags=re.IGNORECASE)
    return re.sub(r'\s{2,}', ' ', text).strip()


def clean_wheels():
    """Clean wheel scrape data: filter by WHEEL_VALID_CODES, qty>=4, zero-price, SKU dedup."""
    input_file = find_input_file("wheels")
    print(f"Input  : {input_file}")
    print(f"Type   : wheels")
    check_input_freshness(input_file)

    with open(input_file, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows   = list(reader)

    print(f"Rows loaded: {len(rows)}")
    raw_count = len(rows)

    all_codes = sorted({r.get("product_type_code", "") for r in rows})
    print(f"product_type_codes found: {all_codes}")

    removed = {
        "code_filter":   0,
        "inactive":      0,
        "zero_price":    0,
        "no_price":      0,
        "low_qty":       0,
        "duplicate_sku": 0,
    }

    # ── 1. Filter by WHEEL_VALID_CODES ────────────────────────────────────────
    # Check if any row has a product_type_code column populated
    has_code_col = any(r.get("product_type_code", "").strip() for r in rows)
    if not has_code_col:
        print("  NOTE: product_type_code field is blank/missing for all rows — skipping code filter, keeping all rows.")
        rows_filtered = rows
    else:
        rows_filtered = []
        for r in rows:
            code = r.get("product_type_code", "").strip().upper()
            if not code or code in WHEEL_VALID_CODES:
                rows_filtered.append(r)
            else:
                removed["code_filter"] += 1
        if removed["code_filter"] > 0:
            print(f"  Removed {removed['code_filter']} rows with non-wheel product_type_code.")
            kept_codes = sorted({r.get("product_type_code","").strip().upper() for r in rows_filtered if r.get("product_type_code","").strip()})
            print(f"  Kept codes: {kept_codes}")
        else:
            print(f"  All rows have valid wheel codes (or blank code field) — none removed by code filter.")
    rows = rows_filtered

    # ── 2. Remove inactive products ───────────────────────────────────────────
    rows_filtered = [
        r for r in rows
        if str(r.get("is_active", "True")).strip().lower() not in ("false", "0", "")
    ]
    removed["inactive"] = len(rows) - len(rows_filtered)
    rows = rows_filtered

    # ── 3. Remove zero-price products ─────────────────────────────────────────
    def has_price(r):
        try:
            return float(r.get("retail_price", 0) or 0) > 0
        except (ValueError, TypeError):
            return False

    rows_filtered = [r for r in rows if has_price(r)]
    removed["zero_price"] = len(rows) - len(rows_filtered)
    rows = rows_filtered

    # ── 4. Qty >= 4 filter ────────────────────────────────────────────────────
    def get_qty(r) -> float:
        def _parse(val):
            try:
                return float(val) if str(val or "").strip() else 0.0
            except (ValueError, TypeError):
                return 0.0
        return max(
            _parse(r.get("available_quantity_related_branches")),
            _parse(r.get("available_quantity")),
        )

    low_qty_rows  = []
    rows_filtered = []
    for r in rows:
        if get_qty(r) >= LOW_QTY_THRESHOLD:
            rows_filtered.append(r)
        else:
            low_qty_rows.append(r)
            removed["low_qty"] += 1
    rows = rows_filtered

    # ── 5. Deduplicate by SKU — keep highest qty per SKU ──────────────────────
    seen_skus   = {}
    no_sku_rows = []
    for r in rows:
        sku = r.get("sku", "").strip()
        if not sku:
            no_sku_rows.append(r)
            continue
        if sku not in seen_skus or get_qty(r) > get_qty(seen_skus[sku]):
            seen_skus[sku] = r

    before_dedup       = len(rows)
    rows               = list(seen_skus.values()) + no_sku_rows
    removed["duplicate_sku"] = before_dedup - len(rows)

    if no_sku_rows:
        print(f"  NOTE: {len(no_sku_rows)} rows have blank SKU — kept aside.")

    # ── 6. Cost price — quarantine rows we cannot price ─────────────────────
    # Backfilling from retail_price kept the row but priced it off the wrong
    # basis (retail + markup, ~1.7x the correct figure). Rows with no usable
    # cost are now excluded from the final dataset and written to the
    # repo-level "no price detected" folder instead. A cost column that is
    # missing entirely is a pre-migration scrape, not bad data, so those rows
    # are kept and the loud downstream fallback still applies.
    rows, _, no_price_summary = no_price.split_records(rows, "emergetires", "wheels")
    no_price.report(no_price_summary, "emergetires", "wheels")
    has_cost_col = not no_price_summary["column_absent"]
    removed["no_price"] = no_price_summary["quarantined"]

    # ── 7. Format prices ──────────────────────────────────────────────────────
    for r in rows:
        r["retail_price"]    = format_price(r.get("retail_price", ""))
        if has_cost_col:
            r["cost_price"]  = format_price(r.get("cost_price", ""))
        r["suggested_price"] = format_price(r.get("suggested_price", ""))

    # ── Output ────────────────────────────────────────────────────────────────
    os.makedirs("Data/cleaned", exist_ok=True)
    ts           = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file  = f"Data/cleaned/cleaned_wheels_{ts}.csv"
    low_qty_file = f"Data/cleaned/low_quantity_wheels.csv"

    fieldnames = list(rows[0].keys()) if rows else []
    if not rows:
        print("WARNING: No rows remain after filtering — check cookie/scrape validity.")
        sys.exit(1)

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
    print(f"  Code filter (non-wheel product_type_code) : {removed['code_filter']}")
    print(f"  Inactive products                         : {removed['inactive']}")
    print(f"  Zero-price products                       : {removed['zero_price']}")
    print(f"  No cost price detected                    : {removed['no_price']}")
    print(f"  Low quantity (qty < {LOW_QTY_THRESHOLD})                   : {removed['low_qty']}")
    print(f"  Duplicate SKUs                            : {removed['duplicate_sku']}")
    print(f"\nRaw rows      : {raw_count}")
    print(f"Final rows    : {len(rows)}")
    print(f"Output        : {output_file}")
    if low_qty_rows:
        print(f"Low qty ref   : {low_qty_file}")
    print("Done.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["tires", "wheels"], required=True)
    args         = parser.parse_args()
    product_type = args.type

    if product_type == "wheels":
        clean_wheels()
        return

    input_file = find_input_file(product_type)
    print(f"Input  : {input_file}")
    print(f"Type   : {product_type}")
    check_input_freshness(input_file)

    with open(input_file, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows   = list(reader)

    print(f"Rows loaded: {len(rows)}")
    raw_count = len(rows)

    # Print distinct product_type_code values so user can spot Emerge-specific codes
    all_codes = sorted({r.get("product_type_code", "") for r in rows})
    print(f"product_type_codes found: {all_codes}")
    print("  >> Review the list above and add any Emerge-specific accessory codes to")
    print("     TIRE_ACCESSORY_CODES before final production run (Phase 4 / OQ-6).")

    removed = {
        "accessories":   0,
        "inactive":      0,
        "zero_price":    0,
        "no_price":      0,
        "low_qty":       0,
        "duplicate_sku": 0,
    }

    # ── 1. Remove accessories / non-product rows by product_type_code ─────────
    rows_filtered = []
    for r in rows:
        code = r.get("product_type_code", "").strip().upper()
        if code not in TIRE_ACCESSORY_CODES:
            rows_filtered.append(r)
        else:
            removed["accessories"] += 1
    rows = rows_filtered

    # ── 2. Remove inactive products ───────────────────────────────────────────
    rows_filtered = [
        r for r in rows
        if str(r.get("is_active", "True")).strip().lower() not in ("false", "0", "")
    ]
    removed["inactive"] = len(rows) - len(rows_filtered)
    rows = rows_filtered

    # ── 3. Remove zero-price products ─────────────────────────────────────────
    def has_price(r):
        try:
            return float(r.get("retail_price", 0) or 0) > 0
        except (ValueError, TypeError):
            return False

    rows_filtered = [r for r in rows if has_price(r)]
    removed["zero_price"] = len(rows) - len(rows_filtered)
    rows = rows_filtered

    # ── 4. Qty >= 4 filter ────────────────────────────────────────────────────
    def get_qty(r) -> float:
        """Return the higher of available_quantity and available_quantity_related_branches.
        Phase 0 finding: available_quantity_related_branches is always 0.0 for Emerge
        (qty lives entirely in available_quantity = Delta branch stock).
        max() is kept for safety in case future data differs.
        """
        def _parse(val):
            try:
                return float(val) if str(val or "").strip() else 0.0
            except (ValueError, TypeError):
                return 0.0
        return max(
            _parse(r.get("available_quantity_related_branches")),
            _parse(r.get("available_quantity")),
        )

    low_qty_rows  = []
    rows_filtered = []
    for r in rows:
        if get_qty(r) >= LOW_QTY_THRESHOLD:
            rows_filtered.append(r)
        else:
            low_qty_rows.append(r)
            removed["low_qty"] += 1
    rows = rows_filtered

    # ── 5. Deduplicate by SKU — keep highest qty per SKU ──────────────────────
    seen_skus   = {}
    no_sku_rows = []
    for r in rows:
        sku = r.get("sku", "").strip()
        if not sku:
            no_sku_rows.append(r)
            continue
        if sku not in seen_skus or get_qty(r) > get_qty(seen_skus[sku]):
            seen_skus[sku] = r

    before_dedup       = len(rows)
    rows               = list(seen_skus.values()) + no_sku_rows
    removed["duplicate_sku"] = before_dedup - len(rows)

    if no_sku_rows:
        print(f"  NOTE: {len(no_sku_rows)} rows have blank SKU — kept aside, flagged in QA.")

    # ── 6. Strip raw-feed artifacts and normalise sizes ───────────────────────
    for r in rows:
        r["model"]       = strip_snowflake_marker(r.get("model", ""))
        r["description"] = strip_snowflake_marker(r.get("description", ""))
        r["size"] = normalize_size(r.get("size", ""), r.get("description", ""))
        r["size"] = r["size"].replace("x", "X")

    # ── 7. Cost price — quarantine rows we cannot price ─────────────────────
    # Backfilling from retail_price kept the row but priced it off the wrong
    # basis (retail + markup, ~1.7x the correct figure). Rows with no usable
    # cost are now excluded from the final dataset and written to the
    # repo-level "no price detected" folder instead. A cost column that is
    # missing entirely is a pre-migration scrape, not bad data, so those rows
    # are kept and the loud downstream fallback still applies.
    rows, _, no_price_summary = no_price.split_records(rows, "emergetires", product_type)
    no_price.report(no_price_summary, "emergetires", product_type)
    has_cost_col = not no_price_summary["column_absent"]
    removed["no_price"] = no_price_summary["quarantined"]

    # ── 8. Format prices ──────────────────────────────────────────────────────
    for r in rows:
        r["retail_price"]    = format_price(r.get("retail_price", ""))
        if has_cost_col:
            r["cost_price"]  = format_price(r.get("cost_price", ""))
        r["suggested_price"] = format_price(r.get("suggested_price", ""))

    # ── Output ────────────────────────────────────────────────────────────────
    os.makedirs("Data/cleaned", exist_ok=True)
    ts           = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file  = f"Data/cleaned/cleaned_{product_type}_{ts}.csv"
    low_qty_file = f"Data/cleaned/low_quantity_{product_type}.csv"

    fieldnames = list(rows[0].keys()) if rows else []
    if not rows:
        print("WARNING: No rows remain after filtering — check cookie/scrape validity.")
        sys.exit(1)

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
    print(f"  No cost price detected                 : {removed['no_price']}")
    print(f"  Low quantity (qty < {LOW_QTY_THRESHOLD})              : {removed['low_qty']}")
    print(f"  Duplicate SKUs                         : {removed['duplicate_sku']}")
    print(f"\nRaw rows      : {raw_count}")
    print(f"Final rows    : {len(rows)}")
    print(f"Output        : {output_file}")
    if low_qty_rows:
        print(f"Low qty ref   : {low_qty_file}")
    print("Done.")


if __name__ == "__main__":
    main()
