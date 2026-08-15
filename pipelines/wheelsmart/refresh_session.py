#!/usr/bin/env python3
"""Refresh WheelMart Canada session cookies and patch scraper_rims.py and scraper_tires.py."""
import pathlib, re, sys
from playwright.sync_api import sync_playwright

LOGIN_URL = "https://wheelmartcanada.ca/auth/login"
USERNAME  = "savebigontiresburnaby@gmail.com"
PASSWORD  = "123456"
SCRAPERS  = [
    pathlib.Path(__file__).with_name("scraper_rims.py"),
    pathlib.Path(__file__).with_name("scraper_tires.py"),
]
NEEDED_COOKIES = {"_identity", "PHPSESSID"}

def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=60000)
        # WheelMart uses Yii2 — email field (was username on old /site/login)
        # Note: after login the URL stays on /auth/login but page content changes to "Tire Inventory"
        try:
            page.fill('#loginform-email', USERNAME)
            page.fill('#loginform-password', PASSWORD)
            page.click('button[name="login-button"]')
            # URL does not change after login; wait for networkidle then check page title
            page.wait_for_load_state("networkidle", timeout=30000)
            import time as _time; _time.sleep(3)
            page.wait_for_load_state("networkidle", timeout=10000)
            # Confirm login by checking that _identity cookie was set
            found_cookies = {c["name"]: c["value"] for c in ctx.cookies()}
            if "_identity" not in found_cookies:
                raise RuntimeError(f"Login may have failed — _identity not set. Page title: {page.title()}")
            print("Login successful")
        except Exception as e:
            print(f"Login failed: {e}\n{page.content()[:1500]}")
            browser.close(); sys.exit(1)
        cookies = ctx.cookies()
        browser.close()

    kept = {c["name"]: c["value"] for c in cookies if c["name"] in NEEDED_COOKIES}
    print("Captured:", list(kept.keys()))
    if "_identity" not in kept:
        print("ERROR: _identity cookie not found"); sys.exit(1)

    # Patch COOKIES dict in both scraper files
    for scraper in SCRAPERS:
        src = scraper.read_text(encoding="utf-8")
        for name, val in kept.items():
            src = re.sub(
                rf'("{name}"\s*:\s*")[^"]*(")',
                rf'\g<1>{val}\g<2>',
                src,
                count=1,
            )
        scraper.write_text(src, encoding="utf-8")
        print(f"Patched {scraper.name}")

if __name__ == "__main__":
    main()
