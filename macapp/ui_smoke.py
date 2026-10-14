#!/usr/bin/env python3
"""Drive the client-build dashboard in a headless browser (macapp/build.py step 5).

Expects a dashboard server already running for --home (the frozen app's
--sbot-serve in CI). Checks what a client would hit first:

  * the page renders, every tab opens without a Streamlit exception
  * there is no Sync tab (the client copy only produces CSVs)
  * "Prepare upload files" writes an import CSV into Uploads/ in which every
    product row carries its Supplier_ tag

Screenshots of each tab land in --shots for a human look.
"""

import argparse
import sys
import time
import urllib.request
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright


def wait_healthy(url, seconds=180):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/_stcore/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:                                   # noqa: BLE001
            pass
        time.sleep(1)
    sys.exit(f"dashboard at {url} never became healthy")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True, type=Path)
    ap.add_argument("--shots", required=True, type=Path)
    ap.add_argument("--url", default="http://localhost:8642")
    args = ap.parse_args()
    args.shots.mkdir(parents=True, exist_ok=True)
    uploads = args.home / "Uploads"
    wait_healthy(args.url)

    failures = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(args.url)
        page.get_by_text("Inventory Pipeline").first.wait_for(timeout=90_000)
        tabs = page.get_by_role("tab")
        tabs.first.wait_for(timeout=30_000)
        names = [t.inner_text().strip() for t in tabs.all()]
        print("tabs:", names)
        if any("Sync" in n for n in names):
            failures.append(f"client build shows a Sync tab: {names}")
        if not any("Upload" in n for n in names):
            failures.append(f"no Upload tab: {names}")

        for i, name in enumerate(names):
            page.get_by_role("tab", name=name).click()
            time.sleep(2)
            errors = page.locator('[data-testid="stException"]')
            if errors.count():
                failures.append(f"tab {name!r}: {errors.first.inner_text()[:2000]}")
            page.screenshot(path=str(args.shots / f"tab{i + 1}.png"), full_page=True)

        before = set(uploads.glob("*/*.csv")) if uploads.exists() else set()
        page.get_by_role("tab", name=next(n for n in names if "Upload" in n)).click()
        page.get_by_role("button", name="Prepare upload files").click()
        deadline = time.time() + 120
        new = set()
        while time.time() < deadline and not new:
            time.sleep(1)
            new = (set(uploads.glob("*/*.csv")) if uploads.exists() else set()) - before
        page.screenshot(path=str(args.shots / "upload_done.png"), full_page=True)
        if not new:
            failures.append("Prepare upload files wrote nothing to Uploads/")
        for path in sorted(new):
            frame = pd.read_csv(path, dtype=str, keep_default_na=False)
            products = frame[frame["Title"].str.strip() != ""]
            untagged = products[~products["Tags"].str.contains("Supplier_")]
            print(f"{path.name}: {len(products)} product row(s), {len(untagged)} without a supplier tag")
            if products.empty or len(untagged):
                failures.append(f"{path.name}: {len(untagged)} of {len(products)} rows untagged")
        browser.close()

    for f in failures:
        print("FAIL:", f)
    print("ui smoke:", "FAILED" if failures else "OK")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
