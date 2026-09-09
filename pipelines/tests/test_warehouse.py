"""Tests for the local (DuckDB) warehouse backend — production/core/local_warehouse.py.

The whole point of the backend split is that the pipeline steps hold ONE copy of
the business rules and run unmodified against either warehouse. So the failure
mode worth guarding is not "DuckDB is broken" but "the local backend quietly
disagrees with the cloud one" — a sync the client previews on their laptop that
would have done something different when the cron ran it.

The SQL asserted here is copied from the step it comes from. If a step's query
changes and its twin here is not updated, the test still passes but has stopped
covering the thing it names — so keep them together.
"""
import sys
from pathlib import Path

import pytest

PRODUCTION = Path(__file__).resolve().parent.parent.parent / "production"

duckdb = pytest.importorskip("duckdb", reason="local warehouse backend needs duckdb")


@pytest.fixture
def wh(tmp_path, monkeypatch):
    """A LocalWarehouse on a throwaway file, with production/ importable.

    production/ and pipelines/shared/ both contain a module named `config`;
    production's modules do a bare `import config`, so production must come
    first on sys.path for the duration of the test.
    """
    monkeypatch.syspath_prepend(str(PRODUCTION))
    for stale in ("config", "core", "core.warehouse", "core.local_warehouse"):
        sys.modules.pop(stale, None)

    from core.local_warehouse import LocalWarehouse

    w = LocalWarehouse(path=tmp_path / "test.duckdb")
    w.migrate()
    yield w
    w.close()


def _seed_run(wh, mode="live", types=("tires",)):
    return wh.start_run(mode=mode, triggered_by="pytest", product_types=list(types))


# ── schema + bookkeeping ──────────────────────────────────────────────────────

def test_migrate_is_idempotent(wh):
    """The file is long-lived and re-opened on every sync, so replaying the
    migration must not fail or duplicate the supplier seed rows."""
    wh.migrate()
    wh.migrate()
    assert wh.select_one("select count(*)::int as n from suppliers")["n"] == 5


def test_run_ids_increment(wh):
    first = _seed_run(wh)
    second = _seed_run(wh)
    assert second == first + 1


def test_step_and_run_bookkeeping_round_trips(wh):
    run_id = _seed_run(wh)
    step_id = wh.insert_step(run_id, "merge")
    wh.update_step(step_id, "success", 10, 9, None, {"by_type": {"tires": 9}})
    wh.finalize_run(run_id, "success", {"merge": {"rows_out": 9}}, "all good")

    step = wh.select_one(f"select * from run_steps where id = {step_id}")
    assert (step["status"], step["rows_in"], step["rows_out"]) == ("success", 10, 9)
    run = wh.select_one(f"select * from pipeline_runs where run_id = {run_id}")
    assert run["status"] == "success"
    assert run["notes"] == "all good"


def test_bookkeeping_failure_does_not_raise(wh):
    """Bookkeeping is best-effort by design — the file log is the fallback
    record. A broken bookkeeping write must not take down a sync that is
    otherwise fine."""
    wh.update_step(None, "success", 1, 1, None, {})       # no step id
    wh.finalize_run(999999, "success", {}, None)          # no such run


# ── insert_many ───────────────────────────────────────────────────────────────

def test_insert_many_handles_rows_with_missing_keys(wh):
    """Feeds without a brand/size column omit those keys entirely. Binding by
    the first row's keys would shift later rows into the wrong columns."""
    run_id = _seed_run(wh)
    wh.insert_many("supplier_staging", [
        {"run_id": run_id, "supplier": "dttire", "product_type": "tires",
         "sku": "A", "price": 100.0, "qty": 5, "brand": "Toyo"},
        {"run_id": run_id, "supplier": "dttire", "product_type": "tires",
         "sku": "B", "price": 200.0, "qty": 1},           # no brand key at all
    ])
    rows = {r["sku"]: r for r in wh.select("select sku, brand, qty from supplier_staging")}
    assert rows["A"]["brand"] == "Toyo"
    assert rows["B"]["brand"] is None
    assert rows["B"]["qty"] == 1


def test_insert_many_serialises_dict_columns(wh):
    """validation_results.details is a json column fed a python dict."""
    run_id = _seed_run(wh)
    wh.insert_many("validation_results", [{
        "run_id": run_id, "check_name": "qty_delta:tires",
        "severity": "error", "passed": False, "details": {"prev_qty": 10, "cur_qty": 1},
    }])
    row = wh.select_one("select details from validation_results")
    assert "prev_qty" in str(row["details"])


