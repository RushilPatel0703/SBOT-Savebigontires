#!/usr/bin/env python3
"""Sync: diff-based price + inventory push to Shopify via GraphQL.

The diff is computed in SQL (merged_snapshot × shopify_products mirror) so only
changed values hit the API — a weekly steady-state run touches hundreds of SKUs,
not the whole 10k catalog. Every intended change lands in sync_actions with its
outcome. A circuit breaker aborts if the failure rate climbs.

Actions:
  price_update   — round(merged_price) differs from live price
                   (merged_price is already the final, marked-up price)
  inventory_set  — merged_qty differs from live qty
  inventory_zero — mapped SKU of this type absent from snapshot, live qty ≠ 0
"""

import config
from core.shopify_gql import ShopifyError


class CircuitBreaker:
    def __init__(self, log):
        self.done = 0
        self.failed = 0
        self.log = log

    def record(self, ok):
        self.done += 1
        self.failed += not ok
        if self.done >= 50:
            pct = self.failed / self.done * 100
            if pct > config.MAX_SYNC_FAILURE_PCT:
                raise RuntimeError(
                    f"Sync circuit breaker tripped: {self.failed}/{self.done} "
                    f"failures ({pct:.1f}% > {config.MAX_SYNC_FAILURE_PCT}%)"
                )


def _types_sql(ctx):
    return ", ".join(f"'{t}'" for t in ctx.product_types)


def _shopify_types_sql(ctx):
    return ", ".join(f"'{config.SHOPIFY_PRODUCT_TYPE[t]}'" for t in ctx.product_types)


# ── prices ─────────────────────────────────────────────────────────────────────
def run_prices(ctx, shopify, dry_run):
    with ctx.step("sync_prices") as step:
        diff = ctx.hasura.select(f"""
            select ms.sku, sp.product_id, sp.variant_id,
                   sp.current_price::float as old_price,
                   round(ms.merged_price, 2)::float as new_price
            from merged_snapshot ms
            join shopify_products sp on sp.sku = ms.sku
            where ms.run_id = {ctx.run_id}
              and ms.product_type in ({_types_sql(ctx)})
              and ms.merged_price > 0
              and round(ms.merged_price, 2)
                  between {config.MIN_PRICE} and {config.MAX_PRICE}
              and round(ms.merged_price, 2)
                  is distinct from sp.current_price
            order by ms.sku;
        """)
        unmapped = ctx.hasura.select_one(f"""
            select count(*)::int as n from merged_snapshot ms
            where ms.run_id = {ctx.run_id}
              and ms.product_type in ({_types_sql(ctx)})
              and not exists (select 1 from shopify_products sp where sp.sku = ms.sku);
        """)
        step.rows_in = len(diff)
        step.details["skus_not_in_store"] = int(unmapped["n"]) if unmapped else 0
        ctx.log.info(f"Price diff: {len(diff)} SKUs to update "
                     f"({step.details['skus_not_in_store']} snapshot SKUs not in store — "
                     f"not created by create_products, e.g. no SEO definition)")

        breaker = CircuitBreaker(ctx.log)
        actions, applied = [], 0
        for row in diff:
            status, error = ("dry_run", None)
            if not dry_run:
                try:
                    shopify.update_variant_price(row["product_id"], row["variant_id"],
                                                 float(row["new_price"]))
                    status = "applied"
                except ShopifyError as e:
                    status, error = "failed", str(e)[:500]
                breaker.record(status == "applied")
            applied += status in ("applied", "dry_run")
            actions.append({"sku": row["sku"], "action": "price_update",
                            "old_value": str(row["old_price"]), "new_value": str(row["new_price"]),
                            "status": status, "error": error})
            if len(actions) >= 500:
                ctx.record_sync_actions(actions)
                actions = []
                ctx.log.info(f"  price sync progress: {breaker.done}/{len(diff)}")
        ctx.record_sync_actions(actions)

        step.rows_out = applied
        step.details["failed"] = breaker.failed
        ctx.log.info(f"Price sync done: {applied} {'previewed' if dry_run else 'applied'}, "
                     f"{breaker.failed} failed")
    return applied


