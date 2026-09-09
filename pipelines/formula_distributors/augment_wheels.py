#!/usr/bin/env python3
"""Formula Wheels — Shopify SEO Augmentation
Reads Data/formula_wheels_*.csv and outputs Data/Shopify_SEO_wheels.csv.

Key differences from augment.py (tires):
  - SKU goes in "Variant SKU" (not "SKU") to match create_products.py --type wheels
  - Price = retail_price + per-supplier markup (markup applied here, not in update_prices.py)
  - Bolt pattern, diameter, offset, hub bore parsed from size + description fields
  - Title includes bolt pattern to keep products with same model/size unique
"""

import sys
import glob
import os
import re
import logging
import pandas as pd
from datetime import datetime

from config import price_with_markup

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared')))
from size_parser import parse_wheel_size, format_wheel_size
from dedupe import dedupe_titles

MARKUP_SUPPLIER = "formula"
# Keep qty >= 4, matching every other supplier. This was 3, which quietly let
# formula wheels onto the store at a stock level every other feed treats as too
# low to list. See northcountry/emergetires/formula clean.py and
# wheelsmart/augment_{tires,rims}.py, all of which keep qty >= 4; dttire spells
# the same rule as "drop qty <= 3".
LOW_QTY_THRESHOLD = 4


def get_output_file():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"Data/shopify/Shopify_SEO_wheels_{ts}.csv"

SHOPIFY_COLUMNS = [
    "Handle",
    "Title",
    "Body (HTML)",
    "Vendor",
    "Type",
    "Tags",
    "Published",
    "Option1 Name",
    "Option1 Value",
    "Variant SKU",
    "Variant Grams",
    "Variant Inventory Tracker",
    "Variant Inventory Policy",
    "Variant Fulfillment Service",
    "Variant Price",
    "Variant Requires Shipping",
    "Variant Taxable",
    "Image Src",
    "Image Position",
    "Gift Card",
    "SEO Title",
    "SEO Description",
    "Google Shopping / Google Product Category",
    "Google Shopping / Condition",
    "Google Shopping / Custom Product",
    "Google Shopping / MPN",
    "Product Type",
    "Brand",
    "Identifier Exist",
]

LOCATIONS = [
    "Burnaby", "Langley", "MapleRidge",
    "Kelowna", "Kamloops", "SalmonArm",
]


def get_logger():
    os.makedirs("logs", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = f"logs/augment_wheels_{ts}.log"
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()]
    )
    return logging.getLogger("augment_wheels")


def find_input_file():
    matches = sorted(
        glob.glob("Data/raw/formula_wheels_*.csv"),
        key=os.path.getmtime,
        reverse=True,
    )
    if not matches:
        print("ERROR: No formula_wheels_*.csv found in Data/raw/ — run scraper_wheels.py first.")
        sys.exit(1)
    return matches[0]


def slugify(text):
    text = str(text).lower().strip()
    # "+" carries real distinguishing meaning ("T/A" vs "T/A+" are different
    # products) but is not \w, so the strip below used to delete it silently --
    # both titles produced the identical Handle, and Shopify groups rows by
    # Handle, so importing them as-is would collide two different listings.
    text = text.replace("+", " plus ")  # spaces so the collapse step below
                                        # inserts proper hyphen boundaries
                                        # ("t-plus-h", not "t-plush")
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def parse_bolt_patterns(raw_bp, description=""):
    """Return list of normalized bolt patterns like ['5x114.3', '5x127'].
    Tries dedicated column first, falls back to parsing description.
    Handles both '/' and '-' as dual-fitment separators.
    """
    src = str(raw_bp).strip() if str(raw_bp).strip() else str(description)

    m = re.search(r'\b(\d)-([\d.]+)((?:[/\-][\d.]+)*)', src)
    if not m:
        return []

    lug  = m.group(1)
    rest = m.group(2) + m.group(3)
    raw_pcds = re.findall(r'[\d.]+', rest)

    result = []
    seen   = set()
    for pcd in raw_pcds:
        try:
            val = float(pcd)
            # Valid PCD range: 80–200 (covers 5x114.3 up to 8x170 truck patterns)
            if 80.0 <= val <= 200.0:
                # 114.3 stays; 127.0 → 127; 139.7 stays
                formatted = f"{val:.1f}".rstrip("0").rstrip(".")
                key = f"{lug}x{formatted}"
                if key not in seen:
                    result.append(key)
                    seen.add(key)
        except ValueError:
            pass
    return result


