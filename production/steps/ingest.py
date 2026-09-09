#!/usr/bin/env python3
"""Ingest: latest supplier CSV per (supplier, type) → supplier_staging.

The raw CSVs on disk (produced by the existing scrape/clean pipelines) are the
immutable raw layer; this step normalizes sku/price/qty and loads them into the
warehouse keyed by run_id. Freshness and row-count facts are recorded here and
judged in the validate step.
"""

import importlib.util
import os
import time
from datetime import datetime

import pandas as pd

import config

# normalize_sku is the single canonical implementation, shared with the
# pipelines/ layer — loaded by file path (matching the pipelines/*/config.py
# re-export pattern) rather than duplicated here, since this exact logic
# duplicating and drifting between the pipelines/merged and production paths
# is what previously caused leading-zero SKUs to collide only on one side.
# See pipelines/shared/sku_normalize.py for the rationale.
_sku_normalize_spec = importlib.util.spec_from_file_location(
    "_sku_normalize", config.PIPELINES_DIR / "shared" / "sku_normalize.py")
_sku_normalize_mod = importlib.util.module_from_spec(_sku_normalize_spec)
_sku_normalize_spec.loader.exec_module(_sku_normalize_mod)
normalize_sku = _sku_normalize_mod.normalize_sku
disambiguate_skus = _sku_normalize_mod.disambiguate_skus


def find_latest(pattern_path):
    import glob
    matches = sorted(glob.glob(str(pattern_path)), key=os.path.getmtime, reverse=True)
    return matches[0] if matches else None


def coerce_qty(val):
    s = str(val).strip().rstrip("+")    # WheelMart "20+" → 20
    try:
        return max(int(float(s)), 0)
    except (ValueError, TypeError):
        return 0


