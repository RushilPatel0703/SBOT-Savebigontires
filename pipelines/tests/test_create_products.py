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
    def __init__(self):
        self.created = []

    def primary_location_id(self):
        return "gid://shopify/Location/1"

    def online_store_publication_id(self):
        return None                       # token without the publication scopes

    def create_product(self, **kw):
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
