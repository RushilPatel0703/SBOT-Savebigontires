#!/usr/bin/env python3
"""WheelMart Canada — Rim/Wheel Shopify SEO Augmentation
Reads the most-recent Data/raw/wheelmart_rims_*.csv and outputs
Data/Shopify/Shopify_SEO_wheelsmart_rims_TIMESTAMP.csv.

Run from pipelines/wheelsmart/ directory:
    python augment_rims.py
"""

import glob
import logging
import os
import re
import sys
import time
from datetime import datetime

import pandas as pd

from config import markup_for


# ── Constants ─────────────────────────────────────────────────────────────────

MARKUP            = markup_for("wheelsmart")

# Canonical SKU collision resolution, shared with production/steps/ingest.py so
# both derivations of the WheelMart rim SKU stay identical.
from config import disambiguate_skus
LOW_QTY_THRESHOLD = 4   # qty >= 4 kept; qty <= 3 dropped

# WheelMart has no separate clean.py — this augment step reads the raw scrape
# directly, so it's also where a stale "latest" raw file (scraper step
# silently produced nothing this run) would otherwise go unnoticed. See
# dttire/clean.py for the fuller rationale.
STALE_INPUT_WARN_HOURS = 24

VENDOR  = "Save Big On Tires"
SUPPLIER_TAG = "Supplier_Wheelsmart"

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

GOOGLE_PRODUCT_CATEGORY = (
    "Vehicles & Parts > Vehicle Parts & Accessories > "
    "Wheel & Tire Assemblies > Rims & Wheels"
)

# PCD values that WheelMart truncates (missing the decimal part)
PCD_CORRECTIONS = {
    "139": "139.7",
    "114": "114.3",
    "165": "165.1",
    "127": "127.0",
    "120": "120.0",
    "112": "112.0",
    "100": "100.0",
    "108": "108.0",
    "98":  "98.0",
    "135": "135.0",
    "4.5": "114.3",
}

# Words that are never brands on their own
GENERIC_WORDS = {
    "wheel", "wheels", "rim", "rims", "disc", "discs",
    "black", "chrome", "silver", "gold", "white", "bronze",
    "titanium", "gunmetal", "graphite", "face", "lip", "spoke",
    "gloss", "matte", "matt", "machined", "polished", "painted",
    "dark", "grey", "gray", "style", "finish", "steel",
    "b+m", "bm", "gm", "gb", "mf", "mgra",
    "machine", "satin", "hyper", "multi", "metallic",
    "lupo", "road", "red", "dirt", "advanti",
    "metal", "rivets", "letters", "clear", "coat", "windows",
    "window", "closs", "bore", "goss",
}

# SKU prefixes (first segment before '-') that identify the brand RAC when the
# name prefix is WHEEL: (i.e. the scraper didn't use RAC: for these)
_RAC_SKU_PREFIXES = {
    "A05GB", "A05MB", "A06MB", "A19GB", "A20GB", "A20MB", "A21GB", "A21MB",
    "A23MB", "B01MB", "B02MB", "B03B", "B09GB", "B14GB", "B14MB", "B15GM",
    "B17GB", "B21MB", "B24MB", "B25HB", "B26GB", "B26MB", "B27ML", "B28GB",
    "B29B", "BN2GB", "CH1GB", "D01B", "D01GM", "D01TT", "L05GB", "L05MB",
    "L08GB", "L09GB", "L09MB", "LEX2GB", "M04GB", "M04MB", "M06MB", "M12GB",
    "M12MB", "M14MB", "M18GB", "M18MB", "M19MB", "M24GB", "P01GB", "P07GB",
    "P09GB", "P09MB", "P11B", "P13", "R01B", "R01GB", "R01TT", "R02GB",
    "R05GB", "R05TT", "R07BZ", "R07GB", "R07LM", "R07TT", "R08GB", "R08GBZ",
    "R09BZ", "R09GB", "R09GW", "R11GB", "R12GB", "R13MB", "R14GB", "R14TT",
    "R15GB", "R15HB", "R17GB", "R18B", "R97GB", "TD1B", "TS1B", "TS1GB",
    "TS1GM", "TS3GB", "TS4B", "V03GB", "V03GM", "V08GB",
}


