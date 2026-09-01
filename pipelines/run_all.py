#!/usr/bin/env python3
"""
Master Orchestrator — Save Big On Tires Inventory Pipeline

Runs all 5 supplier pipelines sequentially. Shopify sync is handled separately
by production/run_weekly.py.
Each supplier pipeline runs from its own directory (scripts use relative paths).

Run from: pipelines/
Usage:
    python run_all.py                          # full run — all suppliers
    python run_all.py --skip-scraping         # skip scraping on all suppliers (reuse existing CSVs)
    python run_all.py --suppliers formula nc  # run specific suppliers only
"""

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent   # pipelines/
LOGS = HERE / "logs"
LOGS.mkdir(exist_ok=True)

ts = datetime.now().strftime("%Y%m%d_%H%M%S")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOGS / f"run_all_{ts}.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("run_all")

_UTF8_ENV = {**os.environ, "PYTHONUTF8": "1"}

SUPPLIERS = {
    "formula": {
        "dir":       HERE / "formula_distributors",
        "run_args":  ["--skip", "create_tires", "create_wheels"],
        "skip_args": ["--skip", "scraper", "scraper_wheels", "create_tires", "create_wheels"],
    },
    "nc": {
        "dir":       HERE / "northcountry",
        "run_args":  ["--skip", "create_products", "update_images", "create_wheels", "update_images_wheels"],
        # skip_args also excludes brand_sweep (live API) and wheels steps (no raw CSV yet)
        "skip_args": ["--skip", "scraper_tires", "scraper_wheels", "brand_sweep",
                      "clean_wheels", "augment_wheels",
                      "create_products", "update_images", "create_wheels", "update_images_wheels"],
    },
    "wheelsmart": {
        "dir":       HERE / "wheelsmart",
        "run_args":  ["--skip", "create_tires", "create_rims"],
        "skip_args": ["--skip", "scraper_tires", "scraper_rims", "create_tires", "create_rims"],
    },
    "emerge": {
        "dir":       HERE / "emergetires",
        "run_args":  ["--skip", "create_tires"],
        "skip_args": ["--skip", "scraper", "create_tires"],
    },
    "dttire": {
        "dir":       HERE / "dttire",
        "run_args":  ["--skip", "create_tires", "create_wheels"],
        "skip_args": ["--skip", "scraper_tires", "scraper_wheels", "create_tires", "create_wheels"],
    },
}


def run_step(label: str, cmd: list, cwd: Path) -> bool:
    log.info("=" * 60)
    log.info(f"START: {label}")
    log.info("=" * 60)
    result = subprocess.run(cmd, cwd=str(cwd), env=_UTF8_ENV)
    if result.returncode == 0:
        log.info(f"OK: {label}\n")
        return True
    else:
        log.error(f"FAILED: {label} (exit {result.returncode})\n")
        return False


def main():
    parser = argparse.ArgumentParser(description="Save Big On Tires — Full Pipeline")
    parser.add_argument("--skip-scraping", action="store_true",
                        help="Skip scraping on all suppliers — reuse most recent raw CSVs")
    parser.add_argument("--suppliers",     nargs="+", choices=list(SUPPLIERS),
                        default=list(SUPPLIERS),
                        help="Suppliers to run (default: all). Choices: formula nc wheelsmart emerge dttire")
    args = parser.parse_args()

    log.info(f"Pipeline run started — suppliers: {args.suppliers}")
    if args.skip_scraping:
        log.info("Mode: SKIP SCRAPING — using existing raw CSVs")

    results = {}

    # ── Supplier pipelines ─────────────────────────────────────────────────────
    for key in args.suppliers:
        cfg = SUPPLIERS[key]
        extra = cfg["skip_args"] if args.skip_scraping else cfg["run_args"]
        cmd   = [sys.executable, "run_pipeline.py"] + extra
        results[key] = run_step(f"Supplier: {key}", cmd, cfg["dir"])

    # ── Summary ────────────────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)
    for key, ok in results.items():
        log.info(f"  {'OK    ' if ok else 'FAILED'} — {key}")

    failed = [k for k, ok in results.items() if not ok]
    if failed:
        log.error(f"{len(failed)} step(s) failed: {failed}")
        sys.exit(1)
    else:
        log.info("All steps completed successfully.")


if __name__ == "__main__":
    main()