# ── inventory ──────────────────────────────────────────────────────────────────
def run_inventory(ctx, shopify, dry_run):
    with ctx.step("sync_inventory") as step:
        sets = ctx.hasura.select(f"""
            select ms.sku, sp.inventory_item_id,
                   sp.current_qty::int as old_qty, ms.merged_qty::int as new_qty
            from merged_snapshot ms
            join shopify_products sp on sp.sku = ms.sku
            where ms.run_id = {ctx.run_id}
              and ms.product_type in ({_types_sql(ctx)})
              and ms.merged_qty is distinct from sp.current_qty
            order by ms.sku;
        """)
        zeros = ctx.hasura.select(f"""
            select sp.sku, sp.inventory_item_id, sp.current_qty::int as old_qty
            from shopify_products sp
            where sp.product_type in ({_shopify_types_sql(ctx)})
              and coalesce(sp.current_qty, 0) <> 0
              and not exists (
                  select 1 from merged_snapshot ms
                  where ms.run_id = {ctx.run_id} and ms.sku = sp.sku
              )
            order by sp.sku;
        """)
        work = ([("inventory_set", r["sku"], r["inventory_item_id"], r["old_qty"], int(r["new_qty"]))
                 for r in sets] +
                [("inventory_zero", r["sku"], r["inventory_item_id"], r["old_qty"], 0)
                 for r in zeros])
        step.rows_in = len(work)
        ctx.log.info(f"Inventory diff: {len(sets)} qty changes, {len(zeros)} zero-outs")

        location_gid = None if dry_run else shopify.primary_location_id()
        breaker = CircuitBreaker(ctx.log)
        applied = 0
        batch_size = config.SHOPIFY_INVENTORY_BATCH

        for i in range(0, len(work), batch_size):
            batch = work[i:i + batch_size]
            outcomes = {}                     # index → (status, error)
            if dry_run:
                outcomes = {j: ("dry_run", None) for j in range(len(batch))}
            else:
                try:
                    shopify.set_inventory_quantities(
                        location_gid, [(item[2], item[4]) for item in batch])
                    outcomes = {j: ("applied", None) for j in range(len(batch))}
                except ShopifyError as batch_err:
                    # the mutation is all-or-nothing: one bad item fails the whole
                    # batch, so isolate the offender(s) by retrying individually
                    ctx.log.warning(f"  inventory batch failed, retrying items "
                                    f"individually: {str(batch_err)[:200]}")
                    for j, item in enumerate(batch):
                        try:
                            shopify.set_inventory_quantities(
                                location_gid, [(item[2], item[4])])
                            outcomes[j] = ("applied", None)
                        except ShopifyError as e:
                            outcomes[j] = ("failed", str(e)[:500])
                for status, _ in outcomes.values():
                    breaker.record(status == "applied")
            applied += sum(1 for s, _ in outcomes.values() if s in ("applied", "dry_run"))
            ctx.record_sync_actions([
                {"sku": sku, "action": action,
                 "old_value": str(old), "new_value": str(new),
                 "status": outcomes[j][0], "error": outcomes[j][1]}
                for j, (action, sku, _, old, new) in enumerate(batch)
            ])
            n_failed = sum(1 for s, _ in outcomes.values() if s == "failed")
            ctx.log.info(f"  inventory batch {i // batch_size + 1}: {len(batch)} items, "
                         f"{n_failed} failed")

        step.rows_out = applied
        step.details.update({"qty_changes": len(sets), "zero_outs": len(zeros),
                             "failed": breaker.failed})
        ctx.log.info(f"Inventory sync done: {applied} {'previewed' if dry_run else 'applied'}, "
                     f"{breaker.failed} failed")
    return applied
