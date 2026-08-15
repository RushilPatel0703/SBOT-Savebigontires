#!/usr/bin/env python3
"""Formula Tires — Shopify SEO Augmentation
Reads the cleaned CSV and outputs Data/Shopify_SEO_tires.csv.
"""

import sys
import glob
import os
import re
import logging
import pandas as pd
from datetime import datetime

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared')))
from size_parser import parse_tire_size, parse_load_speed

MARKUP = 30.00


def normalize_size(size):
    """Convert dash-format sizes to standard slash/R format.

    LT265-70-17   → LT265/70R17
    LT35-12.50-20 → LT35/12.50R20
    225-65-17     → 225/65R17
    ST205-75-15   → ST205/75R15
    """
    size = size.strip()
    m = re.match(r'^(LT|ST|P)?(\d+)-(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$', size, re.IGNORECASE)
    if m:
        prefix = (m.group(1) or '').upper()
        width  = m.group(2)
        aspect = m.group(3)
        rim    = m.group(4)
        return f"{prefix}{width}/{aspect}R{rim}"
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


def get_output_file():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"Data/shopify/Shopify_SEO_tires_{ts}.csv"


def get_logger():
    os.makedirs("logs", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = f"logs/augment_{ts}.log"
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()]
    )
    return logging.getLogger("augment")


SHOPIFY_COLUMNS = [
    "SKU",
    "Handle",
    "Title",
    "Body (HTML)",
    "Vendor",
    "Type",
    "Tags",
    "Published",
    "Option1 Name",
    "Option1 Value",
    "Option2 Name",
    "Option2 Value",
    "Option3 Name",
    "Option3 Value",
    "Variant SKU",
    "Variant Grams",
    "Variant Inventory Tracker",
    "Variant Inventory Policy",
    "Variant Fulfillment Service",
    "Variant Price",
    "Variant Compare At Price",
    "Variant Requires Shipping",
    "Variant Taxable",
    "Variant Barcode",
    "Image Src",
    "Image Position",
    "Image Alt Text",
    "Gift Card",
    "SEO Title",
    "SEO Description",
    "Google Shopping / Google Product Category",
    "Google Shopping / Gender",
    "Google Shopping / Age Group",
    "Google Shopping / MPN",
    "Google Shopping / AdWords Grouping",
    "Google Shopping / AdWords Labels",
    "Google Shopping / Condition",
    "Google Shopping / Custom Product",
    "Google Shopping / Custom Label 0",
    "Google Shopping / Custom Label 1",
    "Google Shopping / Custom Label 2",
    "Google Shopping / Custom Label 3",
    "Google Shopping / Custom Label 4",
    "Variant Image",
    "Variant Weight Unit",
    "Variant Tax Code",
    "Cost per item",
    "Product Type",
    "Brand",
    "Identifier Exist",
]

# Maps designation value (from clean.py) → Shopify Type_ tag value
DESIGNATION_MAP = {
    "All Season":       "All-Season",
    "All Season (LT)":  "All-Season",   # LT tag added separately via size prefix
    "All Weather":      "All-Weather",
    "All Summer":       "All-Summer",
    "All Terrain":      "All-Terrain",
    "Winter":           "Winter",
    "Summer":           "Summer",
    "Mud Snow":         "Mud-Snow",
    "Mud Terrain":      "Mud-Terrain",
    "Commercial":       "All-Season",   # no commercial collection yet
    "Unknown":          "All-Season",   # fallback until manually reviewed
}

# Speed ratings that indicate a performance tire
PERFORMANCE_RATINGS = {"W", "Y", "Z"}

LOCATIONS = [
    "Burnaby", "Langley", "MapleRidge",
    "Kelowna", "Kamloops", "SalmonArm",
]


def find_input_file():
    matches = sorted(
        glob.glob("Data/cleaned/cleaned_tires_*.csv"),
        key=os.path.getmtime,
        reverse=True,
    )
    if not matches:
        print("ERROR: No cleaned_tires_*.csv found. Run clean.py first.")
        sys.exit(1)
    return matches[0]


