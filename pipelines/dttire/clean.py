#!/usr/bin/env python3
"""DT Tire — Data Cleaner
Reads the most recent raw scrape output from Data/raw/ and produces:

  Tires (default, --type tires):
    - Data/cleaned/cleaned_tires_TIMESTAMP.csv      (passenger / LT / ST, qty_total >= 4)
    - Data/cleaned/low_quantity_tires_TIMESTAMP.csv  (qty_total <= 4)
    - Data/cleaned/excluded_tires_TIMESTAMP.csv      (farm / OTR / commercial truck — for audit)

  Wheels (--type wheels):
    - Data/cleaned/cleaned_wheels_TIMESTAMP.csv     (qty_total >= 4, valid cost_price)
    - Data/cleaned/low_quantity_wheels_TIMESTAMP.csv (qty_total <= 4)

Run from pipelines/dttire/ as working directory:
    python clean.py
    python clean.py --type wheels
"""

import os
import re
import sys
import time
import argparse
import datetime
import pandas as pd

from config import find_latest, get_logger

LOW_QTY_THRESHOLD = 3

# Above this age, the "latest" raw file found on disk is old enough that it's
# more likely a stale leftover (scraper step silently produced nothing this
# run) than genuinely fresh input. clean.py still uses it — a full pipeline
# run legitimately reuses existing raw data via --skip scraper_* — but the
# age must be flagged loudly rather than silently accepted (this is exactly
# how a 12-day-old raw file got used for a wheels clean run with no warning).
STALE_INPUT_WARN_HOURS = 24


def _check_input_freshness(input_file, log):
    age_hours = (time.time() - os.path.getmtime(input_file)) / 3600
    if age_hours > STALE_INPUT_WARN_HOURS:
        log.warning(
            f"Input file {input_file} is {age_hours / 24:.1f} day(s) old "
            f"(> {STALE_INPUT_WARN_HOURS}h threshold). Proceeding, but this "
            f"usually means the scraper step didn't produce a fresh file this "
            f"run — verify before trusting this output."
        )

# ---------------------------------------------------------------------------
# Tire-type filter — keep passenger, LT, ST only
# ---------------------------------------------------------------------------

# Performance categories that are definitively non-passenger/LT/ST
_EXCL_PERF_EXACT = {
    "Turf",
    "Steer",
    "Drive",
    "Medium duty trailer",
    "Forklift tire",
    "All-terrain vehicle",
}

# Performance substrings that indicate farm / OTR / commercial truck
_EXCL_PERF_SUBS = [
    "farm", "ag.", "ag ", "agricultural", "ind. ag",
    "lawn", "turf",
    "forklift", "skid steer", "backhoe",
    "long haul", "steer / ap", "regional - steer",
    "all positions - steer", "all positions - trailer",
]

# Size patterns to KEEP
_KEEP_METRIC    = re.compile(
    r'^(?:P|LT|ST|HL|T)?\d{3}/\d{2,3}', re.IGNORECASE
)  # 225/65R17, LT265/70R17, P225/45R17, HL285/30R21, T125/70R17 (spare)
_KEEP_FLOTATION = re.compile(
    r'^(?:LT|ST)?\d{2,3}[Xx]\d+(?:\.\d+)?[Rr]\d+', re.IGNORECASE
)  # 33X12.50R17, LT30X9.50R15, ST235X80R16
_KEEP_NO_RATIO  = re.compile(
    r'^(?:P|LT|ST|T)?\d{2,3}[Rr]\d{2}C?$', re.IGNORECASE
)  # 155R12C, 185R14C, 195R15C (van), ST145R12 (trailer)

# Size patterns to EXCLUDE
_EXCL_DECIMAL_W = re.compile(r'^\d{1,2}\.\d+\s*[-Rr]', re.IGNORECASE)  # 14.9-28, 18.4R38
_EXCL_LARGE_L   = re.compile(r'^\d{2,3}L[LRr-]', re.IGNORECASE)        # 28L-26, 28LR26, 28LL-26
_EXCL_SKID      = re.compile(r'^\d{1,2}[-.]?\d*-\d{2}', re.IGNORECASE) # 12-16.5, 8-14.5
_EXCL_AG_PREFIX = re.compile(r'^(?:LSW|VF|IF)\d+', re.IGNORECASE)      # LSW380/70R28, VF600/65R28
_RIM_HALF       = re.compile(r'[RrDd](\d+\.\d+)')                       # extract rim like 17.5, 22.5 (R or D construction)

