#!/usr/bin/env python3
"""Build the Shopify upload bundle from each supplier's latest Shopify_SEO CSV.

WHY THIS IS ITS OWN MODULE
--------------------------
This logic used to live only inside dashboard.py, reachable solely by clicking
a button in the Streamlit UI. That is how a bundle goes stale without anyone
noticing: regenerating a supplier's final CSV does NOT rebuild the bundle, and
nothing on disk records which inputs a given bundle was built from. A bundle
dated after a supplier CSV can still have been built before it - which is
exactly what happened on 18 August, where the bundle was written 21 seconds
before the corrected North Country file existed and therefore carried 97 rows
that had already been quarantined.

Pulling it out here makes the bundle reproducible from the command line, so it
can run at the end of a pipeline rather than only from a browser, and lets the
build record its own inputs.

    python upload_bundle.py                 # build from every supplier
    python upload_bundle.py --dry-run       # show what WOULD go in, write nothing

Each build writes a manifest.txt beside the CSVs naming every source file and
its row count, so a bundle can always be traced back to what produced it.
"""

import argparse
import csv
import io
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

CSV_TARGET_MB = 14.5          # safety margin under Shopify's hard 15 MB limit
_TS_SUFFIX = re.compile(r"_\d{8}_\d{6}$")

_HERE = Path(__file__).resolve().parent
PIPELINES_DIR = _HERE.parent
REPO_ROOT = PIPELINES_DIR.parent
UPLOAD_READY = REPO_ROOT / "upload_ready"

# Supplier directories that hold a Data/shopify folder. Kept here rather than
# imported from dashboard.py so this module stays importable without Streamlit.
SUPPLIER_DIRS = [
    PIPELINES_DIR / "formula_distributors",
    PIPELINES_DIR / "northcountry",
    PIPELINES_DIR / "dttire",
    PIPELINES_DIR / "wheelsmart",
    PIPELINES_DIR / "emergetires",
]


