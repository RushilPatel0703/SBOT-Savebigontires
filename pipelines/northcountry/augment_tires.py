#!/usr/bin/env python3
"""North Country Tire — Shopify SEO Augmentation (Tires)
Reads Data/cleaned/cleaned_tires_*.csv → Data/shopify/Shopify_SEO_northcountry_tires_<ts>.csv

Only processes NEW products (net-new SKUs for create_products.py).
Price/inventory sync for existing products is handled by pipelines/merged/.
"""

import sys
import glob
import os
import re
import pandas as pd
from datetime import datetime

from config import markup_for

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared')))
from size_parser import (parse_tire_size, parse_load_speed, coerce_load_index,
                         default_passenger_load_range)
from dedupe import dedupe_titles

MARKUP = markup_for("northcountry")

LOCATIONS = [
    "Burnaby", "Langley", "MapleRidge",
    "Kelowna", "Kamloops", "SalmonArm",
]

# Maps derived designation → Shopify Type_ tag value
DESIGNATION_MAP = {
    "Winter":      "Winter",
    "All-Weather": "All-Weather",
    "All-Season":  "All-Season",
}

DESIGNATION_LABEL = {
    "Winter":      "winter",
    "All-Weather": "all-weather",
    "All-Season":  "all-season",
}

SHOPIFY_COLUMNS = [
    "SKU", "Handle", "Title", "Body (HTML)", "Vendor", "Type", "Tags",
    "Published", "Option1 Name", "Option1 Value", "Option2 Name", "Option2 Value",
    "Option3 Name", "Option3 Value", "Variant SKU", "Variant Grams",
    "Variant Inventory Tracker", "Variant Inventory Policy",
    "Variant Fulfillment Service", "Variant Price", "Variant Compare At Price",
    "Variant Requires Shipping", "Variant Taxable", "Variant Barcode",
    "Image Src", "Image Position", "Image Alt Text", "Gift Card",
    "SEO Title", "SEO Description",
    "Google Shopping / Google Product Category", "Google Shopping / Gender",
    "Google Shopping / Age Group", "Google Shopping / MPN",
    "Google Shopping / AdWords Grouping", "Google Shopping / AdWords Labels",
    "Google Shopping / Condition", "Google Shopping / Custom Product",
    "Google Shopping / Custom Label 0", "Google Shopping / Custom Label 1",
    "Google Shopping / Custom Label 2", "Google Shopping / Custom Label 3",
    "Google Shopping / Custom Label 4", "Variant Image", "Variant Weight Unit",
    "Variant Tax Code", "Cost per item", "Product Type", "Brand", "Identifier Exist",
]

PERFORMANCE_RATINGS = {"W", "Y", "Z"}


def find_input_file():
    matches = sorted(
        glob.glob("Data/cleaned/cleaned_tires_*.csv"),
        key=os.path.getmtime,
        reverse=True,
    )
    if not matches:
        print("ERROR: No cleaned_tires_*.csv found in Data/cleaned/ — run clean.py first.")
        sys.exit(1)
    return matches[0]