# ── Logging ───────────────────────────────────────────────────────────────────

def get_logger():
    os.makedirs("logs", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = f"logs/augment_rims_{ts}.log"
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger("augment_rims")


# ── File discovery ─────────────────────────────────────────────────────────────

def find_input_file():
    matches = sorted(
        glob.glob("Data/raw/wheelmart_rims_*.csv"),
        key=os.path.getmtime,
        reverse=True,
    )
    if not matches:
        print(
            "ERROR: No wheelmart_rims_*.csv found in Data/raw/ "
            "— run scraper_rims.py first."
        )
        sys.exit(1)
    return matches[0]


def get_output_file():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"Data/shopify/Shopify_SEO_wheelsmart_rims_{ts}.csv"


# ── Quantity parsing ───────────────────────────────────────────────────────────

def parse_qty(raw):
    """Parse quantity_on_hand: '20+' → 20, negative → 0, non-numeric → 0."""
    s = str(raw).strip()
    s = s.rstrip("+")            # '20+' → '20'
    try:
        val = int(float(s))
        return max(val, 0)
    except (ValueError, TypeError):
        return 0


# ── SKU extraction ─────────────────────────────────────────────────────────────

def extract_sku(name):
    """Split name on ':' and return the last stripped part.

    'WHEEL:24\\' WHEEL:EVO-2763-241001M' → 'EVO-2763-241001M'
    'RAC: V08GB-1911242'                 → 'V08GB-1911242'
    """
    parts = [p.strip() for p in str(name).split(":")]
    return parts[-1] if parts else str(name).strip()


def name_prefix(name):
    """Return the first segment before ':' in uppercase: 'WHEEL' or 'RAC'."""
    parts = [p.strip() for p in str(name).split(":")]
    return parts[0].upper() if parts else ""




# ── Brand extraction ───────────────────────────────────────────────────────────

def extract_rim_brand(description, sku_code, row_name_prefix):
    """Return the best brand guess for a wheel row.

    Priority:
      1. name prefix 'RAC:' → 'RAC'
      2. SKU prefix known to be a RAC model code → 'RAC'
      3. 'EVO' SKU prefix → 'Evolution'
      4. 'RD' SKU prefix → 'Red Dirt Road'
      5. Last non-generic, non-numeric, non-spec word in description
      6. Empty string (steel/unbranded wheels)
    """
    sku_prefix = sku_code.split("-")[0].upper() if "-" in sku_code else sku_code.upper()

    if row_name_prefix == "RAC":
        return "RAC"
    if sku_prefix in _RAC_SKU_PREFIXES:
        return "RAC"
    if sku_prefix.startswith("EVO"):
        return "Evolution"
    if sku_prefix.startswith("RD"):
        return "Red Dirt Road"

    words = str(description).strip().split()
    for w in reversed(words):
        clean = w.strip(".,;:").lower()
        if not clean:
            continue
        if clean in GENERIC_WORDS:
            continue
        # Skip purely numeric / punctuation tokens (e.g. '+20', '-18', '139.7')
        if re.match(r"^[\d\+\-/x\.\\\n]+$", clean):
            continue
        # Skip spec tokens: CB78.1, ET20, MM, BORE...
        if re.match(r"^(cb|et|mm|bore)\d", clean):
            continue
        # Skip size-like tokens: 22x9.5, 20X10
        if re.match(r"^\d+[xX]\d", clean):
            continue
        # Skip finish-code tokens that are all letters/digits combos with no vowels (GB, MF, MGRA…)
        if re.match(r"^[gbmfhltscprdvakuwnoeiy]{2,6}$", clean) and not any(v in clean for v in "aeiou"):
            continue
        return w.strip(".,;:").title()

    return ""


# ── Model name from SKU ────────────────────────────────────────────────────────

def model_from_sku(sku_code):
    """Extract a human-readable model segment from the SKU.

    'EVO-2763-241001M' → '2763'
    'EVO-MRGW1826'     → 'MRGW1826'
    'L09GB-229512045'  → 'L09GB'
    'V08GB-1911242'    → 'V08GB'
    """
    parts = sku_code.split("-")
    if len(parts) >= 2:
        if parts[0].upper() in ("EVO",):
            return parts[1]
        return parts[0]
    return sku_code


# ── Spec parsing ───────────────────────────────────────────────────────────────

def _parse_size_from_description(description):
    """Find the first NxM token in description → (diameter: int, width: float)."""
    m = re.search(r"\b(\d{2,3})[Xx](\d+\.?\d*)\b", str(description))
    if m:
        try:
            return int(float(m.group(1))), float(m.group(2))
        except ValueError:
            pass
    return None, None


def parse_diameter(raw_diameter, description, name=""):
    """Return diameter as int. Prefers structured column; falls back to description then name."""
    s = str(raw_diameter).strip()
    if s and s not in ("0", "0.0"):
        try:
            return int(float(s))
        except ValueError:
            pass
    dia, _ = _parse_size_from_description(description)
    if dia is not None:
        return dia
    # Third fallback: extract from name prefix "WHEEL:17'" or trailing "NxM" in name
    m = re.search(r"WHEEL:(\d+)'", str(name), re.IGNORECASE)
    if m:
        return int(m.group(1))
    m2 = re.search(r'\s(\d{2})[xX][\d.]+', str(name))
    if m2:
        return int(m2.group(1))
    return None


def parse_width(raw_width, description, name=""):
    """Return width as float. Prefers structured column; falls back to description then name."""
    s = str(raw_width).strip()
    if s and s not in ("0", "0.0"):
        try:
            return float(s)
        except ValueError:
            pass
    _, width = _parse_size_from_description(description)
    if width is not None:
        return width
    # Third fallback: trailing "NxM" in name (e.g. "7M55VW-5586K 17x7.5")
    m = re.search(r'\s\d{2}[xX]([\d.]+)', str(name))
    if m:
        return float(m.group(1))
    return None


def normalize_pcd(val):
    """Normalize a single PCD string: '6x139' → '6x139.7', '5x120' → '5x120'."""
    m = re.match(r"^(\d+)[xX]([\d.]+)$", val.strip())
    if not m:
        return val.strip()
    lugs, pcd = m.group(1), m.group(2)
    if "." not in pcd:
        pcd = PCD_CORRECTIONS.get(pcd, pcd)
    # Format pcd: strip trailing zeros after decimal (139.7 stays, 120.0 → 120)
    try:
        pcd_f = float(pcd)
        pcd = f"{pcd_f:g}" if pcd_f == int(pcd_f) else pcd
    except ValueError:
        pass
    return f"{lugs}x{pcd}"


def parse_bolt_patterns(raw_pcd, description):
    """Return a list of normalized bolt pattern strings.

    Tries pcd column first (space-separated for dual fitment), then falls back
    to parsing description.
    """
    s = str(raw_pcd).strip()
    if s:
        # Space-separated dual fitment: '6x139.7 6x135'
        candidates = s.split()
        results = []
        seen = set()
        for c in candidates:
            norm = normalize_pcd(c)
            if norm and norm not in seen:
                # Validate: must match NxPCD format with lug 3–12 and PCD ≥ 60
                m = re.match(r"^(\d+)[xX]([\d.]+)$", norm)
                if m:
                    try:
                        lugs = int(m.group(1))
                        pcd_val = float(m.group(2))
                        if 3 <= lugs <= 12 and pcd_val >= 60:
                            results.append(norm)
                            seen.add(norm)
                    except ValueError:
                        pass
        if results:
            return results

    # Fallback: parse description
    # Normalize PCD values with trailing finish-code letter (e.g. "5X112B"→"5X112", "5X120F"→"5X120")
    # Apply to the full original description BEFORE any stripping
    desc_norm = re.sub(r'(\b\d{2,3})[A-Za-z](?=[\s+\-]|CB|ET|$)', r'\1', str(description))
    # Search the whole normalized description; size tokens (e.g. 22X9.5) filtered by lug range below
    bolt_matches = re.findall(r"\b(\d{1,2})[xX](\d+(?:\.\d+)?)", desc_norm)
    results = []
    seen = set()
    for lugs_s, pcd_s in bolt_matches:
        try:
            lugs = int(lugs_s)
            pcd_val = float(pcd_s)
            if 3 <= lugs <= 12 and pcd_val >= 60:
                # Apply corrections if no decimal
                if "." not in pcd_s:
                    pcd_s = PCD_CORRECTIONS.get(pcd_s, pcd_s)
                # Strip trailing .0 (120.0 → 120, 139.7 stays)
                pcd_fmt = f"{float(pcd_s):g}" if float(pcd_s) == int(float(pcd_s)) else pcd_s
                norm = f"{lugs}x{pcd_fmt}"
                if norm not in seen:
                    results.append(norm)
                    seen.add(norm)
        except ValueError:
            pass
    # Also check for imperial patterns like '4-100', '5-4.5', '6-139.7'
    if not results:
        imperial = re.findall(r"\b(\d{1,2})[-](\d+\.?\d*)\b", desc_norm)
        for lugs_s, pcd_s in imperial:
            try:
                lugs = int(lugs_s)
                pcd_val = float(pcd_s)
                # Could be imperial inches × 25.4; accept if 60 ≤ pcd ≤ 200 mm or if × 25.4 falls in that range
                if 3 <= lugs <= 12:
                    if 60 <= pcd_val <= 200:
                        pcd_fmt = f"{pcd_val:g}" if pcd_val == int(pcd_val) else str(pcd_val)
                        norm = f"{lugs}x{pcd_fmt}"
                        if norm not in seen:
                            results.append(norm)
                            seen.add(norm)
                    elif 2 <= pcd_val <= 8:
                        # Likely inches — e.g. 4.5" × 25.4 = 114.3 mm
                        converted = round(pcd_val * 25.4, 1)
                        pcd_s_mm = PCD_CORRECTIONS.get(str(pcd_val), f"{converted}")
                        norm = f"{lugs}x{pcd_s_mm}"
                        if norm not in seen:
                            results.append(norm)
                            seen.add(norm)
            except ValueError:
                pass
    return results


def parse_offset(raw_offset, description):
    """Return offset as string. Prefers column; falls back to ET... or O:... in description."""
    s = str(raw_offset).strip()
    if s:
        return s
    m = re.search(r"\bET\s*([-+]?\d+\.?\d*)", str(description), re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"\bO:\s*([-+]?\d+\.?\d*)", str(description), re.IGNORECASE)
    if m:
        return m.group(1)
    return ""


def parse_center_bore(raw_cb, description):
    """Return center bore as string. Prefers column; falls back to CB... in description."""
    s = str(raw_cb).strip()
    if s and s not in ("0", "0.0"):
        return s
    m = re.search(r"\bCB\s*([\d.]+)", str(description), re.IGNORECASE)
    return m.group(1) if m else ""


# ── Size tag (for tags field) ─────────────────────────────────────────────────

def _is_missing(val):
    """Return True if val is None or NaN."""
    if val is None:
        return True
    try:
        import math
        return math.isnan(float(val))
    except (TypeError, ValueError):
        return False


def make_size_tag(dia, width):
    """'24, 10.0' → '24X100'; '22, 9.5' → '22X95'. dia-only → str(dia)."""
    if _is_missing(dia):
        return ""
    dia_i = int(dia)
    if _is_missing(width):
        return str(dia_i)
    return f"{dia_i}X{int(round(float(width) * 10))}"


# ── Slugify ────────────────────────────────────────────────────────────────────

def slugify(text):
    text = str(text).lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


# ── Shopify field builders ─────────────────────────────────────────────────────

def _fmt_width(width):
    """Format width as compact string: 10.0 → '10', 9.5 → '9.5'."""
    w = float(width)
    return f"{w:g}"


def build_title(brand, model, dia, width, bolts, finish=""):
    """e.g. 'Evolution 2763 24X10 6x139.7 Gloss Black Machined Face'"""
    dia_ok   = not _is_missing(dia)
    width_ok = not _is_missing(width)
    if dia_ok and width_ok:
        size_display = f"{int(dia)}X{_fmt_width(width)}"
    elif dia_ok:
        size_display = str(int(dia))
    else:
        size_display = ""
    bp_display = " / ".join(bolts) if bolts else ""
    finish_display = finish.title() if finish else ""
    return " ".join(filter(None, [brand, model, size_display, bp_display, finish_display])).strip()


def build_body_html(dia, width, bolts, offset, center_bore, finish):
    lines = []
    if not _is_missing(dia):
        lines.append(f'Wheel Diameter: {int(dia)}"')
    if not _is_missing(width):
        lines.append(f'Wheel Width: {_fmt_width(width)}"')
    if bolts:
        lines.append(f"Bolt Pattern: {' / '.join(bolts)}")
    if offset:
        lines.append(f"Offset: {offset}mm")
    if center_bore:
        lines.append(f"Centre Bore: {center_bore}mm")
    if finish:
        lines.append(f"Finish: {finish.title()}")

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
        f"&#8226; Real Expertise: We are BC&#8217;s dedicated tire and wheel experts&#8212;"
        f"not a generalist oil change shop.<br>"
    )


