#!/usr/bin/env python3
"""Preflight checks for a sync run.

Answers "would a sync work right now, and what would it write to?" without
writing anything. The dashboard runs this before enabling its Sync button, and
the cloud cron should run it as the first thing in the job so a broken token or
a suspended warehouse fails the job immediately instead of halfway through a
catalog export.

Every check is read-only. Nothing here starts a run or touches pipeline tables.

Usage (from production/):
    python preflight.py
    python preflight.py --json
"""

import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config

OK, WARN, FAIL = "ok", "warning", "failed"


def _check(name, status, detail, **extra):
    return {"check": name, "status": status, "detail": detail, **extra}


# ── individual checks ──────────────────────────────────────────────────────────
def check_store():
    """Shop reachable, token valid, scopes sufficient, primary location unambiguous."""
    from core.shopify_gql import ShopifyGQL, ShopifyError

    if not config.SHOPIFY_SHOP or not config.SHOPIFY_TOKEN:
        return [_check("shopify_credentials", FAIL,
                       "SHOPIFY_SHOP / SHOPIFY_TOKEN are not set (production/.env, or the "
                       "SBOT_SHOPIFY_* names used by pipelines/shared/config.py).")]

    results = []
    client = ShopifyGQL()
    try:
        data = client.execute(
            "{ shop { name myshopifyDomain } "
            "  currentAppInstallation { accessScopes { handle } } "
            "  locations(first: 20, includeInactive: false) { nodes { id name isPrimary } } }",
            label="preflight")
    except ShopifyError as e:
        return [_check("shopify_reachable", FAIL,
                       f"Could not reach {config.SHOPIFY_SHOP}: {str(e)[:300]}")]

    shop = data["shop"]
    kind = "DEV store" if config.is_dev_store() else "LIVE store"
    results.append(_check(
        "shopify_reachable", OK,
        f"{shop['name']} - {shop['myshopifyDomain']} ({kind})",
        shop=shop["myshopifyDomain"], is_dev=config.is_dev_store()))

    # Price writes need write_products; inventory writes need write_inventory and
    # read_locations. Missing any of them fails only at the first mutation
    # otherwise, i.e. after the catalog export has already run for minutes.
    scopes = sorted(s["handle"] for s in data["currentAppInstallation"]["accessScopes"])
    required = {"read_products", "write_products", "read_inventory",
                "write_inventory", "read_locations"}
    missing = sorted(required - set(scopes))
    results.append(_check(
        "shopify_scopes", FAIL if missing else OK,
        f"missing scope(s): {missing}" if missing else f"{len(scopes)} scopes, all required present",
        scopes=scopes, missing=missing))

    # The reason primary_location_id() no longer trusts locations(first:1) — a
    # second location appearing is exactly what broke inventory writes before.
    nodes = data["locations"]["nodes"]
    primary = [n for n in nodes if n.get("isPrimary")]
    if len(primary) == 1:
        others = [n["name"] for n in nodes if not n.get("isPrimary")]
        detail = f"'{primary[0]['name']}' is primary"
        if others:
            detail += f" (inventory will NOT be written to: {', '.join(others)})"
        results.append(_check("shopify_location", OK, detail,
                              location=primary[0]["name"], all_locations=[n["name"] for n in nodes]))
    else:
        results.append(_check(
            "shopify_location", FAIL,
            f"expected exactly one location with isPrimary=true, found {len(primary)} "
            f"among {[n['name'] for n in nodes]} — inventory sync would refuse to guess."))
    return results


def check_warehouse():
    from core.warehouse import get_warehouse, WarehouseError
    try:
        wh = get_warehouse()
    except WarehouseError as e:
        return [_check("warehouse", FAIL, str(e))]
    try:
        wh.check_ready()
    except Exception as e:
        return [_check("warehouse", FAIL,
                       f"{wh.describe()} is not usable: {str(e)[:300]}",
                       backend=wh.name)]
    return [_check("warehouse", OK, wh.describe(), backend=wh.name)]


def check_markup():
    """The markup table prices every SKU — a malformed edit must stop the run."""
    try:
        rules = config.load_markups()
    except config.MarkupConfigError as e:
        return [_check("markup", FAIL, str(e))]
    summary = ", ".join(f"{s}={config.describe_markup(s, rules)}" for s in sorted(rules))
    return [_check("markup", OK, summary, rules=rules)]


def check_feeds(product_types=None):
    """Which supplier CSVs exist and how old they are.

    Mirrors steps/ingest.py's file discovery and steps/validate.py's freshness
    rule so the dashboard can warn about a stale feed before the run rather than
    aborting at the validate gate after the catalog export.
    """
    product_types = product_types or list(config.PRODUCT_TYPES)
    results = []
    for product_type in product_types:
        required = set(config.REQUIRED_SUPPLIERS.get(product_type, ()))
        for supplier, cfg in config.SUPPLIERS.items():
            if product_type not in cfg["types"]:
                continue
            file_type = cfg.get("type_alias", {}).get(product_type, product_type)
            pattern = config.PIPELINES_DIR / cfg["pattern"].format(type=file_type)
            matches = sorted(glob.glob(str(pattern)), key=os.path.getmtime, reverse=True)
            name = f"feed:{supplier}:{product_type}"
            severity = FAIL if supplier in required else WARN
            if not matches:
                results.append(_check(name, severity, "no CSV found - run this supplier's pipeline",
                                      supplier=supplier, product_type=product_type))
                continue
            age_days = (time.time() - os.path.getmtime(matches[0])) / 86400
            stale = age_days > config.MAX_CSV_AGE_DAYS
            results.append(_check(
                name, severity if stale else OK,
                f"{os.path.basename(matches[0])} - {age_days:.1f} days old"
                + (f" (older than the {config.MAX_CSV_AGE_DAYS}-day limit)" if stale else ""),
                supplier=supplier, product_type=product_type, age_days=round(age_days, 2)))
    return results


# ── driver ─────────────────────────────────────────────────────────────────────
def run(product_types=None, include_store=True):
    """All checks. Returns (results, worst_status)."""
    results = []
    results += check_warehouse()
    results += check_markup()
    if include_store:
        results += check_store()
    results += check_feeds(product_types)

    worst = OK
    if any(r["status"] == WARN for r in results):
        worst = WARN
    if any(r["status"] == FAIL for r in results):
        worst = FAIL
    return results, worst


def main():
    ap = argparse.ArgumentParser(description="Preflight checks for a sync run")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--type", nargs="+", choices=list(config.PRODUCT_TYPES),
                    default=list(config.PRODUCT_TYPES))
    ap.add_argument("--no-store", action="store_true",
                    help="Skip the Shopify checks (offline)")
    args = ap.parse_args()

    results, worst = run(args.type, include_store=not args.no_store)
    if args.json:
        print(json.dumps({"status": worst, "checks": results}, indent=2, default=str))
    else:
        icon = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}
        for r in results:
            print(f"[{icon[r['status']]}] {r['check']:<32} {r['detail']}")
        print(f"\noverall: {worst}")
    return 1 if worst == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
