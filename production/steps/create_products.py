#!/usr/bin/env python3
"""Create products: push new SKUs to Shopify via productSet — no manual imports.

New SKUs = merged_snapshot(run) minus the shopify_products mirror. Each one is
looked up in the latest Shopify_SEO_*.csv per supplier (the same files the
manual admin import used), created as a single-variant ACTIVE product with
tracked inventory at the primary location, then published to the Online Store
channel. Created products are inserted into the mirror immediately so the
price/inventory sync steps that follow see no diff.

Price comes from merged_snapshot + markup (single pricing source of truth, same
as sync_prices) — the SEO file's Variant Price may be stale. Qty comes from
merged_snapshot. Everything else (title, body, tags, image, SEO) comes from the
SEO file.

SKUs with no SEO definition are counted, not created (summary only). A cap of
MAX_NEW_PRODUCTS_PER_RUN aborts the step if a bad merge would flood the store.
"""

import os
import time

import pandas as pd

import config
from core.shopify_gql import ShopifyError
from steps.ingest import find_latest, normalize_sku
from steps.sync import CircuitBreaker


def _split_tags(raw):
    return [t.strip() for t in str(raw or "").split(",") if t.strip()]


def load_seo_definitions(ctx, product_type):
    """sku → SEO row dict, searching suppliers in ingest order (first wins)."""
    defs, files = {}, []
    for supplier, cfg in config.SUPPLIERS.items():
        if product_type not in cfg["types"] or "seo_pattern" not in cfg:
            continue
        file_type = cfg.get("type_alias", {}).get(product_type, product_type)
        pattern = config.PIPELINES_DIR / cfg["seo_pattern"].format(type=file_type)
        path = find_latest(pattern)
        if not path:
            ctx.log.warning(f"[seo/{supplier}/{product_type}] no file matching {pattern}")
            continue
        age_days = (time.time() - os.path.getmtime(path)) / 86400
        df = pd.read_csv(path, dtype=str, low_memory=False).fillna("")
        if "Variant SKU" not in df.columns:
            ctx.log.error(f"[seo/{supplier}/{product_type}] no 'Variant SKU' column in {path}")
            continue
        split_colon = cfg.get("sku_split_colon", False)
        added = 0
        for row in df.to_dict("records"):
            sku = normalize_sku(row["Variant SKU"], split_colon)
            if sku and sku not in defs:
                defs[sku] = row
                added += 1
        files.append({"supplier": supplier, "file": os.path.basename(path),
                      "age_days": round(age_days, 2), "skus": added})
        ctx.log.info(f"[seo/{supplier}/{product_type}] {added} SKU definitions "
                     f"from {os.path.basename(path)} (age {age_days:.1f}d)")
    return defs, files


def _types_sql(ctx):
    return ", ".join(f"'{t}'" for t in ctx.product_types)


