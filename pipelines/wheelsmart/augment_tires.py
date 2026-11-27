#!/usr/bin/env python3
"""WheelMart Canada — Shopify SEO Augmentation (Tires)

Reads Data/raw/wheelmart_tires_*.csv (most recent by mtime), enriches
quantities from the WheelMart API, filters low-qty rows, extracts brand/
model/designation from description text, and writes a Shopify-ready CSV to
Data/shopify/Shopify_SEO_wheelsmart_tires_<ts>.csv.

Run from pipelines/wheelsmart/ — all paths are relative.
"""

import sys
import glob
import os
import re
import time
import requests
import pandas as pd
from datetime import datetime

# APPEND, never insert(0): every pipeline has its own config.py and so does
# shared/. Putting shared/ first on the path shadows the local one and silently
# swaps in the wrong markup.
sys.path.append(os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "shared")))
import no_price

from config import price_with_markup

# WheelMart has no separate clean.py — this augment step reads the raw scrape
# directly, so it's also where a stale "latest" raw file (scraper step
# silently produced nothing this run) would otherwise go unnoticed. See
# dttire/clean.py for the fuller rationale.
STALE_INPUT_WARN_HOURS = 24

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared')))
from terrain import terrain_tag
from size_parser import parse_tire_size, default_passenger_load_range

# ── Constants ─────────────────────────────────────────────────────────────────

MARKUP_SUPPLIER = "wheelsmart"
LOW_QTY_THRESHOLD = 4          # keep if qty >= 4; drop if qty < 4
API_URL = (
    "https://wheelmartcanada.ca/v1/tires/"
    "?api_client_id=5&api_key=IhSkw9YNImRW3QKIZowLePXeSvxFqM"
)

LOCATIONS = [
    "Burnaby", "Langley", "MapleRidge",
    "Kelowna", "Kamloops", "SalmonArm",
]

