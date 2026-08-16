#!/usr/bin/env python3
"""DT Tire — Shopify SEO Augmentation (Wheels)
Reads Data/cleaned/cleaned_wheels_*.csv → Data/shopify/Shopify_SEO_dttire_wheels_<ts>.csv
"""

import sys
import os
import re
import pandas as pd
from datetime import datetime

from config import find_latest, get_logger, markup_for

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared')))
from dedupe import dedupe_titles

MARKUP = markup_for("dttire")

LOCATIONS = [
    "Burnaby", "Langley", "MapleRidge",
    "Kelowna", "Kamloops", "SalmonArm",
]

SHOPIFY_COLUMNS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Type", "Tags",
    "Published", "Option1 Name", "Option1 Value", "Variant SKU",
    "Variant Grams", "Variant Inventory Tracker", "Variant Inventory Policy",
    "Variant Fulfillment Service", "Variant Price", "Variant Requires Shipping",
    "Variant Taxable", "Image Src", "Image Position", "Gift Card",
    "SEO Title", "SEO Description",
    "Google Shopping / Google Product Category",
    "Google Shopping / Condition", "Google Shopping / Custom Product",
    "Google Shopping / MPN", "Product Type", "Brand", "Identifier Exist",
]


def get_output_file():
    os.makedirs("Data/shopify", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"Data/shopify/Shopify_SEO_dttire_wheels_{ts}.csv"


def slugify(text):
    text = str(text).lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def _safe_dia(val):
    """Parse diameter to int; return None on failure/zero."""
    try:
        f = float(val)
        return None if (f != f or f == 0) else int(f)
    except (TypeError, ValueError):
        return None


def _safe_width(val):
    """Parse width to float; return None on failure/zero."""
    try:
        f = float(val)
        return None if (f != f or f == 0) else f
    except (TypeError, ValueError):
        return None


def normalize_bolt_pattern(val):
    """Normalize '5x114.3', '5-114.3', '5X114.3', '[5X114.3]' → '5x114.3'."""
    val = re.sub(r"[\[\]]", "", str(val).strip())
    m = re.match(r"^(\d)[xX\-]([\d.]+)$", val)
    if not m:
        return val.lower() if val else val
    lug = m.group(1)
    try:
        pcd = float(m.group(2))
        formatted = f"{pcd:.1f}".rstrip("0").rstrip(".")
        return f"{lug}x{formatted}"
    except ValueError:
        return val


def parse_bolt_patterns(bolt_str: str) -> list:
    """Parse bolt_pattern field into a list of normalized patterns.

    DT Tire bolt pattern examples:
      '5X114.3'        → ['5x114.3']
      '[5X114.3]'      → ['5x114.3']
      '5X114.3/5X120'  → ['5x114.3', '5x120']
      ''               → []
    """
    if not bolt_str or str(bolt_str).strip() in ("", "nan"):
        return []
    cleaned = re.sub(r"[\[\]]", "", str(bolt_str).strip())
    patterns = []
    seen = set()
    for part in re.split(r"[/,]", cleaned):
        part = part.strip()
        if not part:
            continue
        norm = normalize_bolt_pattern(part)
        if norm and norm not in seen:
            patterns.append(norm)
            seen.add(norm)
    return patterns


def format_size_tag(diameter_val, width_val) -> str:
    """Format diameter + width as compact size tag: e.g. 18X70 (18x7.0)."""
    d = _safe_dia(diameter_val)
    w = _safe_width(width_val)
    if d is None:
        return ""
    if w is None:
        return str(d)
    return f"{d}X{int(round(w * 10))}"


def build_body_html(row) -> str:
    diameter = _safe_dia(row.get("_dia"))
    width    = row.get("_width")
    bolts    = row.get("_bolts", [])
    offset   = str(row.get("offset",       "")).strip()
    hub_bore = str(row.get("centre_bore",  "")).strip()
    finish   = str(row.get("finish",       "")).strip().title()
    material = str(row.get("category",     "")).strip().title()  # 'Steel', 'Alloy'

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
        f"&#8226; Burnaby &#8226; Langley &#8226; Maple Ridge "
        f"&#8226; Kelowna &#8226; Kamloops &#8226; Salmon Arm<br><br>"
        f"Expert Services &amp; Precision Fitment<br>"
        f"&#8226; TPMS Support: Professional sensor programming and installation for all makes and models.<br>"
        f"&#8226; 3D Wheel Alignment: Available at select locations to ensure your new wheels drive straight.<br>"
        f"&#8226; Real Expertise: We are BC&#8217;s dedicated tire and wheel experts&#8212;not a generalist oil change shop.<br>"
    )


