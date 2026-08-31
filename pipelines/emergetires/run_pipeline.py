#!/usr/bin/env python3
"""Emerge Tires Pipeline Orchestrator

Steps (in order):
  1. scraper       — scrape raw tire data from Emerge Tires Costar portal
  2. clean_tires   — clean/deduplicate tire raw data (qty >= 4 filter)
  3. enrich        — shared enrichment layer (pipelines/shared/enrichment.py
                      --apply --supplier emergetires): applies already-cached
                      (already-paid-for) image fixes to the cleaned CSV before
                      augment reads it. Zero network calls, so it runs by
                      default like every other step.
  4. augment_tires — build Shopify_SEO_emergetires_tires_<ts>.csv
  5. qa_check      — internal QA gate (exit 1 on FAIL, blocks sign-off)

Note: price/inventory sync is handled by production/run_weekly.py.
      scraper_wheels / augment_wheels added here only if OQ-1 confirms Emerge sells wheels.

See monitoring/ENRICHMENT_APPROVAL.md and monitoring/SERP_LAYER_DESIGN.md for the
enrichment layer's design and current free-vs-paid numbers.

Usage:
  python run_pipeline.py                              # full run, enrich included
  python run_pipeline.py --skip scraper               # skip scraping, use existing raw data
  python run_pipeline.py --skip enrich                # skip the free cache-apply step
  python run_pipeline.py --only augment_tires         # run one step only
  python run_pipeline.py --from clean_tires           # run from a step onwards

IMPORTANT — before first run, complete Phase 0 (see SCRAPER_SPEC.md):
  1. Verify/refresh COOKIE_STRING in scraper_tires.py
  2. Extract _SIGNING_KEY from Emerge's costar.min.js
  3. Confirm SPEC_CATEGORY_GUIDS from a live Product/Search payload
  4. Probe a broad term to confirm SEARCH_CAP and TOTAL_COUNT_KEY
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
    ("scraper",       "scraper_tires.py",  []),
    ("clean_tires",   "clean.py",          ["--type", "tires"]),
    ("enrich",        "../shared/enrichment.py", ["--apply", "--supplier", "emergetires"]),
    ("augment_tires", "augment_tires.py",  []),
    ("qa_check",      "qa_check.py",       []),
    ("create_tires",  "create_products.py", []),
    # ("scraper_wheels",  "scraper_wheels.py",  []),   # add when OQ-1 resolved
    # ("augment_wheels",  "augment_wheels.py",  []),   # add when OQ-1 resolved
]

DRY_RUN_STEPS = {"create_tires"}

STEP_NAMES = [n for n, _, _ in STEPS]


def run_step(name: str, script: str, args: list, dry_run: bool = False):
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
    parser = argparse.ArgumentParser(description="Emerge Tires Pipeline")
    parser.add_argument("--skip", nargs="*", default=[], metavar="STEP",
                        help=f"Steps to skip. Choices: {STEP_NAMES}")
    parser.add_argument("--only", default=None, metavar="STEP",
                        help="Run only this step")
    parser.add_argument("--from", dest="from_step", default=None, metavar="STEP",
                        help="Run from this step onwards")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pass --dry-run to create steps (no Shopify writes)")
    parser.add_argument("--enable-enrich", action="store_true",
                        help="No longer needed — the 'enrich' step runs by default (free "
                             "cache-apply, zero network calls). Kept as a no-op so old commands "
                             "don't break. Use --skip enrich to omit it.")
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
