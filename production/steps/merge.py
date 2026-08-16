#!/usr/bin/env python3
"""Merge: supplier_staging → merged_snapshot for this run.

Set-based SQL in Neon (via Hasura run_sql) — same rules as the old
merge_suppliers.py: per SKU, price = min positive supplier price,
qty = sum of qualifying supplier quantities, sources = pipe-joined list.

Markup note: markup is per supplier (client-updated July 2026), so it is applied
here, INSIDE the min(), and merged_price is therefore the FINAL selling price.
While every supplier shared one flat markup, min(cost) and min(cost + markup)
always selected the same supplier and the markup could safely be added later, at
sync time. With differing markups they diverge — cost 100 + 46.50 = 146.50 loses
to cost 105 + 36.50 = 141.50 — so adding markup after the min() would pick the
cheapest *cost* rather than the cheapest *price* and systematically overcharge.
Downstream steps must NOT add markup again.
"""

import config


def run(ctx):
    with ctx.step("merge") as step:
        # Fail loudly on a staged supplier with no configured markup. Letting the
        # CASE fall through to NULL would drop that supplier out of the min()
        # silently, which is a pricing error rather than a visible failure.
        staged = ctx.hasura.select(f"""
            select distinct supplier from supplier_staging where run_id = {ctx.run_id};
        """)
        unknown = sorted({r["supplier"] for r in staged} - set(config.SUPPLIER_MARKUP))
        if unknown:
            raise RuntimeError(
                f"No markup configured for staged supplier(s): {unknown}. "
                f"Known: {sorted(config.SUPPLIER_MARKUP)}. Refusing to merge — "
                f"an unpriced supplier would be dropped from the price comparison."
            )

        markup_case = "case supplier " + " ".join(
            f"when '{s}' then {m:.2f}" for s, m in sorted(config.SUPPLIER_MARKUP.items())
        ) + " end"

        result = ctx.hasura.run_sql(f"""
            insert into merged_snapshot (run_id, product_type, sku, merged_price, merged_qty, sources)
            select
                {ctx.run_id},
                product_type,
                sku,
                min(price + {markup_case}) filter (where price > 0),
                sum(qty),
                string_agg(distinct supplier, '|' order by supplier)
            from supplier_staging
            where run_id = {ctx.run_id}
            group by product_type, sku;
        """)
        # run_sql returns CommandOk for INSERT; count separately
        counts = ctx.hasura.select(f"""
            select product_type,
                   count(*)::int as skus,
                   sum(merged_qty)::bigint as total_qty,
                   count(*) filter (where merged_price is null)::int as no_price,
                   count(*) filter (where sources like '%|%')::int as multi_supplier
            from merged_snapshot
            where run_id = {ctx.run_id}
            group by product_type;
        """)
        total = 0
        for c in counts:
            total += int(c["skus"])
            ctx.log.info(f"[{c['product_type']}] merged {c['skus']} SKUs, "
                         f"total qty {c['total_qty']}, multi-supplier {c['multi_supplier']}, "
                         f"no-price {c['no_price']}")
        step.rows_out = total
        step.details["by_type"] = counts
        if total == 0:
            raise RuntimeError("Merge produced 0 rows — nothing was staged.")
    return total