# ── the mirror ────────────────────────────────────────────────────────────────

def _mirror_row(sku, run_id, **over):
    row = {"sku": sku, "product_id": f"p{sku}", "variant_id": f"v{sku}",
           "inventory_item_id": f"i{sku}", "handle": None, "title": None,
           "product_type": "Vehicle Tires", "tags": None,
           "current_price": 100.0, "current_qty": 5,
           "refreshed_at": "now()", "last_run_id": run_id}
    row.update(over)
    return row


def test_upsert_replaces_existing_mirror_row(wh):
    run_id = _seed_run(wh)
    wh.upsert_shopify_products([_mirror_row("A", run_id)])
    wh.upsert_shopify_products([_mirror_row("A", run_id, current_price=150.0, current_qty=9)])
    rows = wh.select("select sku, current_price::double as p, current_qty from shopify_products")
    assert len(rows) == 1
    assert rows[0]["p"] == pytest.approx(150.0)
    assert rows[0]["current_qty"] == 9


def test_upsert_resolves_the_now_sentinel(wh):
    """steps/catalog.py passes refreshed_at as the literal string "now()" —
    which Hasura accepts and a bound DuckDB parameter cannot."""
    run_id = _seed_run(wh)
    wh.upsert_shopify_products([_mirror_row("A", run_id)])
    assert wh.select_one("select refreshed_at from shopify_products")["refreshed_at"] is not None


def test_delete_stale_keeps_rows_from_this_run(wh):
    run_id = _seed_run(wh)
    wh.upsert_shopify_products([_mirror_row("A", run_id), _mirror_row("B", run_id - 1)])
    assert wh.delete_stale_shopify_products(run_id) == 1
    assert [r["sku"] for r in wh.select("select sku from shopify_products")] == ["A"]


def test_delete_stale_leaves_null_last_run_id_alone(wh):
    """Matches Hasura's `_neq`, where a null never satisfies the filter. The two
    backends must agree even on a case that should not occur."""
    run_id = _seed_run(wh)
    wh.upsert_shopify_products([_mirror_row("A", run_id, last_run_id=None)])
    assert wh.delete_stale_shopify_products(run_id) == 0


# ── dialect ───────────────────────────────────────────────────────────────────

def test_float_cast_is_double_precision(wh):
    """Postgres `::float` is float8; DuckDB's is float4. Untranslated, the price
    written to the sync_actions audit trail came back as 128.10000610351562
    instead of 128.1 — see _translate() in local_warehouse.py."""
    row = wh.select_one("select 128.10::numeric::float as p")
    assert row["p"] == pytest.approx(128.10, abs=1e-9)


def test_translate_leaves_sized_float_types_alone(wh):
    from core.local_warehouse import _translate
    assert _translate("select x::float from t")  == "select x::double from t"
    assert _translate("select x::float8 from t") == "select x::float8 from t"
    assert _translate("select float_col from t") == "select float_col from t"


# ── the merge rule, which is the reason all of this exists ────────────────────

def test_merge_sql_applies_markup_inside_the_min(wh):
    """The rule from steps/merge.py: markup is per supplier, so it must be added
    BEFORE min() picks a winner. Cheapest cost and cheapest price are different
    suppliers here — dttire is cheaper to buy (100 < 105) but wheelsmart is
    cheaper to sell (128.10 < 146.50). Adding markup after the min() would pick
    dttire and undercharge by comparison with the real rule.
    """
    run_id = _seed_run(wh)
    wh.insert_many("supplier_staging", [
        {"run_id": run_id, "supplier": "dttire", "product_type": "tires",
         "sku": "A", "price": 100.00, "qty": 5},
        {"run_id": run_id, "supplier": "wheelsmart", "product_type": "tires",
         "sku": "A", "price": 105.00, "qty": 3},
    ])
    # exactly the expression production/config.py builds via markup.sql_price_expr
    price_expr = ("round((case supplier "
                  "when 'dttire' then price + 46.50 "
                  "when 'wheelsmart' then price * 1.220000 end)::numeric, 2)")
    wh.run_sql(f"""
        insert into merged_snapshot (run_id, product_type, sku, merged_price, merged_qty, sources)
        select {run_id}, product_type, sku,
               min({price_expr}) filter (where price > 0),
               sum(qty),
               string_agg(distinct supplier, '|' order by supplier)
        from supplier_staging where run_id = {run_id}
        group by product_type, sku;
    """)
    row = wh.select_one(
        f"select merged_price::double as p, merged_qty, sources "
        f"from merged_snapshot where run_id = {run_id}")
    assert row["p"] == pytest.approx(128.10)      # wheelsmart, not dttire's 146.50
    assert row["merged_qty"] == 8                 # quantities sum across suppliers
    assert row["sources"] == "dttire|wheelsmart"


