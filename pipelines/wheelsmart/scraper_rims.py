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
PRODUCT_URL = f"{BASE_URL}/product/rims"

COOKIES = {
    "_identity": "93dfc22644ea3f5d23a9bea988a54528628d4965f14521883bf5d2d624ab36a2a%3A2%3A%7Bi%3A0%3Bs%3A9%3A%22_identity%22%3Bi%3A1%3Bs%3A48%3A%22%5B409%2C%22HnB5HZV0OHwmNe9vml8upqll2EmRCmCk%22%2C2592000%5D%22%3B%7D",
    "PHPSESSID": "c0108e21cdda3a59196bbb03d6575b3a",
}
# A refreshed cookie (refresh_cookies.py) wins over the literal above.
COOKIES = cookie_store.get("wheelsmart", COOKIES)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
}

OUTPUT_FILE = f"Data/raw/wheelmart_rims_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
PER_PAGE = 100

FIELDNAMES = [
    "web_id", "name", "description",
    "diameter", "width", "offset", "pcd", "center_bore", "finish",
    "weight", "backspace", "seat", "made", "load_rating",
    "quantity_on_hand", "retail_price", "user_group_price", "remarks",
    "image",
]


def get_total_pages(soup):
    summary = soup.select_one("div.summary")
    if not summary:
        return 1
    match = re.search(r"of\s+(\d+)", summary.get_text())
    if not match:
        return 1
    total = int(match.group(1))
    return (total + PER_PAGE - 1) // PER_PAGE


def parse_rows(soup):
    rows = soup.select("#rims-list table tbody tr")
    records = []

    for row in rows:
        web_id = row.get("data-key", "")
        tds = row.find_all("td", recursive=False)
        # Columns: 0=thumb, 1=model, 2=name, 3=description, 4=dimension, 5=width,
        #          6=offset, 7=pcd, 8=cb, 9=finish, 10=weight, 11=backspace,
        #          12=seat, 13=made, 14=load_rating, 15=remarks, 16=price_suggested, 17=on_hand, 18=ordered
        if len(tds) < 18:
            continue

        # Image
        img_tag = tds[0].find("img")
        image = img_tag["src"] if img_tag else ""

        # Name (SKU-like identifier)
        name = tds[2].get_text(strip=True)

        # Description: extract user_group_price from span, then strip all spans
        desc_td = tds[3]
        price_span = desc_td.find("span", class_="product-price")
        user_group_price = price_span.get_text(strip=True).replace("$", "").strip() if price_span else ""
        for span in desc_td.find_all("span"):
            span.decompose()
        description = desc_td.get_text(strip=True)

        # Spec columns
        diameter    = tds[4].get_text(strip=True)
        width       = tds[5].get_text(strip=True)
        offset      = tds[6].get_text(strip=True)
        pcd         = tds[7].get_text(strip=True)
        center_bore = tds[8].get_text(strip=True)
        finish      = tds[9].get_text(strip=True)
        weight      = tds[10].get_text(strip=True)
        backspace   = tds[11].get_text(strip=True)
        seat        = tds[12].get_text(strip=True)
        made        = tds[13].get_text(strip=True)
        load_rating = tds[14].get_text(strip=True)
        remarks     = tds[15].get_text(strip=True)

        # Retail price (suggested)
        retail_price = tds[16].get_text(strip=True).replace("$", "").strip()

        # Quantity on hand
        quantity_on_hand = tds[17].get_text(strip=True)

        records.append({
            "web_id": web_id,
            "name": name,
            "description": description,
            "diameter": diameter,
            "width": width,
            "offset": offset,
            "pcd": pcd,
            "center_bore": center_bore,
            "finish": finish,
            "weight": weight,
            "backspace": backspace,
            "seat": seat,
            "made": made,
            "load_rating": load_rating,
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
    return BeautifulSoup(resp.text, "html.parser")


def main():
    os.makedirs("Data/raw", exist_ok=True)

    session = requests.Session()
    session.cookies.update(COOKIES)
    session.headers.update(HEADERS)

    print("Fetching page 1 to determine total pages...")
    soup = fetch_page(session, 1)
    total_pages = get_total_pages(soup)
    print(f"  Total pages: {total_pages}")

    all_records = []
    all_records.extend(parse_rows(soup))
    print(f"  Page 1: {len(all_records)} rows")

    for page in range(2, total_pages + 1):
        soup = fetch_page(session, page)
        rows = parse_rows(soup)
        all_records.extend(rows)
        print(f"  Page {page}: {len(rows)} rows (total so far: {len(all_records)})")

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_records)

    print(f"\nDone. {len(all_records)} rows saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
