#!/usr/bin/env python3
"""Build a Shopify product-import CSV that changes ONLY terrain tags.

Why start from an export: a hand-made CSV with just Handle/Title/Tags makes
Shopify's import preview show default variant, price and status values for the
columns it lacks, and it is unclear whether "overwrite" writes them. A file
round-tripped from the store's own Products export carries every live value, so
an overwrite writes back what is already there, and the only difference is Tags.

Steps for the user:
  1. Shopify admin -> Products -> Export -> "All products", "CSV for Excel,
     numbers or other spreadsheet programs". (Do this per store: dev, then live.)
  2. python tags_csv_from_export.py products_export_1.csv [more parts...] \\
         --tags ../TAGS_UPDATE_2026-11-22/tags_update_terrain.csv --out OUT.csv
  3. Import OUT.csv with "Overwrite existing products" on.

What it does to the export:
  * keeps only products whose Handle is in the tags CSV (all their rows: extra
    variants and image rows keep their cells exactly as exported)
  * on each product's first row, adds the Terrain_ tag and retires
    Type_All-Terrain / Type_Mud-Terrain (same rule as update_tags.plan)
  * DROPS every inventory-quantity column ("Inventory Quantity", "Variant
    Inventory Qty", "Inventory Available: <location>", "Inventory On Hand: ..."),
    because importing quantities is what breaks per-location availability.
    Tracker and policy columns are kept so those settings stay as they are.
  * leaves every other cell as exported, byte for byte (read as text, never
    re-typed, so SKUs like 011551 keep their zeros)
"""
import argparse
import csv
import sys

from update_tags import plan

QTY_PREFIXES = ("Inventory Available", "Inventory On Hand", "Inventory Committed",
                "Inventory Incoming", "Inventory Unavailable")
QTY_EXACT = {"Inventory Quantity", "Variant Inventory Qty", "Variant Inventory Quantity"}


def is_qty(col):
    return col in QTY_EXACT or col.startswith(QTY_PREFIXES)


def split_tags(s):
    return [t.strip() for t in s.split(",") if t.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("exports", nargs="+", help="Shopify products export CSV(s)")
    ap.add_argument("--tags", required=True, help="Handle,Tags CSV with the wanted tags")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    with open(a.tags, newline="", encoding="utf-8") as f:
        want = {r["Handle"]: split_tags(r["Tags"]) for r in csv.DictReader(f)}

    header, kept_cols, out_rows = None, None, []
    seen_first, changed, in_store = set(), 0, set()
    for path in a.exports:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rd = csv.reader(f)
            h = next(rd)
            if header is None:
                header = h
                for need in ("Handle", "Tags"):
                    if need not in header:
                        sys.exit(f"{path}: no '{need}' column - is this a Products export?")
                kept_cols = [i for i, c in enumerate(header) if not is_qty(c)]
                hi, ti = header.index("Handle"), header.index("Tags")
            elif h != header:
                sys.exit(f"{path}: columns differ from the first export file")
            for row in rd:
                row += [""] * (len(header) - len(row))
                handle = row[hi]
                if handle not in want:
                    continue
                in_store.add(handle)
                if handle not in seen_first:          # Tags live on a product's first row
                    seen_first.add(handle)
                    have = split_tags(row[ti])
                    add, remove = plan(have, want[handle])
                    if add or remove:
                        new = [t for t in have if t not in remove] + sorted(add)
                        row[ti] = ", ".join(new)
                        changed += 1
                out_rows.append([row[i] for i in kept_cols])

    # A handle can appear on a later part before its first row never did; check coverage.
    missing = sorted(set(want) - in_store)
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([header[i] for i in kept_cols])
        w.writerows(out_rows)
    dropped = [c for c in header if is_qty(c)]
    print(f"products in tags file {len(want)} | found in export {len(in_store)} | "
          f"missing {len(missing)} | tags changed {changed} | rows written {len(out_rows)}")
    print("inventory columns dropped:", dropped or "none present")
    if missing:
        print("not in this export (first 5):", missing[:5])


if __name__ == "__main__":
    main()
