#!/usr/bin/env python3
"""Give store products that have no SKU the SKU their handle belongs to.

This is a one-off repair tool. It is not part of a run.

WHAT THIS IS FOR
----------------
In October 2026 the dev store held 1,187 products typed 'Wheels'. 1,184 of
them had a blank SKU on their only variant; they came in through the CSV
imports of 2026-09-01/02. The sync matches store products to supplier rows by
SKU, so none of those wheels has ever been priced or restocked. All of them
are ACTIVE at quantity 0. Every run, create_products also saw their SKUs as
new and tried to create them again. Shopify refused each attempt because the
handle was taken, and in run #11 those refusals tripped the circuit breaker.
create_products now skips a taken handle, so a run no longer fails on them.
It doesn't repair them, though. This script does.

HOW IT MATCHES
--------------
It matches by handle. Each row in a Shopify_SEO_* file has a handle and the
SKU that create_products would create under it. The lookup is
load_seo_definitions, the same one create_products uses, so the SKU written
here is exactly the one the sync joins on. A product gets a SKU only when all
of these hold:

  * it has exactly one variant, and that variant's SKU is blank
  * its handle belongs to exactly one SKU in the SEO files for --types
  * its title matches that row's title (case and spacing aside)
  * no other variant in the store already has that SKU

Anything else is listed with the reason, never guessed at.

SAFETY
------
  * dry-run is the default; --apply is required to write anything
  * writing to a live store is refused unless --allow-live is passed as
    well. A dry run against live is allowed, because it only reads.
  * the store is read fresh with a bulk export, not from the catalog mirror,
    which never holds a product without a SKU
  * it only fills a blank SKU, never overwrites one
  * every run writes logs/sku_backfill_<mode>_<timestamp>.csv with one row
    per SKU-less product: the plan, and on --apply the outcome
  * after writing, each variant is read back from Shopify and its SKU checked

To undo it, set the SKU back to blank. The CSV lists every variant id.

Usage (from production/):
    python backfill_skus.py                  # dry run: plan + CSV
    python backfill_skus.py --apply
    python backfill_skus.py --types wheels tires
"""

import argparse
import csv
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config
from core.shopify_gql import ShopifyGQL, ShopifyError
from steps.create_products import load_seo_definitions

CSV_FIELDS = ["decision", "reason", "detail", "sku", "handle", "title",
              "product_type", "product_id", "variant_id", "result"]

WRITE_CHUNK = 250                  # progress is printed after each chunk

