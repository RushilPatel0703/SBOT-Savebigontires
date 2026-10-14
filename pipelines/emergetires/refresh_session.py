#!/usr/bin/env python3
"""Refresh Emerge Tires Costar session cookies into shared/cookie_store (read by both scrapers).

The Costar SPA sets CostarWebAppData (with CwsVersion) only after the User/Login API
call completes — this requires a headed browser (headless=False). The login button is
id="Login" in the AngularJS SPA (not type="submit").
"""
import pathlib, re, sys, time, urllib.parse
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "shared"))
from tls_chain import browser_needs_chain_bypass
import cookie_store

LOGIN_URL = "https://order.emergetires.com/WebOrder/Product/Search"
USERNAME  = "SAVEB"
PASSWORD  = "saveburnaby604"

NEEDED_COOKIES = {"CostarWebAppData", "CostarWebUserData", "ASP.NET_SessionId"}


def _cwsversion_from_cookie(cookies: list) -> str:
    """Parse CwsVersion out of CostarWebAppData cookie value."""
    for c in cookies:
        if c["name"] == "CostarWebAppData":
            val = urllib.parse.unquote(c["value"])
            m = re.search(r'CwsVersion=([^&]+)', val)
            if m:
                return m.group(1).strip()
    return ""


def main():
    with sync_playwright() as pw:
        # Must use headless=False — the Costar SPA only calls User/Login API
        # (which sets CwsVersion in CostarWebAppData) when a real browser is used.
        browser = pw.chromium.launch(headless=False)
        # order.emergetires.com omits its issuing intermediate; Chromium will not
        # fetch it and refuses the page. See ../shared/tls_chain.py — the bypass
        # only applies to a certificate verified there first.
        ctx = browser.new_context(
            ignore_https_errors=browser_needs_chain_bypass(
                urllib.parse.urlparse(LOGIN_URL).hostname)
        )
        page = ctx.new_page()
        print("Navigating to login page...")
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=60000)

        # Costar AngularJS SPA — the login form is rendered by Angular inside the page.
        # We need to wait for Angular to bootstrap and render the login form.
        try:
            # Wait for username field to appear after Angular bootstraps
            print("Waiting for Angular login form to render...")
            page.wait_for_selector('[name="UserName"]', timeout=30000)
            print("Login form found, filling credentials...")
            page.fill('[name="UserName"]', USERNAME)
            page.fill('[name="Password"]', PASSWORD)
            # The Costar SPA login button is id="Login" (no type="submit")
            page.click('#Login')
            # After login, Costar SPA may stay on same URL but change state.
            # Wait for networkidle then check we have the needed cookies.
            page.wait_for_load_state("networkidle", timeout=30000)
            print("Login click done, current URL:", page.url)
        except Exception as e:
            print(f"Login failed: {e}")
            # Dump full page HTML for debugging
            print("Current URL:", page.url)
            print(page.content()[:3000])
            browser.close()
            sys.exit(1)

        # Poll until CostarWebAppData has a non-empty CwsVersion (SPA fully initialized).
        # The cookie is set after the User/Login CostarApi call completes (~1-3s after networkidle).
        print("Waiting for Costar SPA to fully initialize (CwsVersion to populate)...")
        deadline = time.time() + 15
        cws_ver = ""
        while time.time() < deadline:
            cookies = ctx.cookies()
            cws_ver = _cwsversion_from_cookie(cookies)
            if cws_ver:
                print(f"  CwsVersion populated: {cws_ver}")
                break
            time.sleep(0.5)

        if not cws_ver:
            print(f"  WARNING: CwsVersion never populated within 15s — API calls may fail with #InvalidSessionGuid")

        cookies = ctx.cookies()
        browser.close()

    print("Captured cookies:", [c["name"] for c in cookies])
    kept = {c["name"]: c["value"] for c in cookies if c["name"] in NEEDED_COOKIES}
    if not kept.get("ASP.NET_SessionId"):
        print("ERROR: ASP.NET_SessionId not found — login may have failed")
        sys.exit(1)

    # Verify CwsVersion is present in the final cookie
    cws_check = _cwsversion_from_cookie(cookies)
    if not cws_check:
        print("WARNING: CostarWebAppData still has empty CwsVersion — API calls may get #InvalidSessionGuid")
    else:
        print(f"CwsVersion confirmed: {cws_check}")

    cookie_str = "; ".join(f"{k}={v}" for k, v in kept.items())
    print(f"New COOKIE_STRING (first 80 chars): {cookie_str[:80]}...")

    cookie_store.put("emerge", cookie_str)
    print(f"Saved fresh cookies to {cookie_store.STORE.name} (read by both scrapers).")

if __name__ == "__main__":
    main()