def collect_source_csvs(supplier_dirs=None):
    """Most recent Shopify_SEO_*.csv per product type per supplier."""
    found = []
    for supplier_dir in (supplier_dirs or SUPPLIER_DIRS):
        shopify_dir = Path(supplier_dir) / "Data" / "shopify"
        if not shopify_dir.exists():
            continue
        csvs = sorted(shopify_dir.glob("Shopify_SEO_*.csv"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        seen = set()
        for path in csvs:
            base = _TS_SUFFIX.sub("", path.stem)
            if base not in seen:
                seen.add(base)
                found.append(path)
    return found


def _split_chunks(merged, limit_bytes):
    """Split rows into chunks by exact encoded byte size."""
    row_io = io.StringIO()
    row_w = csv.writer(row_io, lineterminator="\n")
    row_w.writerow(merged.columns)
    header = row_io.getvalue()
    header_bytes = len(header.encode("utf-8"))

    chunks, current, current_bytes = [], [], header_bytes
    for row in merged.itertuples(index=False, name=None):
        row_io.seek(0)
        row_io.truncate(0)
        row_w.writerow(row)
        line = row_io.getvalue()
        line_bytes = len(line.encode("utf-8"))
        if current and current_bytes + line_bytes > limit_bytes:
            chunks.append(current)
            current, current_bytes = [], header_bytes
        current.append(line)
        current_bytes += line_bytes
    if current:
        chunks.append(current)
    return header, chunks


def _dedupe_handles(df):
    """Guarantee Handle uniqueness across every supplier's rows.

    Two catalog rows can describe genuinely different, separately-purchasable
    products (distinct SKU, often distinct price) that still slugify to an
    identical Handle - e.g. a "(Y)" vs "Y" speed-rating notation, a "P-ZERO"
    vs "P ZERO" spacing variance in the model name, or a Nexen SKU variant
    whose difference from its sibling isn't in the displayed text at all.
    Shopify groups same-Handle rows into one product, so on an empty store
    the second row of such a pair is silently dropped ("already exists")
    instead of imported - the product it represents never reaches the store.
    Disambiguating with a Variant SKU suffix keeps both as separate listings.
    """
    if "Handle" not in df.columns:
        return df, 0
    dupe_mask = df["Handle"].duplicated(keep=False)
    if not dupe_mask.any():
        return df, 0

    used = set(df["Handle"])
    seen = set()
    fixed = 0
    for idx in df.index[dupe_mask]:
        handle = df.at[idx, "Handle"]
        if handle not in seen:
            seen.add(handle)
            continue  # first occurrence keeps the original handle
        sku = str(df.at[idx, "Variant SKU"]) if "Variant SKU" in df.columns else ""
        suffix = re.sub(r"[^a-z0-9]", "", sku.lower())[-6:] or "dup"
        new_handle = f"{handle}-{suffix}"
        n = 1
        while new_handle in used:
            n += 1
            new_handle = f"{handle}-{suffix}-{n}"
        df.at[idx, "Handle"] = new_handle
        used.add(new_handle)
        fixed += 1
    return df, fixed


def prepare_upload_files(supplier_dirs=None, dry_run=False, out_root=None):
    """Merge every supplier's latest CSV and split into Shopify-sized parts.

    Returns (out_files, error). On dry_run, returns ([], "") after printing the
    inputs it would use - useful for confirming freshness before writing.
    """
    sources = collect_source_csvs(supplier_dirs)
    if not sources:
        return [], "No Shopify CSV files found — run the augment steps first."

    frames, manifest = [], []
    for path in sources:
        try:
            frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        except Exception as exc:                        # noqa: BLE001
            return [], f"Error reading {path.name}: {exc}"
        frames.append(frame)
        manifest.append((path, len(frame),
                         datetime.fromtimestamp(path.stat().st_mtime)))

    print(f"Sources ({len(manifest)}):")
    for path, rows, mtime in manifest:
        print(f"  {mtime:%Y-%m-%d %H:%M}  {rows:7,d}  {path.name}")
    total_rows = sum(rows for _, rows, _ in manifest)
    print(f"  {'':16s}  {total_rows:7,d}  TOTAL")

    if dry_run:
        print("\n--dry-run: nothing written.")
        return [], ""

    merged = pd.concat(frames, ignore_index=True)
    # Suppliers without a given column (wheels files lack Cost/Compare-At Price)
    # leave NaN after concat, which csv.writer renders as the literal "nan" and
    # Shopify then rejects as an invalid price.
    merged = merged.fillna("")
    merged, handle_fixes = _dedupe_handles(merged)
    if handle_fixes:
        print(f"  Disambiguated {handle_fixes} Handle collision(s) "
              f"(same brand/size/spec text, different SKU) so Shopify "
              f"doesn't drop them as duplicates")

    now = datetime.now()
    folder = f"{now.day} {now:%B %Y} {now:%H-%M-%S}"
    out_dir = Path(out_root or UPLOAD_READY) / folder
    out_dir.mkdir(parents=True, exist_ok=True)

    header, chunks = _split_chunks(merged, int(CSV_TARGET_MB * 1024 * 1024))
    out_files = []
    for idx, chunk in enumerate(chunks, 1):
        name = (f"shopify_upload_{idx}of{len(chunks)}.csv"
                if len(chunks) > 1 else "shopify_upload.csv")
        out_path = out_dir / name
        out_path.write_text(header + "".join(chunk), encoding="utf-8", newline="")
        out_files.append(out_path)

    # Record what this bundle was built from. Without it, a bundle's folder
    # timestamp is the only clue to its contents, and that timestamp lies
    # whenever a source CSV is regenerated moments later.
    lines = [f"Bundle built {now:%Y-%m-%d %H:%M:%S}",
             f"Total rows: {len(merged):,}",
             f"Handle collisions disambiguated: {handle_fixes}", "", "Sources:"]
    lines += [f"  {m:%Y-%m-%d %H:%M}  {r:7,d}  {p.relative_to(REPO_ROOT)}"
              for p, r, m in manifest]
    lines += ["", "Parts:"]
    lines += [f"  {p.name}" for p in out_files]
    (out_dir / "manifest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nWrote {len(out_files)} part(s) to {out_dir}")
    print(f"  {len(merged):,} rows total")
    return out_files, ""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="List the source CSVs that would be bundled; write nothing.")
    args = ap.parse_args()

    _, error = prepare_upload_files(dry_run=args.dry_run)
    if error:
        print(f"ERROR: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