# Agricultural metric: width >= 340mm — not applied to LT-prefixed sizes
_METRIC_WIDTH   = re.compile(r'^(?:P|LT|ST|HL|T)?(\d{3})/', re.IGNORECASE)

# Performance categories that override the ag_width check (wide passenger tires for exotic vehicles)
_UHP_PERFS = {
    "ultra high performance", "extreme performance", "high performance",
    "race - autocross", "race - slick", "race - rain",
}


def _has_half_rim(size: str) -> bool:
    """Return True if size contains a .5 rim diameter (commercial truck only)."""
    m = _RIM_HALF.search(size)
    return bool(m)


def is_keep(row) -> tuple:
    """Return (True, reason) to keep or (False, reason) to exclude."""
    size = str(row.get("size", "")).strip()
    perf_raw = str(row.get("performance", "")).strip()
    perf = perf_raw.lower()

    # --- Performance-based exclusion ---
    if perf_raw in _EXCL_PERF_EXACT:
        return False, f"perf_exact='{perf_raw}'"
    if any(sub in perf for sub in _EXCL_PERF_SUBS):
        return False, f"perf_sub='{perf_raw}'"

    # --- Size-based exclusion ---
    if _EXCL_DECIMAL_W.match(size):
        return False, f"decimal_ag size='{size}'"
    if _EXCL_LARGE_L.match(size):
        return False, f"large_L size='{size}'"
    if _EXCL_SKID.match(size):
        return False, f"skid/drive size='{size}'"
    if _EXCL_AG_PREFIX.match(size):
        return False, f"ag_prefix size='{size}'"

    size_upper = size.upper()

    # Commercial truck: .5 rim sizes (17.5, 19.5, 22.5, 24.5)
    # LT-prefixed sizes are exempt — 16.5" rims appear on older HD pickups (LT37X12.50R16.5)
    if _has_half_rim(size) and not size_upper.startswith("LT"):
        return False, f"commercial_rim size='{size}'"

    # Agricultural metric with oversized width (>= 340mm)
    # LT-prefixed sizes are exempt (LT375/45R22 is a valid mud-terrain truck tire)
    # UHP/race performance categories are exempt (345/30R21 Potenza Sport is a passenger car tire)
    mw = _METRIC_WIDTH.match(size)
    if mw and int(mw.group(1)) >= 340 and not size_upper.startswith("LT"):
        if perf not in _UHP_PERFS:
            return False, f"ag_width size='{size}'"

    # --- Size-based keep ---
    if _KEEP_METRIC.match(size) or _KEEP_FLOTATION.match(size) or _KEEP_NO_RATIO.match(size):
        return True, "size_ok"

    # Ambiguous size with a recognised passenger/LT/commercial performance category → keep
    _KEEP_PERF_HINTS = {
        "passenger", "light truck", "trailer", "sport", "touring", "winter",
        "all season", "all weather", "highway", "all terrain", "mud",
        "performance", "rugged", "recreational", "commercial",
    }
    if any(h in perf for h in _KEEP_PERF_HINTS):
        return True, f"perf_hint='{perf_raw}'"

    return False, f"unknown_size='{size}'"


# ---------------------------------------------------------------------------
# Designation inference: season → performance → model keywords → size fallback
# ---------------------------------------------------------------------------

_SEASON_TO_DESIG = {
    "Winter":      "Winter",
    "All-Season":  "All Season",
    "All-Weather": "All Season",
    "Summer":      "Summer",
}

_WINTER_PERF_SUBS  = ["winter", "ice and snow", "studdable", "studded", "nordic"]
_SUMMER_PERF_SUBS  = ["summer", "high performance", "ultra high", "extreme performance",
                       "race - slick", "race - autocross", "race - rain"]
