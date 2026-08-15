#!/usr/bin/env python3
"""Formula Distributors Pipeline Orchestrator

Regular sync steps (in order):
  1. scraper          — scrape raw tire data from Formula Distributors portal
  2. scraper_wheels   — scrape raw wheel data from Formula Distributors portal
  3. clean_tires      — clean/deduplicate tire raw data (qty >= 4 filter applied)
  4. clean_wheels     — clean/deduplicate wheel raw data (qty >= 4 filter applied)
  5. serp_enrich      — SerpAPI designation enrichment for Layer 4/5 and Unknown rows
  6. augment          — build Shopify_SEO_tires_<ts>.csv (prices include $30 markup)
  7. augment_wheels   — build Shopify_SEO_wheels_<ts>.csv

Note: build_sku_map and price/inventory sync are handled by pipelines/merged/run_sync.py.
      create_products.py is run manually for initial product import only.
      update_wheels.py is run manually to push updated tags/body to existing wheel products.

Usage:
  python run_pipeline.py                        # full run
  python run_pipeline.py --skip scraper         # skip scraping, use existing raw data
  python run_pipeline.py --only augment         # run one step only
  python run_pipeline.py --from serp_enrich     # run from a step onwards
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
        logging.StreamHandler()
    ]
)
log = logging.getLogger("pipeline")

_UTF8_ENV = {**os.environ, "PYTHONUTF8": "1"}

STEPS = [
    ("scraper",        "scraper.py",        []),
    ("scraper_wheels", "scraper_wheels.py", []),
    ("clean_tires",    "clean.py",          ["--type", "tires"]),
    ("clean_wheels",   "clean.py",          ["--type", "wheels"]),
    ("serp_enrich",    "serp_enrich.py",    []),
    ("augment",        "augment.py",        []),
    ("augment_wheels", "augment_wheels.py", []),
    ("create_tires",   "create_products.py", ["--type", "tires"]),
    ("create_wheels",  "create_products.py", ["--type", "wheels"]),
]

DRY_RUN_STEPS = {"create_tires", "create_wheels"}

STEP_NAMES = [n for n, _, _ in STEPS]


def run_step(name, script, args, dry_run=False):
    log.info(f"{'='*55}")
    log.info(f"STEP: {name} ({script})")
    log.info(f"{'='*55}")
    extra = ["--dry-run"] if dry_run and name in DRY_RUN_STEPS else []
    result = subprocess.run([sys.executable, script] + args + extra, env=_UTF8_ENV)
    if result.returncode != 0:
        log.error(f"Step '{name}' failed (exit {result.returncode}). Pipeline stopped.")
        sys.exit(result.returncode)
    log.info(f"Step '{name}' — OK\n")


def main():
    parser = argparse.ArgumentParser(description="Formula Distributors Pipeline")
    parser.add_argument("--skip", nargs="*", default=[], metavar="STEP",
                        help=f"Steps to skip. Choices: {STEP_NAMES}")
    parser.add_argument("--only", default=None, metavar="STEP",
                        help="Run only this step")
    parser.add_argument("--from", dest="from_step", default=None, metavar="STEP",
                        help="Run from this step onwards")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pass --dry-run to create steps (no Shopify writes)")
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
        idx = STEP_NAMES.index(args.from_step)
        steps_to_run = STEPS[idx:]
    elif args.skip:
        unknown = [s for s in args.skip if s not in STEP_NAMES]
        if unknown:
            log.error(f"Unknown steps to skip: {unknown}. Valid: {STEP_NAMES}")
            sys.exit(1)
        steps_to_run = [(n, s, a) for n, s, a in STEPS if n not in args.skip]

    log.info(f"Pipeline start — {len(steps_to_run)} step(s): {[n for n, _, _ in steps_to_run]}")

    for name, script, extra_args in steps_to_run:
        run_step(name, script, extra_args, dry_run=args.dry_run)

    log.info(f"{'='*55}")
    log.info("Pipeline complete.")
    log.info(f"{'='*55}")


if __name__ == "__main__":
    main()
