#!/usr/bin/env python3
"""North Country Tire Pipeline Orchestrator

Tire steps:
  1. scraper_tires    — scrape raw tire data → Data/raw/
  2. clean_tires      — clean/filter (qty >= 4) → Data/cleaned/
  3. augment_tires    — build Shopify SEO CSV → Data/shopify/
  4. create_products  — create new tire SKUs on Shopify
  5. update_images    — push tire image URLs to existing Shopify products

Wheel steps:
  6. scraper_wheels        — scrape raw wheel data → Data/raw/
  7. clean_wheels          — clean/filter → Data/cleaned/
  8. augment_wheels        — build Shopify SEO CSV → Data/shopify/
  9. create_wheels         — create new wheel SKUs on Shopify
  10. update_images_wheels — push wheel image URLs to existing Shopify products

Note: build_sku_map and price/inventory sync are in pipelines/merged/run_sync.py.
      Run run_sync.py after this pipeline completes.

Usage:
  python run_pipeline.py                              # full run (tires + wheels)
  python run_pipeline.py --skip scraper_tires         # skip scraping, use existing raw data
  python run_pipeline.py --only augment_wheels        # just one step
  python run_pipeline.py --from clean_wheels          # resume from a step
  python run_pipeline.py --dry-run                    # preview create steps only
"""

import sys
import subprocess
import argparse
import logging
import os
from datetime import datetime

os.makedirs("logs", exist_ok=True)
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(f"logs/pipeline_{ts}.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("pipeline")

_UTF8_ENV = {**os.environ, "PYTHONUTF8": "1"}

STEPS = [
    ("scraper_tires",        "scraper_tires.py",    []),
    ("brand_sweep",          "brand_sweep.py",      ["--merge"]),  # gap-fill via brand axis
    ("clean_tires",          "clean.py",            ["--type", "tires"]),
    ("augment_tires",        "augment_tires.py",    []),
    ("create_products",      "create_products.py",  ["--type", "tires"]),
    ("update_images",        "update_images.py",    ["--type", "tires"]),
    ("scraper_wheels",       "scraper_wheels.py",   []),
    ("clean_wheels",         "clean.py",            ["--type", "wheels"]),
    ("augment_wheels",       "augment_wheels.py",   []),
    ("create_wheels",        "create_products.py",  ["--type", "wheels"]),
    ("update_images_wheels", "update_images.py",    ["--type", "wheels"]),
]

DRY_RUN_STEPS = {"create_products", "update_images", "create_wheels", "update_images_wheels"}
FULL_STEPS    = {"scraper_tires", "scraper_wheels"}  # steps that accept --full (rebuild zero-terms cache)

STEP_NAMES = [n for n, _, _ in STEPS]


def run_step(name, script, extra_args, dry_run=False, full=False):
    log.info("=" * 55)
    log.info(f"STEP: {name} ({script})")
    log.info("=" * 55)
    args = extra_args[:]
    if dry_run and name in DRY_RUN_STEPS:
        args.append("--dry-run")
    if full and name in FULL_STEPS:
        args.append("--full")
    result = subprocess.run([sys.executable, script] + args, env=_UTF8_ENV)
    if result.returncode != 0:
        log.error(f"Step '{name}' failed (exit {result.returncode}). Pipeline stopped.")
        sys.exit(result.returncode)
    log.info(f"Step '{name}' — OK\n")


def main():
    parser = argparse.ArgumentParser(description="North Country Tire Pipeline")
    parser.add_argument("--skip", nargs="*", default=[], metavar="STEP",
                        help=f"Steps to skip. Choices: {STEP_NAMES}")
    parser.add_argument("--only", default=None, metavar="STEP",
                        help="Run only this step")
    parser.add_argument("--from", dest="from_step", default=None, metavar="STEP",
                        help="Run from this step onwards")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pass --dry-run to create_products step (no Shopify writes)")
    parser.add_argument("--full", action="store_true",
                        help="Pass --full to scraper steps (search all terms, rebuild zero-terms cache)")
    args = parser.parse_args()

    steps_to_run = STEPS

    if args.only:
        steps_to_run = [(n, s, a) for n, s, a in STEPS if n == args.only]
        if not steps_to_run:
            log.error(f"Unknown step: {args.only}. Valid: {STEP_NAMES}")
            sys.exit(1)
    elif args.from_step:
        if args.from_step not in STEP_NAMES:
            log.error(f"Unknown step: {args.from_step}. Valid: {STEP_NAMES}")
            sys.exit(1)
        idx          = STEP_NAMES.index(args.from_step)
        steps_to_run = STEPS[idx:]
    elif args.skip:
        unknown = [s for s in args.skip if s not in STEP_NAMES]
        if unknown:
            log.error(f"Unknown steps to skip: {unknown}. Valid: {STEP_NAMES}")
            sys.exit(1)
        steps_to_run = [(n, s, a) for n, s, a in STEPS if n not in args.skip]

    log.info(f"Pipeline start — {len(steps_to_run)} step(s): {[n for n, _, _ in steps_to_run]}")
    if args.dry_run:
        log.info("DRY RUN: create_products step will preview only.")
    if args.full:
        log.info("FULL: scrapers will search all terms and rebuild the zero-terms cache.")

    for name, script, extra_args in steps_to_run:
        run_step(name, script, extra_args, dry_run=args.dry_run, full=args.full)

    log.info("=" * 55)
    log.info("Pipeline complete.")
    log.info("=" * 55)


if __name__ == "__main__":
    main()