_ALLSEAS_PERF_SUBS = ["all season", "all-season", "all weather", "all-weather",
                       "touring", "highway", "all terrain", "all-terrain",
                       "mud terrain", "mud and snow", "mud & snow",
                       "rugged terrain", "recreational trailer", "temporary",
                       "light truck", "commercial"]

_WINTER_MODEL  = re.compile(
    r'\b(winter|ice(?:guard|bear|fighter)?|blizzak|nordman|arctic|snowflake|'
    r'winterforce|sottozero|pilot\s*alpin|wintercontact|wintersport|'
    r'nivopi|snowprox|icelander|direzza.{0,2}winter|v[.\-]winter|'
    r'altimax\s*win|x[\-.]ice|x[\-. ]ice)\b',
    re.IGNORECASE,
)
_ALLSEAS_MODEL = re.compile(
    r'\b(all[\-. ]?season|all[\-. ]?weather|(?<!\w)a/s(?!\w)|'
    r'touring|h/t|highway|all[\-. ]?terrain|(?<!\w)a/t(?!\w)|'
    r'cross[\-. ]terrain|crossover|suv)\b',
    re.IGNORECASE,
)
_SUMMER_MODEL  = re.compile(
    r'\b(summer|pilot\s*sport\s*[45]\b|ps\d\s*s\b|'
    r'potenza(?!.*(?:a/s|re97|all\s*season))|'
    r'eagle\s*f1\s*(?!a/s)|p\s*zero(?!\s*(?:as|all)))\b',
    re.IGNORECASE,
)


def infer_designation(row) -> str:
    """
    Derive seasonal designation using a four-layer fallback:
      1. season column (portal-provided)
      2. performance column keyword match
      3. model name keyword match
      4. size structural type (ST → All Season)
    """
    # 1. season column
    season = str(row.get("season", "")).strip()
    if season in _SEASON_TO_DESIG:
        return _SEASON_TO_DESIG[season]

    # 2. performance column
    perf = str(row.get("performance", "")).strip().lower()
    if any(k in perf for k in _WINTER_PERF_SUBS):
        return "Winter"
    if any(k in perf for k in _SUMMER_PERF_SUBS):
        return "Summer"
    if any(k in perf for k in _ALLSEAS_PERF_SUBS):
        return "All Season"

    # 3. model name keywords
    model = str(row.get("model", "")).strip()
    if _WINTER_MODEL.search(model):
        return "Winter"
    if _ALLSEAS_MODEL.search(model):
        return "All Season"
    if _SUMMER_MODEL.search(model):
        return "Summer"

    # 4. structural size fallback
    size = str(row.get("size", "")).upper().strip()
    if size.startswith("ST"):
        return "All Season"
    if _KEEP_FLOTATION.match(size):
        return "All Season"  # UTV/ATV/LT flotation tires with no other signal default to all-season

    return "Unknown"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_model(brand, model):
    """Strip leading brand name and trim dashes/spaces."""
    if pd.isna(model):
        return model
    result = str(model).strip()
    brand_str = str(brand).strip() if not pd.isna(brand) else ""
    if brand_str and result.upper().startswith(brand_str.upper()):
        result = result[len(brand_str):].strip()
    return result.strip("- ").strip()


def parse_price(val):
    """Extract first numeric price from value; handles concatenated format like '157.68$178.12'."""
    if pd.isna(val):
        return None
    m = re.match(r'\$?(\d+(?:\.\d+)?)', str(val).strip().replace(",", ""))
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="DT Tire data cleaner")
    parser.add_argument(
        "--type", dest="product_type", choices=["tires", "wheels"], default="tires",
        help="Product type to clean (default: tires)",
    )
    args = parser.parse_args()

    if args.product_type == "wheels":
        _clean_wheels()
        return

    _clean_tires()


