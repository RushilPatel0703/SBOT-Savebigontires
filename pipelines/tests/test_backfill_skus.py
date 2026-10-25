"""Tests for production/backfill_skus.py and the Shopify client calls it and
create_products lean on (update_variants_batched, lookup_products_by_handle).

The backfill writes SKUs onto live store products, so what it decides to
touch is pinned down here. It must fill a blank SKU only when the handle,
the title and the SKU's absence elsewhere in the store all agree, and report
everything else.
"""
import logging
import sys
from pathlib import Path

import pytest

PRODUCTION = Path(__file__).resolve().parent.parent.parent / "production"

pytest.importorskip("pandas")

SEO_HEADER = ("Handle,Title,Body (HTML),Vendor,Product Category,Type,Tags,Published,"
              "Variant SKU,Variant Price,Image Src,SEO Title,SEO Description")


@pytest.fixture
def production(monkeypatch, tmp_path):
    """production/ importable, with SEO files resolved under tmp_path. Same
    sys.modules juggling as test_create_products.py: production's `config`
    must not leak into test files that expect pipelines/shared's."""
    def _ours(name):
        return (name in ("config", "backfill_skus")
                or name.split(".")[0] in ("core", "steps"))

    saved = {k: v for k, v in sys.modules.items() if _ours(k)}
    for name in saved:
        del sys.modules[name]
    monkeypatch.syspath_prepend(str(PRODUCTION))
    import config
    import backfill_skus
    from core import shopify_gql
    monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)

    class Modules:
        pass
    m = Modules()
    m.backfill, m.gql = backfill_skus, shopify_gql
    yield m
    for name in [k for k in sys.modules if _ours(k)]:
        del sys.modules[name]
    sys.modules.update(saved)


def _product(n, handle, title="Torque F014 18X85 5x114.3", product_type="Wheels"):
    return {"id": f"gid://shopify/Product/{n}", "handle": handle, "title": title,
            "productType": product_type}


def _variant(n, product_n, sku=None):
    return {"id": f"gid://shopify/ProductVariant/{n}", "sku": sku,
            "__parentId": f"gid://shopify/Product/{product_n}"}


def _seo(sku, title="Torque F014 18X85 5x114.3"):
    return (sku, {"Title": title})


# ── plan ───────────────────────────────────────────────────────────────────────
def test_plan_sets_sku_when_handle_title_and_store_agree(production):
    rows = production.backfill.plan(
        [_product(1, "torque-f014")], [_variant(1, 1, sku=None)],
        {"torque-f014": _seo("0148851143")})

    assert len(rows) == 1
    r = rows[0]
    assert (r["decision"], r["sku"]) == ("set_sku", "0148851143")
    assert r["variant_id"] == "gid://shopify/ProductVariant/1"


def test_plan_ignores_products_that_already_have_a_sku(production):
    rows = production.backfill.plan(
        [_product(1, "torque-f014")], [_variant(1, 1, sku="EXISTING")],
        {"torque-f014": _seo("0148851143")})

    assert rows == []                               # never overwrites a SKU


@pytest.mark.parametrize("products, variants, by_handle, ambiguous, reason", [
    ([_product(1, "h")], [_variant(1, 1), _variant(2, 1)], {"h": _seo("S")}, set(),
     "2 variants"),
    ([_product(1, "h")], [_variant(1, 1)], {}, set(),
     "handle not in the SEO files"),
    ([_product(1, "h")], [_variant(1, 1)], {"h": _seo("S", title="Torque HALO 17X75")}, set(),
     "title differs from the SEO file"),
    ([_product(1, "h"), _product(2, "other")], [_variant(1, 1), _variant(2, 2, sku="S")],
     {"h": _seo("S")}, set(),
     "SKU already on another store product"),
    ([_product(1, "h")], [_variant(1, 1)], {"h": _seo("S")}, {"h"},
     "handle belongs to several SKUs in the SEO files"),
])
def test_plan_skips_with_reason(production, products, variants, by_handle, ambiguous, reason):
    rows = production.backfill.plan(products, variants, by_handle, ambiguous)

    skipped = [r for r in rows if r["decision"] == "skip"]
    assert [r["reason"] for r in skipped] == [reason]
    assert not [r for r in rows if r["decision"] == "set_sku"]


