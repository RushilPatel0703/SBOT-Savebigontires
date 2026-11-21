#!/usr/bin/env python3
"""Supplier quantities as a Shopify inventory-import CSV, for a store with no API.

The sync (run_weekly.py) sets quantities through the Admin API. A store we hold
no API token for never gets that step, so every product imported into it sits
at Shopify's default of 0 regardless of what the suppliers have. This is the
manual stand-in: export the store's inventory (Products → Inventory → Export),
run this against it, import the result (Products → Inventory → Import).

The numbers come from the real pipeline, not a second copy of it: ingest, merge
and validate run unchanged, so the size filter, the product denylist, the Emerge
prefix, the low-qty floor and the safety gates all apply exactly as in a sync.
They run against their own DuckDB file (Data/warehouse_sbot.duckdb by default)
so this store's run history never mixes with the API store's.

Only one location gets the quantity. Shopify adds every location together, so
writing the supplier quantity to all nine showed nine times the real stock. The
merged quantity goes on SUPPLIER_LOCATION and every store is set to 0: the stores
hold no stock of their own, and the customer's store travels on the order as a
cart attribute. A store where a product is "not stocked" stays that way.

Only rows whose value changes are written; Shopify leaves the rest alone. A SKU
with no supplier row keeps its SUPPLIER_LOCATION quantity — the export carries
no product type, so it cannot tell a dropped supplier product from one the client
manages by hand (see validate's zero_out_cap note for the same gap on the API
side). Its stores still go to 0, or an earlier multiplied import would survive
on every SKU that has since left the feed.

Usage (from production/):
    python inventory_csv.py --export ../inventory_export_1.csv
    python inventory_csv.py --export ../inventory_export_1.csv --skus EMRG-VPWTS27 EMRG-VPWTS28
    python inventory_csv.py --export ../inventory_export_1.csv --force
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

import config
from core.local_warehouse import LocalWarehouse
from core.run_context import RunContext, make_logger
from steps import ingest, merge, validate

DEFAULT_WAREHOUSE = config.DATA_DIR / "warehouse_sbot.duckdb"

NOT_STOCKED = "not stocked"
SUPPLIER_LOCATION = "Supplier Ordering Stock"
ON_HAND_NEW = "On hand (new)"
REQUIRED_COLS = ("Handle", "SKU", "Location", "On hand (current)", ON_HAND_NEW,
                 "Available (not editable)", "Committed (not editable)",
                 "Unavailable (not editable)")


def _num(v):
    """Export cell → int; 'not stocked' and blanks count as 0."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def load_export(path):
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise SystemExit(f"{path} is not a Shopify inventory export — missing {missing}")
    df["_sku"] = df["SKU"].str.strip()
    df["_stocked"] = df["On hand (current)"] != NOT_STOCKED
    df["_available"] = df["Available (not editable)"].map(_num)
    # Shopify's import sets On hand; the sync sets Available. On hand =
    # available + committed + unavailable, so carry the other two across or an
    # open order would quietly eat into the supplier quantity.
    df["_held"] = (df["Committed (not editable)"].map(_num)
                   + df["Unavailable (not editable)"].map(_num))
    return df


def mirror_rows(df, run_id):
    """The export as shopify_products rows, so validate's store-side checks have
    a catalog to read. One row per SKU (first wins, as in steps/catalog.py);
    current_qty is the SKU's total available across locations, which is what
    the API's inventoryQuantity reports."""
    rows = []
    totals = df.groupby("_sku")["_available"].sum()
    for sku, g in df[df["_sku"] != ""].groupby("_sku", sort=False):
        first = g.iloc[0]
        # The export carries no Shopify IDs and the schema requires them. These
        # placeholders are never sent anywhere — this tool makes no API call.
        ph = f"csv-export:{first['Handle']}"
        rows.append({
            "sku": sku, "product_id": ph, "variant_id": ph, "inventory_item_id": ph,
            "handle": first["Handle"], "title": first.get("Title"), "product_type": None,
            "tags": None, "current_price": None, "current_qty": int(totals[sku]),
            "refreshed_at": "now()", "last_run_id": run_id,
        })
    return rows


def build_import_every_location(df, targets, only_skus=None, write_all=False):
    """The pre-2026-10-30 layout, for a store still set up that way (--every-location):
    every location row of a supplier SKU gets the merged quantity, including rows
    currently "not stocked". SKUs with no supplier row are left untouched.
    Shopify adds the locations together, so the store total reads 9x the stock."""
    out = df[df["_sku"].isin(targets.index)].copy()
    if only_skus:
        out = out[out["_sku"].isin(only_skus)]
    out["_new_on_hand"] = out["_sku"].map(targets).astype(int) + out["_held"]
    if not write_all:
        changed = (~out["_stocked"]) | (out["_new_on_hand"] != out["On hand (current)"].map(_num))
        out = out[changed]
    out[ON_HAND_NEW] = out["_new_on_hand"].astype(str)
    return out


