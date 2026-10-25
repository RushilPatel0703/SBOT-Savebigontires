import requests
import csv
import os
import re
from datetime import datetime
from bs4 import BeautifulSoup

import sys
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared')))
import cookie_store

BASE_URL = "https://wheelmartcanada.ca"
PRODUCT_URL = f"{BASE_URL}/product/tires"
API_URL = f"{BASE_URL}/v1/tires/?api_client_id=5&api_key=IhSkw9YNImRW3QKIZowLePXeSvxFqM"

COOKIES = {
    "_identity": "93dfc22644ea3f5d23a9bea988a54528628d4965f14521883bf5d2d624ab36a2a%3A2%3A%7Bi%3A0%3Bs%3A9%3A%22_identity%22%3Bi%3A1%3Bs%3A48%3A%22%5B409%2C%22HnB5HZV0OHwmNe9vml8upqll2EmRCmCk%22%2C2592000%5D%22%3B%7D",
    "PHPSESSID": "c0108e21cdda3a59196bbb03d6575b3a",
}
# A refreshed cookie (refresh_cookies.py) wins over the literal above.
COOKIES = cookie_store.get("wheelsmart", COOKIES)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
}

OUTPUT_FILE = f"Data/raw/wheelmart_tires_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
PER_PAGE = 100

FIELDNAMES = [
    "web_id", "sku", "description",
    "size_raw", "width", "aspect_ratio", "rim_diameter", "load_index",
    "quantity_on_hand", "retail_price", "user_group_price",
    "remarks", "image",
]


# Standard tire size notation: 235/50ZR18 101W  ->  width/aspect[type]rim load
_SIZE_RE = re.compile(r"^(\d{2,3})/(\d{2,3})[A-Z]{0,2}(\d{2})\s+(\d{2,3})", re.IGNORECASE)


def parse_size_from_desc(desc):
    """Primary parser: extract (width, aspect, rim, load_index) from the
    human-readable description using standard tire size notation."""
    m = _SIZE_RE.match(desc.strip())
    if m:
        return m.group(1), m.group(2), m.group(3), m.group(4)
    return "", "", "", ""


def decode_size(size_str):
    """Fallback parser: decode encoded size string e.g. '195701491' -> 195, 70, 14, 91.
    Patterns: 3+2+2+2=9, 3+2+2+3=10, 2+2+2+2=8
    """
    s = str(size_str).strip()
    if not s or s == "0":
        return "", "", "", ""
    if len(s) == 10:
        return s[0:3], s[3:5], s[5:7], s[7:10]
    if len(s) == 9:
        return s[0:3], s[3:5], s[5:7], s[7:9]
    if len(s) == 8:
        return s[0:2], s[2:4], s[4:6], s[6:8]
    return s, "", "", ""


def fetch_api_quantities():
    """Return {web_id_str: quantity_int} from the API for all products."""
    resp = requests.get(API_URL, timeout=30)
    resp.raise_for_status()
    return {str(item["WebID"]): item["Quantity"] for item in resp.json()}


def get_total_pages(soup):
    summary = soup.select_one("div.summary")
    if not summary:
        return 1
    text = summary.get_text()
    match = re.search(r"of\s+<b>(\d+)</b>|of\s+(\d+)", summary.decode_contents())
    if not match:
        match = re.search(r"of\s+(\d+)", text)
        if not match:
            return 1
        total = int(match.group(1))
    else:
        total = int(match.group(1) or match.group(2))
    return (total + PER_PAGE - 1) // PER_PAGE


