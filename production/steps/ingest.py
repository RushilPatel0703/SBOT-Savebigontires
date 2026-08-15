#!/usr/bin/env python3
"""Ingest: latest supplier CSV per (supplier, type) → supplier_staging.

The raw CSVs on disk (produced by the existing scrape/clean pipelines) are the
immutable raw layer; this step normalizes sku/price/qty and loads them into the
warehouse keyed by run_id. Freshness and row-count facts are recorded here and
judged in the validate step.
"""

import os
import time
from datetime import datetime

import pandas as pd

import config


def find_latest(pattern_path):
    import glob
    matches = sorted(glob.glob(str(pattern_path)), key=os.path.getmtime, reverse=True)
    return matches[0] if matches else None


def normalize_sku(val, split_colon=False):
    s = str(val).strip()
    if split_colon and ":" in s:
        s = s.split(":")[-1].strip()
    try:
        return str(int(float(s)))       # numeric SKUs: "12345.0" → "12345"
    except (ValueError, OverflowError):
        return s


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

    qty_col = next((c for c in cfg["qty_cols"] if c in df.columns), None)
    if qty_col is None:
        log.error(f"[{supplier}/{product_type}] none of qty columns {cfg['qty_cols']} in {path}")
        return [], meta
    price_col = cfg["price_col"]

    split_colon = cfg.get("sku_split_colon", False)
    sku_prefix  = cfg.get("sku_prefix", "")
    df["_sku"]   = df[sku_col].apply(lambda v: normalize_sku(v, split_colon))
    if sku_prefix:
        df["_sku"] = df["_sku"].apply(
            lambda s: s if not s or s.startswith(sku_prefix) else sku_prefix + s)
    df["_price"] = pd.to_numeric(df.get(price_col, ""), errors="coerce").fillna(0).round(2)
    df["_qty"]   = df[qty_col].apply(coerce_qty)

    df = df[df["_sku"] != ""]
    if not cfg.get("prefiltered", True):
        before = len(df)
        df = df[df["_qty"] >= cfg.get("min_qty", 4)]
        log.info(f"[{supplier}/{product_type}] low-qty filter: {before} → {len(df)}")

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
                    inserted = ctx.hasura.insert_many("supplier_staging", rows)
                    total += inserted
        step.rows_out = total
        step.details["feeds"] = all_meta
        ctx.log.info(f"Ingest complete: {total} staged rows across "
                     f"{sum(1 for m in all_meta if m['rows'])} feeds")

    return all_meta
