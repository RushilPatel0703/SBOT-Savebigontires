#!/usr/bin/env python3
"""Emerge Tires — QA Gate
Internal pass/fail gate — NOT a client report. See QA_GATE_SPEC.md.

Exit 0 = all checks PASS (or WARN only).
Exit 1 = at least one FAIL.

Usage:
    python qa_check.py
"""

import sys
import glob
import os
import csv
import re
from collections import Counter

from config import markup_for

MARKUP = markup_for("emerge")

# ── Thresholds ────────────────────────────────────────────────────────────────
RIMDIATAG_MIN_PCT   = 99.0
BRANDTAG_MIN_PCT    = 99.0
TYPETAG_MIN_PCT     = 100.0
SUPPLIERTAG_MIN_PCT = 100.0
SPEEDRATING_WARN_PCT = 70.0
LOADRANGE_WARN_PCT   = 70.0
SIZE_PARSE_MAX_FAIL  = 5.0   # %

# ── Size parser (verbatim from augment_tires.py) ──────────────────────────────
_SIZE_ANCHORED = re.compile(
    r'^(?P<prefix>LT|ST|P)?(?P<width>\d{3})/(?P<ratio>\d{2,3})'
    r'[A-Za-z]*[Rr][Ff]?(?P<rim>\d{2,3}(?:\.\d+)?)',
    re.IGNORECASE,
)
_SIZE_SEARCH = re.compile(
    r'(?P<width>\d{3})/(?P<ratio>\d{2,3})[A-Za-z]*[Rr][Ff]?(?P<rim>\d{2,3}(?:\.\d+)?)',
    re.IGNORECASE,
)
_SIZE_SLASH_TRIPLET = re.compile(
    r'(?P<prefix>LT|ST|P)?(?P<width>\d{3})/(?P<ratio>\d{2,3})/(?P<rim>\d{2,3}(?:\.\d+)?)',
    re.IGNORECASE,
)
_SIZE_FLOTATION = re.compile(
    r'(?P<prefix>LT)?(?P<width>\d{2,3})[Xx](?P<ratio>\d+(?:\.\d+)?)[Rr](?P<rim>\d{2,3}(?:\.\d+)?)',
    re.IGNORECASE,
)
_SIZE_ATV = re.compile(
    r'(?P<width>\d+(?:\.\d+)?)[Xx](?P<ratio>\d+(?:\.\d+)?)-(?P<rim>\d+(?:\.\d+)?)',
    re.IGNORECASE,
)
_SIZE_NO_RATIO = re.compile(
    r'(?P<prefix>LT|ST)?(?P<width>\d+(?:\.\d+)?)[Rr](?P<rim>\d+(?:\.\d+)?)',
    re.IGNORECASE,
)


def parse_tire_size(size: str, description: str = "") -> dict:
    for pat, text in (
        (_SIZE_ANCHORED,      size),
        (_SIZE_SEARCH,        size),
        (_SIZE_SEARCH,        str(description)),
        (_SIZE_SLASH_TRIPLET, size),
        (_SIZE_SLASH_TRIPLET, str(description)),
    ):
        m = pat.match(text) if pat is _SIZE_ANCHORED else pat.search(text)
        if m:
            d = m.groupdict()
            return {'width': d.get('width', ''), 'rim': d.get('rim', '')}
    for pat in (_SIZE_FLOTATION, _SIZE_ATV, _SIZE_NO_RATIO):
        m = pat.search(size)
        if m:
            d = m.groupdict()
            return {'width': d.get('width', ''), 'rim': d.get('rim', '')}
    return {'width': '', 'rim': ''}


# ── File finders ──────────────────────────────────────────────────────────────

def _latest(pattern: str, label: str) -> str:
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not matches:
        print(f"ERROR: No file matching {pattern!r} — run the pipeline first.")
        sys.exit(1)
    print(f"  {label}: {matches[0]}")
    return matches[0]


def load_csv(path: str) -> list:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── QA checks ─────────────────────────────────────────────────────────────────

def pct(count: int, total: int) -> float:
    return 100.0 * count / total if total else 0.0


def has_tag_prefix(tags_str: str, prefix: str) -> bool:
    return any(t.strip().startswith(prefix) for t in tags_str.split(","))


