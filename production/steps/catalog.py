#!/usr/bin/env python3
"""Catalog: mirror the live Shopify store into shopify_products.

One bulk-operation GraphQL export (no pagination, no throttling) replaces the
old REST build_sku_map.py. The mirror is the join target for diff-based sync,
so it must be refreshed at the start of every run.
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
