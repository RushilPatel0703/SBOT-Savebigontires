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
  inventory_activate — like inventory_set, for an item with no inventory level
                   at the stock location yet: inventoryActivate stocks it there
                   with the quantity (inventorySetQuantities would refuse it)
  inventory_store_zero — a supplier SKU in this run's snapshot that holds units
                   at a location other than the stock location; set to 0 there
                   (config.ZERO_STORE_LOCATIONS). SKUs outside the snapshot keep
                   their store stock: they may be ones the client manages by hand.

Quantities are per location: the catalog step sets current_qty to the stock
location's available, and every write goes to one location.
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
        diff = ctx.wh.select(f"""
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
        unmapped = ctx.wh.select_one(f"""
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
        applied = 0

        # Pushed in aliased batches (core/shopify_gql.py::update_prices_batched)
        # rather than one HTTP request per SKU — a full reprice is thousands of
        # SKUs and the per-request latency, not Shopify's cost budget, was the
        # binding constraint.
        #
        # The outer chunk exists for the circuit breaker: it can only abort work
        # that has not been sent yet, so results have to come back periodically
        # rather than once at the end. 500 keeps that check frequent while still
        # collapsing ~500 round-trips into ~20.
        CHUNK = 500
        for start in range(0, len(diff), CHUNK):
            rows = diff[start:start + CHUNK]
            if dry_run:
                statuses = [("dry_run", None)] * len(rows)
            else:
                # results come back in input order, one entry per SKU
                results = shopify.update_prices_batched(
                    [(r["sku"], r["product_id"], r["variant_id"], float(r["new_price"]))
                     for r in rows])
                statuses = [("applied", None) if ok else ("failed", err)
                            for _sku, ok, err in results]
                for status, _ in statuses:
                    breaker.record(status == "applied")

            applied += sum(1 for s, _ in statuses if s in ("applied", "dry_run"))
            ctx.record_sync_actions([
                {"sku": r["sku"], "action": "price_update",
                 "old_value": str(r["old_price"]), "new_value": str(r["new_price"]),
                 "status": statuses[j][0], "error": statuses[j][1]}
                for j, r in enumerate(rows)
            ])
            for j, (status, error) in enumerate(statuses):
                if status == "failed":
                    ctx.log.warning(f"  price failed SKU {rows[j]['sku']}: {str(error)[:200]}")
            ctx.log.info(f"  price sync progress: {min(start + CHUNK, len(diff))}/{len(diff)}")

        step.rows_out = applied
        step.details["failed"] = breaker.failed
        if not dry_run:
            step.details["shopify_cost"] = shopify.cost_summary()
        ctx.log.info(f"Price sync done: {applied} {'previewed' if dry_run else 'applied'}, "
                     f"{breaker.failed} failed")
    return applied


# ── inventory ──────────────────────────────────────────────────────────────────
def run_inventory(ctx, shopify, dry_run):
    with ctx.step("sync_inventory") as step:
        sets = ctx.wh.select(f"""
            select ms.sku, sp.inventory_item_id,
                   sp.current_qty::int as old_qty, ms.merged_qty::int as new_qty
            from merged_snapshot ms
            join shopify_products sp on sp.sku = ms.sku
            where ms.run_id = {ctx.run_id}
              and ms.product_type in ({_types_sql(ctx)})
              and ms.merged_qty is distinct from sp.current_qty
            order by ms.sku;
        """)
        zeros = ctx.wh.select(f"""
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
        location_gid = None if dry_run else shopify.primary_location_id()
        unstocked = getattr(ctx, "unstocked_items", None) or set()
        work = ([("inventory_activate" if r["inventory_item_id"] in unstocked else "inventory_set",
                  r["sku"], r["inventory_item_id"], r["old_qty"], int(r["new_qty"]), location_gid)
                 for r in sets] +
                [("inventory_zero", r["sku"], r["inventory_item_id"], r["old_qty"], 0, location_gid)
                 for r in zeros])
        store_zeros = _store_zeros(ctx)
        work += [("inventory_store_zero", o["sku"], o["inventory_item_id"], o["qty"], 0,
                  o["location_id"]) for o in store_zeros]
        step.rows_in = len(work)
        n_activate = sum(1 for item in work if item[0] == "inventory_activate")
        ctx.log.info(f"Inventory diff: {len(sets)} qty changes ({n_activate} of them first "
                     f"stocked at the stock location), {len(zeros)} zero-outs, "
                     f"{len(store_zeros)} store-location zero-outs")
        for loc, n in sorted(_count_by(store_zeros, "location").items()):
            ctx.log.info(f"  store-location zero-outs at {loc}: {n}")

        breaker = CircuitBreaker(ctx.log)
        applied = 0
        batch_size = config.SHOPIFY_INVENTORY_BATCH

        # One inventorySetQuantities call takes one location per item, so batch
        # by location to keep each call simple and its failures attributable.
        # Activations go in their own batches: a different mutation.
        batches = []
        for key in dict.fromkeys((item[0] == "inventory_activate", item[5]) for item in work):
            items = [item for item in work if (item[0] == "inventory_activate", item[5]) == key]
            batches += [items[i:i + batch_size] for i in range(0, len(items), batch_size)]

        for i, batch in enumerate(batches):
            outcomes = {}                     # index → (status, error)
            if dry_run:
                outcomes = {j: ("dry_run", None) for j in range(len(batch))}
            elif batch[0][0] == "inventory_activate":
                results = shopify.activate_inventory_batched(
                    batch[0][5], [(item[2], item[4]) for item in batch])
                outcomes = {j: (("applied", None) if ok else ("failed", err))
                            for j, (_gid, ok, err) in enumerate(results)}
                for status, _ in outcomes.values():
                    breaker.record(status == "applied")
            else:
                try:
                    shopify.set_inventory_quantities(
                        batch[0][5], [(item[2], item[4]) for item in batch])
                    outcomes = {j: ("applied", None) for j in range(len(batch))}
                except ShopifyError as batch_err:
                    # the mutation is all-or-nothing: one bad item fails the whole
                    # batch, so isolate the offender(s) by retrying individually
                    ctx.log.warning(f"  inventory batch failed, retrying items "
                                    f"individually: {str(batch_err)[:200]}")
                    for j, item in enumerate(batch):
                        try:
                            shopify.set_inventory_quantities(
                                item[5], [(item[2], item[4])])
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
                for j, (action, sku, _, old, new, _loc) in enumerate(batch)
            ])
            n_failed = sum(1 for s, _ in outcomes.values() if s == "failed")
            ctx.log.info(f"  inventory batch {i + 1}: {len(batch)} items, "
                         f"{n_failed} failed")

        step.rows_out = applied
        step.details.update({"qty_changes": len(sets), "activations": n_activate,
                             "zero_outs": len(zeros),
                             "store_zero_outs": len(store_zeros),
                             "failed": breaker.failed})
        if not dry_run:
            step.details["shopify_cost"] = shopify.cost_summary()
        ctx.log.info(f"Inventory sync done: {applied} {'previewed' if dry_run else 'applied'}, "
                     f"{breaker.failed} failed")
    return applied


def _store_zeros(ctx):
    """Non-zero levels at non-stock locations, for SKUs in this run's snapshot."""
    if not config.ZERO_STORE_LOCATIONS:
        return []
    offsite = getattr(ctx, "offsite_stock", None)
    if offsite is None:
        ctx.log.warning("Store locations not checked: the catalog step didn't run in this "
                        "process, so there are no per-location levels to compare.")
        return []
    if not offsite:
        return []
    snap = {r["sku"] for r in ctx.wh.select(f"""
        select sku from merged_snapshot
        where run_id = {ctx.run_id} and product_type in ({_types_sql(ctx)});
    """)}
    return [o for o in offsite if o["sku"] in snap]


def _count_by(rows, key):
    out = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return out
