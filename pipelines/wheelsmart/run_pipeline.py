#!/usr/bin/env python3
"""WheelMart Canada Pipeline Orchestrator

Steps (in order):
  tires: scraper_tires → enrich_tires → augment_tires
  rims:  scraper_rims  → enrich_rims  → augment_rims

Note: WheelMart has no clean.py step — scraper_tires.py fetches from the API directly
and outputs a raw CSV that augment_tires.py reads and filters (qty >= 4 applied there).
enrich_tires/enrich_rims (pipelines/shared/enrichment.py --apply --supplier wheelsmart)
therefore also operate on the raw CSV directly — same file augment*.py reads. They only
apply already-cached (already-paid-for) image fixes; zero network calls, so they run by
default like every other step. See monitoring/ENRICHMENT_APPROVAL.md and
monitoring/SERP_LAYER_DESIGN.md.

Usage:
  python run_pipeline.py                          # full run (tires + rims), enrich included
  python run_pipeline.py --skip scraper_tires     # use existing raw tires CSV
  python run_pipeline.py --skip enrich_tires enrich_rims  # skip the free cache-apply step
  python run_pipeline.py --only augment_rims      # run one step only
  python run_pipeline.py --from augment_tires     # run from step onwards

Session note: if scraper fails with 403/auth errors, run refresh_session.py to get fresh
cookies, then update _identity and PHPSESSID in scraper_tires.py / scraper_rims.py.
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

_ENRICH_ARGS = ["--apply", "--supplier", "wheelsmart"]

STEPS = [
    ("scraper_tires", "scraper_tires.py",  []),
    ("enrich_tires",  "../shared/enrichment.py", _ENRICH_ARGS),
    ("augment_tires", "augment_tires.py",  []),
    ("create_tires",  "create_products.py", ["--type", "tires"]),
    ("scraper_rims",  "scraper_rims.py",   []),
    ("enrich_rims",   "../shared/enrichment.py", _ENRICH_ARGS),
    ("augment_rims",  "augment_rims.py",   []),
    ("create_rims",   "create_products.py", ["--type", "rims"]),
]

DRY_RUN_STEPS = {"create_tires", "create_rims"}

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
    parser = argparse.ArgumentParser(description="WheelMart Canada Pipeline")
    parser.add_argument("--skip", nargs="*", default=[], metavar="STEP",
                        help=f"Steps to skip. Choices: {STEP_NAMES}")
    parser.add_argument("--only", default=None, metavar="STEP",
                        help="Run only this step")
    parser.add_argument("--from", dest="from_step", default=None, metavar="STEP",
                        help="Run from this step onwards")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pass --dry-run to create steps (no Shopify writes)")
    parser.add_argument("--enable-enrich", action="store_true",
                        help="No longer needed — enrich_tires/enrich_rims run by default "
                             "(free cache-apply, zero network calls). Kept as a no-op so old "
                             "commands don't break. Use --skip enrich_tires enrich_rims to omit them.")
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
