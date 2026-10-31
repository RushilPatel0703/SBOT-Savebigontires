"""Tests for production/steps/create_products.py — the live (non-dry-run) branch.

A dry run never reaches the shopify.create_product(...) call, so a wrong SEO
column name there survived every dry run since the initial commit: the step
read definition["URL Handle"] while every Shopify_SEO_*.csv has Shopify's
standard "Handle" column, and the first live run died with KeyError before a
single product was created. These tests drive the live branch end to end with
a fake Shopify client and an SEO file written with the real header row.
"""
import logging
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

PRODUCTION = Path(__file__).resolve().parent.parent.parent / "production"

pytest.importorskip("pandas")

# The header row the enrichment step writes (Shopify's product CSV format).
SEO_HEADER = ("Handle,Title,Body (HTML),Vendor,Product Category,Type,Tags,Published,"
              "Variant SKU,Variant Price,Image Src,SEO Title,SEO Description")


@pytest.fixture
def create_products(monkeypatch, tmp_path):
    """The create_products module with production/ importable and its SEO files
    resolved under tmp_path. production/ and pipelines/shared/ both have a
    `config` module, so production must come first on sys.path — and the
    production modules must be dropped again afterwards, or later test files
    that `import config` get production's instead of pipelines/shared's."""
    def _ours(name):
        return name == "config" or name.split(".")[0] in ("core", "steps")

    saved = {k: v for k, v in sys.modules.items() if _ours(k)}
    for name in saved:
        del sys.modules[name]
    monkeypatch.syspath_prepend(str(PRODUCTION))
    import config
    from steps import create_products as cp
    monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
    yield cp
    for name in [k for k in sys.modules if _ours(k)]:
        del sys.modules[name]
    sys.modules.update(saved)


class _Step:
    def __init__(self):
        self.rows_in = self.rows_out = None
        self.details = {}


class FakeCtx:
    def __init__(self, new_rows):
        self.run_id = 1
        self.product_types = ["tires"]
        self.log = logging.getLogger("test_create_products")
        self.actions, self.validations, self.mirror = [], [], []
        self.step_obj = _Step()
        new = new_rows
        mirror = self.mirror

        class _Wh:
            def select(self, sql):
                return list(new)

            def insert_many(self, table, rows):
                assert table == "shopify_products"
                mirror.extend(rows)

        self.wh = _Wh()

    @contextmanager
    def step(self, name):
        yield self.step_obj

    def record_validation(self, name, passed, severity, details):
        self.validations.append((name, passed))

    def record_sync_actions(self, actions):
        self.actions.extend(actions)


class FakeShopify:
    def __init__(self, taken=None, fail_with=None):
        self.created = []
        self.taken = taken or {}          # handle → {product_id, title, skus}
        self.fail_with = fail_with        # exception every create_product raises

    def primary_location_id(self):
        return "gid://shopify/Location/1"

    def online_store_publication_id(self):
        return None                       # token without the publication scopes

    def lookup_products_by_handle(self, handles):
        return {h: self.taken[h] for h in handles if h in self.taken}

    def create_product(self, **kw):
        if self.fail_with:
            raise self.fail_with
        self.created.append(kw)
        n = len(self.created)
        return {"product_id": f"gid://shopify/Product/{n}",
                "variant_id": f"gid://shopify/ProductVariant/{n}",
                "inventory_item_id": f"gid://shopify/InventoryItem/{n}",
                "handle": kw["handle"]}


