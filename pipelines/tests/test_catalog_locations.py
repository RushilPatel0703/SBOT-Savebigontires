"""Tests for the catalog step's two guards and per-location inventory sync.

Run #13 (2026-10-30) got an empty store export, wiped all 15,170 mirror rows,
passed every gate on nothing and finished "success". The shrink gate must stop
that before the mirror is touched.

The store has one stock location plus nine stores, and Shopify adds them
together. current_qty has to be the stock location's quantity, and units at a
store have to go to 0, or the website shows more stock than the suppliers have.

These run the real steps against a real DuckDB warehouse, with a fake Shopify.
"""
import logging
import sys
from pathlib import Path

import pytest

PRODUCTION = Path(__file__).resolve().parent.parent.parent / "production"
pytest.importorskip("duckdb", reason="local warehouse backend needs duckdb")

STOCK, STORE_A, STORE_B = ("gid://shopify/Location/1", "gid://shopify/Location/2",
                           "gid://shopify/Location/3")


@pytest.fixture
def prod(monkeypatch, tmp_path):
    def _ours(name):
        return name == "config" or name.split(".")[0] in ("core", "steps")

    saved = {k: v for k, v in sys.modules.items() if _ours(k)}
    for name in saved:
        del sys.modules[name]
    monkeypatch.syspath_prepend(str(PRODUCTION))
    import config
    from core.local_warehouse import LocalWarehouse
    from core.run_context import RunContext
    from steps import catalog, sync

    wh = LocalWarehouse(path=tmp_path / "test.duckdb")
    wh.migrate()

    def new_ctx(mode="live", force=False):
        ctx = RunContext(mode=mode, product_types=["tires"], warehouse=wh,
                         logger=logging.getLogger("test_catalog_locations"))
        ctx.force = force
        return ctx

    yield type("P", (), {"config": config, "catalog": catalog, "sync": sync,
                         "wh": wh, "new_ctx": staticmethod(new_ctx)})
    wh.close()
    for name in [k for k in sys.modules if _ours(k)]:
        del sys.modules[name]
    sys.modules.update(saved)


class FakeShopify:
    def __init__(self, skus, levels, primary_name="Supplier Ordering Stock"):
        self.skus = skus                    # sku → total inventoryQuantity
        self.levels = levels                # sku → {location: qty}
        self.primary_name = primary_name
        self.writes = []                    # (location, [(item, qty)])

    def fetch_catalog(self):
        products = [{"id": "gid://shopify/Product/1", "handle": "h", "title": "t",
                     "productType": "Vehicle Tires", "tags": []}]
        variants = [{"id": f"gid://shopify/ProductVariant/{s}", "sku": s, "price": "100.00",
                     "inventoryQuantity": total, "inventoryItem": {"id": f"item-{s}"},
                     "__parentId": "gid://shopify/Product/1"}
                    for s, total in self.skus.items()]
        return products, variants

    def fetch_inventory_levels(self):
        locations = [{"id": STOCK, "name": "Supplier Ordering Stock", "isPrimary": True},
                     {"id": STORE_A, "name": "Kamloops", "isPrimary": False},
                     {"id": STORE_B, "name": "Langley", "isPrimary": False}]
        return locations, {f"item-{s}": dict(lv) for s, lv in self.levels.items()}

    def primary_location_id(self):
        import config
        from core.shopify_gql import ShopifyError
        if config.SHOPIFY_STOCK_LOCATION and self.primary_name != config.SHOPIFY_STOCK_LOCATION:
            raise ShopifyError("primary is not the stock location")
        return STOCK

    def set_inventory_quantities(self, location_gid, items):
        self.writes.append((location_gid, list(items)))

    def activate_inventory_batched(self, location_gid, items):
        self.activations = getattr(self, "activations", []) + [(location_gid, list(items))]
        return [(gid, True, None) for gid, _ in items]

    def cost_summary(self):
        return {}


def _seed_snapshot(wh, run_id, rows):
    wh.insert_many("merged_snapshot", [
        {"run_id": run_id, "product_type": "tires", "sku": s, "merged_price": 100,
         "merged_qty": q, "sources": "dttire", "winning_supplier": "dttire"}
        for s, q in rows.items()])


def _mirror(wh):
    return {r["sku"]: r["current_qty"] for r in
            wh.select("select sku, current_qty from shopify_products order by sku")}


# ── shrink gate ───────────────────────────────────────────────────────────────
def test_empty_export_fails_and_keeps_the_mirror(prod):
    full = FakeShopify({"A": 4, "B": 8}, {"A": {STOCK: 4}, "B": {STOCK: 8}})
    prod.catalog.run(prod.new_ctx(), full)
    assert _mirror(prod.wh) == {"A": 4, "B": 8}

    with pytest.raises(RuntimeError, match="fewer than"):
        prod.catalog.run(prod.new_ctx(), FakeShopify({}, {}))
    assert _mirror(prod.wh) == {"A": 4, "B": 8}       # untouched


def test_force_lets_an_intentional_wipe_through(prod):
    prod.catalog.run(prod.new_ctx(), FakeShopify({"A": 4}, {"A": {STOCK: 4}}))
    prod.catalog.run(prod.new_ctx(force=True), FakeShopify({}, {}))
    assert _mirror(prod.wh) == {}


def test_first_run_on_an_empty_mirror_passes(prod):
    prod.catalog.run(prod.new_ctx(), FakeShopify({"A": 4}, {"A": {STOCK: 4}}))
    assert _mirror(prod.wh) == {"A": 4}