def slugify(text):
    text = str(text).lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def build_body_html(row):
    brand       = str(row.get("brand_name", "")).strip()
    model       = str(row.get("model", "")).strip()
    size        = str(row.get("size", "")).strip()
    designation = str(row.get("designation", "")).strip()
    description = str(row.get("description", "")).strip()

    parsed        = parse_tire_size(size, description)
    section_width = parsed['width']
    aspect_ratio  = parsed['ratio']
    rim           = parsed['rim_display']

    load_index, speed_rating = parse_load_speed(description)
    load_range = str(row.get("load_range", "")).strip()

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


def build_tags(row):
    parts = []

    brand = str(row.get("brand_name", "")).strip()
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
                # taglib.build_tags falls back to the parser when neither grouped
                # shape matches; without this, odd sizes silently lose RimDia_.
                parsed = parse_tire_size(size, str(row.get("description", "")))
                if parsed['rim_tag']:
                    parts.append(f"RimDia_{parsed['rim_tag']}")

    designation = str(row.get("designation", "")).strip()
    type_tag = DESIGNATION_MAP.get(designation, "All-Season")
    parts.append(f"Type_{type_tag}")

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

    parts.append("Supplier_Formulatires")

    return ",".join(parts)


def main():
    log = get_logger()
    input_file = find_input_file()
    log.info(f"Input file : {input_file}")

    df = pd.read_csv(input_file, dtype=str, low_memory=False)
    df = df.fillna("")
    log.info(f"Rows loaded: {len(df)}")

    df["load_range"]    = df.apply(lambda r: extract_load_range(r["size"], r.get("description", "")), axis=1)
    df["size"]          = df["size"].apply(strip_load_range)
    df["size"]          = df["size"].apply(normalize_size)
    df["size"]          = df["size"].str.replace('x', 'X', regex=False)
    df["_speed_rating"] = df["description"].apply(lambda d: parse_load_speed(str(d))[1])
    log.info("Size column normalized; load_range and speed_rating extracted")

    # Use available_quantity if present, otherwise sum branch qty_* columns
    if "available_quantity" in df.columns:
        df["total_qty"] = pd.to_numeric(df["available_quantity"], errors="coerce").fillna(0).astype(int)
        log.info("Using available_quantity for total_qty")
    else:
        qty_cols = [c for c in df.columns if c.startswith("qty_")]
        if qty_cols:
            df["total_qty"] = df[qty_cols].apply(pd.to_numeric, errors="coerce").fillna(0).sum(axis=1).astype(int)
            log.info(f"Branch qty columns summed: {qty_cols}")
        else:
            df["total_qty"] = 0
            log.warning("No qty_* columns found — total_qty set to 0")

    out = pd.DataFrame(index=df.index, columns=SHOPIFY_COLUMNS + ["total_qty"])
    out = out.fillna("")

    out["SKU"]      = df["item"]

    def build_title(row):
        brand       = row["brand_name"].strip()
        size        = row["size"].strip()
        description = row["description"].strip()
        designation = row["designation"].strip()
        size_prefix = "" if size and size in description else size
        description = re.sub(r'[❄️❄️]+', '', description).strip()
        # Strip all parenthetical content except (EV Capable)
        description = re.sub(r'\((?!EV Capable\b)[^)]*\)', '', description, flags=re.IGNORECASE)
        description = re.sub(r'\s{2,}', ' ', description).strip(' -').strip()
        if designation == "Winter":
            desig_label = "(3 Peak Snowflake) ❄️"
        elif designation not in ("Unknown", "Commercial", "All Season (LT)", ""):
            desig_label = designation
        else:
            desig_label = ""
        return " ".join(filter(None, [brand, size_prefix, description, desig_label]))

    title = df.apply(build_title, axis=1)
    out["Title"]      = title
    out["Handle"] = title.apply(slugify)

    out["Body (HTML)"] = df.apply(build_body_html, axis=1)
    out["Vendor"]      = "Save Big On Tires"
    out["Tags"]        = df.apply(build_tags, axis=1)
    out["Published"]   = "TRUE"

    out["Option1 Name"]  = "Title"
    out["Option1 Value"] = "Default Title"

    out["Variant Inventory Tracker"]   = "shopify"
    out["Variant Inventory Policy"]    = "continue"
    out["Variant Fulfillment Service"] = "manual"
    retail_prices = pd.to_numeric(df["retail_price"], errors="coerce").fillna(0)
    out["Variant Price"]               = (retail_prices + MARKUP).round(2).astype(str)
    out["Variant Requires Shipping"]   = "TRUE"
    out["Variant Taxable"]             = "TRUE"

    out["Image Src"]      = df["image_url"]
    out["Image Position"] = out["Image Src"].apply(lambda s: 1 if str(s).strip() else "")

    out["Gift Card"] = "FALSE"

    # SEO Title (3-way split for variety)
    vendor = "Save Big On Tires"
    n      = len(df)
    third  = n // 3

    def seo_base(idx):
        return " ".join(filter(None, [
            df.at[idx, "brand_name"].strip(),
            df.at[idx, "size"].strip(),
            df.at[idx, "description"].strip(),
            df.at[idx, "designation"].strip(),
        ]))

    seo_titles = []
    for i, idx in enumerate(df.index):
        base = seo_base(idx)
        if i < third:
            seo_titles.append(f"Buy {base} | {vendor}")
        elif i < third * 2:
            seo_titles.append(f"Purchase {base} | {vendor}")
        else:
            seo_titles.append(f"{base} Tires near me | {vendor}")
    out["SEO Title"] = seo_titles

    # SEO Description (3-way split for variety)
    seo_descs = []
    for i, idx in enumerate(df.index):
        brand       = df.at[idx, "brand_name"].strip()
        model       = df.at[idx, "model"].strip()
        size        = df.at[idx, "size"].strip()
        designation = df.at[idx, "designation"].strip()

        if designation:
            for_driving     = f"designed for {designation} driving"
            for_performance = f"built for reliable {designation} performance"
            desig_tire      = f"{designation} tire"
        else:
            for_driving     = "designed for year-round driving"
            for_performance = "built for reliable performance"
            desig_tire      = "tire"

        if i < third:
            desc = (
                f"{brand} {model} tire in size {size}, {for_driving}. "
                f"Suitable for passenger vehicles, SUVs, and light trucks, "
                f"offering dependable performance and road safety."
            )
        elif i < third * 2:
            desc = (
                f"{brand} {model} tire, size {size}, {for_performance}. "
                f"Engineered for consistent handling, durability, and everyday "
                f"driving across multiple vehicle types."
            )
        else:
            desc = (
                f"{brand} {model} {desig_tire} in size {size}, designed for stable "
                f"handling and long-lasting performance. Ideal for drivers seeking "
                f"dependable traction and durability."
            )
        seo_descs.append(desc)
    out["SEO Description"] = seo_descs

    out["Google Shopping / Google Product Category"] = "Vehicles & Parts > Vehicle Parts & Accessories"
    out["Google Shopping / Condition"]               = "New"
    out["Google Shopping / Custom Product"]          = "FALSE"

    def build_mpn(row):
        parts = [str(row.get("brand_name", "")).strip(), str(row.get("size", "")).strip()]
        designation = str(row.get("designation", "")).strip()
        if designation:
            parts.append(designation)
        return "-".join(parts)

    out["Google Shopping / MPN"] = df.apply(build_mpn, axis=1)

    DESIGNATION_LABEL = {
        "All Season":       "all-season",
        "All Season (LT)":  "all-season",
        "All Weather":      "all-weather",
        "All Summer":       "all-summer",
        "All Terrain":      "all-terrain",
        "Winter":           "winter",
        "Summer":           "summer",
        "Mud Snow":         "mud-snow",
        "Mud Terrain":      "mud-terrain",
        "Commercial":       "all-season",
        "Unknown":          "all-season",
    }
    out["Google Shopping / Custom Label 0"] = df["designation"].apply(
        lambda d: DESIGNATION_LABEL.get(str(d).strip(), "all-season")
    )

    out["Product Type"]     = "Vehicle Tires"
    out["Brand"]            = df["brand_name"]
    out["Identifier Exist"] = "FALSE"
    out["total_qty"]        = df["total_qty"]

    out["SKU"]         = out["SKU"].astype(str)
    out["Variant SKU"] = out["SKU"]
    output_file = get_output_file()
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    out.to_csv(output_file, index=False)
    log.info(f"Rows written : {len(out)}")
    log.info(f"Output file  : {output_file}")
    log.info("Done!")


if __name__ == "__main__":
    main()
