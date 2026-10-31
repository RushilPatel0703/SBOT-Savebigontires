#!/usr/bin/env python3
"""Delete named products from the store. One-off cleanup tool, not part of a run.

WHAT THIS IS FOR
----------------
Products that are live in Shopify but that the current filters would never
create. The September 2026 storefront image review turned up 59 of them: 52
medium/heavy commercial and ST trailer fitments (19.5"/22.5"/17.5" rims —
steer, drive and trailer positions) plus 7 passenger-size products the client
struck off by name. All of them predate the rules that now exclude them —
size_parser.is_consumer_tire_size() for the commercial ones and
pipelines/shared/Data/product_denylist.json for the rest.

Those rules stop such products being *created*. Nothing retires one already in
the store, because the sync's zero-out pass is scoped to
config.SHOPIFY_PRODUCT_TYPE["tires"] == "Vehicle Tires" and the store holds
only 'Tires' and '' — so it matches an empty set and reports PASS (spec §5.1).
Until that is fixed, anything that slipped through before a rule existed stays
live at its last-known quantity, and this script is how it comes out.

SAFETY
------
A delete is irreversible: the handle, its SEO history and any order association
go with the product. So:

  * dry-run is the default; --apply is required to write anything
  * a live store is refused unless --allow-live is passed as well
  * every SKU is re-resolved against Shopify at delete time rather than trusted
    from the catalog mirror (see ShopifyGQL.lookup_products_by_sku)
  * SKUs that resolve to nothing are reported, never guessed at
  * what was deleted is written to logs/pruned_<timestamp>.csv, so the record
    outlives the products

Usage (from production/):
    python prune_products.py --skus-file ../monitoring/prune_skus.txt
    python prune_products.py --skus-file ../monitoring/prune_skus.txt --apply
"""

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config
from core.shopify_gql import ShopifyGQL, ShopifyError


def read_skus(path):
    """One SKU per line. Blank lines and '#' comments ignored."""
    skus, seen = [], set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        sku = line.split("#")[0].strip()
        if sku and sku not in seen:
            seen.add(sku)
            skus.append(sku)
    return skus


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skus-file", required=True,
                    help="file of SKUs to delete, one per line")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is a dry run)")
    ap.add_argument("--allow-live", action="store_true",
                    help="permit running against a non-dev store")
    args = ap.parse_args()

    skus = read_skus(args.skus_file)
    if not skus:
        print(f"No SKUs in {args.skus_file}.")
        return 1

    kind = "DEV" if config.is_dev_store() else "LIVE"
    print(f"Store: {config.SHOPIFY_SHOP}  ({kind})")
    print(f"SKUs requested: {len(skus)}")
    if not config.is_dev_store() and not args.allow_live:
        print(f"\nREFUSING: {config.SHOPIFY_SHOP} is not a known dev store and "
              f"--allow-live was not given. Deleting products is irreversible.")
        return 1

    client = ShopifyGQL()
    print("\nResolving SKUs against Shopify...")
    try:
        found = client.lookup_products_by_sku(skus)
    except ShopifyError as e:
        print(f"Lookup failed: {e}")
        return 1

    missing = [s for s in skus if s not in found]
    print(f"  {len(found)} resolved, {len(missing)} not found in the store\n")

    for sku in skus:
        if sku in found:
            f = found[sku]
            print(f"  DELETE  {sku:<18} {f['status']:<8} {f['title'][:60]}")
    if missing:
        print("\nNot found (nothing to delete):")
        for sku in missing:
            print(f"  {sku}")

    if not args.apply:
        print(f"\nDRY RUN — nothing deleted. Re-run with --apply to delete "
              f"{len(found)} product(s).")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = config.LOGS_DIR / f"pruned_{stamp}.csv"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    deleted, failed = 0, []
    with open(log_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sku", "product_id", "title", "status", "result"])
        for sku in skus:
            if sku not in found:
                continue
            f = found[sku]
            try:
                client.delete_product(f["product_id"])
            except ShopifyError as e:
                failed.append((sku, str(e)[:200]))
                w.writerow([sku, f["product_id"], f["title"], f["status"], f"FAILED: {e}"])
                print(f"  FAILED  {sku}: {e}")
                continue
            deleted += 1
            w.writerow([sku, f["product_id"], f["title"], f["status"], "deleted"])
            if deleted % 10 == 0:
                print(f"  ...{deleted}/{len(found)} deleted")

    print(f"\nDeleted {deleted} product(s), {len(failed)} failed.")
    print(f"Record written to {log_path}")
    if deleted:
        print("\nThe catalog mirror still lists them until the next sync refreshes it "
              "(production/run_weekly.py, or the catalog step on its own).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