def load_supplier_frame(supplier, cfg, product_type, log):
    """Returns (rows, meta) — rows ready for supplier_staging, meta for validation."""
    file_type = cfg.get("type_alias", {}).get(product_type, product_type)
    pattern   = config.PIPELINES_DIR / cfg["pattern"].format(type=file_type)
    path      = find_latest(pattern)
    meta = {"supplier": supplier, "product_type": product_type,
            "file": None, "age_days": None, "rows": 0}
    if not path:
        log.warning(f"[{supplier}/{product_type}] no file matching {pattern}")
        return [], meta

    age_days = (time.time() - os.path.getmtime(path)) / 86400
    meta["file"]     = os.path.basename(path)
    meta["age_days"] = round(age_days, 2)

    df = pd.read_csv(path, dtype=str, low_memory=False).fillna("")
    sku_col = cfg["sku_col"]
    if isinstance(sku_col, dict):
        sku_col = sku_col[product_type]
    if sku_col not in df.columns:
        log.error(f"[{supplier}/{product_type}] SKU column '{sku_col}' missing in {path}")
        return [], meta

    # Take the MAX across every qty column present, not the first one that
    # happens to exist. Emerge carries both columns but
    # available_quantity_related_branches is always 0 for that supplier (qty
    # lives entirely in available_quantity — see emergetires/clean.py), so
    # first-present-column-wins picked the always-zero one and would have
    # zeroed ~624 of 625 emerge tires in Shopify. Emerge's own clean.py already
    # takes the max of the two; this matches it.
    qty_cols_present = [c for c in cfg["qty_cols"] if c in df.columns]
    if not qty_cols_present:
        log.error(f"[{supplier}/{product_type}] none of qty columns {cfg['qty_cols']} in {path}")
        return [], meta

    # price_cols is ordered cost-first, retail last — a feed scraped before the
    # cost-price migration has no cost column and must fall back rather than
    # price the whole feed at 0. Never default a missing price column to "":
    # that silently zeroes every SKU and would push markup-only prices store-wide.
    price_col = next((c for c in cfg["price_cols"] if c in df.columns), None)
    if price_col is None:
        log.error(f"[{supplier}/{product_type}] none of price columns {cfg['price_cols']} in {path}")
        return [], meta
    if price_col != cfg["price_cols"][0]:
        log.warning(f"[{supplier}/{product_type}] cost column '{cfg['price_cols'][0]}' absent — "
                    f"falling back to '{price_col}' (re-scrape to price off cost)")

    split_colon = cfg.get("sku_split_colon", False)
    sku_prefix  = cfg.get("sku_prefix", "")
    df["_sku"]   = df[sku_col].apply(lambda v: normalize_sku(v, split_colon))
    # Keep the pre-split name prefix ("WHEEL:" / "RAC:") for collision
    # resolution further down — see the disambiguation block below.
    df["_name_prefix"] = (
        df[sku_col].apply(lambda v: str(v).split(":")[0].strip()) if split_colon else "")
    if sku_prefix:
        df["_sku"] = df["_sku"].apply(
            lambda s: s if not s or s.startswith(sku_prefix) else sku_prefix + s)
    df["_price"] = pd.to_numeric(df[price_col], errors="coerce").fillna(0).round(2)
    df["_qty"]   = pd.concat(
        [df[c].apply(coerce_qty) for c in qty_cols_present], axis=1
    ).max(axis=1)

    df = df[df["_sku"] != ""]
    if not cfg.get("prefiltered", True):
        before = len(df)
        df = df[df["_qty"] >= cfg.get("min_qty", 4)]
        log.info(f"[{supplier}/{product_type}] low-qty filter: {before} → {len(df)}")

    # WheelMart lists some rims under both a "WHEEL:" and a "RAC:" name sharing
    # one trailing model code. They are DIFFERENT products (client-confirmed),
    # so without this the drop_duplicates below silently deletes one of the two.
    # Runs after the qty filter and before the dedup so "first occurrence wins"
    # resolves identically to pipelines/wheelsmart/augment_rims.py, which uses
    # this same shared helper — the two derivations must not drift.
    if cfg.get("disambiguate_name_prefix") and split_colon:
        before_dis = df["_sku"].tolist()
        df["_sku"] = disambiguate_skus(before_dis, df["_name_prefix"].tolist())
        changed = sum(1 for a, b in zip(before_dis, df["_sku"]) if a != b)
        if changed:
            log.info(f"[{supplier}/{product_type}] disambiguated {changed} "
                     f"prefix-colliding SKU(s)")

    # duplicates within one supplier feed would double-count qty at merge
    before = len(df)
    df = df.drop_duplicates(subset="_sku", keep="first")
    if len(df) < before:
        log.info(f"[{supplier}/{product_type}] deduped {before - len(df)} duplicate SKUs")

    brand_col = next((c for c in ("brand", "brand_name") if c in df.columns), None)
    size_col  = "size" if "size" in df.columns else None

    rows = [{
        "supplier": supplier,
        "product_type": product_type,
        "sku": r["_sku"],
        "price": float(r["_price"]),
        "qty": int(r["_qty"]),
        "brand": (str(r[brand_col]).strip() or None) if brand_col else None,
        "size":  (str(r[size_col]).strip()  or None) if size_col  else None,
        "source_file": meta["file"],
    } for r in df.to_dict("records")]

    meta["rows"] = len(rows)
    log.info(f"[{supplier}/{product_type}] {len(rows)} rows from {meta['file']} "
             f"(age {age_days:.1f}d)")
    return rows, meta


def run(ctx, suppliers=None):
    """Load all supplier feeds for the run's product types into supplier_staging."""
    suppliers = suppliers or list(config.SUPPLIERS)
    all_meta = []
    total = 0

    with ctx.step("ingest") as step:
        for product_type in ctx.product_types:
            for supplier in suppliers:
                cfg = config.SUPPLIERS[supplier]
                if product_type not in cfg["types"]:
                    continue
                rows, meta = load_supplier_frame(supplier, cfg, product_type, ctx.log)
                all_meta.append(meta)
                if rows:
                    for r in rows:
                        r["run_id"] = ctx.run_id
                    inserted = ctx.wh.insert_many("supplier_staging", rows)
                    total += inserted
        step.rows_out = total
        step.details["feeds"] = all_meta
        ctx.log.info(f"Ingest complete: {total} staged rows across "
                     f"{sum(1 for m in all_meta if m['rows'])} feeds")

    return all_meta