PRICE_COLS = ["Handle", "Title", "Option1 Name", "Option1 Value", "Option2 Name",
              "Option2 Value", "Option3 Name", "Option3 Value", "Variant SKU", "Variant Price"]


def build_prices(df, prices, only_skus=None):
    """A products-import CSV that sets Variant Price, one row per variant in the
    export whose SKU has a merged price. The export carries no current price, so
    every matched variant is written; the import leaves columns it lacks alone."""
    rows = df[(df["_sku"] != "") & df["_sku"].isin(prices.index)]
    if only_skus:
        rows = rows[rows["_sku"].isin(only_skus)]
    key = ["Handle", "Option1 Value", "Option2 Value", "Option3 Value"]
    rows = rows.drop_duplicates(subset=key).copy()
    rows["Variant SKU"] = rows["_sku"]
    rows["Variant Price"] = rows["_sku"].map(prices).map(lambda p: f"{p:.2f}")
    return rows[[c for c in PRICE_COLS if c in rows.columns]]


def build_import(df, targets, only_skus=None):
    """Rows of the export whose On hand (new) changes, with it filled in.

    SUPPLIER_LOCATION gets the supplier quantity and each store gets 0 available;
    _held stays on top of either, so open orders keep their units."""
    out = df[df["_sku"].isin(only_skus)].copy() if only_skus else df.copy()
    at_supplier = out["Location"].str.strip() == SUPPLIER_LOCATION
    out = out[~at_supplier | out["_sku"].isin(targets.index)].copy()
    at_supplier = out["Location"].str.strip() == SUPPLIER_LOCATION

    supplier_qty = out["_sku"].map(targets).where(at_supplier, 0)
    out["_new_on_hand"] = supplier_qty.astype(int) + out["_held"]
    differs = out["_new_on_hand"] != out["On hand (current)"].map(_num)
    changed = (at_supplier & ~out["_stocked"]) | (out["_stocked"] & differs)
    out = out[changed]
    out[ON_HAND_NEW] = out["_new_on_hand"].astype(str)
    return out


def write_parts(out, dest, max_mb):
    """Write out to dest, split into _partN files when it exceeds max_mb.

    Shopify caps CSV imports at 15 MB; a full nine-location store is ~33 MB.
    Each part repeats the header, so every one imports on its own.
    """
    body = out.to_csv(index=False, encoding="utf-8")
    if len(body.encode("utf-8")) <= max_mb * 1e6:
        dest.write_text(body, encoding="utf-8", newline="")
        return [dest]
    header, *lines = body.splitlines(keepends=True)
    files, chunk, size = [], [], len(header.encode("utf-8"))
    for line in lines + [None]:
        n = len(line.encode("utf-8")) if line else 0
        if line is None or (chunk and size + n > max_mb * 1e6):
            part = dest.with_name(f"{dest.stem}_part{len(files) + 1}{dest.suffix}")
            part.write_text(header + "".join(chunk), encoding="utf-8", newline="")
            files.append(part)
            chunk, size = [], len(header.encode("utf-8"))
        if line is not None:
            chunk.append(line)
            size += n
    return files


