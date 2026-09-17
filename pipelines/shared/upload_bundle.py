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

NEW PRODUCTS ONLY
-----------------
An import exists to CREATE products; price and quantity for products the store
already has are the sync's job (production/run_weekly.py). Bundling everything
therefore re-uploads ~15.6k rows to change nothing, and splits into several
15 MB parts instead of one small file. Pass the SKUs already in the store and
only the rest are bundled:

    python ../../production/store_skus.py --out skus.json
    python upload_bundle.py --known-skus skus.json

The build refuses rather than guesses in the two cases where the comparison is
not trustworthy: nothing is new, or nothing matched at all (a stale catalog
mirror, or SKU formats that have diverged between the two sides).

Each build writes a manifest.txt beside the CSVs naming every source file and
its row count, so a bundle can always be traced back to what produced it, plus
whether it was a full or new-products-only build.
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

from size_parser import consumer_size_filter, size_from_tags

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


# The product-type token at the end of a Shopify_SEO name, which is what ties
# an output back to the cleaned/raw CSV it was built from:
#   Shopify_SEO_dttire_wheels_20260821_210547 -> wheels
#   Shopify_SEO_tires_20260821_210309         -> tires   (formula)
_TYPE_TOKEN = re.compile(r"_(tires|wheels|rims)$", re.IGNORECASE)


def _repo_rel(path):
    """Path relative to the repo when it is inside it, absolute when it is not.

    A source outside REPO_ROOT is unusual but legal (a supplier directory can
    be pointed anywhere), and `relative_to` raises on it. That raise landed
    AFTER the CSV parts were already written, leaving a bundle with no
    manifest — the one thing this module exists to guarantee.
    """
    try:
        return Path(path).relative_to(REPO_ROOT)
    except ValueError:
        return Path(path)


def stale_sources(sources):
    """[(shopify_csv, input_csv)] for every output OLDER than the file it was
    built from.

    A backfill or an enrichment apply rewrites a supplier's cleaned/raw CSV;
    the Shopify CSV is only regenerated if something re-runs that supplier's
    augment step. Nothing forces it to. On 21 August the DT Tire wheels
    backfill fixed 505 of 514 images in `cleaned_wheels_*.csv` and the bundle
    still shipped the 130px thumbnails, because `Shopify_SEO_dttire_wheels_*`
    had been written the day before and no step in between rebuilt it.

    The folder timestamp cannot catch this: the bundle really is newer than
    everything in it. Only comparing each output against its own input can.

    Best-effort — the enrichment SOURCES table is the authority on which input
    feeds which supplier/type, and if it cannot be imported this returns
    nothing rather than blocking a build.
    """
    try:
        sys.path.insert(0, str(_HERE))
        import enrichment as E
    except Exception:                                   # noqa: BLE001
        return []

    inputs = {}
    for src in E.SOURCES:
        f = E.latest(src["glob"])
        if f:
            inputs[(src["supplier"], src["type"])] = f

    stale = []
    for out in sources:
        supplier = Path(out).parent.parent.parent.name
        m = _TYPE_TOKEN.search(_TS_SUFFIX.sub("", Path(out).stem))
        if not m:
            continue
        src_file = inputs.get((supplier, m.group(1).lower()))
        if not src_file:
            continue
        if os.path.getmtime(src_file) > os.path.getmtime(out):
            stale.append((Path(out), Path(src_file)))
    return stale


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


SKU_COLUMN = "Variant SKU"


def source_product_type(path):
    """'tires' | 'wheels' | '' for a Shopify_SEO source file, from its name."""
    m = _TYPE_TOKEN.search(_TS_SUFFIX.sub("", Path(path).stem))
    if not m:
        return ""
    token = m.group(1).lower()
    return "wheels" if token == "rims" else token


def filter_consumer_tires(frame, path):
    """Drop ATV/UTV/golf-cart/commercial rows from a tires import file.

    This is the second road to the storefront. production/steps/ingest.py
    filters the road the weekly sync takes; this one is the bundle the client
    uploads by hand from the dashboard, and it is built from the augment
    output rather than from the cleaned CSVs, so the ingest filter never sees
    it. Before this, the first three rows of the North Country bundle were
    Atturo/Kenda 28X10R14 SxS tires — side-by-sides, uploaded as passenger
    stock. Both roads must apply the same rules or the store's contents depend
    on which button was pressed.

    Returns (kept_frame, dropped_count, refusal).
    """
    if source_product_type(path) != "tires":
        return frame, 0, ""
    if "Tags" not in frame.columns:
        return frame, 0, (f"{Path(path).name} has no 'Tags' column, which is where the "
                          f"size lives in a Shopify import file — passed through "
                          f"UNFILTERED")
    sizes = frame["Tags"].apply(size_from_tags)
    keep, _dropped, refusal = consumer_size_filter(sizes)
    if refusal:
        return frame, 0, f"{Path(path).name}: {refusal}"
    kept = frame[pd.Series(keep, index=frame.index)]
    return kept, len(frame) - len(kept), ""