DESIGNATION_LABEL = {
    "Winter":      "winter",
    "All-Weather": "all-weather",
    "All-Season":  "all-season",
    "Unknown":     "unknown",
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

# Tokens that appear between size and brand but are NOT the brand name
_NON_BRAND_TOKENS = frozenset({
    'STD', 'XL', 'ORB', 'RSC', 'RF', 'RFT', 'TL', 'TT', 'HP', 'UHP',
    'AT', 'MT', 'HT', 'RT', 'OWL', 'BSW', 'WSW', 'RBL',
})

# Keywords that terminate model name extraction from description
_DESIG_STOP = re.compile(
    r'\b(?:ALL[\s-]SEASON|ALL[\s-]WEATHER|A/S|A/W|SNOW|WINTER|MUD[\s-]SNOW|'
    r'M\+S|M/S|HIGH[\s-]PERFORMANCE|PERFORMANCE)\b',
    re.IGNORECASE,
)

VALID_SPEED_RATINGS = {
    "A1","A2","A3","A4","A5","A6","A7","A8",
    "B","C","D","E","F","G","H","J","K","L",
    "M","N","P","Q","R","S","T","U","V","W","Y","Z","ZR",
}

# ── Load-range helpers (verbatim from northcountry/augment_tires.py) ──────────

PR_TO_LOAD_RANGE = {
    '4': 'B', '6': 'C', '8': 'D', '10': 'E', '12': 'F', '14': 'G', '16': 'H',
}
_PR_PATTERN = re.compile(r'\b(4|6|8|10|12|14|16)\s*-?\s*(?:PR|PLY)\b', re.IGNORECASE)


def extract_load_range(size: str, description: str = "") -> str:
    # 1. Explicit load range letter at end of size string (e.g. "LT235/75R15 E")
    m = re.search(r'\s+([B-N])\s*$', size.strip())
    if m:
        return m.group(1)
    # 2. PR/PLY count → load range letter
    m = _PR_PATTERN.search(size)
    if m:
        return PR_TO_LOAD_RANGE.get(m.group(1), '')
    m = _PR_PATTERN.search(str(description))
    if m:
        return PR_TO_LOAD_RANGE.get(m.group(1), '')
    # 3. XL / Extra Load — suffix in load+speed token (e.g. 98VXL, 110HXL) or standalone
    if re.search(r'(?<=[0-9A-Za-z])XL\b|\bXL\b|\bExtra.?Load\b', str(description), re.IGNORECASE):
        return 'XL'
    # 4. C-class cargo/van tire → load range C (e.g. 225/65R16C, LT195/75R16C)
    size_upper = size.upper()
    if re.search(r'R\d{2,3}C\b', size_upper):
        return 'C'
    # 5. Standard Load (SL) — passenger/SUV tires without LT/ST prefix
    if not size_upper.startswith(('LT', 'ST')):
        return 'SL'
    return ''


# ── Body-field derivation helpers ─────────────────────────────────────────────

def _extract_load_index_from_desc(description: str) -> str:
    """Extract primary load index from description when raw CSV column is empty."""
    s = description.strip()
    # Strip mud format size
    s = re.sub(r'^(?:LT|ST|P)?\d{2,3}[Xx]\d+\.?\d+[Rr]\d{2,3}\s*', '', s, flags=re.IGNORECASE)
    # Strip residual LT/ST that trails mud sizes (e.g. "35X12.50R22LT " or "LT(POR) " after cleaning)
    s = re.sub(r'^(?:LT|ST)\s+', '', s, flags=re.IGNORECASE)
    # Strip standard slash format size
    s = re.sub(r'^(?:LT|ST|P)?\d{3}/\d{2,3}[A-Za-z]*R?[Ff]?\d{2,3}[A-Za-z]*\s*', '', s, flags=re.IGNORECASE)
    # First number is load index (possibly dual like 119/116)
    m = re.match(r'(\d{2,3})(?:/\d{2,3})?', s.strip())
    if m:
        return m.group(1)
    # Inverted speed+load format (e.g. "S109" where S=speed, 109=load index)
    m2 = re.match(r'^[A-Z](\d{2,3})', s.strip(), re.IGNORECASE)
    if m2:
        return m2.group(1)
    return ''


# ── File helpers ──────────────────────────────────────────────────────────────

def find_input_file() -> str:
    matches = sorted(
        glob.glob("Data/raw/wheelmart_tires_*.csv"),
        key=os.path.getmtime,
        reverse=True,
    )
    if not matches:
        print("ERROR: No wheelmart_tires_*.csv found in Data/raw/")
        sys.exit(1)
    return matches[0]


def get_output_file() -> str:
    os.makedirs("Data/shopify", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"Data/shopify/Shopify_SEO_wheelsmart_tires_{ts}.csv"


# ── Quantity enrichment ───────────────────────────────────────────────────────

def fetch_api_quantities() -> dict:
    """Return {str(WebID): int(Quantity)} from the WheelMart API."""
    resp = requests.get(API_URL, timeout=30)
    resp.raise_for_status()
    return {str(item["WebID"]): int(item["Quantity"]) for item in resp.json()}


def coerce_quantity(val) -> int:
    """Convert quantity_on_hand to int. '20+' → 20, negative → keep, non-numeric → 0."""
    s = str(val).strip()
    if s.endswith("+"):
        try:
            return int(s[:-1])
        except ValueError:
            return 20
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def enrich_quantities(df: pd.DataFrame, api_qty: dict) -> pd.DataFrame:
    """Replace quantity_on_hand with API values where WebID matches."""
    def _enrich(row):
        web_id = str(row["web_id"]).strip()
        if web_id in api_qty:
            return api_qty[web_id]
        return coerce_quantity(row["quantity_on_hand"])

    df["quantity_on_hand"] = df.apply(_enrich, axis=1).astype(int)
    return df


# ── Field extraction ──────────────────────────────────────────────────────────

def extract_sku(raw_sku: str) -> str:
    """Split 'TIRE:18\\' TIRE:2HH2385H1' on ':' and return the last part."""
    parts = str(raw_sku).split(":")
    return parts[-1].strip()


def reconstruct_size(row) -> str:
    """Build size string from description (primary) or structured columns (fallback)."""
    desc  = str(row.get("description", ""))
    width = str(row.get("width", "")).strip()
    ratio = str(row.get("aspect_ratio", "")).strip()
    rim   = str(row.get("rim_diameter", "")).strip()

    # Detect LT/ST/P prefix from start of description
    prefix_match = re.match(r'^(LT|ST|P)(?=\d)', desc.strip(), re.IGNORECASE)
    prefix = prefix_match.group(1).upper() if prefix_match else ""

    # Mud/flotation format first: LT33X12.50R20, 35X12.50R17LT, etc.
    m_mud = re.match(
        r'^(?:LT|ST|P)?(\d{2,3})[Xx](\d+\.?\d+)[Rr](\d{2,3})',
        desc.strip(),
        re.IGNORECASE,
    )
    if m_mud:
        return f"{prefix}{m_mud.group(1)}X{m_mud.group(2)}R{m_mud.group(3)}"

    if not width or not ratio or not rim:
        # Fall back: standard slash format from description
        m = re.match(
            r'^(?:LT|ST|P)?(\d{3})/(\d{2,3})[A-Za-z]*R(\d{2,3})',
            desc.strip(),
            re.IGNORECASE,
        )
        if m:
            width = m.group(1)
            ratio = m.group(2)
            rim   = m.group(3)

    size = f"{prefix}{width}/{ratio}R{rim}"

    # C-class notation (van/cargo tires): description contains R##C
    if re.search(r'R\d{2}C\b', desc, re.IGNORECASE):
        if not size.endswith("C"):
            size += "C"

    return size


def clean_description(desc: str) -> str:
    """Normalize known WheelMart description formatting errors before parsing."""
    # Double slash in size: LT235//85R16 → LT235/85R16
    desc = re.sub(r'(?<=[0-9])//(?=[0-9])', '/', desc)
    # Parenthesis immediately after LT/ST/P prefix: LT(33X → LT33X
    desc = re.sub(r'^(LT|ST|P)\s*\(', r'\1', desc, flags=re.IGNORECASE)
    # Parenthetical inline codes that interrupt spec tokens: (POR), (AO), (BSRR)
    desc = re.sub(r'\([A-Z/]{1,10}\)', ' ', desc)
    desc = re.sub(r'\s+', ' ', desc).strip()
    return desc


def extract_brand(description: str) -> str:
    """Strip size + load/speed token from description, return first brand token."""
    s = description.strip()

    # 1a. Mud/flotation format (e.g. LT33X12.50R20, 35X12.50R17)
    s = re.sub(
        r'^(?:LT|ST|P)?\d{2,3}[Xx]\d+\.?\d+[Rr]\d{2,3}\s*',
        '', s, count=1, flags=re.IGNORECASE,
    )
    # Strip residual LT/ST that trails some mud sizes (e.g. "35X12.50R22LT" → leaves "LT ")
    s = re.sub(r'^(?:LT|ST)\s+', '', s, flags=re.IGNORECASE)

    # 1b. Standard slash format (e.g. 235/45ZR18, LT265/70R17, 225/65R16C, 225/50R17XL)
    s = re.sub(
        r'^(?:LT|ST|P)?\d{3}/\d{2,3}[A-Za-z]*R?[Ff]?\d{2,3}[A-Za-z]{0,2}\s*',
        '', s, count=1, flags=re.IGNORECASE,
    )

    # 2. Dual load index (e.g. "121/118S", "112/110T - 8PR") — PR suffix optional
    s = re.sub(
        r'^\d{2,3}/\d{2,3}[A-Za-z]\s*(?:[-–]\s*\d+\s*(?:PR|PLY)\s*)?',
        '', s, flags=re.IGNORECASE,
    )

    # 3. Standalone PR/PLY left after dual-load strip (e.g. "8PR LANVIGATOR")
    s = re.sub(r'^\d+\s*(?:PR|PLY)\s*', '', s, flags=re.IGNORECASE)

    # 4. Standard adjacent load+speed (e.g. "98VXL", "113XL", "104H", "110HXL")
    #    Avoid consuming into adjacent brand: "113XLDELINTE" → strip "113XL" not "113XLD"
    s = re.sub(r'^\d{2,3}(?:[A-Za-z]?XL|[A-Za-z]{1,2})\s*', '', s)

    # 4b. Dash-separated PR/PLY count after load+speed (e.g. "- 10PR POWERTRAC")
    s = re.sub(r'^[-–]\s*\d+\s*(?:PR|PLY)\s*', '', s, flags=re.IGNORECASE)

    # 5. Space-separated load+speed (e.g. "110 H")
    s = re.sub(r'^\d{2,3}\s+[A-Za-z]\s*', '', s)

    # 6. First token starting with a letter, skipping category codes (STD, XL, HP, etc.)
    for token in s.strip().split():
        brand = token.upper()
        if brand and brand not in _NON_BRAND_TOKENS and re.match(r'^[A-Z]', brand):
            return brand
    return ""


def extract_model(description: str, brand: str) -> str:
    """Return words between brand and first designation keyword."""
    s = description.strip()

    # Find brand position (case-insensitive)
    brand_pat = re.compile(re.escape(brand), re.IGNORECASE)
    m = brand_pat.search(s)
    if not m:
        return ""

    after_brand = s[m.end():].strip()

    # Stop at first designation keyword
    stop = _DESIG_STOP.search(after_brand)
    model_raw = after_brand[:stop.start()].strip() if stop else after_brand.strip()

    # Strip trailing dashes/spaces
    model_raw = model_raw.strip(" -").strip()
    return model_raw


def extract_designation(description: str) -> str:
    """Derive designation from keywords in description text."""
    desc_upper = description.upper()

    # ALL WEATHER takes priority over SNOW
    if re.search(r'\bALL[\s-]WEATHER\b|\bA/W\b', desc_upper):
        return "All-Weather"

    # WINTER/SNOW substring catches WINTERGRIP, CATCHSNOW, SNOWSTAR, etc.
    if "WINTER" in desc_upper or "SNOW" in desc_upper:
        return "Winter"

    if re.search(r'\bALL[\s-]SEASON\b|\bA/S\b', desc_upper):
        return "All-Season"

    # Terrain/service types → All-Season (run year-round)
    if re.search(r'\bA/T\b|\bA-T\b|\bALL[\s-]TERRAIN\b', desc_upper):
        return "All-Season"
    if re.search(r'\bM/T\b|\bM-T\b|\bMUD[\s-]TERRAIN\b|\bMUD\s*&\s*SNOW\b', desc_upper):
        return "All-Season"
    if re.search(r'\bH/T\b|\bH-T\b|\bHIGHWAY[\s-]TERRAIN\b', desc_upper):
        return "All-Season"
    if re.search(r'\bR/T\b|\bR-T\b', desc_upper):
        return "All-Season"

    return "Unknown"


def extract_speed_rating(description: str) -> str:
    """Extract speed rating letter from load+speed token in description."""
    s = description.strip()

    # 1a. Mud/flotation format (e.g. LT33X12.50R20, 35X12.50R17)
    s = re.sub(
        r'^(?:LT|ST|P)?\d{2,3}[Xx]\d+\.?\d+[Rr]\d{2,3}\s*',
        '', s, count=1, flags=re.IGNORECASE,
    )
    # Strip residual LT/ST that trails some mud sizes (e.g. "35X12.50R22LT" → leaves "LT ")
    s = re.sub(r'^(?:LT|ST)\s+', '', s, flags=re.IGNORECASE)

    # 1b. Standard slash format
    s = re.sub(
        r'^(?:LT|ST|P)?\d{3}/\d{2,3}[A-Za-z]*R?[Ff]?\d{2,3}[A-Za-z]*\s*',
        '', s, count=1, flags=re.IGNORECASE,
    )

    # 2. Dual load index — capture speed letter (e.g. "121/118S", "112/110T - 8PR")
    m_dual = re.match(
        r'^(\d{2,3})/(\d{2,3})([A-Z])\s*(?:[-–]\s*\d+\s*(?:PR|PLY)\s*)?',
        s.strip(), re.IGNORECASE,
    )
    if m_dual:
        rating = m_dual.group(3).upper()
        if rating in VALID_SPEED_RATINGS:
            return rating

    # 3. Standard adjacent load+speed: "98VXL", "114Q - 10PR", "104H"
    m = re.match(r'(\d{2,3})([A-Z])', s.strip(), re.IGNORECASE)
    if m:
        rating = m.group(2).upper()
        if rating in VALID_SPEED_RATINGS:
            return rating

    # 4. Space-separated: "110 H"
    m2 = re.match(r'\d{2,3}\s+([A-Z])\b', s.strip(), re.IGNORECASE)
    if m2:
        rating = m2.group(1).upper()
        if rating in VALID_SPEED_RATINGS:
            return rating

    # 5. Inverted speed-then-load: "S109" (some DEL format)
    m3 = re.match(r'^([A-Z])(\d{2,3})', s.strip(), re.IGNORECASE)
    if m3:
        rating = m3.group(1).upper()
        if rating in VALID_SPEED_RATINGS:
            return rating

    # 6. Search anywhere in the size-stripped description for a load+speed token
    #    Handles cases where brand name precedes the load index (e.g. "SUREGRIP PRO SNOW 92V XL GRIPMAX")
    #    Pattern accepts optional trailing XL (e.g. "108YXL") and requires space/end after
    m_any = re.search(r'\b(\d{2,3})([A-Z])(?:XL)?(?:\s|$)', s)
    if m_any:
        rating = m_any.group(2).upper()
        if rating in VALID_SPEED_RATINGS:
            return rating

    # 7. ZR in size notation (e.g. "285/30ZR21") indicates Z speed rating (ZR = Z-rated)
    #    No \b needed — ZR follows aspect ratio digits with no word boundary
    if re.search(r'ZR\d{2}', description, re.IGNORECASE):
        return "Z"

    return ""


# ── Shopify output builders ───────────────────────────────────────────────────

def slugify(text: str) -> str:
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


def build_title(brand: str, size: str, model: str, designation: str) -> str:
    if designation == "Winter":
        desig_label = "(3 Peak Snowflake) ❄️"
    elif designation == "All-Weather":
        desig_label = "All Weather"
    else:
        desig_label = ""

    parts = [p for p in [brand, size, model, desig_label] if p]
    title = " ".join(parts)
    # Collapse double spaces
    title = re.sub(r"\s{2,}", " ", title).strip()
    return title


def build_body_html(
    brand: str,
    model: str,
    width: str,
    aspect_ratio: str,
    rim_diameter: str,
    load_index: str,
    speed_rating: str,
    load_range: str,
    designation: str,
) -> str:
    return (
        f"<br>Brand: {brand}<br>"
        f"Style: {model}<br>"
        f"Section width: {width}<br>"
        f"Aspect ratio: {aspect_ratio}<br>"
        f"Rim diameter: {rim_diameter}<br>"
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


def build_tags(
    brand: str,
    size: str,
    rim_diameter: str,
    designation: str,
    speed_rating: str,
    load_range: str,
    description: str = "",
) -> str:
    parts = []

    if brand:
        parts.append(f"Brand_{brand.title()}")

    size_tag = ""
    if size:
        # canonical Size_: strip speed-rating letter before R+digits, drop dots,
        # normalize X-as-rim-separator, hoist trailing LT/ST to the front
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
                rim_tag = str(rim_diameter).replace(".", "")
                if not rim_tag:
                    # Extract from reconstructed size when raw column is blank
                    m_rim = re.search(r'[Rr](\d{2,3})[CcXx]?$', size)
                    if m_rim:
                        rim_tag = m_rim.group(1)
                if rim_tag:
                    parts.append(f"RimDia_{rim_tag}")

    if designation != "Unknown":
        parts.append(f"Type_{designation}")
    _terr = terrain_tag(description)
    if _terr:
        parts.append(_terr)

    pfx_m = re.match(r'^(LT|ST|P)', size_tag, re.IGNORECASE)
    pfx = pfx_m.group(1).upper() if pfx_m else ""
    if pfx == "LT":
        parts.append("Type_Light-Truck")
    if pfx == "ST":
        parts.append("Type_Trailer")

    if speed_rating:
        parts.append(f"SpeedRating_{speed_rating}")
        if speed_rating in PERFORMANCE_RATINGS:
            parts.append("Type_Performance")

    if load_range:
        parts.append(f"LoadRange_{load_range}")

    for loc in LOCATIONS:
        parts.append(f"Location_{loc}")

    parts.append("Supplier_Wheelsmart")

    return ",".join(parts)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # ── Step 0: find input ────────────────────────────────────────────────────
    input_file = find_input_file()
    print(f"Input file : {input_file}")
    age_hours = (time.time() - os.path.getmtime(input_file)) / 3600
    if age_hours > STALE_INPUT_WARN_HOURS:
        print(
            f"  WARNING: input file is {age_hours / 24:.1f} day(s) old "
            f"(> {STALE_INPUT_WARN_HOURS}h threshold). Proceeding, but this "
            f"usually means scraper_tires.py didn't produce a fresh file this "
            f"run — verify before trusting this output."
        )

    df = pd.read_csv(input_file, dtype=str, low_memory=False).fillna("")
    total_loaded = len(df)
    print(f"Rows loaded: {total_loaded}")
    if total_loaded == 0:
        print(f"ERROR: {input_file} has 0 rows — refusing to write an empty Shopify SEO CSV. "
              f"Check scraper_tires.py's output for this run before re-running augment.")
        sys.exit(1)

    # Deduplicate by SKU (raw CSV sometimes contains the same SKU twice)
    before_dedup = len(df)
    df = df.drop_duplicates(subset=["sku"], keep="first").reset_index(drop=True)
    if len(df) < before_dedup:
        print(f"  Duplicates removed: {before_dedup - len(df)}")

    # ── Step 1: quantity enrichment ───────────────────────────────────────────
    print("Fetching quantities from WheelMart API...")
    try:
        api_qty = fetch_api_quantities()
        print(f"  API returned {len(api_qty)} products")
    except Exception as exc:
        print(f"  WARNING: API fetch failed ({exc}); using raw CSV quantities")
        api_qty = {}

    df = enrich_quantities(df, api_qty)
    matched_count = sum(
        1 for wid in df["web_id"].astype(str).str.strip() if wid in api_qty
    )
    print(f"  Quantity matched: {matched_count} / {total_loaded} rows")

    # ── Step 2: low-qty filter ────────────────────────────────────────────────
    keep_mask = df["quantity_on_hand"] >= LOW_QTY_THRESHOLD
    low_qty_df = df[~keep_mask].copy()
    df = df[keep_mask].reset_index(drop=True)

    # ── Step 2b: quarantine rows with no usable cost price ───────────────────
    # Wheelsmart prices off user_group_price (the wholesale login span). Rows
    # where that span is absent used to fall back to retail_price, which priced
    # them at retail + markup instead of cost + markup. They are now excluded
    # and written to the repo-level "no price detected" folder. Done here, on
    # the post-low-qty frame and BEFORE `out` is built, so the two frames stay
    # index-aligned.
    df, _, no_price_summary = no_price.split_frame(
        df, "wheelsmart", "tires", cost_col="user_group_price")
    no_price.report(no_price_summary, "wheelsmart", "tires", "user_group_price")
    df = df.reset_index(drop=True)

    total_dropped = len(low_qty_df)
    total_kept    = len(df)

    os.makedirs("Data", exist_ok=True)
    low_qty_df.to_csv("Data/low_quantity_tires.csv", index=False, encoding="utf-8")

    print(f"Total rows: {total_loaded} | Dropped (low qty): {total_dropped} | Kept: {total_kept}")

    if total_kept == 0:
        print(f"ERROR: No rows remain after the qty >= {LOW_QTY_THRESHOLD} filter "
              f"({total_loaded} loaded, all dropped) — refusing to write an empty Shopify SEO CSV. "
              f"Check the raw scrape / WheelMart API quantity fetch before re-running.")
        sys.exit(1)

    # ── Step 3: field extraction ──────────────────────────────────────────────
    df["description"] = df["description"].apply(clean_description)
    df["_sku"]          = df["sku"].apply(extract_sku)
    df["_size"]         = df.apply(reconstruct_size, axis=1)
    df["_brand"]        = df["description"].apply(extract_brand)
    df["_model"]        = df.apply(
        lambda r: extract_model(r["description"], r["_brand"]), axis=1
    )
    # Enrich raw width/aspect/rim/load_index from _size when scraper couldn't parse them
    # (LT-prefix, C-class, and dual-load-index tires leave these columns empty in raw CSV)
    for i in df.index:
        if not str(df.at[i, "aspect_ratio"]).strip():
            parsed = parse_tire_size(df.at[i, "_size"], df.at[i, "description"])
            if not str(df.at[i, "width"]).strip():
                df.at[i, "width"] = parsed["width"]
            df.at[i, "aspect_ratio"] = parsed["ratio"]
            if not str(df.at[i, "rim_diameter"]).strip():
                df.at[i, "rim_diameter"] = parsed["rim_display"]
        if not str(df.at[i, "load_index"]).strip():
            df.at[i, "load_index"] = _extract_load_index_from_desc(df.at[i, "description"])

    df["_designation"]  = df["description"].apply(extract_designation)
    df["_speed_rating"] = df["description"].apply(extract_speed_rating)
    df["_load_range"]   = df.apply(
        lambda r: extract_load_range(r["_size"], r["description"])
                  or default_passenger_load_range(r["_size"]), axis=1
    )

    # ── Step 4: build output DataFrame ───────────────────────────────────────
    out = pd.DataFrame(index=df.index, columns=SHOPIFY_COLUMNS + ["total_qty"])
    out = out.fillna("")

    n     = len(df)
    third = n // 3

    titles = [
        build_title(
            df.at[i, "_brand"],
            df.at[i, "_size"],
            df.at[i, "_model"],
            df.at[i, "_designation"],
        )
        for i in df.index
    ]

    out["SKU"]         = df["_sku"]
    out["Variant SKU"] = df["_sku"]
    out["Title"]       = titles

    # Disambiguate duplicate titles by appending load index
    title_counts = pd.Series(titles).value_counts()
    dup_title_set = set(title_counts[title_counts > 1].index)
    if dup_title_set:
        for i in df.index:
            if out.at[i, "Title"] in dup_title_set:
                li = str(df.at[i, "load_index"]).strip()
                if li:
                    out.at[i, "Title"] = f"{out.at[i, 'Title']} {li}"

    out["Handle"]      = out["Title"].apply(slugify)

    out["Body (HTML)"] = [
        build_body_html(
            brand        = df.at[i, "_brand"],
            model        = df.at[i, "_model"],
            width        = df.at[i, "width"],
            aspect_ratio = df.at[i, "aspect_ratio"],
            rim_diameter = df.at[i, "rim_diameter"],
            load_index   = df.at[i, "load_index"],
            speed_rating = df.at[i, "_speed_rating"],
            load_range   = df.at[i, "_load_range"],
            designation  = df.at[i, "_designation"],
        )
        for i in df.index
    ]

    out["Vendor"]    = "Save Big On Tires"
    out["Type"]      = "Tires"
    out["Published"] = "TRUE"

    out["Tags"] = [
        build_tags(
            brand        = df.at[i, "_brand"],
            size         = df.at[i, "_size"],
            rim_diameter = df.at[i, "rim_diameter"],
            designation  = df.at[i, "_designation"],
            speed_rating = df.at[i, "_speed_rating"],
            load_range   = df.at[i, "_load_range"],
            description  = df.at[i, "description"],
        )
        for i in df.index
    ]

    out["Option1 Name"]  = "Title"
    out["Option1 Value"] = "Default Title"

    out["Variant Inventory Tracker"]   = "shopify"
    out["Variant Inventory Policy"]    = "continue"
    out["Variant Fulfillment Service"] = "manual"

    # Price off true cost (user_group_price — the wholesale login price span),
    # not the supplier's suggested retail_price. Some rows have a missing/blank/
    # zero user_group_price (no price span found on the page), and older raw
    # CSVs may not have the column at all — in either case, warn and fall back
    # to retail_price for those rows rather than failing.
    retail_prices = pd.to_numeric(df["retail_price"], errors="coerce").fillna(0)
    if "user_group_price" in df.columns:
        cost_prices = pd.to_numeric(df["user_group_price"], errors="coerce")
    else:
        print("  WARNING: raw CSV has no user_group_price column — using retail_price for all rows.")
        cost_prices = pd.Series(float("nan"), index=df.index)

    # Rows with no usable cost were quarantined in step 2b, so this can only
    # still fire when the column is absent from the raw CSV entirely — a
    # pre-migration scrape, where falling back to retail beats dropping the
    # whole catalogue.
    missing_cost_mask = cost_prices.isna() | (cost_prices <= 0)
    missing_cost_count = int(missing_cost_mask.sum())
    if missing_cost_count:
        print(
            f"  WARNING: {missing_cost_count} / {total_kept} rows have missing/zero "
            f"user_group_price — falling back to retail_price for those rows."
        )

    df["_cost_price"]    = cost_prices.where(~missing_cost_mask, retail_prices)
    out["Variant Price"] = price_with_markup(df["_cost_price"], MARKUP_SUPPLIER).round(2).astype(str)

    out["Variant Requires Shipping"] = "TRUE"
    out["Variant Taxable"]           = "TRUE"
    out["Image Src"]                 = df["image"]
    out["Image Position"]            = out["Image Src"].apply(lambda s: 1 if str(s).strip() else "")
    out["Gift Card"]                 = "FALSE"

    # SEO Title — 3-way rotation
    vendor = "Save Big On Tires"
    seo_titles = []
    for i_pos, idx in enumerate(df.index):
        brand       = df.at[idx, "_brand"]
        size        = df.at[idx, "_size"]
        model       = df.at[idx, "_model"]
        designation = df.at[idx, "_designation"]
        base = re.sub(r"\s{2,}", " ", f"{brand} {size} {model} {designation}".strip())
        if i_pos < third:
            seo_titles.append(f"Buy {base} | {vendor}"[:160])
        elif i_pos < third * 2:
            seo_titles.append(f"Purchase {base} | {vendor}"[:160])
        else:
            seo_titles.append(f"{base} Tires near me | {vendor}"[:160])
    out["SEO Title"] = seo_titles

    # SEO Description — 3-way rotation
    seo_descs = []
    for i_pos, idx in enumerate(df.index):
        brand       = df.at[idx, "_brand"]
        model       = df.at[idx, "_model"]
        size        = df.at[idx, "_size"]
        designation = df.at[idx, "_designation"]
        d_lower     = designation.lower()

        if i_pos < third:
            seo_descs.append(
                f"{brand} {model} tire in size {size}, designed for {d_lower} driving. "
                f"Suitable for passenger vehicles, SUVs, and light trucks, "
                f"offering dependable performance and road safety."
            )
        elif i_pos < third * 2:
            seo_descs.append(
                f"{brand} {model} tire, size {size}, built for reliable {d_lower} performance. "
                f"Engineered for consistent handling, durability, and everyday "
                f"driving across multiple vehicle types."
            )
        else:
            seo_descs.append(
                f"{brand} {model} {d_lower} tire in size {size}, designed for stable "
                f"handling and long-lasting performance. Ideal for drivers seeking "
                f"dependable traction and durability."
            )
    out["SEO Description"] = seo_descs

    out["Google Shopping / Google Product Category"] = (
        "Vehicles & Parts > Vehicle Parts & Accessories"
    )
    out["Google Shopping / Condition"]    = "New"
    out["Google Shopping / Custom Product"] = "FALSE"
    out["Google Shopping / MPN"] = [
        f"{df.at[idx, '_brand']}-{df.at[idx, '_size']}-{df.at[idx, '_designation']}"
        for idx in df.index
    ]
    out["Google Shopping / Custom Label 0"] = df["_designation"].map(
        lambda d: DESIGNATION_LABEL.get(d, "unknown")
    )

    out["Product Type"]     = "Vehicle Tires"
    out["Brand"]            = df["_brand"]
    out["Identifier Exist"] = "FALSE"
    out["total_qty"]        = df["quantity_on_hand"].astype(int)

    # ── Write output ──────────────────────────────────────────────────────────
    output_file = get_output_file()
    out.to_csv(output_file, index=False, encoding="utf-8")

    # ── Summary ───────────────────────────────────────────────────────────────
    missing_brand       = (df["_brand"] == "").sum()
    missing_designation = (df["_designation"] == "Unknown").sum()
    missing_speed       = (df["_speed_rating"] == "").sum()

    print(f"\nOutput file      : {output_file}")
    print(f"Rows written     : {len(out)}")
    print(f"Missing brand    : {missing_brand}")
    print(f"Missing desig.   : {missing_designation}  (designation=Unknown)")
    print(f"Missing speed    : {missing_speed}")
    print(f"Missing cost     : {missing_cost_count}  (fell back to retail_price)")
    print("Done.")


if __name__ == "__main__":
    main()
