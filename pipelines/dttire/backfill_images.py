#!/usr/bin/env python3
"""Upgrade DT Tire's 130x130 thumbnails to the PDP's 600px images, IN PLACE,
on CSVs that are already on disk — no full re-scrape.

WHY A BACKFILL RATHER THAN A RE-SCRAPE
--------------------------------------
scraper_tires.py / scraper_wheels.py now capture `product_code` and upgrade
images themselves (Pass 3), so every FUTURE scrape is correct. But a full DT
Tire pipeline run takes ~92 minutes (measured: dashboard_logs/
dttire_20260818_184737.log, 18:47:37 -> 20:19:44), and the images already on
disk are wrong today. This script fixes the existing CSVs in a few minutes by
re-resolving only the distinct images.

CSVs written before this change have no `product_code` column, so the PDP code
is recovered by searching each representative SKU (large_images.find_code) —
one extra request per distinct image, still ~1.5k requests total rather than
~9.5k.

BOTH STAGES ARE PATCHED
-----------------------
augment_tires.py reads Data/cleaned/cleaned_tires_*.csv, while clean.py reads
Data/raw/*.csv and regenerates the cleaned file. Patching only `cleaned` would
be silently undone the next time clean.py ran, so raw and cleaned are patched
together from one shared lookup.

Usage:
    python backfill_images.py --dry-run          # report only, no writes
    python backfill_images.py --dry-run --limit 20
    python backfill_images.py                    # apply
    python backfill_images.py --type tires       # one product type
"""
import argparse
import glob
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

# Per-worker pacing between lookups, matching the scrapers' own DELAY.
large_images_delay = 0.15

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import large_images
from scraper_tires import make_session

HERE = os.path.dirname(os.path.abspath(__file__))

# (kind, [glob patterns to patch]) — raw AND cleaned, see module docstring.
TARGETS = {
    "tires": ("tire", [
        os.path.join(HERE, "Data", "raw", "dttire_tires_*.csv"),
        os.path.join(HERE, "Data", "cleaned", "cleaned_tires_*.csv"),
    ]),
    "wheels": ("wheel", [
        os.path.join(HERE, "Data", "raw", "dttire_wheels_*.csv"),
        os.path.join(HERE, "Data", "cleaned", "cleaned_wheels_*.csv"),
    ]),
}


def latest(pattern):
    m = glob.glob(pattern)
    return max(m, key=os.path.getmtime) if m else None


def basename_of(url):
    return str(url or "").split("/")[-1].split("?")[0]


def _bad_urls():
    """URLs the enrichment image-quality audit has already judged unusable.

    Lets a re-pass target only the images that are still wrong instead of
    re-resolving thousands that are already fixed. Empty when the audit has
    never run, in which case --only-bad has nothing to work from.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "shared"))
    import enrichment as E
    cache = E.load_quality_cache()
    return {u for u, e in cache.items() if e.get("verdict") in E.BAD_IMAGE_VERDICTS}


def collect(files, image_col="image_url", only_bad=False):
    """Load each file once. Returns (loaded, basename -> representative sku)."""
    loaded = []
    reps = {}
    bad = _bad_urls() if only_bad else None
    if only_bad:
        print(f"  --only-bad: {len(bad)} URL(s) previously measured as unusable")
    for f in files:
        df = pd.read_csv(f, dtype=str, low_memory=False, encoding="utf-8-sig").fillna("")
        if image_col not in df.columns or "sku" not in df.columns:
            print(f"  skip {os.path.basename(f)} (no {image_col}/sku column)")
            continue
        loaded.append((f, df))
        for url, sku in zip(df[image_col], df["sku"]):
            b = basename_of(url)
            if not b or not sku or large_images.is_placeholder(url) or b in reps:
                continue
            if bad is not None and str(url).strip() not in bad:
                continue
            reps[b] = sku
    return loaded, reps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", choices=["tires", "wheels", "both"], default="both")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--limit", type=int, default=0, help="resolve at most N distinct images")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only-bad", action="store_true",
                    help="Only re-resolve images the enrichment quality audit has "
                         "already measured as unusable (run enrich_runner.py --scan "
                         "--check-images first). Turns a full re-pass into a short one.")
    args = ap.parse_args()

    types = ["tires", "wheels"] if args.type == "both" else [args.type]
    session = None

    for t in types:
        kind, patterns = TARGETS[t]
        files = [f for f in (latest(p) for p in patterns) if f]
        if not files:
            print(f"\n[{t}] no CSVs found — skipping")
            continue

        print(f"\n{'=' * 78}\n[{t}] patching {len(files)} file(s):")
        for f in files:
            print(f"    {os.path.relpath(f, HERE)}")

        loaded, reps = collect(files, only_bad=args.only_bad)
        if not loaded:
            continue
        print(f"[{t}] {len(reps)} distinct images to resolve")

        items = sorted(reps.items())
        if args.limit:
            items = items[:args.limit]
            print(f"[{t}] --limit {args.limit} -> resolving {len(items)}")

        if session is None:
            session = make_session()

        # sku -> code -> large url, deduped by image basename. Two requests per
        # distinct image (SKU search, then PDP), run in parallel — the same
        # concurrency the tire scraper already uses against this host.
        resolved = {}
        done = [0]

        def resolve(pair):
            b, sku = pair
            try:
                code = large_images.find_code(session, sku, kind)
                url = large_images.resolve_one(session, code, kind) if code else ""
            except Exception:
                url = ""
            time.sleep(large_images_delay)
            done[0] += 1
            if done[0] % 100 == 0 or done[0] == len(items):
                print(f"    [{done[0]}/{len(items)}] resolved={len(resolved)}")
            return b, url

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for b, url in ex.map(resolve, items):
                if url:
                    resolved[b] = url

        print(f"[{t}] resolved {len(resolved)}/{len(items)} distinct images")

        # apply
        for f, df in loaded:
            new_urls, changed, blanked, refused = [], 0, 0, 0
            for url in df["image_url"]:
                if large_images.is_placeholder(url):
                    new_urls.append("")
                    blanked += 1
                    continue
                b = basename_of(url)
                new = resolved.get(b)
                if not new or new == url:
                    new_urls.append(url)
                elif not large_images.same_product_image(url, new):
                    # A SKU search is a prefix match, so a resolution can land
                    # on a different product. Keep the small-but-correct image.
                    new_urls.append(url)
                    refused += 1
                else:
                    new_urls.append(new)
                    changed += 1
            df["image_url"] = new_urls
            label = os.path.relpath(f, HERE)
            suffix = f", refused {refused} (filename mismatch)" if refused else ""
            if args.dry_run:
                print(f"  [DRY RUN] {label}: would update {changed} row(s), "
                      f"blank {blanked} placeholder(s){suffix}")
            else:
                df.to_csv(f, index=False, encoding="utf-8-sig")
                print(f"  {label}: updated {changed} row(s), "
                      f"blanked {blanked} placeholder(s){suffix}")

    if args.dry_run:
        print("\nDRY RUN — nothing was written.")


if __name__ == "__main__":
    main()
