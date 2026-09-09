"""Tests for per-supplier markup (shared/markup.py).

The client edits these values by hand before a run, so the failure modes worth
guarding are the quiet ones: a bad hand-edit that falls back to the old numbers
instead of stopping, a percentage that reaches one pricing layer but not the
other, and the min() in merge.py picking the cheapest *cost* rather than the
cheapest *price* once suppliers stop sharing a single flat markup.
"""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
import markup as M  # noqa: E402


def _write(tmp_path, suppliers):
    p = tmp_path / "markup_overrides.json"
    p.write_text(json.dumps({"version": 1, "suppliers": suppliers}), encoding="utf-8")
    return p


# ── The two modes ─────────────────────────────────────────────────────────────

def test_dollar_mode_adds_to_cost():
    rules = {"s": {"mode": "dollar", "value": 46.50}}
    assert M.price_with_markup(100.0, "s", rules) == 146.50


def test_percent_mode_multiplies_cost():
    rules = {"s": {"mode": "percent", "value": 22.0}}
    assert M.price_with_markup(100.0, "s", rules) == pytest.approx(122.0)


def test_each_supplier_uses_only_its_own_mode():
    """Either/or, never both: the dollar supplier must not pick up a percentage
    from its neighbour, or vice versa."""
    rules = {"d": {"mode": "dollar", "value": 40.0},
             "p": {"mode": "percent", "value": 40.0}}
    assert M.price_with_markup(200.0, "d", rules) == 240.0
    assert M.price_with_markup(200.0, "p", rules) == pytest.approx(280.0)


def test_percent_scales_with_cost_but_dollar_does_not():
    rules = {"d": {"mode": "dollar", "value": 50.0},
             "p": {"mode": "percent", "value": 50.0}}
    # cheap item: the flat markup is worth more
    assert M.price_with_markup(50.0, "d", rules) == 100.0
    assert M.price_with_markup(50.0, "p", rules) == pytest.approx(75.0)
    # expensive item: the percentage overtakes it
    assert M.price_with_markup(500.0, "d", rules) == 550.0
    assert M.price_with_markup(500.0, "p", rules) == pytest.approx(750.0)


def test_works_on_a_pandas_series():
    """augment_*.py prices a whole column at once — the same call must vectorise."""
    rules = {"p": {"mode": "percent", "value": 25.0},
             "d": {"mode": "dollar", "value": 46.50}}
    costs = pd.Series([100.0, 250.0, 0.0])
    assert list(M.price_with_markup(costs, "p", rules).round(2)) == [125.0, 312.5, 0.0]
    assert list(M.price_with_markup(costs, "d", rules).round(2)) == [146.5, 296.5, 46.5]


# ── merge.py: markup must stay inside the min() ───────────────────────────────

def test_percent_can_change_which_supplier_is_cheapest():
    """The reason merge.py applies markup inside min(). Supplier B has the higher
    cost but the lower selling price, so pricing off cost alone would overcharge.
    """
    rules = {"a": {"mode": "dollar", "value": 46.50},
             "b": {"mode": "percent", "value": 10.0}}
    cost_a, cost_b = 100.0, 105.0
    assert cost_a < cost_b                                    # A is the cheaper cost
    price_a = M.price_with_markup(cost_a, "a", rules)          # 146.50
    price_b = M.price_with_markup(cost_b, "b", rules)          # 115.50
    assert price_b < price_a                                   # but B is the cheaper price


def test_sql_expr_renders_both_modes():
    rules = {"a": {"mode": "dollar", "value": 46.50},
             "b": {"mode": "percent", "value": 22.0}}
    sql = M.sql_price_expr("price", rules)
    assert "when 'a' then price + 46.50" in sql
    assert "when 'b' then price * 1.220000" in sql
    assert sql.startswith("round(") and "::numeric, 2)" in sql


def test_sql_expr_covers_only_the_staged_suppliers():
    rules = {"a": {"mode": "dollar", "value": 1.0},
             "unstaged": {"mode": "dollar", "value": 999.0}}
    sql = M.sql_price_expr("price", rules, suppliers=["a"])
    assert "'a'" in sql and "unstaged" not in sql


def test_sql_expr_has_no_else_branch():
    """An unmatched supplier must yield NULL (dropped by min()) rather than being
    silently priced at cost — merge.py checks the staged list before calling."""
    sql = M.sql_price_expr("price", {"a": {"mode": "dollar", "value": 1.0}})
    assert " else " not in sql


# ── Loading: a bad edit must stop the run, not fall back ──────────────────────

def test_missing_file_falls_back_to_defaults(tmp_path):
    rules = M.load(tmp_path / "nope.json")
    assert rules == M.DEFAULT_RULES


