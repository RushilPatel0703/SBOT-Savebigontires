#!/usr/bin/env python3
"""Quarantine for products with no detectable cost price.

WHY
---
Every supplier pipeline used to backfill a missing or zero cost_price from
retail_price and carry on. That keeps the row in the catalogue but prices it
off the wrong basis: retail plus the supplier markup, rather than cost plus
the markup. On the last North Country run that was 97 tyres silently listed
at roughly 1.7x their correct price.

A product whose cost we do not know cannot be priced. It is now pulled out of
the dataset before the final CSVs are written and parked here instead, so the
gap is visible and reviewable rather than shipped.

WHAT COUNTS AS "NO PRICE DETECTED"
----------------------------------
A row is quarantined when its cost field is missing, blank, non-numeric, or
<= 0. Nothing else - a row with a real cost is never touched.

One deliberate exception: when the cost column is absent from the raw scrape
ENTIRELY (a feed captured before the cost-price migration), the rows are NOT
quarantined. Quarantining an entire supplier would empty its catalogue over
what is a schema-version problem, not a data problem. That case keeps the
existing loud warning and leaves the column unset so downstream pricing falls
back with its own warning. `column_absent` in the returned summary flags it.

LAYOUT
------
    no price detected/
        northcountry_tires.csv          per supplier and product type
        northcountry_wheels.csv
        wheelsmart_rims.csv
        ...
        no_price_detected_all.csv       every supplier merged, with a
                                        `supplier` column identifying each row

Per-supplier files are rewritten on each run, matching how low_quantity_*.csv
already behaves, so they always describe the current state rather than
accumulating history.
"""

import glob
import os
from datetime import datetime

import pandas as pd

QUARANTINE_DIR_NAME = "no price detected"
MERGED_BASENAME = "no_price_detected_all.csv"

# Columns this module adds to every quarantined row so the merged file is
# self-describing. Prefixed to avoid colliding with supplier columns.
META_SUPPLIER = "supplier"
META_TYPE = "product_type"
META_REASON = "quarantine_reason"
META_WHEN = "quarantined_at"

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))


