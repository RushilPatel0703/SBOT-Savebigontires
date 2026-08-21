#!/usr/bin/env python3
"""DT Tire — Shopify SEO Augmentation (Tires)
Reads Data/cleaned_tires_*.csv → Data/Shopify_SEO_tires_<ts>.csv

Only processes NEW products (net-new SKUs for create_products.py).
Price/inventory sync for existing products is handled by pipelines/merged/.
"""

import sys
import os
import re
import pandas as pd
from datetime import datetime

from config import find_latest, get_logger, markup_for

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared')))
from size_parser import parse_tire_size, coerce_load_index
from dedupe import dedupe_titles

MARKUP = markup_for("dttire")

# Maps cleaned designation → Shopify Type_ tag value
# "Unknown" is intentionally omitted — no Type_ tag is added
DESIGNATION_MAP = {
    "Winter":     "Winter",
    "All Season": "All-Season",
    "Summer":     "Summer",
}

DESIGNATION_LABEL = {
    "Winter":     "winter",
    "All Season": "all-season",
    "Summer":     "summer",
    "Unknown":    "all-season",
}

PERFORMANCE_RATINGS = {"W", "Y", "Z"}

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


def get_output_file():
    os.makedirs("Data/shopify", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"Data/shopify/Shopify_SEO_dttire_tires_{ts}.csv"


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


def build_title(row):
    """Format: {brand} {model} {size} {service_desc} — trailing spaces stripped."""
    brand        = str(row.get("brand",        "")).strip()
    model        = str(row.get("model",        "")).strip()
    size         = str(row.get("size",         "")).strip()
    service_desc = str(row.get("service_desc", "")).strip()
    return " ".join(filter(None, [brand, model, size, service_desc]))


def build_body_html(row):
    brand        = str(row.get("brand",        "")).strip()
    model        = str(row.get("model",        "")).strip()
    size         = str(row.get("size",         "")).strip()
    designation  = str(row.get("designation",  "")).strip()
    # service_desc carries the full service description ("107Y XL"), so it
    # answers a blank load_index column — the same source parse_tire_size
    # already falls back to below.
    load_index   = coerce_load_index(row.get("load_index", ""), row.get("service_desc", ""))
    speed_rating = str(row.get("speed_rating", "")).strip()
    load_range   = str(row.get("load_range",   "")).strip()

    parsed       = parse_tire_size(size, row.get("service_desc", ""))
    section_width = parsed['width']
    aspect_ratio  = parsed['ratio']
    rim           = parsed['rim_display']

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
                parsed = parse_tire_size(size, row.get("service_desc", ""))
                if parsed['rim_tag']:
                    parts.append(f"RimDia_{parsed['rim_tag']}")

    designation = str(row.get("designation", "")).strip()
    if designation in DESIGNATION_MAP:
        parts.append(f"Type_{DESIGNATION_MAP[designation]}")

    pfx_m = re.match(r'^(LT|ST|P)', size_tag, re.IGNORECASE)
    pfx = pfx_m.group(1).upper() if pfx_m else ""
    if pfx == "ST" or str(row.get("model", "")).lower().find("trailer") != -1:
        parts.append("Type_Trailer")
    if pfx == "LT":
        parts.append("Type_Light-Truck")

    speed_rating = re.sub(r'[()]', '', str(row.get("speed_rating", "")).strip())
    if speed_rating and speed_rating != "-":
        parts.append(f"SpeedRating_{speed_rating}")
    if speed_rating in PERFORMANCE_RATINGS:
        parts.append("Type_Performance")

    load_range = str(row.get("load_range", "")).strip()
    if load_range:
        parts.append(f"LoadRange_{load_range}")

    # Note: Location_* tags are NOT added here — applied by pipelines/merged/
    parts.append("Supplier_Dttire")

    return ",".join(parts)


def main():
    log = get_logger("dttire_augment")

    input_file = find_latest(os.path.join("Data", "cleaned", "cleaned_tires_*.csv"))
    if not input_file:
        log.error("No cleaned_tires_*.csv found in Data/cleaned/ — run clean.py first.")
        sys.exit(1)
    log.info(f"Input file : {input_file}")

    df = pd.read_csv(input_file, dtype=str, low_memory=False).fillna("")
    log.info(f"Rows loaded: {len(df)}")
    if len(df) == 0:
        log.error(f"{input_file} has 0 rows — refusing to write an empty Shopify SEO CSV. "
                   f"Check clean.py's output for this run before re-running augment.")
        sys.exit(1)

    # total_qty: use qty_total column if present, else fall back to qty_local + qty_other
    if "qty_total" in df.columns:
        df["total_qty"] = pd.to_numeric(df["qty_total"], errors="coerce").fillna(0).astype(int)
        log.info("Using qty_total for total_qty")
    else:
        qty_cols = [c for c in df.columns if c.startswith("qty_")]
        if qty_cols:
            df["total_qty"] = df[qty_cols].apply(pd.to_numeric, errors="coerce").fillna(0).sum(axis=1).astype(int)
            log.info(f"Summed qty columns: {qty_cols}")
        else:
            df["total_qty"] = 0
            log.warning("No qty_* columns found — total_qty set to 0")

    out = pd.DataFrame(index=df.index, columns=SHOPIFY_COLUMNS + ["total_qty"])
    out = out.fillna("")

    titles = df.apply(build_title, axis=1)
    titles = dedupe_titles(titles, df["sku"])

    out["SKU"]         = df["sku"]
    out["Variant SKU"] = df["sku"]
    out["Title"]       = titles
    out["Handle"]      = titles.apply(slugify)
    out["Body (HTML)"] = df.apply(build_body_html, axis=1)
    out["Vendor"]      = "Save Big On Tires"
    out["Type"]        = "Tires"
    out["Tags"]        = df.apply(build_tags, axis=1)
    out["Published"]   = "TRUE"

    out["Option1 Name"]  = "Title"
    out["Option1 Value"] = "Default Title"

    out["Variant Inventory Tracker"]   = "shopify"
    out["Variant Inventory Policy"]    = "continue"
    out["Variant Fulfillment Service"] = "manual"

    cost_prices          = pd.to_numeric(df["cost_price"], errors="coerce").fillna(0)
    out["Variant Price"] = (cost_prices + MARKUP).round(2).astype(str)

    out["Variant Requires Shipping"] = "TRUE"
    out["Variant Taxable"]           = "TRUE"
    out["Image Src"]                 = df["image_url"] if "image_url" in df.columns else ""
    out["Image Position"]            = out["Image Src"].apply(lambda s: 1 if str(s).strip() else "")
    out["Gift Card"]                 = "FALSE"

    # SEO Title — 3-way split for variety
    n      = len(df)
    third  = n // 3
    vendor = "Save Big On Tires"

    seo_titles = []
    for i_pos, idx in enumerate(df.index):
        base = " ".join(filter(None, [
            df.at[idx, "brand"].strip(),
            df.at[idx, "size"].strip(),
            df.at[idx, "model"].strip(),
            df.at[idx, "designation"].strip(),
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
        designation = df.at[idx, "designation"].strip()

        for_driving     = f"designed for {designation} driving"     if designation else "designed for year-round driving"
        for_performance = f"built for reliable {designation} performance" if designation else "built for reliable performance"
        desig_tire      = f"{designation} tire"                     if designation else "tire"

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
            df.at[idx, "designation"].strip(),
        ]))
        for idx in df.index
    ]
    out["Google Shopping / Custom Label 0"] = df["designation"].apply(
        lambda d: DESIGNATION_LABEL.get(str(d).strip(), "all-season")
    )

    out["Product Type"]     = "Vehicle Tires"
    out["Brand"]            = df["brand"]
    out["Identifier Exist"] = "FALSE"
    out["total_qty"]        = df["total_qty"]

    output_file = get_output_file()
    out.to_csv(output_file, index=False, encoding="utf-8")
    log.info(f"Rows written : {len(out)}")
    log.info(f"Output file  : {output_file}")
    log.info("Done.")


if __name__ == "__main__":
    main()