def _clean_tires():
    log = get_logger("dttire_clean")

    # ── Locate input ───────────────────────────────────────────────────────────
    pattern = os.path.join("Data", "raw", "dttire_tires_*.csv")
    input_file = find_latest(pattern)
    if not input_file:
        log.error(f"No file matching {pattern}. Run scraper_tires.py first.")
        sys.exit(1)
    log.info(f"Input file : {input_file}")
    _check_input_freshness(input_file, log)

    # ── Load ───────────────────────────────────────────────────────────────────
    df = pd.read_csv(input_file, dtype=str, low_memory=False)
    rows_in = len(df)
    log.info(f"Rows loaded: {rows_in}")

    # ── Parse prices ───────────────────────────────────────────────────────────
    df["cost_price"] = df["cost_price"].apply(parse_price)
    df["msrp"]       = df["msrp"].apply(parse_price)
    unparseable = df["cost_price"].isna() | df["msrp"].isna()
    if unparseable.any():
        log.warning(f"Dropping {unparseable.sum()} row(s) with unparseable prices")
        df = df[~unparseable].copy()

    # ── Normalise qty ──────────────────────────────────────────────────────────
    df["qty_total"] = pd.to_numeric(df["qty_total"], errors="coerce").fillna(0)

    # ── SKU dedup ──────────────────────────────────────────────────────────────
    rows_before_dedup = len(df)
    df = df.drop_duplicates(subset="sku", keep="first").copy()
    dupes_dropped = rows_before_dedup - len(df)
    if dupes_dropped:
        log.info(f"Duplicate SKUs dropped: {dupes_dropped}")

    # ── Filter: passenger / LT / ST only ─────────────────────────────────────
    keep_results  = df.apply(lambda r: is_keep(r), axis=1)
    keep_mask     = keep_results.apply(lambda x: x[0])
    excl_df       = df[~keep_mask].copy()
    excl_df["_excl_reason"] = keep_results[~keep_mask].apply(lambda x: x[1]).values
    df            = df[keep_mask].copy()

    rows_excluded = len(excl_df)
    log.info(f"Excluded (farm/OTR/commercial truck): {rows_excluded}")
    if rows_excluded:
        reason_counts = excl_df["_excl_reason"].str.extract(r'^([^=]+)')[0].value_counts()
        log.info(f"Exclusion reasons:\n{reason_counts.to_string()}")

    # ── Split low-quantity ────────────────────────────────────────────────────
    low_mask   = df["qty_total"] <= LOW_QTY_THRESHOLD
    low_qty_df = df[low_mask].copy()
    main_df    = df[~low_mask].copy()

    rows_removed = len(low_qty_df)
    rows_out     = len(main_df)

    if rows_out == 0:
        log.error(f"0 rows survive cleaning ({rows_in} loaded, {rows_excluded} excluded, "
                   f"{rows_removed} low-qty) — refusing to write an empty cleaned_tires CSV. "
                   f"Check the raw scrape before re-running.")
        sys.exit(1)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs("Data/cleaned", exist_ok=True)

    excl_file    = os.path.join("Data", "cleaned", f"excluded_tires_{ts}.csv")
    low_qty_file = os.path.join("Data", "cleaned", f"low_quantity_tires_{ts}.csv")
    excl_df.to_csv(excl_file, index=False)
    low_qty_df.to_csv(low_qty_file, index=False)
    log.info(f"Excluded saved  : {excl_file}")
    log.info(f"Low qty saved   : {low_qty_file}")

    # ── Clean model column ────────────────────────────────────────────────────
    main_df["model"] = main_df.apply(
        lambda r: clean_model(r["brand"], r["model"]), axis=1
    )

    # ── Derive designation ────────────────────────────────────────────────────
    main_df["designation"] = main_df.apply(infer_designation, axis=1)

    desig_counts  = main_df["designation"].value_counts(dropna=False)
    unknown_count = (main_df["designation"] == "Unknown").sum()
    log.info(f"Designation breakdown:\n{desig_counts.to_string()}")
    if unknown_count:
        log.warning(f"{unknown_count} row(s) remain Unknown after all fallbacks")
        sample = main_df[main_df["designation"] == "Unknown"][
            ["sku", "brand", "model", "season", "performance", "size"]
        ].head(10)
        log.warning(f"Unknown sample:\n{sample.to_string()}")

    # ── Write output ──────────────────────────────────────────────────────────
    output_file = os.path.join("Data", "cleaned", f"cleaned_tires_{ts}.csv")
    main_df.to_csv(output_file, index=False)
    log.info(f"Output file : {output_file}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\nRows in            : {rows_in}")
    print(f"Excluded (non-P/LT/ST) : {rows_excluded}")
    print(f"Low qty removed    : {rows_removed}  (qty_total <= {LOW_QTY_THRESHOLD})")
    print(f"Rows out (cleaned) : {rows_out}")
    print(f"Designation breakdown:")
    for desig, cnt in desig_counts.items():
        print(f"  {desig}: {cnt}")
    print(f"\nOutput   : {output_file}")
    print(f"Excluded : {excl_file}")
    print(f"Low qty  : {low_qty_file}")
    print("Done.")


