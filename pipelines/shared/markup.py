#!/usr/bin/env python3
"""Per-supplier markup: the single definition of how cost becomes selling price.

The client sets these by hand before a run (dashboard → "Markup per supplier"),
so the values live in a JSON file rather than in code:

    pipelines/shared/Data/markup_overrides.json

Two separate layers price from this, and they MUST agree:

  A. production/steps/merge.py — builds SQL from sql_price_expr() and applies
     the markup inside min(), so merged_snapshot.merged_price is FINAL.
  B. pipelines/*/augment_*.py  — call price_with_markup() to fill
     "Variant Price" in the Shopify import CSVs.

Historically each layer kept its own copy of the markup table and a comment
telling the next person to keep them in sync. They are both fed from this
module now specifically so that a client edit cannot land in one and not the
other.

Each supplier has ONE mode, never both:

    {"mode": "dollar",  "value": 46.50}  → price = cost + 46.50
    {"mode": "percent", "value": 22.0}   → price = cost * 1.22

Rounding is deliberately NOT done here — callers round at the point they
render a price (``.round(2)`` on a pandas Series, ``round(...)`` in SQL), and
rounding twice on the percent path shifts half-cents.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OVERRIDES_FILE = os.path.join(HERE, "Data", "markup_overrides.json")

DOLLAR = "dollar"
PERCENT = "percent"
MODES = (DOLLAR, PERCENT)

# Baseline used when no override file exists yet — the flat per-supplier rates
# the client set in July 2026, expressed in the new shape. Editing markups is
# the client's job now, so treat this as the "fresh checkout" fallback rather
# than a value to keep updating by hand.
DEFAULT_RULES = {
    "northcountry": {"mode": DOLLAR, "value": 46.50},
    "dttire":       {"mode": DOLLAR, "value": 46.50},
    "formula":      {"mode": DOLLAR, "value": 46.50},
    "wheelsmart":   {"mode": DOLLAR, "value": 36.50},
    "emerge":       {"mode": DOLLAR, "value": 36.50},
    "wheelspro":    {"mode": DOLLAR, "value": 46.50},  # no pipeline yet — kept for when it lands
}

# A percent markup above this is almost certainly a typo (220 meant as 22.0),
# and it would multiply every price on that supplier's feed. Refuse it.
MAX_PERCENT = 300.0
MAX_DOLLAR = 1000.0


class MarkupConfigError(Exception):
    """Raised when the override file is present but not usable.

    Deliberately fatal rather than falling back to DEFAULT_RULES: a client who
    edited markups and got the old numbers anyway would not find out until the
    wrong prices were live in Shopify.
    """


def _validate_rule(supplier, rule):
    """Return a clean {"mode","value"} or raise MarkupConfigError."""
    if not isinstance(rule, dict):
        raise MarkupConfigError(
            f"Markup for {supplier!r} must be an object like "
            f'{{"mode": "dollar", "value": 46.50}}, got {rule!r}')

    mode = rule.get("mode")
    if mode not in MODES:
        raise MarkupConfigError(
            f"Markup for {supplier!r} has mode {mode!r}; expected one of {list(MODES)}.")

    value = rule.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MarkupConfigError(
            f"Markup value for {supplier!r} must be a number, got {value!r}.")
    value = float(value)

    if value < 0:
        raise MarkupConfigError(
            f"Markup for {supplier!r} is negative ({value}) — that would sell below cost.")
    limit = MAX_PERCENT if mode == PERCENT else MAX_DOLLAR
    if value > limit:
        unit = "%" if mode == PERCENT else "$"
        raise MarkupConfigError(
            f"Markup for {supplier!r} is {unit}{value}, above the {unit}{limit} sanity "
            f"limit. If that is genuinely intended, raise the limit in "
            f"pipelines/shared/markup.py.")

    return {"mode": mode, "value": value}


def load(path=None):
    """Merged markup table: DEFAULT_RULES overlaid with the client's file.

    A missing file is fine (fresh checkout → defaults). A file that exists but
    is malformed raises, so a bad hand-edit stops the run instead of quietly
    pricing off the defaults.
    """
    path = os.fspath(path or OVERRIDES_FILE)   # accept a Path as well as a str
    rules = {s: dict(r) for s, r in DEFAULT_RULES.items()}

    if not os.path.exists(path):
        return rules

    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise MarkupConfigError(f"Could not read markup overrides at {path}: {e}") from None

    if not isinstance(doc, dict) or not isinstance(doc.get("suppliers"), dict):
        raise MarkupConfigError(
            f'{path} must be an object with a "suppliers" object, e.g. '
            f'{{"suppliers": {{"dttire": {{"mode": "dollar", "value": 46.50}}}}}}')

    for supplier, rule in doc["suppliers"].items():
        rules[supplier] = _validate_rule(supplier, rule)
    return rules


def save(rules, path=None):
    """Write the markup table the client just edited. Validates before writing."""
    path = os.fspath(path or OVERRIDES_FILE)   # accept a Path as well as a str
    clean = {s: _validate_rule(s, r) for s, r in rules.items()}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "suppliers": clean}, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)   # atomic — a crash mid-write can't leave a half file
    return clean


def rule_for(supplier, rules=None):
    """The markup rule for a supplier, raising on an unknown one.

    Never defaults: a silently-wrong markup misprices every SKU that supplier
    feeds, which is far worse than a loud failure.
    """
    rules = load() if rules is None else rules
    try:
        return rules[supplier]
    except KeyError:
        raise KeyError(
            f"No markup configured for supplier {supplier!r}. "
            f"Known suppliers: {sorted(rules)}") from None


def price_with_markup(cost, supplier, rules=None):
    """Selling price from cost. Works on a scalar or a pandas Series alike.

    Caller rounds. See the module docstring for why.
    """
    rule = rule_for(supplier, rules)
    if rule["mode"] == PERCENT:
        return cost * (1.0 + rule["value"] / 100.0)
    return cost + rule["value"]


def price_floor(supplier, rules=None):
    """Lowest price this supplier's markup alone can produce, i.e. the price of
    a zero-cost item. Used by QA to catch rows priced off a missing cost.

    Dollar mode floors at the markup; percent mode floors at 0, because a
    percentage of nothing is nothing — for those suppliers a zero cost shows up
    as a zero price rather than as a bare markup.
    """
    rule = rule_for(supplier, rules)
    return rule["value"] if rule["mode"] == DOLLAR else 0.0


def sql_price_expr(price_col, rules=None, suppliers=None, supplier_col="supplier"):
    """SQL CASE mapping a cost column to the marked-up price, per supplier.

    Restrict to `suppliers` (the ones actually staged) so an unrelated entry in
    the table can't widen the expression. Values are formatted, not
    parameterised, because this is composed into run_sql — they are floats
    validated by _validate_rule, never raw client strings.
    """
    rules = load() if rules is None else rules
    keys = sorted(rules if suppliers is None else suppliers)

    arms = []
    for s in keys:
        rule = rule_for(s, rules)
        if rule["mode"] == PERCENT:
            arms.append(f"when '{s}' then {price_col} * {1.0 + rule['value'] / 100.0:.6f}")
        else:
            arms.append(f"when '{s}' then {price_col} + {rule['value']:.2f}")
    if not arms:
        raise MarkupConfigError("No suppliers to build a markup expression for.")

    # No ELSE: an unmatched supplier yields NULL, which min() drops. merge.py
    # checks the staged suppliers against the table first so that can't happen
    # silently, and rounds here to keep merged_price a real currency value —
    # the percent path otherwise carries long decimals into the min().
    return "round((case " + supplier_col + " " + " ".join(arms) + " end)::numeric, 2)"


def describe(supplier, rules=None):
    """One-line human rendering, e.g. '$46.50' or '+22.0%'. For logs and UI."""
    rule = rule_for(supplier, rules)
    if rule["mode"] == PERCENT:
        return f"+{rule['value']:g}%"
    return f"${rule['value']:.2f}"
