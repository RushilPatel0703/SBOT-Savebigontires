#!/usr/bin/env python3
"""The set of SKUs currently in the Shopify store.

Feeds the "new products only" upload bundle: pipelines/shared/upload_bundle.py
subtracts this set from the supplier CSVs, so an import creates the products
Shopify does not have instead of re-uploading the whole catalogue to change
nothing.

Read from the shopify_products mirror, which the catalog step refreshes at the
start of every sync — so the answer is only as current as the last run, and this
prints the mirror's age for exactly that reason. `--refresh` re-exports from
Shopify first (a bulk operation; a few minutes on a 10k-product store).

Usage (from production/):
    python store_skus.py                      # summary + age
    python store_skus.py --json               # {count, refreshed_at, age_hours, skus:[...]}
    python store_skus.py --out skus.json      # write the JSON to a file
    python store_skus.py --refresh --out skus.json
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config


def store_skus(warehouse=None):
    """(skus, refreshed_at) — SKUs in the mirror and when it was last refreshed."""
    from core.warehouse import get_warehouse
    wh = warehouse or get_warehouse()
    rows = wh.select("select sku from shopify_products order by sku;")
    meta = wh.select_one("select max(refreshed_at) as refreshed_at from shopify_products;")
    return [r["sku"] for r in rows], (meta or {}).get("refreshed_at")


def refresh_mirror():
    """Re-export the live catalog into the mirror, without running a sync.

    Reuses the real catalog step and a real RunContext so the mirror is written
    exactly as a sync writes it — a second implementation of the mirror refresh
    is the kind of near-duplicate that drifts.
    """
    from core.run_context import RunContext, make_logger
    from core.shopify_gql import ShopifyGQL
    from steps import catalog

    log = make_logger("store_skus")
    ctx = RunContext(mode="dry_run", product_types=list(config.PRODUCT_TYPES), logger=log)
    status = "success"
    try:
        catalog.run(ctx, ShopifyGQL(logger=log))
    except Exception as e:                              # noqa: BLE001
        status = "failed"
        raise
    finally:
        # mode is dry_run so this run can never be mistaken for one that wrote
        # to the store, and validate's qty-delta baseline ignores it.
        ctx.finalize(status, "catalog refresh only (store_skus.py --refresh)")


def _age_hours(refreshed_at):
    if not refreshed_at:
        return None
    if isinstance(refreshed_at, str):
        try:
            refreshed_at = datetime.fromisoformat(refreshed_at)
        except ValueError:
            return None
    return round((datetime.now() - refreshed_at).total_seconds() / 3600, 1)


def main():
    ap = argparse.ArgumentParser(description="SKUs currently in the Shopify store")
    ap.add_argument("--json", action="store_true", help="Print the full JSON payload")
    ap.add_argument("--out", metavar="FILE", help="Write the JSON payload to FILE")
    ap.add_argument("--refresh", action="store_true",
                    help="Re-export the catalog from Shopify before reading")
    args = ap.parse_args()

    if args.refresh:
        refresh_mirror()

    skus, refreshed_at = store_skus()
    payload = {
        "count": len(skus),
        "refreshed_at": str(refreshed_at) if refreshed_at else None,
        "age_hours": _age_hours(refreshed_at),
        "shop": config.SHOPIFY_SHOP,
        "skus": skus,
    }

    if args.out:
        Path(args.out).write_text(json.dumps(payload), encoding="utf-8")

    if args.json:
        # --out already holds the SKUs; keep stdout small when both are given.
        print(json.dumps({**payload, "skus": []} if args.out else payload))
        return 0

    if not skus:
        print("The catalog mirror is empty — run a sync (or --refresh) first.")
        return 1
    age = payload["age_hours"]
    print(f"{len(skus):,} SKUs in {config.SHOPIFY_SHOP}")
    print(f"mirror last refreshed: {payload['refreshed_at']}"
          + (f" ({age} h ago)" if age is not None else ""))
    if age is not None and age > 24:
        print("WARNING: the mirror is over a day old. Products created since then "
              "would be treated as new again — re-run with --refresh.")
    if args.out:
        print(f"written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
