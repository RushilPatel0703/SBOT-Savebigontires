"""
DT Tire scraper — passenger / LT tires.

Platform : SAP Hybris / Groupe Touchette storefront (dttirehub.ca)
Auth     : Programmatic login (session cookies refreshed on each run)
Strategy : Search by section width → paginate via /pagefragment (30/page)
Output   : Data/raw/dttire_tires_TIMESTAMP.csv
"""

import os
import re
import time
import math
import datetime
import concurrent.futures

import requests
import pandas as pd
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL   = "https://dttirehub.ca/touchettestorefront/touchette/en"
LOGIN_URL  = f"{BASE_URL}/j_spring_security_check"
SEARCH_URL = f"{BASE_URL}/tireSearch/tire-size/search"
FRAG_URL   = f"{BASE_URL}/tireSearch/tire-size/search/pagefragment"

USERNAME   = "6043767686"
PASSWORD   = "dttire"

PAGE_SIZE  = 30      # portal returns exactly 30 per page/fragment

# Search terms strategy:
#   Narrow widths (105–200): width-only is fine, very few results per term.
#   Popular widths (205–395): search by width/ratio pairs (e.g. "235/75") to
#     avoid server-side result caps that truncate width-only queries for busy
#     widths. Each width/ratio term returns at most one rim-size family, so
#     result sets stay small and all products are reached through pagination.
#   Flotation: prefix like "28X" already targets exactly one size family.
_NARROW_TERMS  = [str(w) for w in range(105, 205, 5)]          # 105 … 200
_POPULAR_WIDTHS = list(range(205, 400, 5))                      # 205 … 395
_RATIOS        = [25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95]
_WIDE_TERMS    = [f"{w}/{r}" for w in _POPULAR_WIDTHS for r in _RATIOS]
_FLOTATION     = [f"{d}X" for d in range(28, 46)]               # 28X … 45X
SEARCH_TERMS   = _FLOTATION + _NARROW_TERMS + _WIDE_TERMS

MAX_WORKERS = 4      # parallel pagefragment fetches per width
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


def parse_item(li) -> dict:
    """Parse one <li class='search-results__item'> into a flat dict."""
    def sel_txt(css, default=""):
        el = li.select_one(css)
        return el.get_text(strip=True) if el else default

    def sel_attr(css, attr, default=""):
        el = li.select_one(css)
        return el.get(attr, default) if el else default

    # --- identifiers ---
    sku = sel_attr(".js-product-sku", "data-sku")

    brand_img = li.select_one(".search-results__manufacturer-logo")
    brand = brand_img.get("alt", "").replace(" brand", "").strip() if brand_img else ""

    # model from h3; strip brand prefix if it leads
    h3 = li.select_one("h3.search-results__description")
    model_raw = h3.get_text(separator=" ", strip=True) if h3 else ""
    model = model_raw.replace(brand, "").strip().lstrip("- ").strip()

    performance = sel_txt(".performance-cat")

    # --- season ---
    season_el = li.select_one(".c-season__label")
    season = season_el.get_text(strip=True) if season_el else "Unknown"

    # --- size ---
    info_div = li.select_one(".js-product-info")
    size = info_div.get("data-product-size", "") if info_div else ""

    # --- image ---
    img_el = li.select_one(".product-thumbnail")
    image_url = img_el.get("data-src", "") if img_el else ""
    if image_url and image_url.startswith("/"):
        image_url = "https://dttirehub.ca" + image_url

    # --- pricing (PDM block: P=cost, D=discount, M=margin) ---
    cost_price = ""
    for item in li.select(".search-results__pdm-prices li"):
        lbl = item.select_one(".search-results__pdm-content-label")
        val = item.select_one(".search-results__pdm-content-value")
        if lbl and lbl.get_text(strip=True) == "P" and val:
            raw_price = (val.find(string=True, recursive=False) or val.get_text(strip=True)).strip()
            cost_price = raw_price.lstrip("$").replace(",", "")
            break

    msrp_el = li.select_one(".search-results__price-item--msrp .search-results__price-value")
    msrp = msrp_el.get_text(strip=True).lstrip("$").replace(",", "") if msrp_el else ""

    # --- availability ---
    def parse_qty(css):
        el = li.select_one(css)
        raw = el.get_text(strip=True) if el else "0"
        return 99 if raw == "99+" else int(raw or 0)

    qty_local = parse_qty(".search-results__on-hand-item--local_count p")
    qty_other = parse_qty(".search-results__on-hand-item--warehouse_count p")

    # --- detailed specs (already in collapsed expanded-view HTML) ---
    expanded = li.select_one(".search-results-expanded-view")
    specs = _dl_specs(expanded) if expanded else {}

    # Service desc from collapsed view (has load index + speed rating in one string)
    service_desc = ""
    if info_div:
        for dt in info_div.select("dt"):
            if "Service desc" in dt.get_text():
                dd = dt.find_next_sibling("dd")
                if dd:
                    service_desc = dd.get_text(strip=True)
                break

    sidewall = ""
    if info_div:
        for dt in info_div.select("dt"):
            if dt.get_text(strip=True) == "Sidewall:":
                dd = dt.find_next_sibling("dd")
                if dd:
                    sidewall = dd.get_text(strip=True)
                break

    return {
        "sku":           sku,
        "brand":         brand,
        "model":         model,
        "performance":   performance,
        "season":        season,
        "size":          size,
        "service_desc":  service_desc,
        "sidewall":      sidewall,
        "speed_rating":  specs.get("Speed rating", ""),
        "load_index":    specs.get("Load index", ""),
        "load_range":    specs.get("Load range", ""),
        "tread_design":  specs.get("Tread design", ""),
        "cost_price":    cost_price,
        "msrp":          msrp,
        "qty_local":     qty_local,
        "qty_other":     qty_other,
        "qty_total":     qty_local + qty_other,
        "image_url":     image_url,
    }


def parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    return [parse_item(li) for li in soup.select("li.search-results__item")]


# ---------------------------------------------------------------------------
# Search / pagination
# ---------------------------------------------------------------------------
_BASE_PARAMS = {
    "rearTireSize":         "",
    "description":          "",
    "manufacturer":         "",
    "model":                "",
    "modelCode":            "",
    "sku":                  "",
    "season":               "ALL",
    "showOnlyOnSaleResults": "false",
    "searchQuery":          "",
    "sort":                 "relevance",
}


def scrape_width(session: requests.Session, term: str) -> list[dict]:
    """Fetch all pages for one search term and return list of parsed dicts."""
    params = {**_BASE_PARAMS, "tireSize": term}

    # --- initial page ---
    r = session.get(SEARCH_URL, params=params, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    total_match = re.search(r"(\d+)\s+product\(s\)\s+found", r.text)
    total = int(total_match.group(1)) if total_match else 0

    if total == 0:
        return []

    rows = parse_page(r.text)
    pages_needed = math.ceil(total / PAGE_SIZE) - 1  # pages after the first

    if pages_needed <= 0:
        return rows

    # --- fetch remaining pages ---
    def fetch_page(page_num):
        frag_params = {**params, "nextPage": str(page_num)}
        resp = session.get(FRAG_URL, params=frag_params, timeout=30)
        resp.raise_for_status()
        return parse_page(resp.text)

    for page_num in range(1, pages_needed + 1):
        time.sleep(DELAY)
        rows.extend(fetch_page(page_num))

    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = make_session()

    # ── Pass 1: Discovery via width/ratio search terms ───────────────────────
    # Finds which exact sizes DT Tire carries (e.g. "205/55R16", "LT265/70R17").
    print(f"Pass 1: discovery ({len(SEARCH_TERMS)} search terms)...")

    pass1_rows: list[dict] = []
    seen_skus:  set[str]   = set()
    seen_sizes: set[str]   = set()

    total_terms = len(SEARCH_TERMS)
    for i, term in enumerate(SEARCH_TERMS, 1):
        try:
            rows = scrape_width(session, term)
        except Exception as exc:
            print(f"[{i}/{total_terms}] '{term}' — ERROR: {exc}")
            continue

        new_rows = [r for r in rows if r["sku"] not in seen_skus]
        for r in new_rows:
            seen_skus.add(r["sku"])
            if r["size"]:
                seen_sizes.add(r["size"])
        pass1_rows.extend(new_rows)

        if rows:
            print(f"[{i}/{total_terms}] '{term}' — {len(rows)} results, {len(new_rows)} new  (total: {len(seen_skus)})")
        time.sleep(DELAY)

    print(f"\nPass 1 done: {len(seen_skus)} unique SKUs, {len(seen_sizes)} unique sizes discovered.")

    # ── Pass 2: Full-size rescrape for complete coverage ─────────────────────
    # Searches each discovered size exactly (e.g. "205/55R16") so DT Tire returns
    # its complete result set for that size — the same query the coverage checker
    # uses. Pass 1 finds what sizes exist; Pass 2 ensures we get ALL products in
    # each size without server-side dilution from broader width/ratio queries.
    print(f"\nPass 2: full-size rescrape ({len(seen_sizes)} exact sizes)...")

    all_rows: list[dict] = list(pass1_rows)
    n_sizes = len(seen_sizes)

    for i, size in enumerate(sorted(seen_sizes), 1):
        try:
            rows = scrape_width(session, size)
        except Exception as exc:
            print(f"[{i}/{n_sizes}] '{size}' — ERROR: {exc}")
            continue

        new_rows = [r for r in rows if r["sku"] not in seen_skus]
        for r in new_rows:
            seen_skus.add(r["sku"])
        all_rows.extend(new_rows)

        if new_rows:
            print(f"[{i}/{n_sizes}] '{size}' — {len(rows)} results, {len(new_rows)} new  (total: {len(seen_skus)})")
        time.sleep(DELAY)

    print(f"\nPass 2 done: {len(seen_skus)} total unique SKUs.")

    # ── Write output ──────────────────────────────────────────────────────────
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(OUTPUT_DIR, f"dttire_tires_{ts}.csv")
    df = pd.DataFrame(all_rows)
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\nDone. {len(df)} rows ({df['sku'].nunique()} unique SKUs) -> {out_path}")


if __name__ == "__main__":
    main()