def parse_hub_bore(raw_hb, description=""):
    """Return hub bore string from column or 'CB83.7'/'CB 87' in description."""
    if str(raw_hb).strip():
        return str(raw_hb).strip()
    m = re.search(r'CB\s*([\d.]+)', str(description).upper())
    return m.group(1) if m else ""


def parse_offset(raw_offset, description=""):
    """Return offset as int. Non-zero column value wins; falls back to '+35'/'-12' in description."""
    try:
        val = float(str(raw_offset).strip() or "0")
        if val != 0.0:
            return int(val)
    except ValueError:
        pass
    m = re.search(r'([+\-]\d+)\b', str(description))
    return int(m.group(1)) if m else 0


def _safe_dia(val):
    """Return int diameter or None, handling NaN/None/float safely."""
    try:
        f = float(val)
        return None if f != f else int(f)
    except (TypeError, ValueError):
        return None


def build_body_html(row):
    diameter  = _safe_dia(row["_dia"])
    width     = row["_width"]
    bolts     = row["_bolts"]
    offset    = row["_offset"]
    hub_bore  = row["_hub_bore"]
    finish    = str(row.get("finish",   "")).strip().title()
    material  = str(row.get("material", "")).strip().title()

    bp_str = " / ".join(bolts) if bolts else ""

    lines = []
    if diameter is not None:
        lines.append(f"Wheel Diameter: {diameter}\"")
    if width is not None:
        lines.append(f"Wheel Width: {width:.1f}\"")
    if bp_str:
        lines.append(f"Bolt Pattern: {bp_str}")
    if offset:
        lines.append(f"Offset: {offset}mm")
    if hub_bore:
        lines.append(f"Centre Bore: {hub_bore}mm")
    if finish:
        lines.append(f"Finish: {finish}")
    if material:
        lines.append(f"Material: {material}")

    spec_block = "<br>".join(lines)

    return (
        f"{spec_block}<br><br>"
        f"Shop Premium Wheels &amp; Rims at Save Big On Tires | BC&#8217;s Trusted Specialists<br>"
        f"Upgrade your vehicle&#8217;s performance and style with premium wheels and rims from Save Big On Tires. "
        f"Whether you are searching for 17-inch, 18-inch, 20-inch, or 22-inch setups, our selection offers "
        f"the perfect fitment for cars, SUVs, and trucks.<br><br>"
        f"Professional Installation at 6 BC Locations<br>"
        f"Order your wheels online and choose professional mounting, balancing, and installation at any of our "
        f"specialized service centers:<br>"
        f"&#8226; Burnaby &#8226; Langley &#8226; Maple Ridge &#8226; Kelowna &#8226; Kamloops &#8226; Salmon Arm<br><br>"
        f"Expert Services &amp; Precision Fitment<br>"
        f"&#8226; TPMS Support: Professional sensor programming and installation for all makes and models.<br>"
        f"&#8226; 3D Wheel Alignment: Available at select locations to ensure your new wheels drive straight.<br>"
        f"&#8226; Real Expertise: We are BC&#8217;s dedicated tire and wheel experts&#8212;not a generalist oil change shop.<br>"
    )


def build_tags(row):
    parts = []

    brand = str(row.get("brand_name", "")).strip()
    if brand:
        parts.append(f"Brand_{brand.title()}")

    diameter = _safe_dia(row["_dia"])
    if diameter is not None:
        parts.append(f"WheelDia_{diameter}")

    size_fmt = format_wheel_size(str(row.get("size", "")))
    if size_fmt:
        parts.append(f"Size_{size_fmt}")

    for bp in row["_bolts"]:
        parts.append(f"BoltPattern_{bp}")

    for loc in LOCATIONS:
        parts.append(f"Location_{loc}")

    parts.append("Supplier_Formulatires")

    return ",".join(parts)