def test_plan_title_match_ignores_case_and_spacing(production):
    rows = production.backfill.plan(
        [_product(1, "h", title="Torque  F014 18x85 5x114.3 ")], [_variant(1, 1)],
        {"h": _seo("S", title="Torque F014 18X85 5x114.3")})

    assert rows[0]["decision"] == "set_sku"


def test_seo_by_handle_flags_a_handle_shared_by_two_skus(production, tmp_path):
    path = tmp_path / "formula_distributors/Data/shopify/Shopify_SEO_wheels_20261024_000000.csv"
    path.parent.mkdir(parents=True)
    path.write_text(SEO_HEADER + "\n"
                    "shared,T,,V,,Wheels,,TRUE,A1,1,,,\n"
                    "shared,T,,V,,Wheels,,TRUE,A2,1,,,\n"
                    "solo,T,,V,,Wheels,,TRUE,B1,1,,,\n", encoding="utf-8")

    class Ctx:
        log = logging.getLogger("test_backfill_skus")
    by_handle, ambiguous = production.backfill.seo_by_handle(Ctx, ["wheels"])

    assert ambiguous == {"shared"}
    assert by_handle["solo"][0] == "B1"


# ── Shopify client ────────────────────────────────────────────────────────────
def test_update_prices_batched_still_sends_price_inputs(production, monkeypatch):
    # update_prices_batched now runs through update_variants_batched.
    client = production.gql.ShopifyGQL()
    calls = []

    def fake_execute(query, variables=None, label=None):
        calls.append((query, variables, label))
        return {"u0": {"userErrors": []},
                "u1": {"userErrors": [{"field": ["price"], "message": "bad"}]}}
    monkeypatch.setattr(client, "execute", fake_execute)

    results = client.update_prices_batched([("A", "P1", "V1", 12.5), ("B", "P2", "V2", 3)])

    assert results[0] == ("A", True, None)
    assert results[1][:2] == ("B", False) and "bad" in results[1][2]
    query, variables, label = calls[0]
    assert variables == {"p0": "P1", "v0": [{"id": "V1", "price": "12.50"}],
                         "p1": "P2", "v1": [{"id": "V2", "price": "3.00"}]}
    assert label == "update_prices_batched"
    assert "u1: productVariantsBulkUpdate(productId: $p1, variants: $v1)" in query


def test_update_variants_batched_marks_a_whole_failed_request(production, monkeypatch):
    client = production.gql.ShopifyGQL()

    def fake_execute(query, variables=None, label=None):
        raise production.gql.ShopifyError("HTTP 502")
    monkeypatch.setattr(client, "execute", fake_execute)

    results = client.update_variants_batched(
        [("V1", "P1", {"id": "V1", "inventoryItem": {"sku": "S1"}})])

    assert results == [("V1", False, "HTTP 502")]


def test_lookup_products_by_handle_returns_only_taken_handles(production, monkeypatch):
    client = production.gql.ShopifyGQL()
    seen = []

    def fake_execute(query, variables=None, label=None):
        seen.append(variables)
        return {"h0": {"id": "gid://shopify/Product/1", "title": "T",
                       "variants": {"nodes": [{"sku": None}]}},
                "h1": None}
    monkeypatch.setattr(client, "execute", fake_execute)

    found = client.lookup_products_by_handle(["taken", "free", "taken", ""])

    assert seen == [{"h0": "taken", "h1": "free"}]  # deduplicated, blanks dropped
    assert found == {"taken": {"product_id": "gid://shopify/Product/1",
                               "title": "T", "skus": [""]}}
