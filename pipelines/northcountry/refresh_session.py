#!/usr/bin/env python3
"""Refresh North Country Tire Costar session cookies and patch scraper_wheels.py."""
import pathlib, re, sys
from playwright.sync_api import sync_playwright

LOGIN_URL = "https://online.northcountrytire.ca/WebOrder/Product/Search"
USERNAME  = "4s011"
PASSWORD  = "5069362"
SCRAPER   = pathlib.Path(__file__).with_name("scraper_wheels.py")

NEEDED_COOKIES = {"CostarWebAppData", "CostarWebUserData", "ASP.NET_SessionId"}

def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        print("Navigating to login page...")
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=60000)

        # Fill login form — try common Costar selectors
        try:
            page.fill('#UserName, input[name="UserName"]', USERNAME)
            page.fill('#Password, input[name="Password"]', PASSWORD)
            page.click('#Login, input[type="submit"], button[type="submit"]')
            page.wait_for_url("**/Product/Search**", timeout=30000)
            print("Login successful")
        except Exception as e:
            print(f"Login failed: {e}")
            # Dump page HTML for debugging
            print(page.content()[:2000])
            browser.close()
            sys.exit(1)

        cookies = ctx.cookies()
        browser.close()

    print("Captured cookies:", [c["name"] for c in cookies])
    kept = {c["name"]: c["value"] for c in cookies if c["name"] in NEEDED_COOKIES}
    if not kept.get("ASP.NET_SessionId"):
        print("ERROR: ASP.NET_SessionId not found — login may have failed")
        sys.exit(1)

    cookie_str = "; ".join(f"{k}={v}" for k, v in kept.items())
    print(f"New COOKIE_STRING (first 80 chars): {cookie_str[:80]}...")

    # Patch COOKIE_STRING in scraper_wheels.py
    src = SCRAPER.read_text(encoding="utf-8")
    # Replace the COOKIE_STRING = ( ... ) block
    new_src = re.sub(
        r'(COOKIE_STRING\s*=\s*\()[\s\S]*?(\))',
        f'COOKIE_STRING = (\n    "{cookie_str}"\n)',
        src,
        count=1,
    )
    if new_src == src:
        # Fallback: replace single-string assignment
        new_src = re.sub(
            r'(COOKIE_STRING\s*=\s*")[^"]*(")',
            f'\\1{cookie_str}\\2',
            src,
            count=1,
        )
    SCRAPER.write_text(new_src, encoding="utf-8")
    print(f"Patched {SCRAPER.name} with fresh cookies.")

if __name__ == "__main__":
    main()