VERIFY_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) { ... on ProductVariant { id sku } }
}"""


def _same_title(a, b):
    def norm(t):
        return re.sub(r"\s+", " ", t or "").strip().lower()
    return norm(a) == norm(b)


def seo_by_handle(ctx, product_types):
    """(handle → (sku, SEO row), ambiguous handles) over the latest SEO files."""
    by_handle, ambiguous = {}, set()
    for product_type in product_types:
        defs, _files = load_seo_definitions(ctx, product_type)
        for sku, row in defs.items():
            handle = (row.get("Handle") or "").strip()
            if not handle:
                continue
            if handle in by_handle and by_handle[handle][0] != sku:
                ambiguous.add(handle)
            by_handle.setdefault(handle, (sku, row))
    return by_handle, ambiguous


def plan(products, variants, by_handle, ambiguous=frozenset()):
    """One row per store product with no SKU on any variant. decision is
    'set_sku' when every condition in the module docstring holds, else 'skip'
    with the reason."""
    by_product = defaultdict(list)
    sku_owner = {}                     # SKU → product gid, over the whole store
    for v in variants:
        by_product[v.get("__parentId")].append(v)
        sku = (v.get("sku") or "").strip()
        if sku:
            sku_owner.setdefault(sku, v.get("__parentId"))

    rows, planned = [], {}
    for p in products:
        vs = by_product.get(p["id"], [])
        if any((v.get("sku") or "").strip() for v in vs):
            continue
        handle = p.get("handle") or ""
        hit = None if handle in ambiguous else by_handle.get(handle)
        sku = hit[0] if hit else ""
        reason = detail = ""
        if len(vs) != 1:
            reason = f"{len(vs)} variants"
        elif handle in ambiguous:
            reason = "handle belongs to several SKUs in the SEO files"
        elif not hit:
            reason = "handle not in the SEO files"
        elif not _same_title(p.get("title"), hit[1].get("Title")):
            reason, detail = "title differs from the SEO file", hit[1].get("Title")
        elif sku in sku_owner:
            reason, detail = "SKU already on another store product", sku_owner[sku]
        elif sku in planned:
            reason, detail = "SKU planned for another product too", planned[sku]
        else:
            planned[sku] = p["id"]
        rows.append({
            "decision": "skip" if reason else "set_sku",
            "reason": reason, "detail": detail, "sku": sku,
            "handle": handle, "title": p.get("title"),
            "product_type": p.get("productType"), "product_id": p["id"],
            "variant_id": vs[0]["id"] if len(vs) == 1 else "",
            "result": "",
        })
    return rows


def apply(client, todo):
    """Write each planned SKU, then read it back. Fills in row["result"]."""
    for start in range(0, len(todo), WRITE_CHUNK):
        chunk = todo[start:start + WRITE_CHUNK]
        results = client.update_variants_batched(
            [(r["variant_id"], r["product_id"],
              {"id": r["variant_id"], "inventoryItem": {"sku": r["sku"]}}) for r in chunk],
            label="backfill_skus")
        outcome = {variant_id: (ok, err) for variant_id, ok, err in results}
        for r in chunk:
            ok, err = outcome[r["variant_id"]]
            r["result"] = "written" if ok else f"FAILED: {err}"
        print(f"  ...{min(start + WRITE_CHUNK, len(todo))}/{len(todo)} written")

    written = [r for r in todo if r["result"] == "written"]
    for start in range(0, len(written), WRITE_CHUNK):
        chunk = written[start:start + WRITE_CHUNK]
        data = client.execute(VERIFY_QUERY, {"ids": [r["variant_id"] for r in chunk]},
                              label="backfill_skus_verify")
        now = {n["id"]: (n.get("sku") or "").strip() for n in data["nodes"] if n}
        for r in chunk:
            got = now.get(r["variant_id"])
            r["result"] = ("verified" if got == r["sku"]
                           else f"UNVERIFIED: reads back as {got!r}")


def write_csv(rows, mode):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = config.LOGS_DIR / f"sku_backfill_{mode}_{stamp}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return path


def main():
    ap = argparse.ArgumentParser(
        description="Give SKU-less store products the SKU their handle belongs to.")
    ap.add_argument("--types", nargs="+", default=["wheels"],
                    choices=list(config.PRODUCT_TYPES),
                    help="whose SEO files to match handles against (default: wheels)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write the SKUs (default is a dry run)")
    ap.add_argument("--allow-live", action="store_true",
                    help="permit --apply against a non-dev store")
    args = ap.parse_args()

    # Titles carry non-breaking spaces and emoji, and a cp1252 console would
    # otherwise crash halfway through the listing.
    sys.stdout.reconfigure(errors="replace")
    logging.basicConfig(level=logging.INFO, format="  %(message)s", stream=sys.stdout)

    kind = "DEV" if config.is_dev_store() else "LIVE"
    print(f"Store: {config.SHOPIFY_SHOP}  ({kind})")
    if args.apply and not config.is_dev_store() and not args.allow_live:
        print(f"\nREFUSING: {config.SHOPIFY_SHOP} is not a known dev store and "
              f"--allow-live was not given.")
        return 1

    class _Ctx:
        log = logging.getLogger("backfill_skus")

    print(f"\nSEO definitions ({', '.join(args.types)}):")
    by_handle, ambiguous = seo_by_handle(_Ctx, args.types)
    if not by_handle:
        print("No SEO definitions found; nothing to match against.")
        return 1

    client = ShopifyGQL()
    print("\nExporting the store catalog (a bulk operation, about a minute)...")
    try:
        products, variants = client.fetch_catalog()
    except ShopifyError as e:
        print(f"Catalog export failed: {e}")
        return 1
    print(f"  {len(products)} products, {len(variants)} variants")

    rows = plan(products, variants, by_handle, ambiguous)
    todo = [r for r in rows if r["decision"] == "set_sku"]
    print(f"\nProducts with no SKU: {len(rows)}")
    print(f"  {len(todo):>5}  get a SKU")
    for reason, n in Counter(r["reason"] for r in rows if r["reason"]).most_common():
        types = Counter(r["product_type"] or "(blank)" for r in rows if r["reason"] == reason)
        print(f"  {n:>5}  skipped: {reason}  "
              f"[{', '.join(f'{t} {c}' for t, c in types.most_common())}]")
    for r in todo[:10]:
        print(f"    SET  {r['sku']:<22} {r['handle']}")
    if len(todo) > 10:
        print(f"    ... and {len(todo) - 10} more (all in the CSV)")

    if not args.apply:
        path = write_csv(rows, "dryrun")
        print(f"\nDRY RUN: nothing written. Plan saved to {path}")
        print(f"Re-run with --apply to set {len(todo)} SKU(s).")
        return 0

    if todo:
        print(f"\nWriting {len(todo)} SKU(s)...")
        try:
            apply(client, todo)
        except ShopifyError as e:
            print(f"Stopped: {e}")
    path = write_csv(rows, "apply")
    outcomes = Counter(r["result"].split(":")[0] for r in todo)
    print(f"\n{outcomes['verified']} SKU(s) set and verified, "
          f"{outcomes['FAILED']} failed, {outcomes['UNVERIFIED']} unverified, "
          f"{outcomes['written'] + outcomes['']} not checked.")
    print(f"Record written to {path}")
    if outcomes["verified"]:
        print("\nThe next run's catalog step maps these products, and from then on "
              "the sync prices and restocks them.")
    return 0 if outcomes["verified"] == len(todo) else 1


if __name__ == "__main__":
    sys.exit(main())
