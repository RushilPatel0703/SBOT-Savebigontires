#!/usr/bin/env python3
"""North Country Tire Pipeline Orchestrator

Tire steps:
  1. scraper_tires    — scrape raw tire data → Data/raw/
  2. brand_sweep      — gap-fill via brand axis
  3. clean_tires      — clean/filter (qty >= 4) → Data/cleaned/
  4. enrich_tires     — shared enrichment layer, applies cached image fixes
                         (pipelines/shared/enrichment.py) — no network calls
  5. augment_tires    — build Shopify SEO CSV → Data/shopify/
  6. create_products  — create new tire SKUs on Shopify
  7. update_images    — push tire image URLs to existing Shopify products

Wheel steps:
  8. scraper_wheels        — scrape raw wheel data → Data/raw/
  9. clean_wheels          — clean/filter → Data/cleaned/
  10. enrich_wheels        — shared enrichment layer for wheel images
  11. augment_wheels       — build Shopify SEO CSV → Data/shopify/
  12. create_wheels        — create new wheel SKUs on Shopify
  13. update_images_wheels — push wheel image URLs to existing Shopify products

Note: build_sku_map and price/inventory sync are in pipelines/merged/run_sync.py.
      Run run_sync.py after this pipeline completes.

enrich_tires/enrich_wheels both invoke the SAME shared script
(pipelines/shared/enrichment.py --apply --supplier northcountry) — running it
twice is intentional and safe: each invocation only touches whichever cleaned
CSV exists yet (tires exists first; wheels doesn't exist until clean_wheels
runs later in this same list), and --apply only ever writes already-cached
(already-paid-for) results — zero network calls. See
monitoring/ENRICHMENT_APPROVAL.md and monitoring/SERP_LAYER_DESIGN.md.

Usage:
  python run_pipeline.py                              # full run (tires + wheels)
  python run_pipeline.py --skip scraper_tires         # skip scraping, use existing raw data
  python run_pipeline.py --only augment_wheels        # just one step
  python run_pipeline.py --from clean_wheels          # resume from a step
  python run_pipeline.py --dry-run                    # preview create steps only
  python run_pipeline.py --enable-enrich              # include the enrich_tires/enrich_wheels steps
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

_ENRICH_ARGS = ["--apply", "--supplier", "northcountry"]

STEPS = [
    ("scraper_tires",        "scraper_tires.py",    []),
    ("brand_sweep",          "brand_sweep.py",      ["--merge"]),  # gap-fill via brand axis
    ("clean_tires",          "clean.py",            ["--type", "tires"]),
    ("enrich_tires",         "../shared/enrichment.py", _ENRICH_ARGS),
    ("augment_tires",        "augment_tires.py",    []),
    ("create_products",      "create_products.py",  ["--type", "tires"]),
    ("update_images",        "update_images.py",    ["--type", "tires"]),
    ("scraper_wheels",       "scraper_wheels.py",   []),
    ("clean_wheels",         "clean.py",            ["--type", "wheels"]),
    ("enrich_wheels",        "../shared/enrichment.py", _ENRICH_ARGS),
    ("augment_wheels",       "augment_wheels.py",   []),
    ("create_wheels",        "create_products.py",  ["--type", "wheels"]),
    ("update_images_wheels", "update_images.py",    ["--type", "wheels"]),
]

DRY_RUN_STEPS = {"create_products", "update_images", "create_wheels", "update_images_wheels"}
FULL_STEPS    = {"scraper_tires", "scraper_wheels"}  # steps that accept --full (rebuild zero-terms cache)

# enrich_* only ever runs with --apply (cache-hit fixes, zero network calls —
# see pipelines/shared/enrichment.py). Still excluded from default runs and
# opt-in only, since it rewrites cleaned CSVs in place.
ENRICH_OPT_IN_STEPS = {"enrich_tires", "enrich_wheels"}

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
    parser.add_argument("--enable-enrich", action="store_true",
                        help="Include enrich_tires/enrich_wheels in a full/--skip run. They only "
                             "apply already-cached (already-paid-for) designation/image fixes — "
                             "zero network calls — but still rewrite cleaned CSVs in place, so "
                             "they're opt-in like the other steps that mutate data.")
    args = parser.parse_args()

    steps_to_run = STEPS
    explicit_enrich = args.only in ENRICH_OPT_IN_STEPS or args.from_step in ENRICH_OPT_IN_STEPS

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

    if not explicit_enrich and not args.enable_enrich:
        before = len(steps_to_run)
        steps_to_run = [(n, s, a) for n, s, a in steps_to_run if n not in ENRICH_OPT_IN_STEPS]
        if len(steps_to_run) < before:
            log.warning(
                "Skipping enrich_tires/enrich_wheels — they rewrite cleaned CSVs in place. "
                "Pass --enable-enrich to include them, or run one explicitly with --only."
            )

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