def build_tags(row) -> str:
    parts = []

    brand = str(row.get("brand", "")).strip()
    if brand:
        parts.append(f"Brand_{brand.title()}")

    diameter = _safe_dia(row.get("_dia"))
    if diameter is not None:
        parts.append(f"WheelDia_{diameter}")

    size_tag = str(row.get("_size_tag", "")).strip()
    if size_tag:
        parts.append(f"Size_{size_tag}")

    for bp in row.get("_bolts", []):
        parts.append(f"BoltPattern_{bp}")

    for loc in LOCATIONS:
        parts.append(f"Location_{loc}")

    parts.append("Supplier_Dttire")

    return ",".join(parts)


def build_title(row) -> str:
    brand   = str(row.get("brand",       "")).strip()
    desc    = str(row.get("description", "")).strip()
    # Strip brand from front of description to avoid double-up
    if brand and desc.upper().startswith(brand.upper()):
        desc = desc[len(brand):].strip().lstrip("- ").strip()
    size_tag = str(row.get("_size_tag", "")).strip()
    bolts    = row.get("_bolts", [])
    bp_part  = "/".join(bolts) if bolts else ""
    return " ".join(filter(None, [brand, desc, size_tag, bp_part]))


def build_seo_title(row) -> str:
    brand   = str(row.get("brand",       "")).strip()
    desc    = str(row.get("description", "")).strip()
    if brand and desc.upper().startswith(brand.upper()):
        desc = desc[len(brand):].strip().lstrip("- ").strip()
    size_tag = str(row.get("_size_tag", "")).strip()
    bolts    = row.get("_bolts", [])
    dia      = _safe_dia(row.get("_dia"))
    dia_str  = f'{dia}"' if dia is not None else ""
    bp_str   = "/".join(bolts) if bolts else ""
    base     = " ".join(filter(None, [brand, desc, size_tag, bp_str]))
    return f"Buy {base} {dia_str} Wheels | Save Big On Tires".strip()


def build_seo_desc(row) -> str:
    brand   = str(row.get("brand",       "")).strip()
    desc    = str(row.get("description", "")).strip()
    if brand and desc.upper().startswith(brand.upper()):
        desc = desc[len(brand):].strip().lstrip("- ").strip()
    size_tag = str(row.get("_size_tag", "")).strip()
    bolts    = row.get("_bolts", [])
    dia      = _safe_dia(row.get("_dia"))
    dia_str  = f'{dia}"' if dia is not None else ""
    bp_str   = " and ".join(bolts) if bolts else ""
    bp_phrase = f" with bolt pattern {bp_str}" if bp_str else ""
    name = " ".join(filter(None, [brand, desc]))
    return (
        f"{name} {size_tag} {dia_str} wheels{bp_phrase}. "
        f"Available at Save Big On Tires across 6 BC locations — "
        f"Burnaby, Langley, Maple Ridge, Kelowna, Kamloops, and Salmon Arm."
    )