def main():
    ap = argparse.ArgumentParser(description="Supplier quantities as a Shopify inventory import")
    ap.add_argument("--export", required=True, help="Shopify inventory export CSV")
    ap.add_argument("--out", help="Output CSV (default: inventory_import_<ts>.csv beside the export)")
    ap.add_argument("--skus", nargs="+", help="Only write these SKUs (e.g. a test batch)")
    ap.add_argument("--warehouse", default=os.environ.get("SBOT_CSV_WAREHOUSE", str(DEFAULT_WAREHOUSE)),
                    help="DuckDB file for this store's runs")
    ap.add_argument("--max-mb", type=float, default=14.0,
                    help="Split the output into parts no larger than this (Shopify's limit is 15)")
    ap.add_argument("--force", action="store_true",
                    help="Write the CSV even if validation gates fail")
    ap.add_argument("--every-location", action="store_true",
                    help="Old layout: every location gets the supplier quantity")
    ap.add_argument("--all-rows", action="store_true",
                    help="With --every-location: write every row, not only changed ones "
                         "(use when the export is days old)")
    ap.add_argument("--prices", action="store_true",
                    help="Also write a products-import CSV with each variant's Variant Price")
    ap.add_argument("--suppliers", nargs="+", choices=list(config.SUPPLIERS),
                    help="Only these suppliers' feeds (others are not read, so their SKUs are "
                         "left alone and shared SKUs use these suppliers only). Use its own "
                         "--warehouse, or the qty-delta gate compares against an all-supplier run.")
    args = ap.parse_args()
    if args.suppliers:
        # ingest and validate both walk config.SUPPLIERS, so narrowing it here
        # keeps the unread suppliers out of the feed checks as well.
        config.SUPPLIERS = {k: v for k, v in config.SUPPLIERS.items() if k in args.suppliers}
        config.REQUIRED_SUPPLIERS = {t: tuple(s for s in req if s in args.suppliers)
                                     for t, req in config.REQUIRED_SUPPLIERS.items()}

    log = make_logger("inventory_csv")
    export_path = Path(args.export).resolve()
    df = load_export(export_path)
    locations = sorted(df["Location"].unique())
    log.info(f"Export: {len(df):,} rows, {df['_sku'].nunique():,} SKUs, "
             f"{len(locations)} locations ({', '.join(locations)})")
    if not args.every_location and SUPPLIER_LOCATION not in {loc.strip() for loc in locations}:
        raise SystemExit(f"{export_path} has no '{SUPPLIER_LOCATION}' location — "
                         f"found {locations}")

    wh = LocalWarehouse(logger=log, path=args.warehouse)
    wh.migrate()
    ctx = RunContext(mode="dry_run", product_types=list(config.PRODUCT_TYPES),
                     warehouse=wh, logger=log)
    status, notes = "success", f"inventory CSV from {export_path.name}"
    try:
        wh.upsert_shopify_products(mirror_rows(df, ctx.run_id))
        wh.delete_stale_shopify_products(ctx.run_id)
        meta = ingest.run(ctx)
        merge.run(ctx)
        try:
            validate.run(ctx, meta)
        except RuntimeError as e:
            if not args.force:
                raise
            log.warning(f"--force: writing the CSV despite validation failure: {e}")
            notes += " (forced past validation failure)"

        snap = wh.select(f"select sku, merged_qty, merged_price from merged_snapshot where run_id = {ctx.run_id};")
        targets = pd.Series({r["sku"]: int(r["merged_qty"] or 0) for r in snap}, dtype="int64")
        if args.every_location:
            out = build_import_every_location(df, targets, args.skus, write_all=args.all_rows)
        else:
            out = build_import(df, targets, args.skus)

        if args.prices:
            prices = pd.Series({r["sku"]: float(r["merged_price"]) for r in snap
                                if r["merged_price"] is not None and float(r["merged_price"]) > 0},
                               dtype="float64")
            price_rows = build_prices(df, prices, args.skus)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            pdest = (Path(args.out).with_name(f"{Path(args.out).stem}_prices.csv") if args.out
                     else export_path.with_name(f"price_import_{ts}.csv"))
            for f in write_parts(price_rows, pdest, args.max_mb):
                log.info(f"Wrote prices {f} ({f.stat().st_size / 1e6:.1f} MB)")
            log.info(f"Prices: {len(price_rows):,} variants, "
                     f"{len(price_rows['Variant SKU'].unique()) if len(price_rows) else 0:,} SKUs")

        in_store = targets.index.isin(df["_sku"])
        log.info(f"Supplier SKUs: {len(targets):,} merged, {in_store.sum():,} in the store, "
                 f"{(~in_store).sum():,} not in the store (import them first)")
        log.info(f"Store SKUs with no supplier row ('{SUPPLIER_LOCATION}' left unchanged, "
                 f"stores set to 0): {(~df['_sku'].drop_duplicates().isin(targets.index)).sum():,}")
        log.info(f"Rows to change: {len(out):,} across {out['_sku'].nunique():,} SKUs "
                 f"({(~out['_stocked']).sum():,} currently 'not stocked' at that location)")
        for loc, n in out["Location"].value_counts().sort_index().items():
            log.info(f"  {loc}: {n:,}")

        if out.empty:
            log.info("Nothing to change — no CSV written.")
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = Path(args.out) if args.out else export_path.with_name(f"inventory_import_{ts}.csv")
            files = write_parts(out[[c for c in df.columns if not c.startswith("_")]],
                                dest, args.max_mb)
            for f in files:
                log.info(f"Wrote {f} ({f.stat().st_size / 1e6:.1f} MB)")
            ctx.stats["csv"] = {"files": [str(f) for f in files], "rows": len(out),
                                "skus": int(out["_sku"].nunique())}
    except Exception as e:
        status, notes = "failed", str(e)[:2000]
        log.error(f"FAILED: {e}")
        raise
    finally:
        ctx.finalize(status, notes)


if __name__ == "__main__":
    main()
