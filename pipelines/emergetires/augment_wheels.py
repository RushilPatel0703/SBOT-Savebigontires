#!/usr/bin/env python3
"""Emerge Tires — Shopify SEO Augmentation (Wheels)
Reads Data/cleaned/cleaned_wheels_*.csv → Data/shopify/Shopify_SEO_emergetires_wheels_<ts>.csv
"""

import sys
import glob
import os
import re
import pandas as pd
from datetime import datetime

from config import price_with_markup

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared')))
from size_parser import parse_wheel_size, format_wheel_size

MARKUP_SUPPLIER = "emerge"

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


def find_input_file():
    matches = sorted(
        glob.glob("Data/cleaned/cleaned_wheels_*.csv"),
        key=os.path.getmtime,
        reverse=True,
    )
    if not matches:
        print("ERROR: No cleaned_wheels_*.csv found in Data/cleaned/ — run clean.py --type wheels first.")
        sys.exit(1)
    return matches[0]


def get_output_file():
    os.makedirs("Data/shopify", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"Data/shopify/Shopify_SEO_emergetires_wheels_{ts}.csv"


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


def _safe_dia(val):
    try:
        f = float(val)
        return None if (f != f or f == 0) else int(f)
    except (TypeError, ValueError):
        return None


# Spec notation to strip from description-derived product names
_DESC_SPEC_RE = re.compile(
    r'\b(CB|OS|O|ET|Hub|Offset)[\s:]+[-+]?\d+\.?\d*'   # 'CB: 73.1', 'O: 40', 'ET:0', 'OS:131'
    r'|\(\d+[-/onxX][\d.onxX/-]+\)',                     # bolt patterns in parens: '(5-114.3)', '(5on4.5)'
    re.IGNORECASE
)


def _desc_model_name(desc):
    """Extract the product name from the description's first segment (before ' - ').
    Strips spec notation (O:, CB:, ET:, parenthetical bolt patterns) that sometimes
    appears inline in NC descriptions without a ' - ' separator.

    'Vortex - Gloss Black w/ Machined Face - CB: 125.2 O: -24' → 'Vortex'
    'Sendel T03 BM ET:0 Hub: 81.02'                            → 'Sendel T03 BM'
    '(5-4.25/5-4.5) O:40 CB:73'                               → ''
    """
    name = str(desc).split(" - ")[0].strip()
    if re.match(r'^(CB|OS|O|ET)[\s:]', name, re.IGNORECASE):
        return ""
    name = _DESC_SPEC_RE.sub("", name)
    name = re.sub(r"\s{2,}", " ", name).strip().strip("-").strip()
    return name


def _parse_desc_offset(desc):
    """Extract offset from description 'O: 40', 'O:38', 'O: -5', 'O: 40mm'."""
    m = re.search(r'\bO:\s*([-+]?\d+\.?\d*)', str(desc))
    return m.group(1) if m else ""


def _parse_desc_hub_bore(desc):
    """Extract hub bore from description 'CB: 67.1', 'CB:54.1', 'CB: 83.82mm'."""
    m = re.search(r'\bCB:\s*(\d+\.?\d*)', str(desc))
    return m.group(1) if m else ""


def normalize_bolt_pattern(val):
    """Normalize '5x114.3', '5-114.3', '5X114.3' → '5x114.3'."""
    val = str(val).strip()
    m = re.match(r'^(\d)[xX\-]([\d.]+)$', val)
    if not m:
        return val
    lug = m.group(1)
    try:
        pcd = float(m.group(2))
        formatted = f"{pcd:.1f}".rstrip("0").rstrip(".")
        return f"{lug}x{formatted}"
    except ValueError:
        return val


def _norm_pcd(lug, pcd_str, seen, patterns):
    """Parse a PCD value, convert from inches if < 50, and append to patterns."""
    try:
        pcd = float(pcd_str)
        if pcd < 50:        # imperial inches → metric mm
            pcd = round(pcd * 25.4, 1)
        if pcd >= 50:
            formatted = f"{pcd:.1f}".rstrip("0").rstrip(".")
            key = f"{lug}x{formatted}"
            if key not in seen:
                patterns.append(key)
                seen.add(key)
    except ValueError:
        pass


def _parse_size_bolt_patterns(size_str):
    """Extract bolt patterns from NC size field.

    Handles:
      '17 5-114.3 (7.5)'      — standard metric
      '15 4-100/4-114.3 (7)'  — dual metric
      '12 5on4.5 (4)'         — imperial 'on' notation (4.5" × 25.4 = 114.3mm)
      '12 4ON4 (5)'           — imperial 'on' notation (4" × 25.4 = 101.6mm)
      '15 5-4.50 (6)'         — small imperial PCD (4.50" × 25.4 = 114.3mm)
      '15X8 5-139.7'          — DxW followed by bolt pattern
      '16X6 8-165.1'          — same
    """
    s = re.sub(r'(\d),(\d)', r'\1.\2', str(size_str).strip())
    patterns = []
    seen = set()

    # NC format: strip diameter and (width), parse bolt section in the middle
    m = re.match(r'^\d+\.?\d*\s+(.*?)\s*\(\d+\.?\d*\)\s*$', s)
    if m:
        bp_section = m.group(1).strip()
        for part in re.split(r'/', bp_section):
            part = part.strip()
            # Standard dash/x separator: '5-114.3', '5x114.3', '5-4.50'
            bm = re.match(r'^(\d+)[-xX]([\d.]+)$', part)
            if bm:
                _norm_pcd(bm.group(1), bm.group(2), seen, patterns)
                continue
            # Imperial 'on' notation: '5on4.5', '4ON4'
            bm = re.match(r'^(\d+)[Oo][Nn]([\d.]+)$', part)
            if bm:
                _norm_pcd(bm.group(1), bm.group(2), seen, patterns)

    # DxW + trailing bolt pattern: '15X8 5-139.7', '16X6 8-165.1', '22x9.5 6on139.7'
    # Also catches '15 5on4.5' (no DxW — stripping nothing, remainder = '15 5on4.5')
    if not patterns:
        remainder = re.sub(r'^\d+\.?\d*\s*[Xx]\s*\d+\.?\d*\s*', '', s).strip()
        for part in re.split(r'[\s/]+', remainder):
            bm = re.match(r'^(\d+)[-xX]([\d.]+)$', part)
            if bm:
                _norm_pcd(bm.group(1), bm.group(2), seen, patterns)
                continue
            bm = re.match(r'^(\d+)[Oo][Nn]([\d.]+)$', part)
            if bm:
                _norm_pcd(bm.group(1), bm.group(2), seen, patterns)

    return patterns


def get_bolt_patterns(row, bp_met_cols, bp_imp_cols):
    """Return list of normalized bolt patterns.
    Priority: metric spec cols → imperial spec cols → size field → description.
    """
    patterns = []
    seen = set()

    for col in bp_met_cols:
        val = str(row.get(col, "")).strip()
        if val:
            norm = normalize_bolt_pattern(val)
            if norm not in seen:
                patterns.append(norm)
                seen.add(norm)

    if not patterns:
        for col in bp_imp_cols:
            val = str(row.get(col, "")).strip()
            if val:
                norm = normalize_bolt_pattern(val)
                if norm not in seen:
                    patterns.append(norm)
                    seen.add(norm)

    if not patterns:
        # NC size field always contains the bolt pattern: '17 5-114.3 (7.5)'
        patterns = _parse_size_bolt_patterns(str(row.get("size", "")))
        seen.update(patterns)

    if not patterns:
        # Last resort: description parsing — handles '5on114.3', '10-285.75', '5-139.7'
        desc = str(row.get("description", ""))
        for bm in re.finditer(r'\b(\d{1,2})[-xXon]+([\d.]+)', desc, re.IGNORECASE):
            try:
                lug = int(bm.group(1))
                if 3 <= lug <= 12:  # sanity check: real lug counts are 3-12
                    _norm_pcd(str(lug), bm.group(2), seen, patterns)
            except ValueError:
                pass

    return patterns


def build_body_html(row):
    diameter = _safe_dia(row.get("_dia"))
    width    = row.get("_width")
    bolts    = row.get("_bolts", [])
    offset   = str(row.get("_offset",       "")).strip()
    hub_bore = str(row.get("_hub_bore",     "")).strip()
    finish   = str(row.get("spec_finish",   "")).strip().title()
    material = str(row.get("spec_material", "")).strip().title()

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


def build_tags(row):
    parts = []

    brand = _resolve_brand(row)
    if brand:
        parts.append(f"Brand_{brand.title()}")

    diameter = _safe_dia(row.get("_dia"))
    if diameter is not None:
        parts.append(f"WheelDia_{diameter}")

    size_tag = str(row.get("_size_tag", "")).strip() or format_wheel_size(str(row.get("size", "")))
    if size_tag:
        parts.append(f"Size_{size_tag}")

    for bp in row.get("_bolts", []):
        parts.append(f"BoltPattern_{bp}")

    for loc in LOCATIONS:
        parts.append(f"Location_{loc}")

    parts.append("Supplier_Emergetires")

    return ",".join(parts)


def _resolve_brand(row):
    """Brand from brand col → manufacturer_code → empty string."""
    brand = str(row.get("brand", "")).strip()
    if not brand:
        brand = str(row.get("manufacturer_code", "")).strip().title()
    return brand


def _resolve_name_parts(row):
    """Return (brand, desc_name, model) with desc_name included only when it
    adds info not already present in brand or model (e.g. 'Vortex' vs 'AT162BM').
    """
    brand     = _resolve_brand(row)
    model     = str(row.get("model", "")).strip()
    desc_name = _desc_model_name(str(row.get("description", "")))
    # Skip desc_name if it's a substring of brand or model (avoids 'Alpha' when model='Alpha-GB')
    if desc_name and (
        desc_name.lower() in brand.lower() or desc_name.lower() in model.lower()
    ):
        desc_name = ""
    return brand, desc_name, model


def build_title(row):
    brand, desc_name, model = _resolve_name_parts(row)
    size_fmt = str(row.get("_size_tag", "")).strip() or format_wheel_size(str(row.get("size", "")))
    bolts    = row.get("_bolts", [])
    bp_part  = "/".join(bolts) if bolts else ""
    return " ".join(filter(None, [brand, desc_name, model, size_fmt, bp_part]))


def build_seo_title(row, vendor):
    brand, desc_name, model = _resolve_name_parts(row)
    size_fmt = str(row.get("_size_tag", "")).strip() or format_wheel_size(str(row.get("size", "")))
    bolts    = row.get("_bolts", [])
    dia      = _safe_dia(row.get("_dia"))
    dia_str  = f'{dia}"' if dia is not None else ""
    bp_str   = "/".join(bolts) if bolts else ""
    base     = " ".join(filter(None, [brand, desc_name, model, size_fmt, bp_str]))
    return f"Buy {base} {dia_str} Wheels | {vendor}".strip()


def build_seo_desc(row):
    brand, desc_name, model = _resolve_name_parts(row)
    size_fmt = str(row.get("_size_tag", "")).strip() or format_wheel_size(str(row.get("size", "")))
    bolts    = row.get("_bolts", [])
    dia      = _safe_dia(row.get("_dia"))
    dia_str  = f'{dia}"' if dia is not None else ""
    bp_str   = " and ".join(bolts) if bolts else ""
    bp_phrase = f" with bolt pattern {bp_str}" if bp_str else ""
    name = " ".join(filter(None, [brand, desc_name, model]))
    return (
        f"{name} {size_fmt} {dia_str} wheels{bp_phrase}. "
        f"Available at Save Big On Tires across 6 BC locations — "
        f"Burnaby, Langley, Maple Ridge, Kelowna, Kamloops, and Salmon Arm."
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

    # Detect bolt pattern and hub bore spec columns at runtime (codes vary by API version)
    bp_met_cols = sorted([c for c in df.columns if re.search(r'boltpattern.*met', c)])
    bp_imp_cols = sorted([c for c in df.columns if re.search(r'boltpattern.*imp', c)])
    hb_col      = (
        next((c for c in df.columns if re.search(r'hubbore.*met', c)), None)
        or next((c for c in df.columns if 'hubbore' in c), None)
    )

    print(f"Bolt pattern metric cols  : {bp_met_cols or '(none — will use imperial or description)'}")
    print(f"Bolt pattern imperial cols: {bp_imp_cols or '(none)'}")
    print(f"Hub bore col              : {hb_col or '(none)'}")

    # Derived columns — prefer spec columns, fall back to size/description fields
    def _dia_from_row(r):
        spec = str(r.get("spec_wheeldiameter", "")).strip()
        if spec:
            try:
                v = int(float(spec))
                if v > 0:
                    return v
            except ValueError:
                pass
        d, _ = parse_wheel_size(r.get("size", ""))
        return int(d) if d is not None else None

    def _width_from_row(r):
        spec = str(r.get("spec_wheelwidth", "")).strip()
        if spec:
            try:
                v = float(spec)
                if v > 0:
                    return v
            except ValueError:
                pass
        _, w = parse_wheel_size(r.get("size", ""))
        return float(w) if w is not None else None

    def _offset_from_row(r):
        spec = str(r.get("spec_offset", "")).strip()
        if spec:
            return spec
        return _parse_desc_offset(r.get("description", ""))

    def _hub_bore_from_row(r):
        val = str(r.get(hb_col, "")).strip() if hb_col else ""
        if val:
            try:
                if float(val) > 0:
                    return val
            except ValueError:
                pass
        return _parse_desc_hub_bore(r.get("description", ""))

    df["_dia"]   = df.apply(_dia_from_row,   axis=1)
    df["_width"] = df.apply(_width_from_row, axis=1)
    df["_size_tag"] = df.apply(
        lambda r: f"{int(r['_dia'])}X{int(round(r['_width'] * 10))}"
                  if pd.notna(r["_dia"]) and pd.notna(r["_width"]) else "",
        axis=1,
    )
    df["_bolts"]    = df.apply(lambda r: get_bolt_patterns(r, bp_met_cols, bp_imp_cols), axis=1)
    df["_hub_bore"] = df.apply(_hub_bore_from_row, axis=1)
    df["_offset"]   = df.apply(_offset_from_row,   axis=1)

    out = pd.DataFrame(index=df.index, columns=SHOPIFY_COLUMNS + ["total_qty"])
    out = out.fillna("")

    vendor = "Save Big On Tires"

    out["Title"]       = df.apply(build_title,    axis=1)
    out["Handle"]      = out["Title"].apply(slugify)
    out["Body (HTML)"] = df.apply(build_body_html, axis=1)
    out["Vendor"]      = vendor
    out["Tags"]        = df.apply(build_tags,     axis=1)
    out["Published"]   = "TRUE"

    out["Option1 Name"]  = "Title"
    out["Option1 Value"] = "Default Title"

    out["Variant SKU"]                 = df["sku"]
    out["Variant Inventory Tracker"]   = "shopify"
    out["Variant Inventory Policy"]    = "continue"
    out["Variant Fulfillment Service"] = "manual"
    out["Variant Requires Shipping"]   = "TRUE"
    out["Variant Taxable"]             = "TRUE"

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
    out["Variant Price"] = price_with_markup(price_basis, MARKUP_SUPPLIER).round(2).astype(str)

    out["Image Src"]      = df["image_url"] if "image_url" in df.columns else ""
    out["Image Position"] = out["Image Src"].apply(lambda s: 1 if str(s).strip() else "")
    out["Gift Card"]      = "FALSE"

    out["SEO Title"]       = df.apply(lambda r: build_seo_title(r, vendor), axis=1)
    out["SEO Description"] = df.apply(build_seo_desc, axis=1)

    out["Google Shopping / Google Product Category"] = (
        "Vehicles & Parts > Vehicle Parts & Accessories > Wheel & Tire Assemblies > Rims & Wheels"
    )
    out["Google Shopping / Condition"]      = "New"
    out["Google Shopping / Custom Product"] = "FALSE"
    out["Google Shopping / MPN"] = df.apply(
        lambda r: (
            f"{_resolve_brand(r)}"
            f"-{str(r.get('model','')).strip()}"
            f"-{format_wheel_size(str(r.get('size','')))}"
        ),
        axis=1,
    )

    out["Type"]             = "Wheels"
    out["Product Type"]     = "Wheels"
    # Brand column previously read df["brand"] directly, bypassing the
    # brand -> manufacturer_code fallback that _resolve_brand() already
    # applies for Title/Tags/Body — MPN above and Brand here now use it too.
    out["Brand"]            = df.apply(_resolve_brand, axis=1).str.strip().str.title()
    out["Identifier Exist"] = "FALSE"
    out["total_qty"]        = pd.to_numeric(
        df.get("available_quantity_related_branches", df["available_quantity"]),
        errors="coerce",
    ).fillna(0).astype(int)

    output_file = get_output_file()
    out.to_csv(output_file, index=False, encoding="utf-8")
    print(f"Rows written : {len(out)}")
    print(f"Output file  : {output_file}")

    no_dia   = df["_dia"].isna().sum()
    no_bolts = (df["_bolts"].apply(len) == 0).sum()
    print(f"Missing diameter parse : {no_dia}")
    print(f"Missing bolt pattern   : {no_bolts}")
    print("Done.")


if __name__ == "__main__":
    main()