def main():
    print("=" * 65)
    print("Emerge Tires — QA Gate")
    print("=" * 65)

    print("\nLocating input files...")
    raw_file     = _latest("Data/raw/emergetires_tires_*.csv",              "raw")
    cleaned_file = _latest("Data/cleaned/cleaned_tires_*.csv",              "cleaned")
    shopify_file = _latest("Data/shopify/Shopify_SEO_emergetires_tires_*.csv", "shopify")

    raw_rows     = load_csv(raw_file)
    cleaned_rows = load_csv(cleaned_file)
    shopify_rows = load_csv(shopify_file)

    n_raw     = len(raw_rows)
    n_cleaned = len(cleaned_rows)
    n_shopify = len(shopify_rows)

    results: list[tuple] = []   # (check_label, value_str, threshold_str, verdict)
    fails = 0
    warns = 0

    def record(label, value_str, threshold_str, verdict):
        nonlocal fails, warns
        results.append((label, value_str, threshold_str, verdict))
        if verdict == "FAIL":
            fails += 1
        elif verdict == "WARN":
            warns += 1

    # ── Check 1a: Row counts ─────────────────────────────────────────────────
    record("1a: raw rows",     str(n_raw),     "—",   "INFO")
    record("1a: cleaned rows", str(n_cleaned), "—",   "INFO")
    record("1a: shopify rows", str(n_shopify), "—",   "INFO")
    record(
        "1a: raw->cleaned drop",
        f"{n_raw - n_cleaned} ({pct(n_raw - n_cleaned, n_raw):.1f}%)",
        "explained by clean.py counters",
        "INFO",
    )

    # ── Check 1b: cleaned == shopify (augment must be 1:1) ──────────────────
    if n_cleaned == n_shopify:
        record("1b: cleaned==shopify (1:1)", f"{n_cleaned}=={n_shopify}", "exact match", "PASS")
    else:
        record("1b: cleaned==shopify (1:1)", f"{n_cleaned}!={n_shopify}", "exact match", "FAIL")

    # ── Check 2: Required columns blank rate ─────────────────────────────────
    REQUIRED_COLS = ["Variant SKU", "Title", "Tags", "Variant Price", "Handle", "SEO Title"]
    for col in REQUIRED_COLS:
        blank_count = sum(1 for r in shopify_rows if not str(r.get(col, "")).strip())
        verdict = "FAIL" if blank_count > 0 else "PASS"
        record(f"2: blank '{col}'", str(blank_count), "0", verdict)

    # ── Check 3: Duplicate Variant SKU ───────────────────────────────────────
    sku_counts = Counter(r.get("Variant SKU", "") for r in shopify_rows)
    dupe_skus  = {sku: cnt for sku, cnt in sku_counts.items() if cnt > 1 and sku}
    dupe_count = sum(cnt - 1 for cnt in dupe_skus.values())
    record("3: duplicate Variant SKUs", str(dupe_count), "0",
           "FAIL" if dupe_count > 0 else "PASS")
    if dupe_skus:
        print(f"\n  [3] Duplicate SKUs: {list(dupe_skus.items())[:10]}")

    # ── Check 4: Tag coverage ────────────────────────────────────────────────
    def tag_coverage(prefix: str) -> float:
        covered = sum(1 for r in shopify_rows if has_tag_prefix(r.get("Tags", ""), prefix))
        return pct(covered, n_shopify)

    rimdia_pct   = tag_coverage("RimDia_")
    brand_pct    = tag_coverage("Brand_")
    type_pct     = tag_coverage("Type_")
    supplier_pct = tag_coverage("Supplier_Emergetires")
    speed_pct    = tag_coverage("SpeedRating_")
    load_pct     = tag_coverage("LoadRange_")

    record("4a: RimDia_ tag coverage",         f"{rimdia_pct:.1f}%",   f">={RIMDIATAG_MIN_PCT}%",
           "PASS" if rimdia_pct >= RIMDIATAG_MIN_PCT else "FAIL")
    record("4b: Brand_ tag coverage",           f"{brand_pct:.1f}%",   f">={BRANDTAG_MIN_PCT}%",
           "PASS" if brand_pct >= BRANDTAG_MIN_PCT else "FAIL")
    record("4c: Type_ tag coverage",            f"{type_pct:.1f}%",    f"={TYPETAG_MIN_PCT}%",
           "PASS" if type_pct >= TYPETAG_MIN_PCT else "FAIL")
    record("4d: Supplier_Emergetires coverage", f"{supplier_pct:.1f}%", f"={SUPPLIERTAG_MIN_PCT}%",
           "PASS" if supplier_pct >= SUPPLIERTAG_MIN_PCT else "FAIL")
    record("4e: SpeedRating_ coverage (info)",  f"{speed_pct:.1f}%",   f">={SPEEDRATING_WARN_PCT}%",
           "PASS" if speed_pct >= SPEEDRATING_WARN_PCT else "WARN")
    record("4f: LoadRange_ coverage (info)",    f"{load_pct:.1f}%",    f">={LOADRANGE_WARN_PCT}%",
           "PASS" if load_pct >= LOADRANGE_WARN_PCT else "WARN")

    # ── Check 5: Size-parse failure rate ─────────────────────────────────────
    parse_fails = 0
    for r in shopify_rows:
        parsed = parse_tire_size(r.get("Title", ""), "")
        if not parsed["width"] or not parsed["rim"]:
            parse_fails += 1
    fail_rate = pct(parse_fails, n_shopify)
    record("5: size-parse failure rate", f"{fail_rate:.1f}% ({parse_fails}/{n_shopify})",
           f"<={SIZE_PARSE_MAX_FAIL}%",
           "PASS" if fail_rate <= SIZE_PARSE_MAX_FAIL else "FAIL")

    # ── Check 6: Price sanity ─────────────────────────────────────────────────
    price_issues = 0
    for r in shopify_rows:
        try:
            price = float(r.get("Variant Price", 0) or 0)
            if price <= MARKUP:
                price_issues += 1
        except (ValueError, TypeError):
            price_issues += 1
    record("6: Variant Price > MARKUP", f"{price_issues} issues", "0",
           "PASS" if price_issues == 0 else "FAIL")

    # ── Check 7: Duplicate size in title ─────────────────────────────────────
    # Any title where a tire-size pattern appears more than once indicates the
    # size was not correctly deduplicated from the description.
    _TITLE_SIZE = re.compile(r'\d{3}/\d{2,3}(?:[A-Za-z]*R\d{2,3}|/\d{2,3})')
    dup_title_count = 0
    dup_title_examples = []
    for r in shopify_rows:
        title = r.get("Title", "")
        matches = _TITLE_SIZE.findall(title)
        if len(matches) > 1:
            dup_title_count += 1
            if len(dup_title_examples) < 5:
                dup_title_examples.append(f"{r.get('Variant SKU', '?')}: {title}")
    record("7: titles with duplicate size", str(dup_title_count), "0",
           "FAIL" if dup_title_count > 0 else "PASS")
    if dup_title_examples:
        print("\n  [7] Duplicate-size title examples:")
        for ex in dup_title_examples:
            print(f"      {ex}")

    # ── Check 8: Empty size components in Body HTML ───────────────────────────
    # Section width, aspect ratio, or rim diameter blank means parse_tire_size
    # failed — the product will have broken spec details on the storefront.
    empty_comp_count = 0
    empty_comp_examples = []
    for r in shopify_rows:
        body = r.get("Body (HTML)", "")
        if ("Section width: <br>" in body
                or "Aspect ratio: <br>" in body
                or "Rim diameter: <br>" in body):
            empty_comp_count += 1
            if len(empty_comp_examples) < 5:
                empty_comp_examples.append(r.get("Variant SKU", "?"))
    record("8: empty size components in Body HTML", str(empty_comp_count), "0",
           "FAIL" if empty_comp_count > 0 else "PASS")
    if empty_comp_examples:
        print(f"\n  [8] SKUs with empty size components: {empty_comp_examples}")

    # ── Print results table ───────────────────────────────────────────────────
    print(f"\n{'Check':<40} {'Value':<28} {'Threshold':<22} Result")
    print("-" * 100)
    for label, value, threshold, verdict in results:
        print(f"  {label:<38} {value:<28} {threshold:<22} {verdict}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    if fails == 0 and warns == 0:
        print("VERDICT: PASS — all checks passed.")
    elif fails == 0:
        print(f"VERDICT: PASS (with {warns} warning(s)) — review WARNs above.")
    else:
        print(f"VERDICT: FAIL — {fails} check(s) failed. Fix before sign-off.")
    print(f"{'='*65}")

    sys.exit(1 if fails > 0 else 0)


if __name__ == "__main__":
    main()