def test_override_replaces_only_the_named_supplier(tmp_path):
    p = _write(tmp_path, {"dttire": {"mode": "percent", "value": 30.0}})
    rules = M.load(p)
    assert rules["dttire"] == {"mode": "percent", "value": 30.0}
    assert rules["emerge"] == M.DEFAULT_RULES["emerge"]


def test_malformed_json_raises_instead_of_using_defaults(tmp_path):
    """The dangerous case: the client edits markups, the file won't parse, and the
    run quietly prices off the old table anyway."""
    p = tmp_path / "markup_overrides.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(M.MarkupConfigError):
        M.load(p)


def test_missing_suppliers_key_raises(tmp_path):
    p = tmp_path / "markup_overrides.json"
    p.write_text(json.dumps({"dttire": {"mode": "dollar", "value": 1.0}}), encoding="utf-8")
    with pytest.raises(M.MarkupConfigError):
        M.load(p)


@pytest.mark.parametrize("bad", [
    {"mode": "pct", "value": 5.0},          # not a real mode
    {"mode": "percent", "value": -1.0},     # would sell below cost
    {"mode": "dollar", "value": -0.01},
    {"mode": "percent", "value": 5000.0},   # 50x typo
    {"mode": "dollar", "value": 99999.0},
    {"mode": "dollar", "value": "46.50"},   # string, not a number
    {"mode": "dollar", "value": True},      # bool is not a markup
    {"mode": "dollar"},                     # no value
    {"value": 46.50},                       # no mode
    "46.50",                                # not an object
])
def test_invalid_rules_are_rejected(tmp_path, bad):
    p = _write(tmp_path, {"dttire": bad})
    with pytest.raises(M.MarkupConfigError):
        M.load(p)


def test_zero_markup_is_allowed(tmp_path):
    """Selling at cost is a legitimate choice; only negatives are refused."""
    p = _write(tmp_path, {"dttire": {"mode": "dollar", "value": 0.0}})
    assert M.price_with_markup(100.0, "dttire", M.load(p)) == 100.0


def test_unknown_supplier_raises_rather_than_defaulting():
    with pytest.raises(KeyError):
        M.rule_for("nosuchsupplier", M.DEFAULT_RULES)


# ── Saving ────────────────────────────────────────────────────────────────────

def test_save_load_round_trip(tmp_path):
    p = tmp_path / "markup_overrides.json"
    M.save({"dttire": {"mode": "percent", "value": 18.5},
            "emerge": {"mode": "dollar", "value": 12.0}}, p)
    rules = M.load(p)
    assert rules["dttire"] == {"mode": "percent", "value": 18.5}
    assert rules["emerge"] == {"mode": "dollar", "value": 12.0}


def test_save_rejects_a_bad_rule_without_writing(tmp_path):
    p = tmp_path / "markup_overrides.json"
    M.save({"dttire": {"mode": "dollar", "value": 10.0}}, p)
    with pytest.raises(M.MarkupConfigError):
        M.save({"dttire": {"mode": "percent", "value": -5.0}}, p)
    assert M.load(p)["dttire"] == {"mode": "dollar", "value": 10.0}   # unchanged


def test_save_accepts_ints(tmp_path):
    """The dashboard's number_input can hand back an int for a whole number."""
    p = tmp_path / "markup_overrides.json"
    M.save({"dttire": {"mode": "percent", "value": 20}}, p)
    assert M.load(p)["dttire"]["value"] == 20.0


# ── Helpers used by QA and the UI ─────────────────────────────────────────────

def test_price_floor_is_the_markup_for_dollar_and_zero_for_percent():
    """qa_check flags rows at or below this. A zero cost under a dollar markup
    still produces the bare markup; under a percent markup it produces 0."""
    rules = {"d": {"mode": "dollar", "value": 36.50},
             "p": {"mode": "percent", "value": 36.5}}
    assert M.price_floor("d", rules) == 36.50
    assert M.price_floor("p", rules) == 0.0
    assert M.price_with_markup(0.0, "d", rules) == M.price_floor("d", rules)
    assert M.price_with_markup(0.0, "p", rules) == M.price_floor("p", rules)


def test_describe_reads_as_currency_or_percent():
    rules = {"d": {"mode": "dollar", "value": 46.5},
             "p": {"mode": "percent", "value": 22.0}}
    assert M.describe("d", rules) == "$46.50"
    assert M.describe("p", rules) == "+22%"


def test_defaults_cover_every_supplier_the_production_run_ingests():
    """production/config.py SUPPLIERS keys must all have a markup, or merge.py
    refuses to run."""
    for supplier in ("northcountry", "dttire", "formula", "wheelsmart", "emerge"):
        assert supplier in M.DEFAULT_RULES