def test_small_shrink_passes(prod):
    skus = {s: 1 for s in "ABCDEFGHIJ"}
    prod.catalog.run(prod.new_ctx(), FakeShopify(skus, {s: {STOCK: 1} for s in skus}))
    nine = {s: 1 for s in "ABCDEFGHI"}                  # 10% fewer
    prod.catalog.run(prod.new_ctx(), FakeShopify(nine, {s: {STOCK: 1} for s in nine}))
    assert len(_mirror(prod.wh)) == 9


# ── per-location stock ────────────────────────────────────────────────────────
def test_current_qty_is_the_stock_location_not_the_total(prod):
    shop = FakeShopify({"A": 13}, {"A": {STOCK: 4, STORE_A: 9}})
    ctx = prod.new_ctx()
    prod.catalog.run(ctx, shop)
    assert _mirror(prod.wh) == {"A": 4}
    assert [(o["sku"], o["location"], o["qty"]) for o in ctx.offsite_stock] == [("A", "Kamloops", 9)]


def test_primary_must_be_the_stock_location(prod):
    shop = FakeShopify({"A": 4}, {"A": {STOCK: 4}}, primary_name="Shop location")
    with pytest.raises(Exception, match="stock location"):
        prod.catalog.run(prod.new_ctx(), shop)


def test_sync_zeroes_store_units_for_supplier_skus_only(prod):
    # A is a supplier SKU with units at two stores; M is managed by hand (not in
    # the snapshot) and keeps its store stock.
    shop = FakeShopify({"A": 20, "M": 3},
                       {"A": {STOCK: 4, STORE_A: 9, STORE_B: 7}, "M": {STORE_A: 3}})
    ctx = prod.new_ctx()
    prod.catalog.run(ctx, shop)
    _seed_snapshot(prod.wh, ctx.run_id, {"A": 6})
    prod.sync.run_inventory(ctx, shop, dry_run=False)

    by_loc = {loc: items for loc, items in shop.writes}
    assert by_loc[STOCK] == [("item-A", 6)]
    assert by_loc[STORE_A] == [("item-A", 0)]
    assert by_loc[STORE_B] == [("item-A", 0)]
    assert all(item != "item-M" for _, items in shop.writes for item, _ in items)

    actions = prod.wh.select(f"select action, sku from sync_actions where run_id = {ctx.run_id} "
                             "order by action, sku")
    assert [(a["action"], a["sku"]) for a in actions] == [
        ("inventory_set", "A"), ("inventory_store_zero", "A"), ("inventory_store_zero", "A")]


def test_settled_store_makes_no_writes(prod):
    """Once the stock location matches and the stores are 0, the diff is empty.
    With the old total-based current_qty it never settled."""
    shop = FakeShopify({"A": 4}, {"A": {STOCK: 4, STORE_A: 0}})
    ctx = prod.new_ctx()
    prod.catalog.run(ctx, shop)
    _seed_snapshot(prod.wh, ctx.run_id, {"A": 4})
    prod.sync.run_inventory(ctx, shop, dry_run=False)
    assert shop.writes == []


def test_dry_run_counts_store_zeros_without_writing(prod):
    shop = FakeShopify({"A": 13}, {"A": {STOCK: 4, STORE_A: 9}})
    ctx = prod.new_ctx(mode="dry_run")
    prod.catalog.run(ctx, shop)
    _seed_snapshot(prod.wh, ctx.run_id, {"A": 4})
    prod.sync.run_inventory(ctx, shop, dry_run=True)
    assert shop.writes == []
    rows = prod.wh.select(f"select action, status from sync_actions where run_id = {ctx.run_id}")
    assert [(r["action"], r["status"]) for r in rows] == [("inventory_store_zero", "dry_run")]


def test_client_credentials_read_the_box_names(monkeypatch):
    for k in ("SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SBOT_SHOPIFY_CLIENT_ID", "id-from-box")
    monkeypatch.setenv("SBOT_SHOPIFY_CLIENT_SECRET", "secret-from-box")
    monkeypatch.syspath_prepend(str(PRODUCTION))
    saved = sys.modules.pop("config", None)
    try:
        import config
        assert config.SHOPIFY_CLIENT_ID == "id-from-box"
        assert config.SHOPIFY_CLIENT_SECRET == "secret-from-box"
    finally:
        sys.modules.pop("config", None)
        if saved is not None:
            sys.modules["config"] = saved


def test_item_not_stocked_at_the_stock_location_is_activated(prod):
    """6,913 dev-store SKUs (2026-11-20) were stocked only at the old primary,
    'Shop location'. inventorySetQuantities refuses those with
    ITEM_NOT_STOCKED_AT_LOCATION, so they must be activated there instead."""
    shop = FakeShopify({"A": 0, "B": 2}, {"A": {STORE_A: 0}, "B": {STOCK: 2}})
    ctx = prod.new_ctx()
    prod.catalog.run(ctx, shop)
    assert ctx.unstocked_items == {"item-A"}
    _seed_snapshot(prod.wh, ctx.run_id, {"A": 5, "B": 3})
    prod.sync.run_inventory(ctx, shop, dry_run=False)

    assert shop.activations == [(STOCK, [("item-A", 5)])]
    assert shop.writes == [(STOCK, [("item-B", 3)])]
    actions = prod.wh.select(f"select action, sku from sync_actions where run_id = {ctx.run_id} "
                             "order by sku")
    assert [(a["action"], a["sku"]) for a in actions] == [
        ("inventory_activate", "A"), ("inventory_set", "B")]