def _clean_wheels():
    """Clean raw wheel scrape: dedup by SKU, drop zero/missing price, split low-qty."""
    log = get_logger("dttire_clean_wheels")

    # ── Locate input ───────────────────────────────────────────────────────────
    pattern = os.path.join("Data", "raw", "dttire_wheels_*.csv")
    input_file = find_latest(pattern)
    if not input_file:
        log.error(f"No file matching {pattern}. Run scraper_wheels.py first.")
        sys.exit(1)
    log.info(f"Input file : {input_file}")
    _check_input_freshness(input_file, log)

    # ── Load ───────────────────────────────────────────────────────────────────
    df = pd.read_csv(input_file, dtype=str, low_memory=False)
    rows_in = len(df)
    log.info(f"Rows loaded: {rows_in}")

    # ── SKU dedup ──────────────────────────────────────────────────────────────
    df = df.drop_duplicates(subset="sku", keep="first").copy()
    rows_after_dedup = len(df)
    dupes_dropped = rows_in - rows_after_dedup
    if dupes_dropped:
        log.info(f"Duplicate SKUs dropped: {dupes_dropped}")

    # ── Parse prices ───────────────────────────────────────────────────────────
    df["cost_price"] = df["cost_price"].apply(parse_price)
    df["msrp"]       = df["msrp"].apply(parse_price)

    # Drop rows with missing or zero cost_price
    bad_price = df["cost_price"].isna() | (df["cost_price"] == 0)
    if bad_price.any():
        log.warning(f"Dropping {bad_price.sum()} row(s) with missing/zero cost_price")
        df = df[~bad_price].copy()

    # ── Normalise qty ──────────────────────────────────────────────────────────
    df["qty_total"] = pd.to_numeric(df["qty_total"], errors="coerce").fillna(0)

    # ── Split low-quantity ────────────────────────────────────────────────────
    low_mask   = df["qty_total"] <= LOW_QTY_THRESHOLD
    low_qty_df = df[low_mask].copy()
    main_df    = df[~low_mask].copy()

    rows_removed = len(low_qty_df)
    rows_out     = len(main_df)

    if rows_out == 0:
        log.error(f"0 rows survive cleaning ({rows_in} loaded, {rows_removed} low-qty/bad-price) "
                   f"— refusing to write an empty cleaned_wheels CSV. "
                   f"Check the raw scrape before re-running.")
        sys.exit(1)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs("Data/cleaned", exist_ok=True)

    low_qty_file = os.path.join("Data", "cleaned", f"low_quantity_wheels_{ts}.csv")
    low_qty_df.to_csv(low_qty_file, index=False)
    log.info(f"Low qty saved   : {low_qty_file}")

    # ── Write output ──────────────────────────────────────────────────────────
    output_file = os.path.join("Data", "cleaned", f"cleaned_wheels_{ts}.csv")
    main_df.to_csv(output_file, index=False)
    log.info(f"Output file : {output_file}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\nRows in           : {rows_in}")
    print(f"Dupes dropped     : {dupes_dropped}")
    print(f"Low qty removed   : {rows_removed}  (qty_total <= {LOW_QTY_THRESHOLD})")
    print(f"Rows out (cleaned): {rows_out}")
    print(f"\nOutput   : {output_file}")
    print(f"Low qty  : {low_qty_file}")
    print("Done.")


if __name__ == "__main__":
    main()
