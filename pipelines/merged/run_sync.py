#!/usr/bin/env python3
"""Merged Sync Pipeline Orchestrator
Runs after all supplier pipelines have completed their scrape → clean → augment steps.

Steps (in order):
  1. build_sku_map    — rebuild Shopify product map (shared/build_sku_map.py)
  2. merge_tires      — merge all supplier cleaned tire CSVs
  3. merge_wheels     — merge all supplier cleaned wheel CSVs
  4. update_prices    — push merged tire prices to Shopify
  5. update_inv_tires — push merged tire inventory to Shopify
  6. update_inv_wheels— push merged wheel inventory to Shopify

Usage:
  python run_sync.py                           # full sync (tires + wheels)
  python run_sync.py --skip merge_wheels update_inv_wheels   # tires only
  python run_sync.py --only build_sku_map
  python run_sync.py --from merge_tires
  python run_sync.py --dry-run                 # preview inventory changes only
"""

import sys
import subprocess
import argparse
import logging
import os
from datetime import datetime

_HERE   = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.normpath(os.path.join(_HERE, "..", "shared"))

os.makedirs(os.path.join(_HERE, "logs"), exist_ok=True)
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(os.path.join(_HERE, "logs", f"sync_{ts}.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("run_sync")

_UTF8_ENV = {**os.environ, "PYTHONUTF8": "1"}

# (step_name, script_path, extra_args, cwd)
STEPS = [
    ("build_sku_map",     os.path.join(_SHARED, "build_sku_map.py"), [],                           _SHARED),
    ("merge_tires",       os.path.join(_HERE,   "merge_suppliers.py"), ["--type", "tires"],         _HERE),
    ("merge_wheels",      os.path.join(_HERE,   "merge_suppliers.py"), ["--type", "wheels"],        _HERE),
    ("update_prices_tires",  os.path.join(_HERE, "update_prices.py"),  ["--type", "tires"],         _HERE),
    ("update_prices_wheels", os.path.join(_HERE, "update_prices.py"),  ["--type", "wheels"],        _HERE),
    ("update_inv_tires",  os.path.join(_HERE,   "update_inventory.py"), ["--type", "tires"],        _HERE),
    ("update_inv_wheels", os.path.join(_HERE,   "update_inventory.py"), ["--type", "wheels"],       _HERE),
]

STEP_NAMES = [n for n, _, _, _ in STEPS]


def run_step(name, script, extra_args, cwd, dry_run=False):
    log.info("=" * 55)
    log.info(f"STEP: {name}")
    log.info("=" * 55)

    cmd = [sys.executable, script] + extra_args
    if dry_run and name.startswith("update_inv"):
        cmd.append("--dry-run")

    result = subprocess.run(cmd, cwd=cwd, env=_UTF8_ENV)
    if result.returncode != 0:
        log.error(f"Step '{name}' failed (exit {result.returncode}). Sync stopped.")
        sys.exit(result.returncode)
    log.info(f"Step '{name}' — OK\n")


def main():
    parser = argparse.ArgumentParser(description="Merged Sync Pipeline")
    parser.add_argument("--skip", nargs="*", default=[], metavar="STEP",
                        help=f"Steps to skip. Choices: {STEP_NAMES}")
    parser.add_argument("--only", default=None, metavar="STEP")
    parser.add_argument("--from", dest="from_step", default=None, metavar="STEP")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pass --dry-run to inventory update steps (no Shopify writes)")
    args = parser.parse_args()

    steps_to_run = STEPS

    if args.only:
        steps_to_run = [(n, s, a, c) for n, s, a, c in STEPS if n == args.only]
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
        steps_to_run = [(n, s, a, c) for n, s, a, c in STEPS if n not in args.skip]

    log.info(f"Pipeline start — {len(steps_to_run)} step(s): {[n for n, *_ in steps_to_run]}")
    if args.dry_run:
        log.info("DRY RUN: inventory steps will preview only.")

    for name, script, extra_args, cwd in steps_to_run:
        run_step(name, script, extra_args, cwd, dry_run=args.dry_run)

    log.info("=" * 55)
    log.info("Pipeline complete.")
    log.info("=" * 55)


if __name__ == "__main__":
    main()