def get_output_file():
    os.makedirs("Data/shopify", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"Data/shopify/Shopify_SEO_northcountry_tires_{ts}.csv"


def slugify(text):
    text = str(text).lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def normalize_size(size):
    """Convert dash-format sizes to standard slash/R format (safety net — NC API usually correct)."""
    size = size.strip()
    m = re.match(r'^(LT|ST|P)?(\d+)-(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$', size, re.IGNORECASE)
    if m:
        prefix = (m.group(1) or "").upper()
        return f"{prefix}{m.group(2)}/{m.group(3)}R{m.group(4)}"
    return size


PR_TO_LOAD_RANGE = {
    '4': 'B', '6': 'C', '8': 'D', '10': 'E', '12': 'F', '14': 'G', '16': 'H',
}
_PR_PATTERN = re.compile(r'\b(4|6|8|10|12|14|16)\s*-?\s*(?:PR|PLY)\b', re.IGNORECASE)


def extract_load_range(size, description=""):
    """Extract load range letter from size string or description.

    Checks in order:
      1. Explicit letter at end of size  (e.g. 'LT225/75R16 E' → 'E')
      2. PR notation in size field       (e.g. '6PR' or '6 PR' → 'C')
      3. PR notation in description      (e.g. '10 PR All Season' → 'E')
    """
    m = re.search(r'\s+([B-N])\s*$', size.strip())
    if m:
        return m.group(1)
    m = _PR_PATTERN.search(size)
    if m:
        return PR_TO_LOAD_RANGE.get(m.group(1), '')
    m = _PR_PATTERN.search(str(description))
    if m:
        return PR_TO_LOAD_RANGE.get(m.group(1), '')
    if re.search(r'\bXL\b|\bExtra.?Load\b', str(description), re.IGNORECASE):
        return 'XL'
    return ''


def strip_load_range(size):
    """Remove load range notation from size string (trailing letter or PR rating)."""
    size = re.sub(r'\s+[B-N]\s*$', '', size.strip())
    size = _PR_PATTERN.sub('', size)
    return size.strip()


def get_designation(row):
    """Derive designation from NC API flags. Defaults to All-Season."""
    if str(row.get("is_winter_tire", "")).strip().lower() in ("true", "1"):
        return "Winter"
    if str(row.get("is_all_weather", "")).strip().lower() in ("true", "1"):
        return "All-Weather"
    return "All-Season"


def _get_size_components(row):
    """Return (width, ratio, rim_display, rim_tag) preferring spec columns over regex."""
    parsed  = parse_tire_size(str(row.get("size", "")), str(row.get("description", "")))
    width   = str(row.get("spec_sectionwidth", "")).strip() or parsed["width"]
    ratio   = str(row.get("spec_aspectratio",  "")).strip() or parsed["ratio"]
    rim_raw = str(row.get("spec_rimsize",       "")).strip() or parsed["rim_display"]
    return width, ratio, rim_raw, rim_raw.replace(".", "")


def build_title(row, designation):
    brand       = str(row.get("brand", "")).strip()
    size        = str(row.get("size", "")).strip()
    description = str(row.get("description", "")).strip()

    description = re.sub(r'[❄️❄️]+', '', description).strip()
    description = re.sub(r'\((?!EV Capable\b)[^)]*\)', '', description, flags=re.IGNORECASE)
    description = re.sub(r'\s{2,}', ' ', description).strip(' -').strip()

    if designation == "Winter":
        desig_label = "(3 Peak Snowflake) ❄️"
    elif designation != "All-Season":
        desig_label = designation
    else:
        desig_label = ""

    size_prefix = "" if size and size in description else size
    return " ".join(filter(None, [brand, size_prefix, description, desig_label]))


def build_tags(row, designation):
    parts = []

    brand = str(row.get("brand", "")).strip()
    if brand:
        parts.append(f"Brand_{brand.title()}")

    size = str(row.get("size", "")).strip()
    size_tag = ""
    if size:
        # canonical Size_
        size_tag = re.sub(r'[A-Z](?=R\d)', '', size.replace('.', ''), flags=re.IGNORECASE).upper()
        size_tag = re.sub(r'^([A-Z]{0,3}\d{2,3}X\d{2,4})X(\d{2,3})', r'\1R\2', size_tag)
        if size_tag[:2] not in ('LT', 'ST', 'HL') and size_tag[:1] != 'P':
            m = re.match(r'^(.*?)(LT|ST)$', size_tag)
            if m:
                size_tag = m.group(2) + m.group(1)
        parts.append(f"Size_{size_tag}")

        # grouped axes — digits only, construction prefix and service suffix dropped
        m = (re.match(r'^([A-Z]{0,3})(\d{2,3})/(\d{2,3})R(\d{2,3})([A-Z]*)$', size_tag)
             or re.match(r'^([A-Z]{0,3})(\d{2,3})X(\d{2,4})[RX](\d{2,3})([A-Z]*)$', size_tag))
        if m:
            parts.append(f"Width_{m.group(2)}")
            parts.append(f"Ratio_{m.group(3)}")
            parts.append(f"RimDia_{m.group(4)}")
        else:
            m = re.match(r'^([A-Z]{0,3})(\d{2,3})R(\d{2,3})([A-Z]*)$', size_tag)   # no aspect ratio
            if m:
                parts.append(f"Width_{m.group(2)}")
                parts.append(f"RimDia_{m.group(3)}")
            else:
                # taglib falls back to the parser when neither grouped shape
                # matches; without this, odd sizes silently lose RimDia_.
                rim_tag = str(row.get("_rim_tag", "")).strip()
                if rim_tag:
                    parts.append(f"RimDia_{rim_tag}")

    parts.append(f"Type_{DESIGNATION_MAP[designation]}")

    pfx_m = re.match(r'^(LT|ST|P)', size_tag, re.IGNORECASE)
    pfx = pfx_m.group(1).upper() if pfx_m else ""
    if pfx == "LT":
        parts.append("Type_Light-Truck")
    if pfx == "ST":
        parts.append("Type_Trailer")

    speed_rating = str(row.get("_speed_rating", "")).strip()
    if speed_rating:
        parts.append(f"SpeedRating_{speed_rating}")
    if speed_rating in PERFORMANCE_RATINGS:
        parts.append("Type_Performance")

    load_range = str(row.get("load_range", "")).strip()
    if load_range:
        parts.append(f"LoadRange_{load_range}")

    for loc in LOCATIONS:
        parts.append(f"Location_{loc}")

    parts.append("Supplier_Northcountry")

    return ",".join(parts)


def build_body_html(row, designation):
    brand = str(row.get("brand", "")).strip()
    model = str(row.get("model", "")).strip()

    section_width = str(row.get("_width",        "")).strip()
    aspect_ratio  = str(row.get("_ratio",         "")).strip()
    rim           = str(row.get("_rim_display",   "")).strip()
    load_index    = str(row.get("_load_index",    "")).strip()
    speed_rating  = str(row.get("_speed_rating",  "")).strip()
    load_range    = str(row.get("load_range",     "")).strip()

    return (
        f"<br>Brand: {brand}<br>"
        f"Style: {model}<br>"
        f"Section width: {section_width}<br>"
        f"Aspect ratio: {aspect_ratio}<br>"
        f"Rim diameter: {rim}<br>"
        f"Load index: {load_index}<br>"
        f"Speed rating: {speed_rating}<br>"
        f"Load range: {load_range}<br>"
        f"Designation: {designation}<br><br>"
        f"2 YEARS LIMITED TIRE WARRANTY.<br><br>"
        f"{brand} tires in Burnaby, Langley, Maple Ridge, Kelowna, Kamloops, and Salmon Arm "
        f"are available at Save Big On Tires across British Columbia.<br>"
        f"Experience superior handling and safety with the full lineup of {brand} all-season, "
        f"winter, SUV, truck, and performance tires for city driving, highway travel, and "
        f"mountain conditions like the Coquihalla.<br><br>"
        f"Complete Tire Care Package<br>"
        f"Buying new tires is an investment. At Save Big On Tires, we protect that investment "
        f"with professional, tech-driven maintenance:<br>"
        f"&#8226; 3D Wheel Alignment using advanced imaging for factory-level precision<br>"
        f"&#8226; Computerized Balancing for a vibration-free ride<br>"
        f"&#8226; TPMS Specialists for sensor service and diagnostics<br>"
        f"&#8226; Professional Installation for passenger and light truck vehicles<br><br>"
        f"Serving Drivers Across BC<br>"
        f"Book your appointment at our convenient locations:<br>"
        f"&#8226; Metro Vancouver: Burnaby, Langley, Maple Ridge<br>"
        f"&#8226; BC Interior: Kelowna, Kamloops, Salmon Arm<br><br>"
    )


def main():
    input_file = find_input_file()
    print(f"Input file : {input_file}")

    df = pd.read_csv(input_file, dtype=str, low_memory=False).fillna("")
    print(f"Rows loaded: {len(df)}")
    if len(df) == 0:
        print(f"ERROR: {input_file} has 0 rows — refusing to write an empty Shopify SEO CSV. "
              f"Check clean.py's output for this run before re-running augment.")
        sys.exit(1)

    df["description"] = df["description"].str.replace(r'\s*\(\d{7,}\)\s*$', '', regex=True).str.strip()

    # Brand backfill: manufacturer_code is often populated even when brand is
    # blank (Costar quirk — e.g. SKU CW251210: brand="" but manufacturer_code=
    # "RUBBERMASTER"). Same fallback/title-casing already used by
    # augment_wheels.py's _resolve_brand(); applied once here so every
    # downstream Title/Tags/Body/SEO/Brand use benefits.
    _brand_was_blank = df["brand"].str.strip() == ""
    df["brand"] = df.apply(
        lambda r: str(r.get("brand", "")).strip() or str(r.get("manufacturer_code", "")).strip().title(),
        axis=1,
    )
    _brand_recovered = int((_brand_was_blank & (df["brand"].str.strip() != "")).sum())
    print(f"Brand backfilled from manufacturer_code: {_brand_recovered} / {int(_brand_was_blank.sum())} previously-blank rows")

    # load_range: prefer spec_loadrange, fall back to regex, then to SL for a
    # passenger size. NC's feed marks only the XL side, so an empty cell on a
    # passenger tire is the standard rating rather than a missing one.
    df["load_range"] = df.apply(
        lambda r: str(r.get("spec_loadrange", "")).strip()
                  or extract_load_range(r["size"], r.get("description", ""))
                  or default_passenger_load_range(r["size"]),
        axis=1,
    )

    df["size"] = df["size"].apply(strip_load_range)
    df["size"] = df["size"].apply(normalize_size)
    df["size"] = df["size"].str.replace("x", "X", regex=False)

    # Size components: prefer spec columns, fall back to regex
    df[["_width", "_ratio", "_rim_display", "_rim_tag"]] = df.apply(
        lambda r: pd.Series(_get_size_components(r)), axis=1
    )

    # load_index: prefer spec_loadindex, fall back to description regex.
    # Costar writes an unknown index as "0.0000", which a plain `or` treats as
    # a real value — see coerce_load_index() for what that shipped.
    df["_load_index"] = df.apply(
        lambda r: coerce_load_index(r.get("spec_loadindex", ""), r.get("description", "")),
        axis=1,
    )

    # speed_rating: prefer spec_speedrating, fall back to description regex
    df["_speed_rating"] = df.apply(
        lambda r: str(r.get("spec_speedrating", "")).strip()
                  or parse_load_speed(str(r.get("description", "")))[1],
        axis=1,
    )

    designations = df.apply(get_designation, axis=1)

    out = pd.DataFrame(index=df.index, columns=SHOPIFY_COLUMNS + ["total_qty"])
    out = out.fillna("")

    titles = pd.Series([build_title(df.loc[i], designations.at[i]) for i in df.index], index=df.index)
    titles = dedupe_titles(titles, df["sku"])

    out["SKU"]         = df["sku"]
    out["Variant SKU"] = df["sku"]
    out["Title"]       = titles
    out["Handle"]      = titles.apply(slugify)
    out["Body (HTML)"] = [build_body_html(df.loc[i], designations.at[i]) for i in df.index]
    out["Vendor"]      = "Save Big On Tires"
    out["Tags"]        = [build_tags(df.loc[i], designations.at[i]) for i in df.index]
    out["Published"]   = "TRUE"

    out["Option1 Name"]  = "Title"
    out["Option1 Value"] = "Default Title"

    out["Variant Inventory Tracker"]   = "shopify"
    out["Variant Inventory Policy"]    = "continue"
    out["Variant Fulfillment Service"] = "manual"

    # Price basis: true cost_price when available, falling back to retail_price
    # per-row when cost_price is missing/zero (e.g. raw CSVs scraped before the
    # cost-price capture was added, or a ShoppingCart batch that failed).
    retail_prices = pd.to_numeric(df["retail_price"], errors="coerce").fillna(0)
    if "cost_price" in df.columns:
        cost_prices = pd.to_numeric(df["cost_price"], errors="coerce").fillna(0)
    else:
        print("WARNING: cost_price column missing — falling back to retail_price for all rows.")
        cost_prices = pd.Series(0, index=df.index)
    fallback_mask = cost_prices <= 0
    if fallback_mask.any():
        print(f"WARNING: {fallback_mask.sum()} / {len(df)} rows missing/zero cost_price — "
              f"falling back to retail_price for Variant Price basis.")
    price_basis = cost_prices.where(~fallback_mask, retail_prices)
    out["Variant Price"] = (price_basis + MARKUP).round(2).astype(str)

    out["Variant Requires Shipping"] = "TRUE"
    out["Variant Taxable"]           = "TRUE"
    out["Image Src"]                 = df["image_url"] if "image_url" in df.columns else ""
    out["Image Position"]            = out["Image Src"].apply(lambda s: 1 if str(s).strip() else "")
    out["Gift Card"]                 = "FALSE"

    # SEO Title — 3-way split for variety
    n     = len(df)
    third = n // 3
    vendor = "Save Big On Tires"

    seo_titles = []
    for i_pos, idx in enumerate(df.index):
        base = " ".join(filter(None, [
            df.at[idx, "brand"].strip(),
            df.at[idx, "size"].strip(),
            df.at[idx, "description"].strip(),
            designations.at[idx],
        ]))
        if i_pos < third:
            seo_titles.append(f"Buy {base} | {vendor}"[:160])
        elif i_pos < third * 2:
            seo_titles.append(f"Purchase {base} | {vendor}"[:160])
        else:
            seo_titles.append(f"{base} Tires near me | {vendor}"[:160])
    out["SEO Title"] = seo_titles

    # SEO Description — 3-way split
    seo_descs = []
    for i_pos, idx in enumerate(df.index):
        brand       = df.at[idx, "brand"].strip()
        model       = df.at[idx, "model"].strip()
        size        = df.at[idx, "size"].strip()
        designation = designations.at[idx]

        for_driving     = f"designed for {designation} driving" if designation else "designed for year-round driving"
        for_performance = f"built for reliable {designation} performance" if designation else "built for reliable performance"
        desig_tire      = f"{designation} tire" if designation else "tire"

        if i_pos < third:
            seo_descs.append(
                f"{brand} {model} tire in size {size}, {for_driving}. "
                f"Suitable for passenger vehicles, SUVs, and light trucks, "
                f"offering dependable performance and road safety."
            )
        elif i_pos < third * 2:
            seo_descs.append(
                f"{brand} {model} tire, size {size}, {for_performance}. "
                f"Engineered for consistent handling, durability, and everyday "
                f"driving across multiple vehicle types."
            )
        else:
            seo_descs.append(
                f"{brand} {model} {desig_tire} in size {size}, designed for stable "
                f"handling and long-lasting performance. Ideal for drivers seeking "
                f"dependable traction and durability."
            )
    out["SEO Description"] = seo_descs

    out["Google Shopping / Google Product Category"] = "Vehicles & Parts > Vehicle Parts & Accessories"
    out["Google Shopping / Condition"]               = "New"
    out["Google Shopping / Custom Product"]          = "FALSE"
    out["Google Shopping / MPN"] = [
        "-".join(filter(None, [
            df.at[idx, "brand"].strip(),
            df.at[idx, "size"].strip(),
            designations.at[idx],
        ]))
        for idx in df.index
    ]
    out["Google Shopping / Custom Label 0"] = designations.map(
        lambda d: DESIGNATION_LABEL.get(d, "all-season")
    )

    out["Product Type"]     = "Vehicle Tires"
    out["Brand"]            = df["brand"]
    out["Identifier Exist"] = "FALSE"
    out["total_qty"]        = pd.to_numeric(
        df.get("available_quantity_related_branches", df["available_quantity"]),
        errors="coerce",
    ).fillna(0).astype(int)

    output_file = get_output_file()
    out.to_csv(output_file, index=False, encoding="utf-8")
    print(f"Rows written : {len(out)}")
    print(f"Output file  : {output_file}")
    print("Done.")


if __name__ == "__main__":
    main()
