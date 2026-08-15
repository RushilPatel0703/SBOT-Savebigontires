#!/usr/bin/env python3
"""Merge: supplier_staging → merged_snapshot for this run.

Set-based SQL in Neon (via Hasura run_sql) — same rules as the old
merge_suppliers.py: per SKU, price = min positive supplier price,
qty = sum of qualifying supplier quantities, sources = pipe-joined list.
"""


def run(ctx):
    with ctx.step("merge") as step:
        result = ctx.hasura.run_sql(f"""
            insert into merged_snapshot (run_id, product_type, sku, merged_price, merged_qty, sources)
            select
                {ctx.run_id},
                product_type,
                sku,
                min(price) filter (where price > 0),
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