def build_tags(brand, dia, size_tag, bolts):
    parts = []
    if brand:
        parts.append(f"Brand_{brand.title()}")
    if not _is_missing(dia):
        parts.append(f"WheelDia_{int(dia)}")
    if size_tag:
        parts.append(f"Size_{size_tag}")
    for bp in bolts:
        parts.append(f"BoltPattern_{bp}")
    for loc in LOCATIONS:
        parts.append(f"Location_{loc}")
    parts.append(SUPPLIER_TAG)
    return ",".join(parts)


def build_seo_title(brand, model, dia, width, bolts):
    dia_ok   = not _is_missing(dia)
    width_ok = not _is_missing(width)
    if dia_ok and width_ok:
        size_display = f"{int(dia)}X{_fmt_width(width)}"
    elif dia_ok:
        size_display = str(int(dia))
    else:
        size_display = ""
    bp_display = " / ".join(bolts) if bolts else ""
    base = " ".join(filter(None, [brand, model, size_display, bp_display]))
    return f"Buy {base} Wheels | Save Big On Tires"[:160]


def build_seo_desc(brand, model, size_tag, bolts):
    bp_display = " / ".join(bolts) if bolts else ""
    return (
        f"{brand} {model} {size_tag} wheels with bolt pattern {bp_display}. "
        f"Available at Save Big On Tires across 6 BC locations — "
        f"Burnaby, Langley, Maple Ridge, Kelowna, Kamloops, and Salmon Arm."
    ).strip()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log = get_logger()

    input_file = find_input_file()
    log.info(f"Input file : {input_file}")
    age_hours = (time.time() - os.path.getmtime(input_file)) / 3600
    if age_hours > STALE_INPUT_WARN_HOURS:
        log.warning(
            f"input file is {age_hours / 24:.1f} day(s) old "
            f"(> {STALE_INPUT_WARN_HOURS}h threshold). Proceeding, but this "
            f"usually means scraper_rims.py didn't produce a fresh file this "
            f"run — verify before trusting this output."
        )

    df = pd.read_csv(input_file, dtype=str, low_memory=False)
    df = df.fillna("")
    total_loaded = len(df)
    log.info(f"Rows loaded: {total_loaded}")
    if total_loaded == 0:
        log.error(f"{input_file} has 0 rows — refusing to write an empty Shopify SEO CSV. "
                   f"Check scraper_rims.py's output for this run before re-running augment.")
        sys.exit(1)

    # ── Quantity ──────────────────────────────────────────────────────────────
    df["total_qty"] = df["quantity_on_hand"].apply(parse_qty)

    # Low-quantity filter
    low_qty_mask = df["total_qty"] < LOW_QTY_THRESHOLD
    low_qty_count = int(low_qty_mask.sum())
    low_df = df[low_qty_mask].copy()
    os.makedirs("Data/raw", exist_ok=True)
    low_df.to_csv("Data/low_quantity_rims.csv", index=False)
    log.info(
        f"Low-qty rows dropped (qty < {LOW_QTY_THRESHOLD}): {low_qty_count} "
        f"→ Data/low_quantity_rims.csv"
    )

    df = df[~low_qty_mask].copy()
    log.info(f"Rows after low-qty filter: {len(df)}")
    if len(df) == 0:
        log.error(f"No rows remain after the qty >= {LOW_QTY_THRESHOLD} filter "
                   f"({total_loaded} loaded, all dropped) — refusing to write an empty Shopify SEO CSV. "
                   f"Check the raw scrape quantities before re-running.")
        sys.exit(1)

    # ── SKU derivation ────────────────────────────────────────────────────────
    # Resolved on the post-low-qty-filter frame (in file order) so "first
    # occurrence wins the plain SKU" matches exactly what production's
    # drop_duplicates(subset="_sku", keep="first") sees — that step also runs
    # after its own low-qty filter. Resolving on the full unfiltered frame
    # instead would disambiguate rows whose colliding counterpart never even
    # reaches the live feed (e.g. a WHEEL: listing sitting at qty=0), silently
    # renaming a SKU that's already live in Shopify — exactly what must not
    # happen.
    _raw_skus          = df["name"].apply(extract_sku)
    df["_name_prefix"] = df["name"].apply(name_prefix)
    df["_sku"]         = disambiguate_skus(_raw_skus, df["_name_prefix"])
    _collided = int((df["_sku"] != _raw_skus).sum())
    if _collided:
        log.info(
            f"SKU disambiguation: {_collided} row(s) had a raw SKU shared "
            f"with a different-prefix row (both in-stock) — suffixed with "
            f"the name prefix to keep them as distinct products."
        )

    # ── Parse specs ───────────────────────────────────────────────────────────
    # (_sku / _name_prefix already computed above, on the post-filter frame)
    df["_brand"]        = df.apply(
        lambda r: extract_rim_brand(r["description"], r["_sku"], r["_name_prefix"]),
        axis=1,
    )
    df["_model"]        = df["_sku"].apply(model_from_sku)
    df["_dia"]          = df.apply(
        lambda r: parse_diameter(r["diameter"], r["description"], r["name"]), axis=1
    )
    df["_width"]        = df.apply(
        lambda r: parse_width(r["width"], r["description"], r["name"]), axis=1
    )
    df["_bolts"]        = df.apply(
        lambda r: parse_bolt_patterns(r["pcd"], r["description"]), axis=1
    )
    df["_offset"]       = df.apply(
        lambda r: parse_offset(r["offset"], r["description"]), axis=1
    )
    df["_center_bore"]  = df.apply(
        lambda r: parse_center_bore(r["center_bore"], r["description"]), axis=1
    )
    df["_finish"]       = df.apply(
        lambda r: r["finish"].strip() if r["finish"].strip() else "", axis=1
    )
    df["_size_tag"]     = df.apply(
        lambda r: make_size_tag(r["_dia"], r["_width"]), axis=1
    )

    # ── Build output DataFrame ────────────────────────────────────────────────
    out = pd.DataFrame(index=df.index, columns=SHOPIFY_COLUMNS + ["total_qty"])
    out = out.fillna("")

    def _title(r):
        return build_title(r["_brand"], r["_model"], r["_dia"], r["_width"], r["_bolts"], r["_finish"])

    out["Title"]      = df.apply(_title, axis=1)

    # Disambiguate duplicate titles: first try appending ET offset, then SKU suffix
    title_counts = out["Title"].value_counts()
    dup_title_set = set(title_counts[title_counts > 1].index)
    if dup_title_set:
        seen_titles: dict = {}
        for i in df.index:
            t = out.at[i, "Title"]
            if t not in dup_title_set:
                seen_titles[t] = True
                continue
            # Try adding offset
            offset_val = str(df.at[i, "_offset"]).strip()
            new_t = f"{t} ET{offset_val}" if offset_val else t
            if new_t in seen_titles:
                # Offset also collides — use last segment of SKU to guarantee uniqueness
                sku_suffix = str(df.at[i, "_sku"]).split("-")[-1]
                new_t = f"{new_t} {sku_suffix}"
            out.at[i, "Title"] = new_t
            seen_titles[new_t] = True

    out["Handle"] = out["Title"].apply(slugify)

    out["Body (HTML)"] = df.apply(
        lambda r: build_body_html(
            r["_dia"], r["_width"], r["_bolts"],
            r["_offset"], r["_center_bore"], r["_finish"],
        ),
        axis=1,
    )

    out["Vendor"]    = VENDOR
    out["Type"]      = "Wheels"
    out["Published"] = "TRUE"

    out["Tags"] = df.apply(
        lambda r: build_tags(r["_brand"], r["_dia"], r["_size_tag"], r["_bolts"]),
        axis=1,
    )

    out["Option1 Name"]  = "Title"
    out["Option1 Value"] = "Default Title"

    out["Variant SKU"]                 = df["_sku"]
    out["Variant Grams"]               = ""
    out["Variant Inventory Tracker"]   = "shopify"
    out["Variant Inventory Policy"]    = "continue"
    out["Variant Fulfillment Service"] = "manual"
    out["Variant Requires Shipping"]   = "TRUE"
    out["Variant Taxable"]             = "TRUE"

    # Price off true cost (user_group_price — the wholesale login price span),
    # not the supplier's suggested retail_price. Some rows have a missing/blank/
    # zero user_group_price (no price span found on the page), and older raw
    # CSVs may not have the column at all — in either case, warn and fall back
    # to retail_price for those rows rather than failing.
    retail = pd.to_numeric(df["retail_price"], errors="coerce").fillna(0.0)
    if "user_group_price" in df.columns:
        cost = pd.to_numeric(df["user_group_price"], errors="coerce")
    else:
        log.warning("Raw CSV has no user_group_price column — using retail_price for all rows.")
        cost = pd.Series(float("nan"), index=df.index)

    missing_cost_mask = cost.isna() | (cost <= 0)
    missing_cost_count = int(missing_cost_mask.sum())
    if missing_cost_count:
        log.warning(
            f"{missing_cost_count} / {len(df)} rows have missing/zero user_group_price "
            f"— falling back to retail_price for those rows."
        )

    df["_cost_price"] = cost.where(~missing_cost_mask, retail)
    out["Variant Price"] = (df["_cost_price"] + MARKUP).round(2).astype(str)

    out["Image Src"]      = df["image"]
    out["Image Position"] = out["Image Src"].apply(lambda s: 1 if str(s).strip() else "")
    out["Gift Card"]      = "FALSE"

    out["SEO Title"] = df.apply(
        lambda r: build_seo_title(
            r["_brand"], r["_model"], r["_dia"], r["_width"], r["_bolts"]
        ),
        axis=1,
    )
    out["SEO Description"] = df.apply(
        lambda r: build_seo_desc(
            r["_brand"], r["_model"], r["_size_tag"], r["_bolts"]
        ),
        axis=1,
    )

    out["Google Shopping / Google Product Category"] = GOOGLE_PRODUCT_CATEGORY
    out["Google Shopping / Condition"]               = "New"
    out["Google Shopping / Custom Product"]          = "FALSE"
    out["Google Shopping / MPN"] = df.apply(
        lambda r: f"{r['_brand']}-{r['_model']}-{r['_size_tag']}", axis=1
    )

    out["Product Type"]     = "Wheels"
    out["Brand"]            = df["_brand"].str.title()
    out["Identifier Exist"] = "FALSE"
    out["total_qty"]        = df["total_qty"]

    # ── Write output ──────────────────────────────────────────────────────────
    output_file = get_output_file()
    os.makedirs("Data/shopify", exist_ok=True)
    out.to_csv(output_file, index=False)
    log.info(f"Output file: {output_file}")

    # ── Summary ───────────────────────────────────────────────────────────────
    rows_written    = len(out)
    missing_dia     = int(df["_dia"].apply(_is_missing).sum())
    missing_bolts   = int((df["_bolts"].apply(len) == 0).sum())
    missing_brand   = int((df["_brand"] == "").sum())

    print()
    print("=" * 50)
    print(f"  Total rows loaded     : {total_loaded}")
    print(f"  Dropped (low qty)     : {low_qty_count}")
    print(f"  Rows written          : {rows_written}")
    print(f"  Missing diameter      : {missing_dia}")
    print(f"  Missing bolt pattern  : {missing_bolts}")
    print(f"  Missing brand         : {missing_brand}")
    print(f"  Missing cost          : {missing_cost_count}  (fell back to retail_price)")
    print("=" * 50)


if __name__ == "__main__":
    main()