def _write_seo(tmp_path, rows):
    path = tmp_path / "formula_distributors/Data/shopify/Shopify_SEO_tires_20261012_000000.csv"
    path.parent.mkdir(parents=True)
    path.write_text(SEO_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def test_live_branch_creates_product_from_standard_seo_header(create_products, tmp_path):
    _write_seo(tmp_path, [
        "falken-wildpeak-at3w-lt285-70r17,Falken Wildpeak A/T3W LT285/70R17,<p>x</p>,"
        "Falken,,Tires,\"tires, falken\",TRUE,28037115,399.99,https://img/x.jpg,SEO t,SEO d",
    ])
    ctx = FakeCtx([{"product_type": "tires", "sku": "28037115", "price": 412.5, "qty": 8}])
    shopify = FakeShopify()

    created = create_products.run(ctx, shopify, dry_run=False)

    assert created == 1
    assert len(shopify.created) == 1
    kw = shopify.created[0]
    assert kw["handle"] == "falken-wildpeak-at3w-lt285-70r17"
    assert kw["title"] == "Falken Wildpeak A/T3W LT285/70R17"
    assert kw["sku"] == "28037115"
    assert kw["price"] == 412.5            # merged_price, not the SEO file's Variant Price
    assert kw["tags"] == ["tires", "falken"]
    assert [a["status"] for a in ctx.actions] == ["applied"]
    assert ctx.mirror[0]["handle"] == "falken-wildpeak-at3w-lt285-70r17"


def test_live_branch_skips_sku_without_seo_definition(create_products, tmp_path):
    _write_seo(tmp_path, [
        "h1,T1,,Falken,,Tires,,TRUE,111,1,,,",
    ])
    ctx = FakeCtx([{"product_type": "tires", "sku": "999", "price": 200.0, "qty": 4}])
    shopify = FakeShopify()

    assert create_products.run(ctx, shopify, dry_run=False) == 0
    assert shopify.created == []
    assert ctx.step_obj.details["no_seo_definition"] == 1


# ── taken handles (run #11) ──────────────────────────────────────────────────
# Run #11 tripped the circuit breaker on 94 creates that Shopify refused because
# another product already held the handle: 72 imported wheels with no SKU and
# 22 tires listed under a different supplier's SKU. Those are skips, not
# failures, and the breaker must not count them.

def _seo_row(handle, sku, title="T"):
    return f"{handle},{title},,Falken,,Tires,,TRUE,{sku},1,,,"


def test_sku_whose_handle_a_store_product_holds_is_skipped_not_sent(create_products, tmp_path):
    _write_seo(tmp_path, [_seo_row("held", "111"), _seo_row("free", "222")])
    ctx = FakeCtx([{"product_type": "tires", "sku": "111", "price": 200.0, "qty": 4},
                   {"product_type": "tires", "sku": "222", "price": 200.0, "qty": 4}])
    shopify = FakeShopify(taken={"held": {"product_id": "gid://shopify/Product/9",
                                          "title": "T", "skus": [""]}})

    assert create_products.run(ctx, shopify, dry_run=False) == 1

    assert [kw["sku"] for kw in shopify.created] == ["222"]
    skipped = ctx.actions[0]
    assert (skipped["sku"], skipped["status"]) == ("111", "skipped")
    assert skipped["error"].startswith("handle taken: held")
    assert "gid://shopify/Product/9" in skipped["error"] and "no SKU" in skipped["error"]
    details = ctx.step_obj.details
    assert details["skipped_handle_taken"] == 1
    assert details["handle_held_by_product_without_sku"] == 1
    assert details["failed"] == 0


def test_second_sku_sharing_a_handle_within_the_run_is_skipped(create_products, tmp_path):
    # 10905NX and 10905NXS map to one handle; whichever sorts first claims it.
    _write_seo(tmp_path, [_seo_row("nexen-winspike", "10905NX"),
                          _seo_row("nexen-winspike", "10905NXS")])
    ctx = FakeCtx([{"product_type": "tires", "sku": "10905NX", "price": 200.0, "qty": 4},
                   {"product_type": "tires", "sku": "10905NXS", "price": 200.0, "qty": 4}])
    shopify = FakeShopify()

    assert create_products.run(ctx, shopify, dry_run=False) == 1

    assert [kw["sku"] for kw in shopify.created] == ["10905NX"]
    assert ctx.actions[1]["status"] == "skipped"
    assert "SKU 10905NX, earlier in this run" in ctx.actions[1]["error"]


def test_dry_run_previews_the_handle_skips(create_products, tmp_path):
    # Run #10's dry run said all 2,873 creates were fine. Run #11 then failed on
    # 94 of them.
    _write_seo(tmp_path, [_seo_row("held", "111"), _seo_row("free", "222")])
    ctx = FakeCtx([{"product_type": "tires", "sku": "111", "price": 200.0, "qty": 4},
                   {"product_type": "tires", "sku": "222", "price": 200.0, "qty": 4}])
    shopify = FakeShopify(taken={"held": {"product_id": "gid://shopify/Product/9",
                                          "title": "T", "skus": ["OTHER-SKU"]}})

    assert create_products.run(ctx, shopify, dry_run=True) == 1

    assert shopify.created == []
    assert [a["status"] for a in ctx.actions] == ["skipped", "dry_run"]
    assert "OTHER-SKU" in ctx.actions[0]["error"]


def test_tripped_breaker_still_records_and_logs_every_attempt(create_products, tmp_path, caplog):
    # The breaker used to raise before the final flush, which threw away the
    # pending batch (up to 99 actions) along with the failures that tripped it.
    # Failed creates were never logged either.
    _write_seo(tmp_path, [_seo_row(f"h{i}", f"{1000 + i}") for i in range(60)])
    ctx = FakeCtx([{"product_type": "tires", "sku": f"{1000 + i}", "price": 200.0, "qty": 4}
                   for i in range(60)])
    shopify = FakeShopify(fail_with=create_products.ShopifyError("productSet userErrors: boom"))

    with caplog.at_level(logging.WARNING, logger="test_create_products"):
        with pytest.raises(RuntimeError, match="circuit breaker tripped: 50/50"):
            create_products.run(ctx, shopify, dry_run=False)

    assert len(ctx.actions) == 50                  # the tripping attempt included
    assert {a["status"] for a in ctx.actions} == {"failed"}
    assert ctx.actions[-1]["sku"] == "1049"
    failed_lines = [r.message for r in caplog.records if "create failed SKU" in r.message]
    assert len(failed_lines) == 50 and "boom" in failed_lines[0]
    assert ctx.step_obj.details["failed"] == 50