def main():
    log = get_logger("dttire_augment_wheels")

    input_file = find_latest(os.path.join("Data", "cleaned", "cleaned_wheels_*.csv"))
    if not input_file:
        log.error(
            "No cleaned_wheels_*.csv found in Data/cleaned/ — run clean.py --type wheels first."
        )
        sys.exit(1)
    log.info(f"Input file : {input_file}")

    df = pd.read_csv(input_file, dtype=str, low_memory=False).fillna("")
    log.info(f"Rows loaded: {len(df)}")
    if len(df) == 0:
        log.error(f"{input_file} has 0 rows — refusing to write an empty Shopify SEO CSV. "
                   f"Check clean.py's output for this run before re-running augment.")
        sys.exit(1)

    # ── Derived spec columns ───────────────────────────────────────────────────
    # DT wheels CSV has discrete 'diameter' and 'width' columns — use them directly
    df["_dia"]   = df["diameter"].apply(_safe_dia)
    df["_width"] = df["width"].apply(_safe_width)

    df["_size_tag"] = df.apply(
        lambda r: format_size_tag(r["diameter"], r["width"]),
        axis=1,
    )
    df["_bolts"] = df["bolt_pattern"].apply(parse_bolt_patterns)

    # ── Build output dataframe ─────────────────────────────────────────────────
    out = pd.DataFrame(index=df.index, columns=SHOPIFY_COLUMNS + ["total_qty"])
    out = out.fillna("")

    titles = df.apply(build_title, axis=1)
    titles = dedupe_titles(titles, df["sku"])

    out["Title"]      = titles
    out["Handle"] = titles.apply(slugify)
    out["Body (HTML)"] = df.apply(build_body_html, axis=1)
    out["Vendor"]     = "Save Big On Tires"
    out["Type"]       = "Wheels"
    out["Tags"]       = df.apply(build_tags, axis=1)
    out["Published"]  = "TRUE"

    out["Option1 Name"]  = "Title"
    out["Option1 Value"] = "Default Title"

    out["Variant SKU"]                 = df["sku"]
    out["Variant Inventory Tracker"]   = "shopify"
    out["Variant Inventory Policy"]    = "continue"
    out["Variant Fulfillment Service"] = "manual"
    out["Variant Requires Shipping"]   = "TRUE"
    out["Variant Taxable"]             = "TRUE"

    cost_prices          = pd.to_numeric(df["cost_price"], errors="coerce").fillna(0)
    out["Variant Price"] = (cost_prices + MARKUP).round(2).astype(str)

    out["Image Src"]      = df["image_url"] if "image_url" in df.columns else ""
    out["Image Position"] = out["Image Src"].apply(lambda s: 1 if str(s).strip() else "")
    out["Gift Card"]      = "FALSE"

    out["SEO Title"]       = df.apply(build_seo_title, axis=1)
    out["SEO Description"] = df.apply(build_seo_desc,  axis=1)

    out["Google Shopping / Google Product Category"] = (
        "Vehicles & Parts > Vehicle Parts & Accessories > Wheel & Tire Assemblies > Rims & Wheels"
    )
    out["Google Shopping / Condition"]      = "New"
    out["Google Shopping / Custom Product"] = "FALSE"
    out["Google Shopping / MPN"] = df.apply(
        lambda r: (
            f"{str(r.get('brand','')).strip()}"
            f"-{str(r.get('description','')).strip()}"
            f"-{str(r.get('_size_tag','')).strip()}"
        ),
        axis=1,
    )

    out["Type"]             = "Wheels"
    out["Product Type"]     = "Wheels"
    out["Brand"]            = df["brand"].str.strip().str.title()
    out["Identifier Exist"] = "FALSE"
    out["total_qty"]        = pd.to_numeric(
        df["qty_total"], errors="coerce"
    ).fillna(0).astype(int)

    output_file = get_output_file()
    out.to_csv(output_file, index=False, encoding="utf-8")
    log.info(f"Rows written : {len(out)}")
    log.info(f"Output file  : {output_file}")

    no_dia   = df["_dia"].isna().sum()
    no_bolts = (df["_bolts"].apply(len) == 0).sum()
    log.info(f"Missing diameter parse : {no_dia}")
    log.info(f"Missing bolt pattern   : {no_bolts}")
    log.info("Done.")

    print(f"\nRows written : {len(out)}")
    print(f"Output file  : {output_file}")
    print(f"Missing diameter : {no_dia}")
    print(f"Missing bolt pattern : {no_bolts}")
    print("Done.")


if __name__ == "__main__":
    main()