def quarantine_dir(create=True):
    """Absolute path to the repo-level quarantine folder."""
    path = os.path.join(_REPO_ROOT, QUARANTINE_DIR_NAME)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _to_number(value):
    """Parse a price that may arrive as '$1,234.50', '', None or a float."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value == value else None      # NaN -> None
    text = str(value).strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def is_unpriced(value):
    """True when `value` is missing, blank, non-numeric or <= 0."""
    number = _to_number(value)
    return number is None or number <= 0


def unpriced_mask(series):
    """Boolean Series flagging rows whose cost is missing/blank/non-numeric/<=0."""
    numeric = pd.to_numeric(
        series.astype(str).str.replace(r"[$,]", "", regex=True).str.strip(),
        errors="coerce",
    )
    return numeric.isna() | (numeric <= 0)


def _write(frame, supplier, product_type):
    out_path = os.path.join(quarantine_dir(), f"{supplier}_{product_type}.csv")
    frame.to_csv(out_path, index=False, encoding="utf-8")
    return out_path


def _stamp(frame, supplier, product_type, reason):
    frame = frame.copy()
    frame[META_SUPPLIER] = supplier
    frame[META_TYPE] = product_type
    frame[META_REASON] = reason
    frame[META_WHEN] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return frame


def _clear_stale(supplier, product_type):
    """Remove a previous run's file when this run quarantines nothing.

    Without this a supplier that has been fixed keeps advertising old failures,
    and the merged file reports gaps that no longer exist.
    """
    stale = os.path.join(quarantine_dir(), f"{supplier}_{product_type}.csv")
    if os.path.exists(stale):
        os.remove(stale)


def split_frame(df, supplier, product_type, cost_col="cost_price",
                reason="missing or zero cost price"):
    """DataFrame path. Returns (kept_df, dropped_df, summary).

    `summary` carries: quarantined (int), column_absent (bool), path (str|None).
    """
    summary = {"quarantined": 0, "column_absent": False, "path": None,
               "total": len(df)}

    if cost_col not in df.columns:
        summary["column_absent"] = True
        _clear_stale(supplier, product_type)
        return df, df.iloc[0:0].copy(), summary

    bad = unpriced_mask(df[cost_col])
    dropped = df[bad].copy()
    kept = df[~bad].copy()
    summary["quarantined"] = int(bad.sum())

    if summary["quarantined"]:
        summary["path"] = _write(_stamp(dropped, supplier, product_type, reason),
                                 supplier, product_type)
    else:
        _clear_stale(supplier, product_type)
    return kept, dropped, summary


def split_records(rows, supplier, product_type, cost_field="cost_price",
                  reason="missing or zero cost price"):
    """list-of-dicts path, for the pipelines that clean with csv.DictReader.

    Returns (kept, dropped, summary) with the same summary shape as split_frame.
    """
    summary = {"quarantined": 0, "column_absent": False, "path": None,
               "total": len(rows)}

    if not rows:
        return rows, [], summary
    if not any(cost_field in r for r in rows):
        summary["column_absent"] = True
        _clear_stale(supplier, product_type)
        return rows, [], summary

    kept, dropped = [], []
    for row in rows:
        (dropped if is_unpriced(row.get(cost_field)) else kept).append(row)
    summary["quarantined"] = len(dropped)

    if dropped:
        summary["path"] = _write(
            _stamp(pd.DataFrame(dropped), supplier, product_type, reason),
            supplier, product_type)
    else:
        _clear_stale(supplier, product_type)
    return kept, dropped, summary


def report(summary, supplier, product_type, cost_field="cost_price"):
    """One-line human summary for a pipeline log. Returns the string it printed."""
    if summary["column_absent"]:
        line = (f"  NOTE: no '{cost_field}' column in the raw {supplier} "
                f"{product_type} scrape — nothing quarantined; downstream pricing "
                f"falls back to retail with its own warning.")
    elif summary["quarantined"]:
        line = (f"  NO PRICE DETECTED: {summary['quarantined']} of "
                f"{summary['total']} {supplier} {product_type} rows have no cost "
                f"price — excluded from the final dataset, written to "
                f"{os.path.relpath(summary['path'], _REPO_ROOT)}")
    else:
        line = f"  Cost price present on all {summary['total']} {supplier} {product_type} rows."
    print(line)
    return line


def merge_all():
    """Combine every per-supplier quarantine file into one dataset.

    Returns (path, row_count). Writes nothing and returns (None, 0) when no
    supplier quarantined anything.
    """
    folder = quarantine_dir()
    parts = []
    for path in sorted(glob.glob(os.path.join(folder, "*.csv"))):
        if os.path.basename(path) == MERGED_BASENAME:
            continue
        frame = pd.read_csv(path, dtype=str, low_memory=False)
        if len(frame):
            parts.append(frame)

    merged_path = os.path.join(folder, MERGED_BASENAME)
    if not parts:
        if os.path.exists(merged_path):
            os.remove(merged_path)
        return None, 0

    merged = pd.concat(parts, ignore_index=True, sort=False)

    # Lead with the identifying columns so the file reads sensibly when opened
    # in a spreadsheet, where the supplier is the first thing anyone looks for.
    lead = [c for c in (META_SUPPLIER, META_TYPE, META_REASON, META_WHEN)
            if c in merged.columns]
    merged = merged[lead + [c for c in merged.columns if c not in lead]]
    merged.to_csv(merged_path, index=False, encoding="utf-8")
    return merged_path, len(merged)


if __name__ == "__main__":
    path, count = merge_all()
    if path:
        print(f"Merged {count} no-price row(s) -> {path}")
        frame = pd.read_csv(path, dtype=str, low_memory=False)
        print("\nBy supplier:")
        for (supplier, ptype), group in frame.groupby([META_SUPPLIER, META_TYPE]):
            print(f"  {supplier:16s} {ptype:8s} {len(group):6,d}")
    else:
        print("No quarantined rows — every product across every supplier has a cost price.")