def test_merge_sql_ignores_zero_priced_rows(wh):
    """`filter (where price > 0)` — a supplier row with no cost must not win the
    min() at a price of just its markup."""
    run_id = _seed_run(wh)
    wh.insert_many("supplier_staging", [
        {"run_id": run_id, "supplier": "dttire", "product_type": "tires",
         "sku": "A", "price": 0, "qty": 5},
        {"run_id": run_id, "supplier": "wheelsmart", "product_type": "tires",
         "sku": "A", "price": 105.00, "qty": 3},
    ])
    price_expr = ("round((case supplier when 'dttire' then price + 46.50 "
                  "when 'wheelsmart' then price * 1.220000 end)::numeric, 2)")
    wh.run_sql(f"""
        insert into merged_snapshot (run_id, product_type, sku, merged_price, merged_qty, sources)
        select {run_id}, product_type, sku,
               min({price_expr}) filter (where price > 0), sum(qty),
               string_agg(distinct supplier, '|' order by supplier)
        from supplier_staging where run_id = {run_id} group by product_type, sku;
    """)
    row = wh.select_one(
        f"select merged_price::double as p from merged_snapshot where run_id = {run_id}")
    assert row["p"] == pytest.approx(128.10)


# ── the sync diff ─────────────────────────────────────────────────────────────

def _seed_diff_fixture(wh):
    run_id = _seed_run(wh)
    wh.upsert_shopify_products([
        _mirror_row("SAME",    run_id, current_price=110.00, current_qty=4),
        _mirror_row("REPRICE", run_id, current_price=110.00, current_qty=4),
        _mirror_row("GONE",    run_id, current_price=110.00, current_qty=4),
    ])
    wh.insert_many("merged_snapshot", [
        {"run_id": run_id, "product_type": "tires", "sku": "SAME",
         "merged_price": 110.00, "merged_qty": 4, "sources": "dttire"},
        {"run_id": run_id, "product_type": "tires", "sku": "REPRICE",
         "merged_price": 125.00, "merged_qty": 9, "sources": "dttire"},
    ])
    return run_id


def test_price_diff_selects_only_changed_skus(wh):
    """Copied from steps/sync.py::run_prices. An unchanged price must not be
    pushed — the diff is what keeps a weekly run at hundreds of writes rather
    than the whole catalogue."""
    run_id = _seed_diff_fixture(wh)
    diff = wh.select(f"""
        select ms.sku, round(ms.merged_price, 2)::float as new_price
        from merged_snapshot ms
        join shopify_products sp on sp.sku = ms.sku
        where ms.run_id = {run_id}
          and ms.product_type in ('tires')
          and ms.merged_price > 0
          and round(ms.merged_price, 2) between 20.0 and 5000.0
          and round(ms.merged_price, 2) is distinct from sp.current_price
        order by ms.sku;
    """)
    assert [r["sku"] for r in diff] == ["REPRICE"]
    assert diff[0]["new_price"] == pytest.approx(125.00)


def test_inventory_zero_out_targets_only_absent_skus(wh):
    """Copied from steps/sync.py::run_inventory. A mapped SKU that has dropped
    out of the snapshot is what inventory_zero exists for; one still present
    must not be zeroed."""
    run_id = _seed_diff_fixture(wh)
    zeros = wh.select(f"""
        select sp.sku from shopify_products sp
        where sp.product_type in ('Vehicle Tires')
          and coalesce(sp.current_qty, 0) <> 0
          and not exists (
              select 1 from merged_snapshot ms
              where ms.run_id = {run_id} and ms.sku = sp.sku)
        order by sp.sku;
    """)
    assert [r["sku"] for r in zeros] == ["GONE"]
