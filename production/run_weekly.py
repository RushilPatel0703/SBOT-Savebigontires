#!/usr/bin/env python3
"""Weekly production run — Save Big On Tires.

GraphQL-only pipeline: supplier CSVs → warehouse → validated diff sync to
Shopify (GraphQL Admin API). Designed for unattended cron on the Oracle VM;
every run and every change is recorded in the warehouse.

The warehouse is Hasura/Neon in the cloud and an embedded DuckDB file when run
locally (SBOT_WAREHOUSE, default 'local' — see core/warehouse.py). Every step
below is identical either way, which is what makes the dashboard's Sync button
a rehearsal of the cron run rather than a lookalike of it.

Steps: [scrape] → catalog → ingest → merge → validate → create_products
       → sync_prices → sync_inventory

Usage (from production/):
    python run_weekly.py --dry-run              # full pass, preview writes only
    python run_weekly.py                        # live run
    python run_weekly.py --type tires           # tires only
    python run_weekly.py --skip sync_prices     # skip a step
    python run_weekly.py --only merge validate  # run specific steps (reuses --run-id data)
    python run_weekly.py --force                # proceed despite failed validation gates
    python run_weekly.py --with-scrape          # run the supplier scrape pipelines first
"""

import argparse
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config
from core.run_context import RunContext, make_logger
from core.shopify_gql import ShopifyGQL
from steps import catalog, create_products, ingest, merge, sync, validate

STEPS = ["catalog", "ingest", "merge", "validate", "create_products",
         "sync_prices", "sync_inventory"]


def parse_args():
    p = argparse.ArgumentParser(description="Save Big On Tires — weekly production sync")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview all changes; nothing is written to Shopify")
    p.add_argument("--type", nargs="+", choices=list(config.PRODUCT_TYPES),
                   default=list(config.PRODUCT_TYPES))
    p.add_argument("--suppliers", nargs="+", choices=list(config.SUPPLIERS),
                   default=list(config.SUPPLIERS))
    p.add_argument("--skip", nargs="+", choices=STEPS, default=[])
    p.add_argument("--only", nargs="+", choices=STEPS)
    p.add_argument("--from", dest="from_step", choices=STEPS)
    p.add_argument("--force", action="store_true",
                   help="Continue to sync even if validation gates fail (use with care)")
    p.add_argument("--with-scrape", action="store_true",
                   help="Run existing supplier scrape/clean pipelines (pipelines/run_all.py) first")
    return p.parse_args()


def selected(args):
    steps = list(STEPS)
    if args.only:
        steps = [s for s in STEPS if s in args.only]
    if args.from_step:
        steps = steps[steps.index(args.from_step):]
    return [s for s in steps if s not in args.skip]


def run_scrape(log):
    """Delegate to the existing supplier pipelines (scrape + clean + augment)."""
    cmd = [sys.executable, "run_all.py"]
    log.info(f"Running supplier pipelines: {' '.join(cmd)} (cwd={config.PIPELINES_DIR})")
    result = subprocess.run(cmd, cwd=str(config.PIPELINES_DIR))
    if result.returncode != 0:
        raise RuntimeError(f"Supplier scrape pipelines failed (exit {result.returncode})")


def main():
    args = parse_args()
    steps = selected(args)
    log = make_logger("weekly")
    log.info(f"Steps: {steps} | types={args.type} | dry_run={args.dry_run}")

    if args.with_scrape:
        run_scrape(log)

    ctx = RunContext(mode="dry_run" if args.dry_run else "live",
                     product_types=args.type, logger=log)
    shopify = ShopifyGQL(logger=log)
    ingest_meta = []
    status, notes = "success", None

    try:
        if "catalog" in steps:
            catalog.run(ctx, shopify)
        if "ingest" in steps:
            ingest_meta = ingest.run(ctx, suppliers=args.suppliers)
        if "merge" in steps:
            merge.run(ctx)
        if "validate" in steps:
            try:
                validate.run(ctx, ingest_meta)
            except RuntimeError as e:
                if not args.force:
                    raise
                log.warning(f"--force: continuing despite validation failure: {e}")
                notes = "forced past validation failure"
        if "create_products" in steps:
            create_products.run(ctx, shopify, args.dry_run)
        if "sync_prices" in steps:
            sync.run_prices(ctx, shopify, args.dry_run)
        if "sync_inventory" in steps:
            sync.run_inventory(ctx, shopify, args.dry_run)
    except Exception as e:
        status = "failed"
        notes = str(e)[:2000]
        log.error(f"RUN FAILED: {e}")
        log.debug(traceback.format_exc())
    finally:
        ctx.finalize(status, notes)

    sys.exit(0 if status == "success" else 1)


if __name__ == "__main__":
    main()