def parse_rows(soup):
    rows = soup.select("#product-list table tbody tr")
    records = []

    for row in rows:
        web_id = row.get("data-key", "")
        tds = row.find_all("td", recursive=False)
        if len(tds) < 7:
            continue

        # Image
        img_tag = tds[0].find("img")
        image = img_tag["src"] if img_tag else ""

        # Size raw
        size_raw = tds[1].get_text(strip=True)

        # SKU
        sku = tds[2].get_text(strip=True)

        # Description: pull user_group_price from the price span, remove only
        # that span, then get the remaining text (avoids stripping description
        # text that may also be wrapped in a <span>)
        desc_td = tds[3]
        price_span = desc_td.find("span", class_="product-price")
        user_group_price = price_span.get_text(strip=True).replace("$", "").strip() if price_span else ""
        if price_span:
            price_span.decompose()
        description = desc_td.get_text(strip=True)

        # Size fields: parse from description first (more reliable), fall back
        # to decoding the encoded size_raw; warn on disagreement
        width, aspect, rim, load_index = parse_size_from_desc(description)
        dw, da, dr, dl = decode_size(size_raw)
        if not width:
            width, aspect, rim, load_index = dw, da, dr, dl
        elif dw and (dw, da, dr, dl) != (width, aspect, rim, load_index):
            print(f"  WARN id={web_id}: desc=({width},{aspect},{rim},{load_index}) size_raw=({dw},{da},{dr},{dl})")

        # Retail price (suggested)
        retail_price = tds[4].get_text(strip=True).replace("$", "").strip()

        # Quantity on hand (HTML shows capped "20+"; enriched from API later)
        quantity_on_hand = tds[5].get_text(strip=True)

        # Remarks
        remarks = tds[6].get_text(strip=True)

        records.append({
            "web_id": web_id,
            "sku": sku,
            "description": description,
            "size_raw": size_raw,
            "width": width,
            "aspect_ratio": aspect,
            "rim_diameter": rim,
            "load_index": load_index,
            "quantity_on_hand": quantity_on_hand,
            "retail_price": retail_price,
            "user_group_price": user_group_price,
            "remarks": remarks,
            "image": image,
        })

    return records


def fetch_page(session, page):
    resp = session.get(PRODUCT_URL, params={"page": page, "per-page": PER_PAGE}, timeout=30)
    resp.raise_for_status()
    # A dead session is a 302 to the login page, which answers 200 with no
    # product rows — without this the run "succeeds" with a header-only CSV.
    if "/auth/login" in resp.url:
        print(f"\nFATAL: page {page} redirected to the login page — WheelMart session is "
              f"not logged in. Run  python refresh_cookies.py --supplier wheelsmart  from pipelines/.")
        sys.exit(1)
    return BeautifulSoup(resp.text, "html.parser")


def main():
    os.makedirs("Data/raw", exist_ok=True)

    # ── API quantity lookup ───────────────────────────────────────────────────
    print("Fetching exact quantities from API...")
    api_qty = fetch_api_quantities()
    print(f"  API returned {len(api_qty)} products")

    # ── HTML scrape ───────────────────────────────────────────────────────────
    session = requests.Session()
    session.cookies.update(COOKIES)
    session.headers.update(HEADERS)

    print("Fetching page 1 to determine total pages...")
    soup = fetch_page(session, 1)
    total_pages = get_total_pages(soup)
    print(f"  Total pages: {total_pages}")

    seen_ids = set()
    all_records = []

    for page in range(1, total_pages + 1):
        if page > 1:
            soup = fetch_page(session, page)
        new_rows = 0
        for rec in parse_rows(soup):
            if rec["web_id"] in seen_ids:
                continue
            seen_ids.add(rec["web_id"])
            # Replace capped HTML quantity with exact API value
            if rec["web_id"] in api_qty:
                rec["quantity_on_hand"] = api_qty[rec["web_id"]]
            all_records.append(rec)
            new_rows += 1
        print(f"  Page {page}: {new_rows} new rows (total: {len(all_records)})")

    matched = sum(1 for r in all_records if str(r["web_id"]) in api_qty)
    print(f"\nAPI quantity matched: {matched} / {len(all_records)}")

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_records)

    print(f"Done. {len(all_records)} rows saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
