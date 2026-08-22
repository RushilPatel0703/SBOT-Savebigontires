#!/usr/bin/env python3
"""Re-pick every Costar supplier's product images by MEASURED size, in place,
on CSVs already on disk — no re-scrape.

WHY THIS EXISTS
---------------
Costar returns a list of images per product and labels each LARGE or SMALL.
The scrapers used to trust that label. It is not trustworthy. One real North
Country product (Cooper Endeavor Plus 245/60R18), every image measured:

    imageType=LARGE  680e4435-....jpg  ->  200x290   <- what was shipped
    imageType=SMALL  670e4435-....jpg  ->  400x580   <- 4x the pixels, free
    imageType=SMALL  690e4435-....jpg  ->   54x54

Across 200 currently-low-res North Country tires (one product per distinct
image), the label-ranked pick was smaller than the largest image the API
offered on 31 of them — 16%. Those upgrades cost nothing but an HTTP GET the
quality audit was already making.

costar_images.pick_best_image() now ranks on measured pixels when given a
quality cache, so every FUTURE scrape picks correctly. This script applies the
same re-pick to data already scraped, which is what the storefront is serving
today.

WHAT IT WILL AND WILL NOT DO
----------------------------
An image is replaced ONLY when the candidate is measurably larger on its short
side than the one currently in the CSV. Never sideways, never smaller. The
match is on productGuid, which is exact — unlike DT Tire's SKU search (see
dttire/backfill_images.py) there is no prefix-collision risk here, so no
filename guard is needed.

Rows whose image is already the biggest one available are left alone and
reported as such: this fixes the picking, it does not invent pixels Costar
does not have. Costar's own ceiling is 600px on the short side, so a product
whose largest image is 300px stays a 300px product.

BOTH STAGES ARE PATCHED
-----------------------
clean.py regenerates the cleaned CSV from raw, so patching only `cleaned`
would be undone on the next run. Raw and cleaned are patched together from one
shared {productGuid: url} lookup.

Usage:
    python backfill_costar_images.py --dry-run
    python backfill_costar_images.py --dry-run --supplier northcountry
    python backfill_costar_images.py                      # apply
    python backfill_costar_images.py --all-rows           # not just bad ones
"""
import argparse
import asyncio
import glob
import os
import sys
import uuid

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PIPELINES = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, PIPELINES)

import costar_images as CI
import enrichment as E

BATCH = 50

# supplier key -> how to reach that supplier's Costar API and which CSVs carry
# its images. `cookie_key` matches refresh_cookies.SUPPLIERS.
TARGETS = [
    dict(name="northcountry/tires", cookie_key="nc",
         module="northcountry.scraper_tires", api="api_post",
         files=["northcountry/Data/raw/northcountry_tires_*.csv",
                "northcountry/Data/cleaned/cleaned_tires_*.csv"]),
    dict(name="northcountry/wheels", cookie_key="nc",
         module="northcountry.scraper_wheels", api="api_post",
         files=["northcountry/Data/raw/northcountry_wheels_*.csv",
                "northcountry/Data/cleaned/cleaned_wheels_*.csv"]),
    dict(name="formula/tires", cookie_key="formula",
         module="formula_distributors.scraper", api="post",
         files=["formula_distributors/Data/raw/formula_tires_*.csv",
                "formula_distributors/Data/cleaned/cleaned_tires_*.csv"]),
    # formula wheels: augment_wheels.py reads Data/raw directly and the cleaned
    # file is never consumed downstream — see enrichment.SOURCES.
    dict(name="formula/wheels", cookie_key="formula",
         module="formula_distributors.scraper_wheels", api="post",
         files=["formula_distributors/Data/raw/formula_wheels_*.csv"]),
    dict(name="emergetires/tires", cookie_key="emerge",
         module="emergetires.scraper_tires", api="api_post",
         files=["emergetires/Data/raw/emergetires_tires_*.csv",
                "emergetires/Data/cleaned/cleaned_tires_*.csv"]),
]