def build_title(row):
    brand    = str(row.get("brand_name", "")).strip()
    model    = str(row.get("model",      "")).strip()
    size_fmt = format_wheel_size(str(row.get("size", "")))
    bolts    = row["_bolts"]

    # Include bolt pattern so same model/size in different fitments gets a unique title
    bp_part = "/".join(bolts) if bolts else ""
    title = " ".join(filter(None, [brand, model, size_fmt, bp_part]))
    if not title:
        # Source row had no brand/model/size/bolts at all — Shopify rejects a
        # blank Title, so fall back to the SKU rather than dropping the row.
        title = f"Wheel {str(row.get('item', '')).strip()}".strip()
    return title


def build_seo_title(row, vendor):
    brand    = str(row.get("brand_name", "")).strip()
    model    = str(row.get("model",      "")).strip()
    size_fmt = format_wheel_size(str(row.get("size", "")))
    bolts    = row["_bolts"]
    dia      = _safe_dia(row["_dia"])
    dia_str  = f'{dia}"' if dia is not None else ""

    bp_str = "/".join(bolts) if bolts else ""
    base   = " ".join(filter(None, [brand, model, size_fmt, bp_str]))
    return f"Buy {base} {dia_str} Wheels | {vendor}".strip()


def build_seo_desc(row):
    brand    = str(row.get("brand_name", "")).strip()
    model    = str(row.get("model",      "")).strip()
    size_fmt = format_wheel_size(str(row.get("size", "")))
    bolts    = row["_bolts"]
    dia      = _safe_dia(row["_dia"])
    dia_str  = f'{dia}"' if dia is not None else ""

    bp_str    = " and ".join(bolts) if bolts else ""
    bp_phrase = f" with bolt pattern {bp_str}" if bp_str else ""

    return (
        f"{brand} {model} {size_fmt} {dia_str} wheels{bp_phrase}. "
        f"Available at Save Big On Tires across 6 BC locations — "
        f"Burnaby, Langley, Maple Ridge, Kelowna, Kamloops, and Salmon Arm."
    )