def run(ctx, shopify, dry_run):
    with ctx.step("create_products") as step:
        new = ctx.wh.select(f"""
            select ms.product_type, ms.sku,
                   ms.merged_price::float as price, ms.merged_qty::int as qty
            from merged_snapshot ms
            where ms.run_id = {ctx.run_id}
              and ms.product_type in ({_types_sql(ctx)})
              and not exists (select 1 from shopify_products sp where sp.sku = ms.sku)
            order by ms.product_type, ms.sku;
        """)
        step.rows_in = len(new)
        ctx.log.info(f"New SKUs (snapshot minus store mirror): {len(new)}")

        over_cap = len(new) > config.MAX_NEW_PRODUCTS_PER_RUN
        ctx.record_validation("new_products_cap", not over_cap, severity="error",
                              details={"new_skus": len(new),
                                       "max": config.MAX_NEW_PRODUCTS_PER_RUN})
        if over_cap:
            raise RuntimeError(f"create_products cap exceeded: {len(new)} new SKUs "
                               f"> {config.MAX_NEW_PRODUCTS_PER_RUN}")
        if not new:
            step.rows_out = 0
            return 0

        seo, seo_files = {}, []
        for product_type in ctx.product_types:
            defs, files = load_seo_definitions(ctx, product_type)
            seo[product_type] = defs
            seo_files += files
        step.details["seo_files"] = seo_files

        location_gid = publication_gid = None
        if not dry_run:
            location_gid = shopify.primary_location_id()
            publication_gid = shopify.online_store_publication_id()
            if publication_gid is None:
                ctx.log.warning(
                    "Online Store publication unavailable (token lacks "
                    "read_publications/write_publications) — products will be "
                    "created ACTIVE but NOT published to the Online Store channel. "
                    "Add both scopes to the app and re-run.")

        breaker = CircuitBreaker(ctx.log)
        actions, mirror_rows = [], []
        created = no_seo = skipped_price = 0

        def flush():
            ctx.record_sync_actions(actions)
            actions.clear()
            if mirror_rows:
                ctx.wh.insert_many("shopify_products", mirror_rows)
                mirror_rows.clear()

        for row in new:
            product_type, sku = row["product_type"], row["sku"]
            definition = seo[product_type].get(sku)
            if definition is None:
                no_seo += 1
                continue

            # merged_price already includes that supplier's markup (see steps/merge.py)
            price = round(float(row["price"] or 0), 2)
            if not (config.MIN_PRICE <= price <= config.MAX_PRICE):
                skipped_price += 1
                actions.append({"sku": sku, "action": "product_create",
                                "old_value": None, "new_value": f"price={price}",
                                "status": "skipped", "error": "price out of bounds"})
                continue

            status, error = ("dry_run", None)
            if not dry_run:
                try:
                    result = shopify.create_product(
                        title=definition["Title"],
                        description_html=definition.get("Body (HTML)"),
                        vendor=definition.get("Vendor") or "Save Big On Tires",
                        product_type=config.SHOPIFY_PRODUCT_TYPE[product_type],
                        tags=_split_tags(definition.get("Tags")),
                        handle=definition["URL Handle"],
                        sku=sku, price=price, qty=row["qty"],
                        location_gid=location_gid,
                        image_url=(definition.get("Image Src") or "").strip() or None,
                        seo_title=(definition.get("SEO Title") or "").strip() or None,
                        seo_description=(definition.get("SEO Description") or "").strip() or None,
                    )
                    if publication_gid:
                        shopify.publish_product(result["product_id"], publication_gid)
                    mirror_rows.append({
                        "sku": sku,
                        "product_id": result["product_id"],
                        "variant_id": result["variant_id"],
                        "inventory_item_id": result["inventory_item_id"],
                        "handle": result["handle"],
                        "title": definition["Title"],
                        "product_type": config.SHOPIFY_PRODUCT_TYPE[product_type],
                        "tags": definition.get("Tags"),
                        "current_price": price,
                        "current_qty": int(row["qty"]),
                        "refreshed_at": "now()",
                        "last_run_id": ctx.run_id,
                    })
                    status = "applied"
                except ShopifyError as e:
                    status, error = "failed", str(e)[:500]
                breaker.record(status == "applied")
            created += status in ("applied", "dry_run")
            actions.append({"sku": sku, "action": "product_create",
                            "old_value": None,
                            "new_value": f"price={price},qty={row['qty']}",
                            "status": status, "error": error})
            if len(actions) >= 100:
                flush()
                ctx.log.info(f"  create progress: {breaker.done or created}/{len(new)}")
        flush()

        step.rows_out = created
        step.details.update({"no_seo_definition": no_seo,
                             "skipped_price_bounds": skipped_price,
                             "failed": breaker.failed,
                             "published": bool(publication_gid) if not dry_run else None})
        ctx.log.info(f"Product creation done: {created} "
                     f"{'previewed' if dry_run else 'created'}, {no_seo} without SEO "
                     f"definition (skipped), {skipped_price} price out of bounds, "
                     f"{breaker.failed} failed")
    return created