def filter_new_products(frame, exclude_skus):
    """Rows whose SKU is not already in the store. Returns (kept, dropped_count).

    Matching is on the raw string, deliberately: both sides of this comparison
    are "the SKU as Shopify holds it" — the mirror is read back out of Shopify,
    and this column is what a Shopify import writes in. Normalising either side
    (stripping leading zeros, coercing numerics) would invent a difference that
    the store itself does not have. '000539' and '539' are two different
    products here, and pandas reads this file with dtype=str precisely so that
    distinction survives.
    """
    if SKU_COLUMN not in frame.columns:
        return frame, 0
    skus = frame[SKU_COLUMN].astype(str).str.strip()
    keep = ~skus.isin(exclude_skus)
    return frame[keep], int((~keep).sum())


def prepare_upload_files(supplier_dirs=None, dry_run=False, out_root=None,
                         allow_stale=False, exclude_skus=None):
    """Merge every supplier's latest CSV and split into Shopify-sized parts.

    Returns (out_files, error). On dry_run, returns ([], "") after printing the
    inputs it would use - useful for confirming freshness before writing.

    Refuses to build on a stale source (see stale_sources): a Shopify CSV older
    than the cleaned/raw file it was built from means a fix landed in the input
    and never reached the output, and bundling it ships the old data under a
    fresh timestamp. `allow_stale=True` overrides, and names what it is
    ignoring.

    exclude_skus: SKUs already present in the store. When given, only products
    Shopify does not have yet are bundled — the import then creates products
    rather than re-uploading the entire catalogue to change nothing. Price and
    quantity for products that already exist are the sync's job, not an
    import's, so an all-products bundle is mostly redundant work: ~17.6k rows
    to create ~2k products.
    """
    sources = collect_source_csvs(supplier_dirs)
    if not sources:
        return [], "No Shopify CSV files found — run the augment steps first."

    stale = stale_sources(sources)
    if stale:
        detail = "; ".join(f"{o.name} is older than {i.name}" for o, i in stale)
        if not allow_stale:
            return [], (
                f"{len(stale)} supplier CSV(s) are out of date with their inputs — "
                f"re-run those augment steps first, or pass --allow-stale to bundle "
                f"anyway: {detail}")
        print(f"WARNING: bundling {len(stale)} stale source(s) because "
              f"--allow-stale was given: {detail}")

    exclude_skus = set(exclude_skus) if exclude_skus else None

    frames, manifest = [], []
    total_dropped = 0
    total_non_consumer = 0
    for path in sources:
        try:
            frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        except Exception as exc:                        # noqa: BLE001
            return [], f"Error reading {path.name}: {exc}"
        source_rows = len(frame)
        dropped = 0
        frame, non_consumer, refusal = filter_consumer_tires(frame, path)
        if refusal:
            print(f"WARNING: non-consumer size filter {refusal}")
        total_non_consumer += non_consumer
        if exclude_skus is not None:
            if SKU_COLUMN not in frame.columns:
                return [], (f"{path.name} has no '{SKU_COLUMN}' column, so new-vs-existing "
                            f"cannot be determined — refusing to guess which of its "
                            f"{source_rows} rows are new.")
            frame, dropped = filter_new_products(frame, exclude_skus)
            total_dropped += dropped
        frames.append(frame)
        manifest.append((path, len(frame), datetime.fromtimestamp(path.stat().st_mtime),
                         source_rows, dropped, non_consumer))

    print(f"Sources ({len(manifest)}):")
    for path, rows, mtime, source_rows, dropped, non_consumer in manifest:
        why = ([f"{dropped:,} already in store"] if exclude_skus is not None else [])
        why += ([f"{non_consumer:,} non-consumer"] if non_consumer else [])
        suffix = f"  (of {source_rows:,}, {', '.join(why)})" if why else ""
        print(f"  {mtime:%Y-%m-%d %H:%M}  {rows:7,d}  {path.name}{suffix}")
    total_rows = sum(rows for _, rows, _, _, _, _ in manifest)
    print(f"  {'':16s}  {total_rows:7,d}  TOTAL")
    if total_non_consumer:
        print("")
        print(f"Non-consumer size filter: {total_non_consumer:,} ATV/UTV/golf-cart/"
              f"commercial row(s) excluded.")

    if exclude_skus is not None:
        print(f"\nNew-products-only: {total_dropped:,} row(s) skipped as already in the "
              f"store, {total_rows:,} to create.")
        if total_rows == 0:
            return [], ("Every product in the supplier CSVs already exists in the store — "
                        "there is nothing new to import. Use the Sync button to update "
                        "their prices and quantities.")
        # A filter that excluded nothing against a non-empty store means the two
        # sides are not talking about the same SKUs (a stale mirror, or a change
        # in how SKUs are written on one side). Bundling the whole catalogue
        # under a "new products" label is worse than stopping.
        if total_dropped == 0:
            return [], (f"None of the {total_rows:,} rows matched any of the "
                        f"{len(exclude_skus):,} SKUs already in the store, which is not "
                        f"credible — the catalog mirror is probably stale or the SKU "
                        f"formats have diverged. Re-run the sync (it refreshes the "
                        f"mirror) and try again, or bundle all products instead.")

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
    # The folder and the file names both say which kind of bundle this is. A
    # new-products bundle and a full one look identical from the outside and are
    # imported the same way, so the only thing standing between them and a
    # mix-up is the name.
    kind = "NEW products only" if exclude_skus is not None else "all products"
    folder = f"{now.day} {now:%B %Y} {now:%H-%M-%S}"
    if exclude_skus is not None:
        folder += " (new products)"
    out_dir = Path(out_root or UPLOAD_READY) / folder
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = "shopify_new_products" if exclude_skus is not None else "shopify_upload"
    header, chunks = _split_chunks(merged, int(CSV_TARGET_MB * 1024 * 1024))
    out_files = []
    for idx, chunk in enumerate(chunks, 1):
        name = (f"{stem}_{idx}of{len(chunks)}.csv"
                if len(chunks) > 1 else f"{stem}.csv")
        out_path = out_dir / name
        out_path.write_text(header + "".join(chunk), encoding="utf-8", newline="")
        out_files.append(out_path)

    # Record what this bundle was built from. Without it, a bundle's folder
    # timestamp is the only clue to its contents, and that timestamp lies
    # whenever a source CSV is regenerated moments later.
    lines = [f"Bundle built {now:%Y-%m-%d %H:%M:%S}",
             f"Contents: {kind}",
             f"Total rows: {len(merged):,}",
             f"Handle collisions disambiguated: {handle_fixes}"]
    if total_non_consumer:
        lines += [f"Rows excluded as ATV/UTV/golf-cart/commercial: {total_non_consumer:,}"]
    if exclude_skus is not None:
        lines += [f"Rows skipped as already in the store: {total_dropped:,}",
                  f"Store SKUs compared against: {len(exclude_skus):,}"]
    lines += ["", "Sources:"]
    lines += [f"  {m:%Y-%m-%d %H:%M}  {r:7,d}  {_repo_rel(p)}"
              for p, r, m, _sr, _d, _nc in manifest]
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
    ap.add_argument("--allow-stale", action="store_true",
                    help="Bundle even when a supplier's Shopify CSV is older than the "
                         "cleaned/raw file it was built from. The default refusal is "
                         "there because that gap is invisible in the finished bundle — "
                         "it is newer than everything in it and still carries the old "
                         "data.")
    ap.add_argument("--known-skus", metavar="FILE",
                    help="JSON file holding the SKUs already in the store (a list, or "
                         "an object with a 'skus' key — the shape "
                         "production/store_skus.py writes). Only products NOT in that "
                         "set are bundled, so the import creates products instead of "
                         "re-uploading the whole catalogue. Generate it with: "
                         "python ../../production/store_skus.py --out skus.json")
    args = ap.parse_args()

    exclude = None
    if args.known_skus:
        import json
        with open(args.known_skus, encoding="utf-8") as fh:
            doc = json.load(fh)
        exclude = set(doc["skus"] if isinstance(doc, dict) else doc)
        print(f"Filtering against {len(exclude):,} SKUs already in the store.")

    _, error = prepare_upload_files(dry_run=args.dry_run, allow_stale=args.allow_stale,
                                    exclude_skus=exclude)
    if error:
        print(f"ERROR: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