def latest(pattern):
    m = glob.glob(os.path.join(PIPELINES, pattern))
    return max(m, key=os.path.getmtime) if m else None


def load_scraper(target, refresh_cookie=True):
    """Import a scraper module and hand back a callable that fetches
    ProductInfo for a batch of GUIDs.

    The cookie is refreshed up front rather than relying on the session guard's
    reactive re-auth: a stale Costar session does not error, it returns an
    empty productImageList, which would look exactly like "this supplier has no
    better images" and silently do nothing.
    """
    pkg, mod = target["module"].split(".")
    pkg_dir = os.path.join(PIPELINES, pkg)
    sys.path.insert(0, pkg_dir)
    cwd = os.getcwd()
    os.chdir(pkg_dir)
    try:
        # Loaded from its file under a package-qualified name, NOT by bare
        # `import scraper_tires`. Three suppliers each have a module called
        # scraper_tires and two more have scraper_wheels, so a bare import of
        # the second one returns the FIRST supplier's module out of
        # sys.modules — which then queries the wrong host with this supplier's
        # GUIDs and quietly returns nothing.
        import importlib.util
        unique = f"_backfill_{pkg}_{mod}"
        spec = importlib.util.spec_from_file_location(
            unique, os.path.join(pkg_dir, f"{mod}.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique] = module
        spec.loader.exec_module(module)
    finally:
        os.chdir(cwd)

    if refresh_cookie:
        import refresh_cookies as rc
        cookie = asyncio.run(rc.login_costar(rc.SUPPLIERS[target["cookie_key"]]))
        if cookie:
            module._SESSION.cookie = cookie

    if target["api"] == "api_post":
        def fetch(guids):
            body = {"criteriaList": [{"dataField": "ProductGuidList",
                                      "criteriaList": [{"dataField": "ProductGuid",
                                                        "guid": g} for g in guids]}],
                    "requestId": str(uuid.uuid4()).upper()}
            return module.api_post("Product/ProductInfo", body)
    else:                                       # formula: post(session, path, payload)
        # formula's scrapers build their session inline in main() rather than
        # exposing a factory, so the same three lines are repeated here.
        import requests
        session = requests.Session()
        session.headers.update(module.HEADERS)
        session.headers["Cookie"] = module._SESSION.cookie
        def fetch(guids):
            return module.post(session, f"{module.API_BASE}/Product/ProductInfo",
                               module.build_product_info_payload(list(guids)))
    return fetch


def collect_guids(files, quality_cache, all_rows):
    """{productGuid: current_url} for rows worth re-picking, plus the loaded
    frames so each file is read once."""
    loaded, want = [], {}
    for f in files:
        df = pd.read_csv(f, dtype=str, low_memory=False,
                         encoding="utf-8-sig").fillna("")
        if "product_guid" not in df.columns or "image_url" not in df.columns:
            print(f"    skip {os.path.basename(f)} (no product_guid/image_url)")
            continue
        loaded.append((f, df))
        for guid, url in zip(df["product_guid"], df["image_url"]):
            guid, url = str(guid).strip(), str(url).strip()
            if not guid or guid in want:
                continue
            if not all_rows:
                # Only products whose current image is already known bad. A
                # good image is not worth spending requests to maybe improve.
                if E.image_verdict(url, quality_cache) not in E.BAD_IMAGE_VERDICTS:
                    continue
            want[guid] = url
    return loaded, want


def side_of(url, cache):
    e = (cache or {}).get(str(url).strip())
    if not e or not e.get("w") or not e.get("h"):
        return 0
    return min(e["w"], e["h"])


def run_target(target, args, quality_cache):
    files = [f for f in (latest(p) for p in target["files"]) if f]
    if not files:
        print(f"\n[{target['name']}] no CSVs found — skipping")
        return quality_cache

    print(f"\n{'=' * 78}\n[{target['name']}] patching {len(files)} file(s):")
    for f in files:
        print(f"    {os.path.relpath(f, PIPELINES)}")

    loaded, want = collect_guids(files, quality_cache, args.all_rows)
    if not loaded or not want:
        print(f"[{target['name']}] nothing to re-pick")
        return quality_cache
    guids = sorted(want)
    if args.limit:
        guids = guids[:args.limit]
    print(f"[{target['name']}] {len(guids)} product(s) to re-pick")

    fetch = load_scraper(target, refresh_cookie=not args.no_cookie_refresh)

    entries_by_guid, empty_batches = {}, 0
    for i in range(0, len(guids), BATCH):
        batch = guids[i:i + BATCH]
        try:
            data = fetch(batch)
        except Exception as exc:                                    # noqa: BLE001
            print(f"    batch {i//BATCH + 1}: FAILED ({exc}) — skipping")
            continue
        imgs = data.get("productImageList") or []
        if not imgs:
            empty_batches += 1
        for e in imgs:
            g = e.get("productGuid", "")
            if g:
                entries_by_guid.setdefault(g, []).append(e)
        if (i // BATCH + 1) % 10 == 0:
            print(f"    ...{min(i + BATCH, len(guids))}/{len(guids)} products")

    print(f"[{target['name']}] image lists for {len(entries_by_guid)}/{len(guids)} products")
    if empty_batches and not entries_by_guid:
        print(f"[{target['name']}] every batch came back empty — session is "
              f"probably dead; nothing written")
        return quality_cache

    # Measure every candidate ONCE, cached across suppliers and runs.
    all_urls = set()
    for entries in entries_by_guid.values():
        all_urls.update(CI.candidate_urls(entries))
    all_urls.update(want.values())
    quality_cache = E.measure_image_urls(sorted(all_urls), workers=args.workers)

    # Re-pick, and keep only strict upgrades.
    upgrades, already_best, no_better = {}, 0, 0
    gains = []
    for guid, entries in entries_by_guid.items():
        new = CI.pick_best_image(entries, quality_cache)
        cur = want.get(guid, "")
        cur_side, new_side = side_of(cur, quality_cache), side_of(new, quality_cache)
        if not new or new == cur:
            already_best += 1
        elif new_side > cur_side:
            upgrades[guid] = new
            gains.append((cur_side, new_side, guid, new))
        else:
            no_better += 1

    print(f"[{target['name']}] {len(upgrades)} upgrade(s), "
          f"{already_best} already best, {no_better} no larger image available")
    gains.sort(key=lambda g: g[1] - g[0], reverse=True)
    for cur, new, guid, url in gains[:5]:
        print(f"    {cur:4d} -> {new:4d}px  {guid}")

    for f, df in loaded:
        changed = 0
        new_col = []
        for guid, url in zip(df["product_guid"], df["image_url"]):
            up = upgrades.get(str(guid).strip())
            if up and up != str(url).strip():
                new_col.append(up)
                changed += 1
            else:
                new_col.append(url)
        df["image_url"] = new_col
        label = os.path.relpath(f, PIPELINES)
        if args.dry_run:
            print(f"  [DRY RUN] {label}: would update {changed} row(s)")
        else:
            df.to_csv(f, index=False, encoding="utf-8-sig")
            print(f"  {label}: updated {changed} row(s)")
    return quality_cache


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--supplier", help="only this supplier (northcountry/formula/emergetires)")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--limit", type=int, default=0, help="at most N products per target")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--all-rows", action="store_true",
                    help="re-pick every product, not just those whose current "
                         "image the quality audit already judged bad")
    ap.add_argument("--no-cookie-refresh", action="store_true",
                    help="use the cookie already in the scraper (skips Playwright login)")
    args = ap.parse_args()

    quality_cache = E.load_quality_cache()
    for t in TARGETS:
        if args.supplier and not t["name"].startswith(args.supplier):
            continue
        try:
            quality_cache = run_target(t, args, quality_cache)
        except Exception as exc:                                    # noqa: BLE001
            print(f"\n[{t['name']}] FAILED: {type(exc).__name__}: {exc}")

    if args.dry_run:
        print("\nDRY RUN — nothing was written.")


if __name__ == "__main__":
    main()
