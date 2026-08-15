"""
DT Tire scraper — wheels.

Platform : SAP Hybris / Groupe Touchette storefront (dttirehub.ca)
Auth     : Programmatic login (session cookies refreshed on each run)
Strategy : Search by wheel diameter, paginate via /pagefragment (30/page)
Output   : Data/raw/dttire_wheels_TIMESTAMP.csv
"""

import os
import re
import time
import math
import datetime

import requests
import pandas as pd
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL   = "https://dttirehub.ca/touchettestorefront/touchette/en"
LOGIN_URL  = f"{BASE_URL}/j_spring_security_check"
SEARCH_URL = f"{BASE_URL}/wheelSearch/search"
FRAG_URL   = f"{BASE_URL}/wheelSearch/search/pagefragment"

USERNAME   = "6043767686"
PASSWORD   = "dttire"

PAGE_SIZE  = 30      # portal returns exactly 30 per page/fragment

# Sweep every relevant diameter — covers passenger, light truck, trailer,
# commercial, and specialty sizes
DIAMETERS = [
    10, 12, 13, 14, 15, 16, 17, 17.5, 18, 19, 19.5,
    20, 21, 22, 22.5, 24, 26, 28, 30
]

MAX_WORKERS = 4      # parallel pagefragment fetches (unused — sequential for stability)
DELAY       = 0.25   # seconds between sequential requests

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "Data", "raw")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def make_session() -> requests.Session:
    """Log in and return an authenticated requests.Session."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
        ),
        "X-Requested-With": "XMLHttpRequest",
    })
    # Fetch CSRF token from login page
    r = s.get(f"{BASE_URL}/login", timeout=30)
    r.raise_for_status()
    csrf_match = re.search(r'name="CSRFToken"\s+value="([^"]+)"', r.text)
    if not csrf_match:
        raise RuntimeError("Could not find CSRFToken on login page")
    csrf = csrf_match.group(1)

    r2 = s.post(LOGIN_URL, data={
        "j_username": USERNAME,
        "j_password": PASSWORD,
        "CSRFToken":  csrf,
    }, allow_redirects=True, timeout=30)
    r2.raise_for_status()
    if "login" in r2.url:
        raise RuntimeError("Login failed — check USERNAME / PASSWORD")
    print(f"[auth] Logged in. Session: {s.cookies.get('JSESSIONID','')[:20]}…")
    return s


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _dl_specs(container) -> dict:
    """Extract all <dt>:<dd> pairs from all <dl> elements in container."""
    specs = {}
    for dl in container.select("dl"):
        for dt, dd in zip(dl.select("dt"), dl.select("dd")):
            key = dt.get_text(strip=True).rstrip(":").strip()
            specs[key] = dd.get_text(strip=True)
    return specs


def parse_qty(el):
    """Parse a quantity element — handles '99+' and plain integers."""
    if not el:
        return 0
    txt = el.get_text(strip=True)
    if txt == "99+":
        return 99
    try:
        return int(txt)
    except (ValueError, TypeError):
        return 0


def parse_price(raw: str) -> str:
    """Extract first numeric price from raw text — avoids concat bug."""
    m = re.match(r'\$?(\d+(?:\.\d+)?)', raw.replace(",", ""))
    return m.group(1) if m else ""


def parse_item(li) -> dict:
    """Parse one <li class='search-results__item'> into a flat dict."""

    # --- SKU ---
    sku_el = li.select_one("div.sku[data-sku], .js-product-sku[data-sku]")
    sku = sku_el["data-sku"] if sku_el else ""

    # --- brand ---
    brand_img = li.select_one(".search-results__manufacturer-logo")
    brand = brand_img.get("alt", "").replace(" brand", "").strip() if brand_img else ""

    # --- model / description ---
    h3 = li.select_one("h3.search-results__description")
    description = h3.get_text(separator=" ", strip=True) if h3 else ""

    # --- size: from data-product-size attribute ---
    info_div = li.select_one(".js-product-info[data-product-size]")
    size_val = info_div["data-product-size"].upper() if info_div else ""

    # diameter and width from size_val (e.g. "18X7.0")
    parts = size_val.split("X") if "X" in size_val else []
    diameter_val = parts[0] if parts else ""
    width_val    = parts[1] if len(parts) > 1 else ""

    # --- expanded specs (collapsed panel HTML) ---
    expanded = li.select_one(".search-results-expanded-view")
    specs = _dl_specs(expanded) if expanded else {}

    # Fallback diameter/width from expanded specs if not in size_val
    if not diameter_val:
        diameter_val = specs.get("Diameter", "")
    if not width_val:
        width_val = specs.get("Width", "")

    # --- bolt pattern: find <dt> containing "bolt" then get sibling <dd> ---
    bolt_pattern = ""
    for dt in li.select("dt"):
        if "bolt" in dt.get_text(strip=True).lower():
            bp_dd = dt.find_next_sibling("dd")
            if bp_dd:
                # Strip square brackets e.g. "[5X114.3]"
                bolt_pattern = re.sub(r"[\[\]]", "", bp_dd.get_text(strip=True))
            break

    # --- offset ---
    offset = specs.get("Offset", "")
    if not offset:
        for dt in li.select("dt"):
            if dt.get_text(strip=True).lower().startswith("offset"):
                od = dt.find_next_sibling("dd")
                if od:
                    offset = od.get_text(strip=True)
                break

    # --- centre bore ---
    centre_bore = specs.get("Centre bore", specs.get("Centre Bore", ""))

    # --- finish / category ---
    finish   = specs.get("Finish",   "")
    category = specs.get("Category", "")   # e.g. "Steel", "Alloy"

    # --- winter approved ---
    winter_approved = bool(li.select_one("svg.c-icon-wheels-winter-approved"))

    # --- pricing ---
    cost_price = ""
    for pdm_li in li.select("ul.search-results__pdm-prices li"):
        lbl = pdm_li.select_one(".search-results__pdm-content-label")
        val = pdm_li.select_one(".search-results__pdm-content-value")
        if lbl and lbl.get_text(strip=True) == "P" and val:
            raw_price = (
                val.find(string=True, recursive=False) or val.get_text(strip=True)
            ).strip()
            cost_price = parse_price(raw_price)
            break

    msrp_el = li.select_one(
        ".search-results__price-item--msrp .search-results__price-value"
    )
    msrp = parse_price(msrp_el.get_text(strip=True)) if msrp_el else ""

    # --- availability ---
    qty_local     = parse_qty(li.select_one(".search-results__on-hand-item--local_count p"))
    qty_warehouse = parse_qty(li.select_one(".search-results__on-hand-item--warehouse_count p"))
    qty_total     = qty_local + qty_warehouse

    # --- image ---
    img_el = li.select_one(".product-thumbnail, img.product-image, img[src*='wheel'], img")
    if img_el:
        image_url = img_el.get("data-src", "") or img_el.get("src", "")
    else:
        image_url = ""
    if image_url and image_url.startswith("/"):
        image_url = "https://dttirehub.ca" + image_url

    return {
        "sku":             sku,
        "brand":           brand,
        "description":     description,
        "size":            size_val,
        "diameter":        diameter_val,
        "width":           width_val,
        "bolt_pattern":    bolt_pattern,
        "offset":          offset,
        "centre_bore":     centre_bore,
        "finish":          finish,
        "category":        category,
        "winter_approved": winter_approved,
        "cost_price":      cost_price,
        "msrp":            msrp,
        "qty_local":       qty_local,
        "qty_warehouse":   qty_warehouse,
        "qty_total":       qty_total,
        "image_url":       image_url,
    }


def parse_page(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    return [parse_item(li) for li in soup.select("li.search-results__item")]


# ---------------------------------------------------------------------------
# Search / pagination
# ---------------------------------------------------------------------------
_BASE_PARAMS = {
    "wheelSize":              "",
    "boltPattern":            "",
    "sku":                    "",
    "manufacturer":           "",
    "model":                  "",
    "winterApproved":         "false",
    "_showOnlyOnSaleResults": "on",
    "searchQuery":            "",
    "sort":                   "relevance",
}


def scrape_diameter(session: requests.Session, diameter) -> list:
    """Fetch all pages for one diameter and return list of parsed dicts."""
    params = {**_BASE_PARAMS, "wheelSize": str(diameter)}

    # --- initial page ---
    r = session.get(SEARCH_URL, params=params, timeout=30)
    r.raise_for_status()

    total_match = re.search(r"(\d+)\s+product\(s\)\s+found", r.text)
    total = int(total_match.group(1)) if total_match else 0

    if total == 0:
        return []

    rows = parse_page(r.text)
    pages_needed = math.ceil(total / PAGE_SIZE) - 1  # pages after the first

    if pages_needed <= 0:
        return rows

    # --- fetch remaining pages sequentially ---
    for page_num in range(1, pages_needed + 1):
        time.sleep(DELAY)
        frag_params = {**params, "nextPage": str(page_num)}
        resp = session.get(FRAG_URL, params=frag_params, timeout=30)
        resp.raise_for_status()
        rows.extend(parse_page(resp.text))

    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = make_session()

    all_rows:  list = []
    seen_skus: set  = set()

    total_diameters = len(DIAMETERS)
    print(f"Scraping {total_diameters} wheel diameters…")

    for i, diameter in enumerate(DIAMETERS, 1):
        try:
            rows = scrape_diameter(session, diameter)
        except Exception as exc:
            print(f"[{i}/{total_diameters}] diameter={diameter} — ERROR: {exc}")
            continue

        new_rows = [r for r in rows if r["sku"] not in seen_skus]
        for r in new_rows:
            seen_skus.add(r["sku"])
        all_rows.extend(new_rows)

        if rows:
            print(
                f"[{i}/{total_diameters}] diameter={diameter}"
                f" — {len(rows)} results, {len(new_rows)} new"
                f"  (total unique: {len(seen_skus)})"
            )
        else:
            print(f"[{i}/{total_diameters}] diameter={diameter} — 0 results")

        time.sleep(DELAY)

    # ── Write output ───────────────────────────────────────────────────────────
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(OUTPUT_DIR, f"dttire_wheels_{ts}.csv")
    df = pd.DataFrame(all_rows)
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\nDone. {len(df)} rows ({df['sku'].nunique()} unique SKUs) -> {out_path}")


if __name__ == "__main__":
    main()