def main():
    log = get_logger()
    input_file = find_input_file()
    log.info(f"Input file : {input_file}")

    df = pd.read_csv(input_file, dtype=str, low_memory=False)
    df = df.fillna("")
    log.info(f"Rows loaded: {len(df)}")
    if len(df) == 0:
        log.error(f"{input_file} has 0 rows — refusing to write an empty Shopify SEO CSV. "
                   f"Check clean.py's output for this run before re-running augment.")
        sys.exit(1)

    # ── Derived columns (parse once, reuse in build functions) ────────────────
    def _parse_dia(s):
        d, _ = parse_wheel_size(s)
        return d

    def _parse_w(s):
        _, w = parse_wheel_size(s)
        return w

    # Store as Python int/float or None (not NaN) so build functions use simple `is not None`
    df["_dia"]   = df["size"].apply(lambda s: int(_parse_dia(s)) if _parse_dia(s) is not None else None)
    df["_width"] = df["size"].apply(lambda s: float(_parse_w(s)) if _parse_w(s) is not None else None)
    df["_bolts"]    = df.apply(lambda r: parse_bolt_patterns(r.get("bolt_pattern", ""), r.get("description", "")), axis=1)
    df["_hub_bore"] = df.apply(lambda r: parse_hub_bore(r.get("hub_bore", ""), r.get("description", "")), axis=1)
    df["_offset"]   = df.apply(lambda r: parse_offset(r.get("offset", ""), r.get("description", "")), axis=1)

    # ── Inventory: prefer available_quantity, fall back to summing qty_* cols ─
    if "available_quantity" in df.columns:
        df["total_qty"] = pd.to_numeric(df["available_quantity"], errors="coerce").fillna(0).astype(int)
        log.info("Using available_quantity for total_qty")
    else:
        qty_cols = [c for c in df.columns if c.startswith("qty_")]
        if qty_cols:
            df["total_qty"] = df[qty_cols].apply(pd.to_numeric, errors="coerce").fillna(0).sum(axis=1).astype(int)
            log.info(f"Summed qty columns: {qty_cols}")
        else:
            df["total_qty"] = 0
            log.warning("No quantity columns found — total_qty set to 0")

    # ── Filter low-quantity rows ──────────────────────────────────────────────
    low_qty_mask = df["total_qty"] < LOW_QTY_THRESHOLD
    log.info(f"Low-quantity rows excluded (qty < {LOW_QTY_THRESHOLD}): {low_qty_mask.sum()}")
    df = df[~low_qty_mask].copy()
    log.info(f"Rows after low-qty filter: {len(df)}")

    # ── Build output DataFrame ────────────────────────────────────────────────
    out = pd.DataFrame(index=df.index, columns=SHOPIFY_COLUMNS + ["total_qty"])
    out = out.fillna("")

    vendor = "Save Big On Tires"

    out["Title"]      = dedupe_titles(df.apply(build_title, axis=1), df["item"].astype(str))
    out["Handle"] = out["Title"].apply(slugify)

    out["Body (HTML)"] = df.apply(build_body_html, axis=1)
    out["Vendor"]      = vendor
    out["Tags"]        = df.apply(build_tags, axis=1)
    out["Published"]   = "TRUE"

    out["Option1 Name"]  = "Title"
    out["Option1 Value"] = "Default Title"

    out["Variant SKU"]                  = df["item"].astype(str)
    out["Variant Inventory Tracker"]    = "shopify"
    out["Variant Inventory Policy"]     = "continue"
    out["Variant Fulfillment Service"]  = "manual"
    out["Variant Requires Shipping"]    = "TRUE"
    out["Variant Taxable"]             = "TRUE"

    # Price = cost + per-supplier markup, falling back to retail_price per-row when
    # cost_price is missing/zero (e.g. raw CSVs scraped before the cost-price
    # capture was added, or a ShoppingCart batch that failed).
    retail_prices = pd.to_numeric(df["retail_price"], errors="coerce").fillna(0)
    if "cost_price" in df.columns:
        cost_prices = pd.to_numeric(df["cost_price"], errors="coerce").fillna(0)
    else:
        log.warning("cost_price column missing — falling back to retail_price for all rows.")
        cost_prices = pd.Series(0, index=df.index)
    fallback_mask = cost_prices <= 0
    if fallback_mask.any():
        log.warning(f"{fallback_mask.sum()} / {len(df)} rows missing/zero cost_price — "
                    f"falling back to retail_price for Variant Price basis.")
    price_basis = cost_prices.where(~fallback_mask, retail_prices)
    out["Variant Price"] = price_with_markup(price_basis, MARKUP_SUPPLIER).round(2).astype(str)

    out["Image Src"]      = df["image_url"]
    out["Image Position"] = out["Image Src"].apply(lambda s: 1 if str(s).strip() else "")
    out["Gift Card"]      = "FALSE"

    out["SEO Title"]       = df.apply(lambda r: build_seo_title(r, vendor), axis=1)
    out["SEO Description"] = df.apply(build_seo_desc, axis=1)

    out["Google Shopping / Google Product Category"] = (
        "Vehicles & Parts > Vehicle Parts & Accessories > Wheel & Tire Assemblies > Rims & Wheels"
    )
    out["Google Shopping / Condition"]    = "New"
    out["Google Shopping / Custom Product"] = "FALSE"

    out["Google Shopping / MPN"] = df.apply(
        lambda r: f"{str(r.get('brand_name','')).strip()}-{str(r.get('model','')).strip()}-{format_wheel_size(str(r.get('size','')))}",
        axis=1,
    )

    out["Type"]             = "Wheels"
    out["Product Type"]     = "Wheels"
    out["Brand"]            = df["brand_name"].str.strip().str.title()
    out["Identifier Exist"] = "FALSE"
    out["total_qty"]        = df["total_qty"]

    output_file = get_output_file()
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    out.to_csv(output_file, index=False)
    log.info(f"Rows written : {len(out)}")
    log.info(f"Output file  : {output_file}")

    # Summary of what was parsed
    no_dia   = df["_dia"].isna().sum()
    no_bolts = (df["_bolts"].apply(len) == 0).sum()
    log.info(f"Rows missing diameter parse  : {no_dia}")
    log.info(f"Rows missing bolt pattern    : {no_bolts}")
    log.info("Done!")


if __name__ == "__main__":
    main()
