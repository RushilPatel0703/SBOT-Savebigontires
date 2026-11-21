#!/usr/bin/env python3
"""Catalog: mirror the live Shopify store into shopify_products.

One bulk-operation GraphQL export (no pagination, no throttling) replaces the
old REST build_sku_map.py. The mirror is the join target for diff-based sync,
so it must be refreshed at the start of every run.

Two guards sit here rather than in validate, because by validate the mirror has
already been overwritten:

- Shrink gate. An export much smaller than the mirror it replaces (an empty one
  on 2026-10-30, run #13) fails the run before the mirror is touched. Otherwise
  every supplier SKU looks "not in store", every gate passes on nothing, and a
  run with create_products tries to create the whole catalog again. --force
  (ctx.force) lets an intentional wipe through.
- Per-location stock. current_qty is the stock location's available quantity,
  not the variant's total over all locations, which is what sync writes back.
  Units sitting at any other location are collected in ctx.offsite_stock so
  sync_inventory can set them to 0. Items with no inventory level at the stock
  location go in ctx.unstocked_items: sync activates them there instead of
  setting a quantity, which Shopify would refuse (ITEM_NOT_STOCKED_AT_LOCATION).
"""

import config


def gid_num(gid):
    """gid://shopify/Product/123 → '123' (stored raw gid too? we store gid)."""
    return gid


def run(ctx, shopify):
    with ctx.step("catalog") as step:
        products, variants = shopify.fetch_catalog()
        step.rows_in = len(variants)

        by_product = {p["id"]: p for p in products}
        rows, no_sku, dupes = [], 0, 0
        seen = set()
        for v in variants:
            sku = (v.get("sku") or "").strip()
            if not sku:
                no_sku += 1
                continue
            if sku in seen:
                dupes += 1
                continue                      # first variant wins, matches old sku_map behaviour
            seen.add(sku)
            p = by_product.get(v.get("__parentId"), {})
            rows.append({
                "sku": sku,
                "product_id": v.get("__parentId"),
                "variant_id": v["id"],
                "inventory_item_id": (v.get("inventoryItem") or {}).get("id"),
                "handle": p.get("handle"),
                "title": p.get("title"),
                "product_type": p.get("productType"),
                "tags": ",".join(p.get("tags", [])) if isinstance(p.get("tags"), list) else p.get("tags"),
                "current_price": float(v["price"]) if v.get("price") else None,
                "current_qty": v.get("inventoryQuantity"),
                "refreshed_at": "now()",
                "last_run_id": ctx.run_id,
            })

        _shrink_gate(ctx, len(rows))
        ctx.offsite_stock, ctx.unstocked_items = (
            _apply_location_stock(ctx, shopify, rows) if rows else ([], set()))
        step.details["offsite_levels"] = len(ctx.offsite_stock)
        step.details["not_stocked_at_stock_location"] = len(ctx.unstocked_items)

        # upsert mirror, then remove mirror rows for products no longer in the
        # store. Both are warehouse-backend calls rather than inline GraphQL:
        # Hasura reaches them through auto-generated mutations and DuckDB
        # through `on conflict do update`, and this step should not care which.
        upserted = ctx.wh.upsert_shopify_products(rows)
        deleted  = ctx.wh.delete_stale_shopify_products(ctx.run_id)

        step.rows_out = len(rows)
        step.details.update({"no_sku_variants": no_sku, "duplicate_skus": dupes,
                             "removed_from_mirror": deleted})
        ctx.log.info(f"Catalog mirror: {len(rows)} SKUs upserted, {deleted} stale removed, "
                     f"{no_sku} variants without SKU, {dupes} duplicate SKUs ignored")
    return len(rows)


def _shrink_gate(ctx, new_count):
    prev = ctx.wh.select_one("select count(*)::int as n from shopify_products;")
    prev_count = int(prev["n"]) if prev else 0
    if prev_count == 0:
        return                        # first run, or a store that really is empty
    shrink = (prev_count - new_count) / prev_count * 100
    ok = shrink <= config.MAX_CATALOG_SHRINK_PCT
    details = {"mirror_skus": prev_count, "export_skus": new_count,
               "shrink_pct": round(shrink, 1), "max_pct": config.MAX_CATALOG_SHRINK_PCT}
    ctx.record_validation("catalog_shrink", ok, "error", details)
    if ok:
        return
    msg = (f"Store export has {new_count} SKUs, {shrink:.1f}% fewer than the {prev_count} in "
           f"the mirror (max {config.MAX_CATALOG_SHRINK_PCT}%). The mirror was left as it was. "
           f"Check the store and the token's store before rerunning; use --force only if the "
           f"store really was emptied.")
    if getattr(ctx, "force", False):
        ctx.log.warning(f"--force: {msg}")
        return
    raise RuntimeError(msg)


def _apply_location_stock(ctx, shopify, rows):
    """Set each row's current_qty to the stock location's available quantity.
    Returns (non-zero levels at every other location, inventory items with no
    level at the stock location)."""
    stock_loc = shopify.primary_location_id()      # also checks SHOPIFY_STOCK_LOCATION
    locations, levels = shopify.fetch_inventory_levels()
    names = {loc["id"]: loc["name"] for loc in locations}
    offsite, unstocked = [], set()
    for r in rows:
        at = levels.get(r["inventory_item_id"]) or {}
        r["current_qty"] = at.get(stock_loc, 0)
        if stock_loc not in at and r["inventory_item_id"]:
            unstocked.add(r["inventory_item_id"])
        for loc, qty in at.items():
            if loc != stock_loc and qty != 0:
                offsite.append({"sku": r["sku"], "inventory_item_id": r["inventory_item_id"],
                                "location_id": loc, "location": names.get(loc, loc),
                                "qty": qty})
    units = sum(o["qty"] for o in offsite)
    ctx.log.info(f"Stock location: {names.get(stock_loc, stock_loc)}. Other locations hold "
                 f"{units} units across {len(offsite)} levels"
                 + (" (sync_inventory sets supplier SKUs there to 0)" if offsite else ""))
    if unstocked:
        ctx.log.info(f"{len(unstocked)} items aren't stocked at the stock location yet "
                     f"(sync_inventory activates them there)")
    return offsite, unstocked
