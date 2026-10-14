#!/usr/bin/env python3
"""
Cookie refresh utility — programmatic login for all 4 suppliers.
Run from: pipelines/
Saves each fresh session to shared/Data/session_cookies.json (see
shared/cookie_store.py), which every scraper reads at startup.

Usage:
    python refresh_cookies.py              # refresh all
    python refresh_cookies.py --supplier formula nc emerge wheelsmart
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR / "shared"))
from tls_chain import browser_needs_chain_bypass  # noqa: E402
import cookie_store  # noqa: E402
_HEADED = False

# ── Supplier configs ──────────────────────────────────────────────────────────
SUPPLIERS = {
    "formula": {
        "name": "Formula Distributors",
        "base_url": "https://weborder.formuladistributors.com/WebOrder/Product/Search",
        "login_url": "https://weborder.formuladistributors.com/WebOrder/",
        "username": "12656",
        "password": "torque",
        "platform": "costar",
    },
    "nc": {
        "name": "North Country",
        "base_url": "https://online.northcountrytire.ca/WebOrder/Product/Search",
        "login_url": "https://online.northcountrytire.ca/WebOrder/",
        "username": "4s011",
        "password": "5069362",
        "platform": "costar",
    },
    "emerge": {
        "name": "Emerge Tires",
        "base_url": "https://order.emergetires.com/WebOrder/Product/Search",
        "login_url": "https://order.emergetires.com/WebOrder/",
        "username": "SAVEB",
        "password": "saveburnaby604",
        "platform": "costar",
    },
    "wheelsmart": {
        "name": "WheelMart Canada",
        "base_url": "https://wheelmartcanada.ca",
        "login_url": "https://wheelmartcanada.ca/auth/login",
        "username": "savebigontiresburnaby@gmail.com",
        "password": "123456",
        "platform": "wheelsmart",
    },
}


async def login_costar(supplier: dict) -> str | None:
    """Log into a Costar WebOrder portal and return a fresh COOKIE_STRING."""
    host = urlparse(supplier["login_url"]).hostname
    # Emerge serves its chain without the issuing intermediate, which Chromium
    # (unlike a desktop browser) will not fetch for itself — page.goto() dies on
    # ERR_CERT_AUTHORITY_INVALID before a login form is ever rendered. Ignore
    # that error only for a certificate we have verified ourselves; see
    # shared/tls_chain.py.
    ignore_https_errors = browser_needs_chain_bypass(host)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not _HEADED)
        ctx = await browser.new_context(
            ignore_https_errors=ignore_https_errors,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()

        print(f"  [{supplier['name']}] navigating to login page...")
        try:
            await page.goto(supplier["login_url"], timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as e:
            print(f"  [{supplier['name']}] goto failed: {e}")
            await browser.close()
            return None

        # Costar login: wait for the UserName field (page IS the login page at /WebOrder/)
        try:
            await page.wait_for_selector('#UserName', timeout=10000)
        except Exception:
            print(f"  [{supplier['name']}] #UserName not found — page: {page.url}")
            await browser.close()
            return None

        await page.fill('#UserName', supplier["username"])
        await page.fill('#Password', supplier["password"])

        print(f"  [{supplier['name']}] submitting credentials...")
        # Wait for Login button to be visible then click it by ID
        try:
            await page.wait_for_selector('#Login', state='visible', timeout=10000)
            await page.click('#Login')
        except Exception:
            # Fallback: press Enter in password field
            await page.press('#Password', 'Enter')
        try:
            await page.wait_for_url(lambda u: 'WebOrder' in u and 'Account' not in u and u != supplier['login_url'],
                                    timeout=20000)
        except Exception:
            await page.wait_for_load_state("networkidle", timeout=15000)

        # Still on login page means bad credentials
        if await page.query_selector('#UserName') is not None:
            print(f"  [{supplier['name']}] still on login page — credentials may be wrong")
            await browser.close()
            return None

        # Extract cookies
        cookies = await ctx.cookies()
        cookie_parts = []
        costar_names = {"CostarWebAppData", "CostarWebUserData", "ASP.NET_SessionId",
                        "CwsVersion", "DbVersion", "UnifiedBuildNumber",
                        "CostarLanguage", "SessionID", "Ux"}
        # Prefer exact Costar cookies; fallback: grab all
        named = {c["name"]: c["value"] for c in cookies}

        if "ASP.NET_SessionId" not in named:
            print(f"  [{supplier['name']}] ASP.NET_SessionId not found — login may have failed")
            print(f"  [{supplier['name']}] Got cookies: {list(named.keys())}")
            await browser.close()
            return None

        # Build cookie string in the same order as the originals
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies
                               if c["name"] in costar_names or c["name"] == "ASP.NET_SessionId")
        if not cookie_str:
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        print(f"  [{supplier['name']}] login OK — {len(cookies)} cookies captured")
        await browser.close()
        return cookie_str


async def login_wheelsmart(supplier: dict) -> dict | None:
    """Log into WheelMart and return fresh {_identity, PHPSESSID} dict."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not _HEADED)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        print(f"  [{supplier['name']}] navigating to login page...")
        try:
            await page.goto(supplier["login_url"], timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as e:
            print(f"  [{supplier['name']}] goto failed: {e}")
            await browser.close()
            return None

        # WheelMart Yii2: /auth/login with LoginForm[email] + LoginForm[password]
        try:
            await page.wait_for_selector('#loginform-email', timeout=10000)
        except Exception:
            print(f"  [{supplier['name']}] #loginform-email not found — page: {page.url}")
            await browser.close()
            return None

        await page.fill('#loginform-email', supplier["username"])
        await page.fill('#loginform-password', supplier["password"])

        print(f"  [{supplier['name']}] submitting credentials...")
        # WheelMart: click the named submit button directly
        await page.click('button[name="login-button"]')
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        # NOTE: WheelMart URL stays on /auth/login after login — only page content changes.
        # Check for the _identity cookie to confirm success, not the URL.
        cookies = await ctx.cookies()
        named = {c["name"]: c["value"] for c in cookies}

        if "_identity" not in named and "PHPSESSID" not in named:
            print(f"  [{supplier['name']}] session cookies not found: {list(named.keys())}")
            await browser.close()
            return None

        print(f"  [{supplier['name']}] login OK — identity/session captured")
        await browser.close()
        return {"_identity": named.get("_identity", ""),
                "PHPSESSID": named.get("PHPSESSID", "")}


def update_costar_files(supplier: dict, cookie_str: str):
    """Save a Costar supplier's fresh COOKIE_STRING to shared/cookie_store.

    The scrapers read the store at startup (falling back to their own literal),
    so nothing here edits a scraper's source any more. The name is kept because
    costar_session and backfill_costar_images call it."""
    cookie_store.put(_store_key(supplier), cookie_str)
    print(f"  [saved] {supplier['name']} cookie -> {cookie_store.STORE.name}")
    return [cookie_store.STORE.name]


def update_wheelsmart_files(supplier: dict, cookies: dict):
    """Save WheelMart's _identity / PHPSESSID to shared/cookie_store."""
    kept = {k: v for k, v in cookies.items() if k in ("_identity", "PHPSESSID") and v}
    if not kept:
        print(f"  [no change] {supplier['name']} - login returned no cookies")
        return []
    current = cookie_store.get("wheelsmart", {}) or {}
    cookie_store.put("wheelsmart", {**current, **kept})
    print(f"  [saved] {supplier['name']} cookies -> {cookie_store.STORE.name}")
    return [cookie_store.STORE.name]


def _store_key(supplier: dict) -> str:
    """SUPPLIERS key for a supplier config dict (the store is keyed the same way)."""
    for key, cfg in SUPPLIERS.items():
        if cfg is supplier or cfg["name"] == supplier["name"]:
            return key
    raise KeyError(f"unknown supplier {supplier.get('name')!r}")


async def refresh_supplier(key: str):
    supplier = SUPPLIERS[key]
    print(f"\n{'='*50}")
    print(f"Refreshing: {supplier['name']}")
    print(f"{'='*50}")

    if supplier["platform"] == "costar":
        cookie_str = await login_costar(supplier)
        if cookie_str:
            update_costar_files(supplier, cookie_str)
            print(f"  DONE: {supplier['name']} cookies updated")
            return True
        else:
            print(f"  FAILED: {supplier['name']} login unsuccessful")
            return False
    elif supplier["platform"] == "wheelsmart":
        cookies = await login_wheelsmart(supplier)
        if cookies:
            update_wheelsmart_files(supplier, cookies)
            print(f"  DONE: {supplier['name']} cookies updated")
            return True
        else:
            print(f"  FAILED: {supplier['name']} login unsuccessful")
            return False


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--supplier", nargs="+", choices=list(SUPPLIERS.keys()),
                        default=list(SUPPLIERS.keys()))
    parser.add_argument("--headed", action="store_true",
                        help="Show browser window (useful for diagnosing login failures)")
    args = parser.parse_args()

    global _HEADED
    _HEADED = args.headed

    print(f"Refreshing cookies for: {args.supplier}")
    results = await asyncio.gather(*[refresh_supplier(k) for k in args.supplier])

    print(f"\n{'='*50}")
    print("SUMMARY")
    print(f"{'='*50}")
    for key, ok in zip(args.supplier, results):
        status = "OK" if ok else "FAILED"
        print(f"  {SUPPLIERS[key]['name']:<25} {status}")


if __name__ == "__main__":
    asyncio.run(main())
