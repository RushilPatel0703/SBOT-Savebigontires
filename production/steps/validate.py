#!/usr/bin/env python3
"""Validate: safety gates between merge and any Shopify write.

Every check is recorded in validation_results. Any failed error-severity check
aborts the run before sync (unless --force). The gates exist for one scenario
above all: a silently broken scrape must never zero out or reprice the store.
"""

import config


def run(ctx, ingest_meta):
    failures = 0

    with ctx.step("validate") as step:

        # 1 — feed presence + freshness + minimum rows
        for product_type in ctx.product_types:
            required = set(config.REQUIRED_SUPPLIERS.get(product_type, ()))
            metas = {m["supplier"]: m for m in ingest_meta if m["product_type"] == product_type}
            for supplier, cfg in config.SUPPLIERS.items():
                if product_type not in cfg["types"]:
                    continue
                m = metas.get(supplier)
                severity = "error" if supplier in required else "warning"
                name = f"feed_present:{supplier}:{product_type}"
                if not m or not m["file"]:
                    ctx.record_validation(name, False, severity,
                                          {"reason": "no CSV found"})
                    failures += severity == "error"
                    continue
                ctx.record_validation(name, True, severity, {"file": m["file"]})

                fresh = m["age_days"] is not None and m["age_days"] <= config.MAX_CSV_AGE_DAYS
                ctx.record_validation(f"feed_fresh:{supplier}:{product_type}", fresh, severity,
                                      {"age_days": m["age_days"], "max": config.MAX_CSV_AGE_DAYS})
                failures += (not fresh and severity == "error")

                enough = m["rows"] >= config.MIN_ROWS_PER_SUPPLIER
                ctx.record_validation(f"feed_min_rows:{supplier}:{product_type}", enough, severity,
                                      {"rows": m["rows"], "min": config.MIN_ROWS_PER_SUPPLIER})
                failures += (not enough and severity == "error")

        # 2 — total qty delta vs last successful run
        for product_type in ctx.product_types:
            row = ctx.hasura.select_one(f"""
                with prev as (
                    select coalesce(sum(ms.merged_qty), 0) as qty
                    from merged_snapshot ms
                    join pipeline_runs pr on pr.run_id = ms.run_id
                    where pr.status = 'success'
                      and pr.mode = 'live'
                      and ms.product_type = '{product_type}'
                      and ms.run_id = (
                          select max(pr2.run_id) from pipeline_runs pr2
                          join merged_snapshot ms2 on ms2.run_id = pr2.run_id
                          where pr2.status = 'success' and pr2.mode = 'live'
                            and ms2.product_type = '{product_type}'
                      )
                ),
                cur as (
                    select coalesce(sum(merged_qty), 0) as qty
                    from merged_snapshot
                    where run_id = {ctx.run_id} and product_type = '{product_type}'
                )
                select prev.qty as prev_qty, cur.qty as cur_qty from prev, cur;
            """)
            prev_qty = int(row["prev_qty"]) if row else 0
            cur_qty  = int(row["cur_qty"]) if row else 0
            if prev_qty == 0:
                ctx.record_validation(f"qty_delta:{product_type}", True, "warning",
                                      {"note": "no previous successful run — baseline",
                                       "cur_qty": cur_qty})
            else:
                delta_pct = abs(cur_qty - prev_qty) / prev_qty * 100
                ok = delta_pct <= config.MAX_TOTAL_QTY_DELTA_PCT
                ctx.record_validation(f"qty_delta:{product_type}", ok, "error",
                                      {"prev_qty": prev_qty, "cur_qty": cur_qty,
                                       "delta_pct": round(delta_pct, 1),
                                       "max_pct": config.MAX_TOTAL_QTY_DELTA_PCT})
                failures += not ok

        # 3 — zero-out cap: mapped catalog SKUs absent from this snapshot
        for product_type in ctx.product_types:
            shopify_type = config.SHOPIFY_PRODUCT_TYPE[product_type]
            row = ctx.hasura.select_one(f"""
                select
                    count(*)::int as catalog,
                    count(*) filter (
                        where sp.sku not in (
                            select sku from merged_snapshot
                            where run_id = {ctx.run_id} and product_type = '{product_type}'
                        )
                        and coalesce(sp.current_qty, 0) <> 0
                    )::int as would_zero
                from shopify_products sp
                where sp.product_type = '{shopify_type}';
            """)
            catalog    = int(row["catalog"]) if row else 0
            would_zero = int(row["would_zero"]) if row else 0
            if catalog == 0:
                ctx.record_validation(f"zero_out_cap:{product_type}", True, "warning",
                                      {"note": "no products of this type in store"})
                continue
            pct = would_zero / catalog * 100
            ok = pct <= config.MAX_ZERO_OUT_PCT
            ctx.record_validation(f"zero_out_cap:{product_type}", ok, "error",
                                  {"catalog": catalog, "would_zero": would_zero,
                                   "pct": round(pct, 1), "max_pct": config.MAX_ZERO_OUT_PCT})
            failures += not ok

        # 4 — price sanity on final prices (merged_price is already marked up)
        row = ctx.hasura.select_one(f"""
            select
                count(*) filter (where merged_price is null or merged_price <= 0)::int as unpriced,
                count(*) filter (where merged_price < {config.MIN_PRICE}
                              or merged_price > {config.MAX_PRICE})::int as out_of_bounds
            from merged_snapshot where run_id = {ctx.run_id};
        """)
        ctx.record_validation("price_bounds", True, "warning", {
            "unpriced_skus": int(row["unpriced"]),
            "out_of_bounds": int(row["out_of_bounds"]),
            "note": "these SKUs are skipped by price sync",
        })

        # 5 — per-SKU price swing vs live store (warning only)
        row = ctx.hasura.select_one(f"""
            select count(*)::int as big_swings
            from merged_snapshot ms
            join shopify_products sp on sp.sku = ms.sku
            where ms.run_id = {ctx.run_id}
              and ms.merged_price > 0 and sp.current_price > 0
              and abs(ms.merged_price - sp.current_price)
                  / sp.current_price * 100 > {config.MAX_PRICE_SWING_PCT};
        """)
        ctx.record_validation("price_swings", True, "warning", {
            "skus_over_swing_pct": int(row["big_swings"]),
            "threshold_pct": config.MAX_PRICE_SWING_PCT,
        })

        step.details["error_failures"] = failures
        if failures:
            raise RuntimeError(
                f"{failures} error-severity validation check(s) failed — run aborted "
                f"before any Shopify write. Inspect validation_results for run "
                f"{ctx.run_id}, fix the feed (or rerun with --force if intentional)."
            )
        ctx.log.info("All error-severity validation gates passed.")

    return failures
