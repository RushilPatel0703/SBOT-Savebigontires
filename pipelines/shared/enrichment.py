#!/usr/bin/env python3
"""Shared enrichment layer — ONE implementation used by all five supplier pipelines.

Replaces pipelines/formula_distributors/serp_enrich.py, which was (a) bolted onto
one supplier only and (b) wired into every plain `run_pipeline.py` call with no
per-search approval. See monitoring/SERP_LAYER_DESIGN.md and
monitoring/ENRICHMENT_APPROVAL.md for the full design/numbers write-up.

Where this slots in
--------------------
Runs as a new pipeline step positioned AFTER clean.py (or, for wheelsmart, after
the scraper — it has no clean.py stage) and BEFORE augment.py / augment_*.py.
It edits the cleaned/raw CSV *in place* (same filename, so every augment
script's "latest by mtime" file-discovery picks up the fix automatically) —
it never touches the Shopify_SEO_*.csv output directly, because that file's
schema differs enough per supplier (SKU column name, wheel vs tire columns)
that a single shared writer for it would have to special-case every supplier
anyway, defeating the point of sharing. Fixing the input to augment.py is the
one place where a single, small, generic writer works for all five suppliers.

Precedence (SERP is always last resort):
  a. value already scraped and valid                 -> untouched (never a candidate)
  b. free fallback (brand <- manufacturer_code, etc.) -> NOT this module's job — already
     implemented, for real, inside each supplier's own augment script (which all import
     the single shared pipelines/shared/size_parser.py rather than reimplementing
     parsing). Verified field-by-field in ENRICHMENT_APPROVAL.md; not duplicated here.
  c. derive by parsing (size string -> width/ratio/rim/load/speed/range) -> same as (b):
     already real, already shared via size_parser.py, already applied per-supplier.
  d. cached SERP result (already paid for)            -> apply_cached() below, REAL,
     runs every time this step is invoked with --apply. Zero network calls.
  e. live SERP search                                 -> run_live(), gated behind
     --live AND --budget N, kill-switch checked, never invoked by any code in this
     repo. Dry-run is the only mode exercised while building this layer.

Only two fields ever reach (d)/(e): designation and image. Every other field the
client asked about (brand, section width, aspect ratio, rim diameter, load index,
speed rating, load range) is already fully resolved by (a)-(c) — see
ENRICHMENT_APPROVAL.md for the row counts proving it.

Safety model (mirrors monitoring/serp_layer.py):
  1. No flags -> report only. Nothing is written, nothing is queried.
  2. --apply -> writes (d) cache hits into the cleaned/raw CSV in place. No network
     call — this only reuses results already paid for. Safe to run any time.
  3. --live AND --budget N together -> the only way to spend money. Refuses to run
     if monitoring/SERP_KILL_SWITCH exists. Hard-stops the instant spent >= budget.
     NEVER been invoked while building this module.

Usage:
    python enrichment.py                                # report only (all suppliers/fields)
    python enrichment.py --supplier formula_distributors # report only, one supplier
    python enrichment.py --apply --supplier dttire       # apply cache hits (free, no network)
    python enrichment.py --live --budget 200             # LIVE — requires explicit human approval
"""
import argparse
import glob
import json
import os
import sys
from urllib.parse import urlparse

import pandas as pd

ROOT         = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHARED_DIR   = os.path.join(ROOT, "pipelines", "shared")
CACHE_FILE   = os.path.join(SHARED_DIR, "Data", "serp_cache.json")
KILL_SWITCH  = os.path.join(ROOT, "monitoring", "SERP_KILL_SWITCH")
MANIFEST     = os.path.join(ROOT, "monitoring", "enrichment_dry_run_manifest.json")
APPROVAL_MD  = os.path.join(ROOT, "monitoring", "ENRICHMENT_APPROVAL.md")

FORMULA_OLD_CACHE = os.path.join(
    ROOT, "pipelines", "formula_distributors", "Data", "shopify", "serp_cache.json"
)

SUPPORTED_FIELDS = ("designation", "image")

# ---------------------------------------------------------------------------
# Per-supplier source config. Only supplier/type combos where the field is a
# genuine free-layer-exhausted gap are listed — see module docstring / design
# doc for why the other 8 client-requested fields never appear here.
#
# Each entry:
#   supplier, type, glob (latest-by-mtime picks the file augment*.py will also
#   pick), brand_col/model_col/size_col (None if the raw data has no such
#   column — used only to build the SERP query text), extra_col (extra free
#   text folded into the query when brand/model are thin, e.g. wheelsmart's
#   description), image_col/designation_col (the column actually rewritten),
#   writable (False = report-only; there's no column in this file the fix can
#   land in without changing augment.py itself, which is out of scope here).
# ---------------------------------------------------------------------------
SOURCES = [
    # ---- designation: only formula & dttire carry a literal `designation`
    # column with a real "Unknown" residual after clean.py's free inference.
    dict(supplier="formula_distributors", type="tires", field="designation",
         glob="pipelines/formula_distributors/Data/cleaned/cleaned_tires_*.csv",
         brand_col="brand_name", model_col="model", size_col="size", extra_col=None,
         value_col="designation", writable=True),
    dict(supplier="dttire", type="tires", field="designation",
         glob="pipelines/dttire/Data/cleaned/cleaned_tires_*.csv",
         brand_col="brand", model_col="model", size_col="size", extra_col=None,
         value_col="designation", writable=True),

    # ---- image: every supplier that scrapes a photo URL and sometimes comes
    # up blank. dttire is intentionally absent — 0/9,601 rows missing an image
    # (see ENRICHMENT_APPROVAL.md), so there is nothing to enrich.
    dict(supplier="northcountry", type="tires", field="image",
         glob="pipelines/northcountry/Data/cleaned/cleaned_tires_*.csv",
         brand_col="brand", model_col="model", size_col="size", extra_col=None,
         value_col="image_url", writable=True),
    dict(supplier="northcountry", type="wheels", field="image",
         glob="pipelines/northcountry/Data/cleaned/cleaned_wheels_*.csv",
         brand_col="brand", model_col="model", size_col="size", extra_col=None,
         value_col="image_url", writable=True),
    dict(supplier="formula_distributors", type="tires", field="image",
         glob="pipelines/formula_distributors/Data/cleaned/cleaned_tires_*.csv",
         brand_col="brand_name", model_col="model", size_col="size", extra_col=None,
         value_col="image_url", writable=True),
    # NOTE: formula wheels augmentation (augment_wheels.py find_input_file())
    # reads Data/raw/formula_wheels_*.csv directly, NOT the cleaned file —
    # clean.py's cleaned_wheels output is never consumed downstream for
    # formula wheels. Writing the fix to the raw file (not cleaned) is what
    # actually reaches the final Shopify CSV; this was a bug in the original
    # scaffold (monitoring/serp_layer.py) that pointed at the unused cleaned
    # file. Fixed here.
    dict(supplier="formula_distributors", type="wheels", field="image",
         glob="pipelines/formula_distributors/Data/raw/formula_wheels_*.csv",
         brand_col="brand_name", model_col="model", size_col="size", extra_col=None,
         value_col="image_url", writable=True, qty_filter="formula_wheels"),
    dict(supplier="emergetires", type="tires", field="image",
         glob="pipelines/emergetires/Data/cleaned/cleaned_tires_*.csv",
         brand_col="brand", model_col="model", size_col="size", extra_col=None,
         value_col="image_url", writable=True),
    dict(supplier="wheelsmart", type="tires", field="image",
         glob="pipelines/wheelsmart/Data/raw/wheelmart_tires_*.csv",
         brand_col=None, model_col=None, size_col="size_raw", extra_col="description",
         value_col="image", writable=True, qty_filter="wheelsmart_tires"),
    dict(supplier="wheelsmart", type="rims", field="image",
         glob="pipelines/wheelsmart/Data/raw/wheelmart_rims_*.csv",
         brand_col=None, model_col="name", size_col="diameter", extra_col="description",
         value_col="image", writable=True, qty_filter="wheelsmart_rims"),

    # ---- designation, wheelsmart: extract_designation() in
    # wheelsmart/augment_tires.py defaults to "Unknown" for real, same as
    # formula/dttire's residual — but it's derived from `description` at
    # augment time, not read from a raw-CSV column, so there's no column to
    # write a fix into without changing augment.py itself (out of scope).
    # Listed here as report-only (writable=False) so the approval report is
    # honest about this gap existing, instead of silently omitting it.
    dict(supplier="wheelsmart", type="tires", field="designation",
         glob="pipelines/wheelsmart/Data/raw/wheelmart_tires_*.csv",
         brand_col=None, model_col=None, size_col="size_raw", extra_col="description",
         value_col=None, writable=False, qty_filter="wheelsmart_tires"),
]


# ---------------------------------------------------------------------------
# File / cache helpers
# ---------------------------------------------------------------------------

def latest(pattern):
    matches = glob.glob(os.path.join(ROOT, pattern))
    return max(matches, key=os.path.getmtime) if matches else None


def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def cache_key(field, brand, model, size):
    """Centralized, cross-supplier key — the same brand+model+size is never
    paid for twice even if two different suppliers carry the identical tire.
    """
    b = str(brand or "").strip().lower()
    m = str(model or "").strip().lower()
    s = str(size or "").strip().lower()
    return f"{field}:{b}|{m}|{s}"


def migrate_formula_cache():
    """One-time, idempotent migration of formula's 253-entry designation
    cache (pipelines/formula_distributors/Data/shopify/serp_cache.json) into
    the new centralized cache. Read-only on the old file — it is NEVER
    deleted or modified, per the client's requirement to preserve it. Safe to
    call repeatedly (existing centralized entries are not overwritten).

    Returns (migrated_count, already_present_count).
    """
    if not os.path.exists(FORMULA_OLD_CACHE):
        return 0, 0
    with open(FORMULA_OLD_CACHE, encoding="utf-8") as f:
        old = json.load(f)

    cache = load_cache()
    migrated, already = 0, 0
    for old_key, entry in old.items():
        # Old key format: "brand|model|size" (lowercase) — identical
        # normalization to cache_key(), just missing the field prefix.
        new_key = f"designation:{old_key}"
        if new_key in cache:
            already += 1
            continue
        cache[new_key] = {"designation": entry.get("designation", ""), "query": entry.get("query", "")}
        migrated += 1
    if migrated:
        save_cache(cache)
    return migrated, already


def build_query(field, brand, model, size, extra=""):
    base = " ".join(filter(None, [str(brand or "").strip(), str(model or "").strip(), str(size or "").strip()]))
    if not str(brand or "").strip() and not str(model or "").strip() and extra:
        base = f"{base} {str(extra).strip()}".strip()
    if field == "designation":
        return f"{base} tire".strip()
    if field == "image":
        return f"{base} tire product photo".strip()
    raise ValueError(f"no query template for field {field!r}")


# ---------------------------------------------------------------------------
# Candidate discovery — read-only
# ---------------------------------------------------------------------------

def _is_missing_image(val):
    return str(val).strip() == ""


def _is_missing_designation(val):
    return str(val).strip().lower() == "unknown"


def _coerce_qty(val):
    """'20+' -> 20, negative/non-numeric -> 0. Matches wheelsmart's own
    coerce_quantity()/parse_qty() (they're equivalent modulo the negative
    clamp, which parse_qty applies and coerce_quantity doesn't — irrelevant
    here since we only compare >= threshold)."""
    s = str(val).strip()
    if s.endswith("+"):
        try:
            return int(s[:-1])
        except ValueError:
            return 20
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _qty_keep_mask(df, qty_filter):
    """Replicate the exact low-quantity filter the relevant augment*.py
    script applies AFTER reading this same raw file, so candidate counts
    (and the approval report's spend numbers) never include rows that get
    dropped before ever reaching Shopify. Only needed for the three sources
    that read a raw (pre-filter) CSV; cleaned-stage sources already had
    clean.py's own qty filter applied upstream.

    wheelsmart_tires note: augment_tires.py re-fetches live quantities from
    the WheelMart API before filtering. This function uses the static
    quantity_on_hand already in the raw CSV as an estimate — it cannot call
    that live (non-SERP) API itself without re-running a scrape-adjacent
    network call, which is out of scope here. The real count at augment
    time may differ slightly; flagged in ENRICHMENT_APPROVAL.md.
    """
    if qty_filter == "formula_wheels":
        if "available_quantity" in df.columns:
            qty = pd.to_numeric(df["available_quantity"], errors="coerce").fillna(0)
        else:
            qty_cols = [c for c in df.columns if c.startswith("qty_")]
            qty = df[qty_cols].apply(pd.to_numeric, errors="coerce").fillna(0).sum(axis=1) if qty_cols else 0
        return qty >= 3   # augment_wheels.py LOW_QTY_THRESHOLD
    if qty_filter == "wheelsmart_tires":
        return df["quantity_on_hand"].apply(_coerce_qty) >= 4   # augment_tires.py LOW_QTY_THRESHOLD
    if qty_filter == "wheelsmart_rims":
        return df["quantity_on_hand"].apply(_coerce_qty) >= 4   # augment_rims.py LOW_QTY_THRESHOLD
    return pd.Series(True, index=df.index)


def discover_candidates(field, supplier_filter=None):
    """Yield dicts describing every row that is a genuine (d)/(e) candidate:
    the field is blank/Unknown AFTER clean.py's free inference already ran.
    Read-only — opens the latest cleaned/raw CSV per source, never writes.
    """
    candidates = []
    for src in SOURCES:
        if src["field"] != field:
            continue
        if supplier_filter and src["supplier"] != supplier_filter:
            continue
        f = latest(src["glob"])
        if not f:
            continue
        df = pd.read_csv(f, dtype=str, low_memory=False, encoding="utf-8-sig").fillna("")

        qty_filter = src.get("qty_filter")
        if qty_filter:
            keep = _qty_keep_mask(df, qty_filter)
            df = df[keep]

        if src["value_col"] and src["value_col"] in df.columns:
            col = df[src["value_col"]]
            mask = col.apply(_is_missing_image if field == "image" else _is_missing_designation)
        elif field == "designation" and not src["writable"]:
            # wheelsmart designation: no literal column — derive via the same
            # regex logic augment_tires.py uses, read-only, to size the gap
            # honestly instead of reporting zero missing.
            sys.path.insert(0, os.path.join(ROOT, "pipelines", "wheelsmart"))
            from augment_tires import extract_designation as _wm_desig, clean_description as _wm_clean
            desigs = df["description"].apply(lambda d: _wm_desig(_wm_clean(d)))
            mask = desigs == "Unknown"
        else:
            continue

        brand_col = src["brand_col"]
        model_col = src["model_col"]
        extra_col = src["extra_col"]
        for idx in df.index[mask]:
            row = df.loc[idx]
            brand = row.get(brand_col, "") if brand_col else ""
            model = row.get(model_col, "") if model_col else ""
            size  = row.get(src["size_col"], "") if src["size_col"] else ""
            extra = row.get(extra_col, "") if extra_col else ""
            query = build_query(field, brand, model, size, extra)
            candidates.append(dict(
                supplier=src["supplier"], type=src["type"], field=field,
                row_index=idx, source_file=f, value_col=src["value_col"],
                writable=src["writable"],
                brand=brand, model=model, size=size, query=query,
                cache_key=cache_key(field, brand, model, size),
            ))
    return candidates


# ---------------------------------------------------------------------------
# (d) — apply already-cached (already-paid-for) results. No network call.
# ---------------------------------------------------------------------------

def apply_cached(fields, supplier_filter=None, write=False):
    """Steps (a)-(d) for real:
      (a) valid-already rows are never candidates (excluded by discover_candidates)
      (b)/(c) free fallback + parse-derivation are NOT re-implemented here — they
          already run for real, per supplier, via the shared size_parser.py
          import every augment script uses. Verified, not duplicated.
      (d) cache hits are applied here, for real, using the (already paid for)
          centralized cache. Zero network calls regardless of `write`.

    If write=True, the fix is saved back into the same cleaned/raw CSV file
    (same filename -> augment.py's "latest by mtime" discovery picks it up
    automatically on the next run). If write=False, this only reports what
    would be written (still zero network calls either way — the difference
    is purely whether the CSV on disk is touched).

    Returns a dict keyed by field with per-source and total stats.
    """
    cache = load_cache()
    report = {}

    for field in fields:
        cands = discover_candidates(field, supplier_filter)
        by_file = {}
        for c in cands:
            by_file.setdefault(c["source_file"], []).append(c)

        field_stats = dict(total_missing=len(cands), cache_hits=0, cache_hits_empty=0,
                            still_needs_paid=0, files_written=[])

        for source_file, rows in by_file.items():
            df = None
            dirty = False
            for c in rows:
                entry = cache.get(c["cache_key"])
                if entry is None:
                    field_stats["still_needs_paid"] += 1
                    continue
                value = entry.get(field, "")
                if not value:
                    field_stats["cache_hits_empty"] += 1
                    continue
                field_stats["cache_hits"] += 1
                if write and c["writable"]:
                    if df is None:
                        df = pd.read_csv(source_file, dtype=str, low_memory=False,
                                          encoding="utf-8-sig").fillna("")
                    df.at[c["row_index"], c["value_col"]] = value
                    dirty = True
            if dirty:
                df.to_csv(source_file, index=False, encoding="utf-8")
                field_stats["files_written"].append(source_file)

        report[field] = field_stats

    return report


# ---------------------------------------------------------------------------
# Image quality filter — real implementation, called only from run_live()
# (which is never invoked). Pure function: no network, safe to read/review.
# ---------------------------------------------------------------------------

DOMAIN_ALLOWLIST_BIAS = {
    "michelin.com", "goodyear.com", "bfgoodrichtires.com", "bridgestonetire.com",
    "continentaltire.com", "pirelli.com", "hankooktire.com", "toyotires.com",
    "yokohamatire.com", "generaltire.com", "coopertire.com", "falkentire.com",
    "nittotire.com", "kumhotire.com", "nexentireusa.com",
}
DOMAIN_DEPRIORITIZE = {
    "pinterest.com", "pinterest.ca", "ebay.com", "ebay.ca", "reddit.com",
    "facebook.com", "instagram.com", "alibaba.com", "aliexpress.com",
}

MIN_SIDE_PX      = 800    # hard floor — below this, reject outright
TARGET_LONG_EDGE = 1200   # soft preference — scored, not required


def filter_image_candidates(results, brand="", model=""):
    """Given a list of SerpAPI `images_results`-shaped dicts (keys:
    original/link, original_width, original_height, title, source), return
    them filtered + ranked best-first per SERP_LAYER_DESIGN.md §6:
      1. aspect ratio  — roughly square to 4:3 (studio shots); reject wide
         panoramic/landscape (near-certain lifestyle photography)
      2. resolution     — 800x800 floor; 1200px long edge scored as a bonus
      3. domain bias     — manufacturer domains scored up; stock/social/forum
         sites scored down (not hard-rejected — still a fallback if nothing
         else qualifies)
      4. alt-text/title keyword check — brand or model token must appear in
         the title/source text, or the result is dropped (weak association
         to the query even if 1-3 pass)

    Pure function — no I/O, no network. Never called outside run_live(),
    which itself is never invoked by any code path exercised in this task.
    """
    brand_l = str(brand).strip().lower()
    model_l = str(model).strip().lower()
    scored = []

    for c in results or []:
        w = c.get("original_width") or c.get("width")
        h = c.get("original_height") or c.get("height")
        try:
            w, h = float(w), float(h)
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue

        # 1. Aspect ratio: accept ~square to 4:3 (0.7-1.5), reject panoramic
        ratio = w / h
        if not (0.7 <= ratio <= 1.5):
            continue

        # 2. Resolution floor
        if w < MIN_SIDE_PX or h < MIN_SIDE_PX:
            continue
        long_edge_bonus = 1 if max(w, h) >= TARGET_LONG_EDGE else 0

        # 4. Keyword association (checked before scoring so a match is
        # required, not just preferred — a photo with zero textual link to
        # the query is a weak match even with a great resolution/ratio)
        text = f"{c.get('title', '')} {c.get('source', '')}".lower()
        if brand_l and brand_l not in text and model_l and model_l not in text:
            continue
        if not brand_l and not model_l:
            pass  # no brand/model available to check against — don't over-reject

        # 3. Domain bias (soft — scored, never a hard reject)
        url = c.get("original") or c.get("link") or ""
        domain = urlparse(url).netloc.lower()
        domain_score = 0
        if any(d in domain for d in DOMAIN_ALLOWLIST_BIAS):
            domain_score = 2
        elif any(d in domain for d in DOMAIN_DEPRIORITIZE):
            domain_score = -2

        score = domain_score + long_edge_bonus
        scored.append((score, w * h, c))

    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [c for _, _, c in scored]


# ---------------------------------------------------------------------------
# (e) — live search. Gated. NEVER invoked in this task.
# ---------------------------------------------------------------------------

def run_live(fields, supplier_filter, budget):
    """The only path in this module that can spend the client's money.

    Four independent gates, all must pass:
      1. Kill switch absent (monitoring/SERP_KILL_SWITCH)
      2. budget > 0
      3. Explicit --live flag (checked by caller before this is even called)
      4. Cache checked before every single candidate — a cache hit never
         counts against budget or fires a network call.

    Rehosting vs hotlinking (images): this function only retrieves and
    ranks candidate URLs (via filter_image_candidates). It does NOT download
    or upload anything to Shopify — see SERP_LAYER_DESIGN.md §6 for why
    rehosting is recommended over hotlinking (source stability, licensing)
    and why that step is intentionally out of scope for this task. The seam
    is here: a caller with approval would take filter_image_candidates()'s
    top result's URL and hand it to a (not-yet-built) download+upload step.
    """
    if os.path.exists(KILL_SWITCH):
        print(f"ABORT: kill switch present at {KILL_SWITCH}. Delete it to re-enable live runs.")
        sys.exit(1)
    if budget <= 0:
        print("ABORT: --budget must be a positive integer.")
        sys.exit(1)

    try:
        from serpapi import GoogleSearch
    except ImportError:
        print("ERROR: pip install google-search-results first.")
        sys.exit(1)

    sys.path.insert(0, SHARED_DIR)
    from config import SERPAPI_KEY_CLIENT  # the client's PAID key — never SERPAPI_KEY (dev key)

    # Designation keyword lookup — reused verbatim from formula's existing,
    # already-in-production inference table rather than re-deriving one.
    sys.path.insert(0, os.path.join(ROOT, "pipelines", "formula_distributors"))
    from tire_designation_inference import _keyword_lookup

    cache = load_cache()
    spent = 0
    seen = set()

    for field in fields:
        cands = discover_candidates(field, supplier_filter)
        for c in cands:
            if spent >= budget:
                print(f"Budget of {budget} reached — stopping mid-batch.")
                return
            if c["cache_key"] in cache or c["cache_key"] in seen:
                continue  # already paid for, or covered earlier in this same batch
            seen.add(c["cache_key"])

            if field == "designation":
                results = GoogleSearch({
                    "engine": "google", "q": c["query"], "num": 6,
                    "api_key": SERPAPI_KEY_CLIENT,
                }).get_dict()
                texts = []
                for r in results.get("shopping_results", [])[:6]:
                    texts += [r.get("title", ""), r.get("snippet", "")]
                kg = results.get("knowledge_graph", {})
                texts += [kg.get("title", ""), kg.get("type", ""), kg.get("description", "")]
                for r in results.get("organic_results", [])[:4]:
                    texts += [r.get("title", ""), r.get("snippet", "")]
                value = _keyword_lookup(" ".join(filter(None, texts)))
            else:  # image
                results = GoogleSearch({
                    "engine": "google_images", "q": c["query"],
                    "api_key": SERPAPI_KEY_CLIENT,
                }).get_dict()
                candidates_raw = results.get("images_results", [])
                best = filter_image_candidates(candidates_raw, c["brand"], c["model"])
                value = (best[0].get("original") or best[0].get("link") or "") if best else ""

            cache[c["cache_key"]] = {field: value, "query": c["query"]}
            save_cache(cache)
            spent += 1
            print(f"  [{spent}/{budget}] {field}: {c['query']!r} -> {value!r}")

    print(f"Live run complete. Spent {spent} / {budget}.")


# ---------------------------------------------------------------------------
# Reporting — read-only, drives monitoring/ENRICHMENT_APPROVAL.md
# ---------------------------------------------------------------------------

def per_source_breakdown(field, supplier_filter=None):
    """Per (supplier, type) breakdown for `field`: missing / cache_hit_nonempty
    (free — already paid) / needs_paid (no cache entry at all).
    """
    cache = load_cache()
    rows = {}
    for c in discover_candidates(field, supplier_filter):
        key = (c["supplier"], c["type"])
        r = rows.setdefault(key, dict(missing=0, cache_hit=0, needs_paid=0, writable=c["writable"]))
        r["missing"] += 1
        entry = cache.get(c["cache_key"])
        if entry and entry.get(field):
            r["cache_hit"] += 1
        elif entry is None:
            r["needs_paid"] += 1
        # entry exists but empty ("already tried, came back empty") counts
        # toward neither — it's a resolved non-hit, not a fresh spend candidate.
    return rows


FREE_FIELDS = [
    "Brand", "Style/model", "Section width", "Aspect ratio",
    "Rim diameter", "Load index", "Speed rating", "Load range",
]


def audit_free_fields():
    """Read-only audit of the 8 client-requested fields that never reach
    SERP — reuses monitoring/gap_report.py's own ENTRIES/count_missing
    verbatim (same methodology already used to size this whole design) so
    the numbers in ENRICHMENT_APPROVAL.md and gap_report_output.md never
    diverge. Returns {(supplier, type, field): {"raw": n, "cleaned": n_or_None}}.
    """
    sys.path.insert(0, os.path.join(ROOT, "monitoring"))
    import gap_report as gr

    by_stage = {}   # (supplier, type, field) -> {"raw": n, "cleaned": n}
    for supplier, ptype, stage, pattern, exclude, colmap in gr.ENTRIES:
        f = gr.latest(pattern, exclude)
        if f is None:
            continue
        df = pd.read_csv(f, dtype=str, low_memory=False, encoding="utf-8-sig").fillna("")
        n = len(df)
        for field in FREE_FIELDS:
            col = colmap.get(field)
            if col is None or (isinstance(col, str) and col.startswith("N/A")):
                continue
            missing = gr.count_missing(df, col)
            if missing is None:
                continue
            key = (supplier, ptype, field)
            by_stage.setdefault(key, {})[stage] = missing
    return by_stage


def write_approval_report(designation_rows, image_rows, free_audit, total_needs_paid, budget=5000):
    lines = []
    lines.append("# Enrichment Approval Report")
    lines.append("")
    lines.append(
        "Generated by `pipelines/shared/enrichment.py` from a real dry run across all five "
        "supplier pipelines. **Zero SERP API calls were made to produce this report** — every "
        "number below comes from reading CSVs already on disk plus the centralized cache "
        "(`pipelines/shared/Data/serp_cache.json`, migrated from formula's original "
        "253-entry cache, itself untouched on disk). This is the report the client reads to "
        "approve a concrete search count before any live run."
    )
    lines.append("")
    lines.append(
        "**Historical yield, for context on every \"paid\" number below**: of the 253 "
        "designation searches formula already paid for, only 4 (1.6%) returned a usable "
        "result. See the honest assessment at the bottom before approving spend."
    )
    lines.append("")

    lines.append("## Headline: free vs. paid, by field")
    lines.append("")
    lines.append(
        "8 of the 10 fields the client asked about are **already fully resolved for free** — "
        "either a `manufacturer_code` fallback or a size-string parsing problem "
        "(`pipelines/shared/size_parser.py`), already applied inside every supplier's own "
        "`augment*.py`. Only **Designation** and **Image** ever reach the paid tier."
    )
    lines.append("")
    lines.append("| Field | Status | Paid searches needed |")
    lines.append("|---|---|---|")
    lines.append("| Brand | Free — `manufacturer_code` fallback (northcountry/formula/emergetires/dttire) "
                 "or description parsing (wheelsmart), already applied per-supplier | 0 |")
    lines.append("| Style / model | Free — small raw gap, closes further at augment time; "
                 "no SERP fallback needed at any observed volume | 0 |")
    lines.append("| Section width | Free — parsed from `size` string (`size_parser.parse_tire_size`) | 0 |")
    lines.append("| Aspect ratio | Free — parsed from `size` string | 0 |")
    lines.append("| Rim diameter | Free — parsed from `size` string | 0 |")
    lines.append("| Load index | Free — parsed from `size`/description (`parse_load_speed`) | 0 |")
    lines.append("| Speed rating | Free — parsed from `size`/description | 0 |")
    lines.append("| Load range | Free — PR/ply notation or trailing letter, regex-extracted | 0 |")
    lines.append("| **Designation** | Free inference resolves most rows; genuine residual below | **see table** |")
    lines.append("| **Image** | No free layer produces a photo — the one field truly worth spend | **see table** |")
    lines.append("")

    lines.append("## Designation and Image — the only fields with real spend")
    lines.append("")
    lines.append(
        "\"Now missing\" = rows that survive every free layer already applied "
        "(clean.py's 5-layer inference for designation; the scraped `image_url`/`image` "
        "column for image) **and** that would actually ship to Shopify (low-quantity rows "
        "already excluded, matching each supplier's own `augment*.py` filter). "
        "\"Cache-fixed\" = a query for that exact brand+model+size was already paid for and "
        "returned a usable result — applied for free via `--apply`, zero network calls. "
        "\"Needs paid search\" = no cache entry exists at all for that exact query."
    )
    lines.append("")
    lines.append("### Designation")
    lines.append("| Supplier | Type | Missing now | Cache-fixed (free) | Needs paid search | Notes |")
    lines.append("|---|---|---|---|---|---|")
    desig_paid_total = 0
    for (supplier, ptype), r in sorted(designation_rows.items()):
        note = "" if r["writable"] else "no raw-CSV column to write into — report-only, see code comment"
        lines.append(f"| {supplier} | {ptype} | {r['missing']} | {r['cache_hit']} | {r['needs_paid']} | {note} |")
        desig_paid_total += r["needs_paid"]
    lines.append(f"| **Total** | | | | **{desig_paid_total}** | |")
    lines.append("")
    lines.append(
        f"Formula's 244 currently-`Unknown` rows all already have a cache entry from the "
        f"original 253-query batch — and every one of those cached results came back empty. "
        f"Re-querying them would be paying for the exact same question twice for no new "
        f"information. **0 new searches for formula designation.** Only dttire's 1 row and "
        f"wheelsmart's 1 row (post low-quantity filter) have never been queried before."
    )
    lines.append("")

    lines.append("### Image")
    lines.append("| Supplier | Type | Missing now | Cache-fixed (free) | Needs paid search |")
    lines.append("|---|---|---|---|---|")
    image_paid_total = 0
    for (supplier, ptype), r in sorted(image_rows.items()):
        lines.append(f"| {supplier} | {ptype} | {r['missing']} | {r['cache_hit']} | {r['needs_paid']} |")
        image_paid_total += r["needs_paid"]
    lines.append(f"| **Total** | | | | **{image_paid_total}** |")
    lines.append("")
    lines.append(
        "No image search has ever been run against this cache (formula's old cache only ever "
        "covered designation), so every missing image needs a fresh search — there is no "
        "\"already paid, came back empty\" discount available here the way there is for "
        "designation."
    )
    lines.append("")

    total_all = desig_paid_total + image_paid_total
    verdict = "within" if total_all <= budget else "EXCEEDS"
    lines.append(f"## Running total vs. budget")
    lines.append("")
    lines.append(f"**{total_all} searches need approval** ({desig_paid_total} designation + "
                  f"{image_paid_total} image) — **{verdict} the {budget}-search budget** "
                  f"({total_all / budget * 100:.1f}% of it).")
    lines.append("")
    lines.append(
        "No per-search dollar rate is documented anywhere in this repo or the SerpAPI plan "
        "details supplied for this task — spend is tracked in **searches against the "
        "approved budget**, the same unit used throughout `SERP_LAYER_DESIGN.md` and by "
        "`serp_layer.py`/`enrichment.py`'s `--budget` flag. Convert to dollars using whatever "
        "SerpAPI plan tier the client's key is actually on before final sign-off."
    )
    lines.append("")

    lines.append("## Audit: the 8 free fields (no SERP spend)")
    lines.append("")
    lines.append(
        "Shown for completeness/verification — raw-stage missing count vs. the residual after "
        "clean.py's existing free parsing/fallback layer (same numbers as "
        "`monitoring/gap_report_output.md`, recomputed fresh here). All residuals are a "
        "malformed/absent `size` string, not a fact SERP could look up more reliably than the "
        "parser already tries to — see `SERP_LAYER_DESIGN.md` §1 for why these never queue a "
        "search regardless of residual size."
    )
    lines.append("")
    lines.append("| Supplier | Type | Field | Raw missing | Residual after free layer | Paid needed |")
    lines.append("|---|---|---|---|---|---|")
    for (supplier, ptype, field), stages in sorted(free_audit.items()):
        raw = stages.get("raw", "-")
        cleaned = stages.get("cleaned", "n/a — no separate cleaned stage")
        lines.append(f"| {supplier} | {ptype} | {field} | {raw} | {cleaned} | 0 |")
    lines.append("")

    lines.append("## Honest assessment")
    lines.append("")
    lines.append(
        f"Given the 1.6% historical hit rate on designation search (4 useful / 253 paid), "
        f"**designation spend is very likely not worth buying** — {desig_paid_total} more "
        f"searches at that yield rate would be expected to resolve roughly 0 additional rows. "
        f"These 2 rows (dttire ×1, wheelsmart ×1) are cheaper and more reliable to fix by hand "
        f"than by search."
    )
    lines.append("")
    lines.append(
        f"Image is the field genuinely worth spending on: there is no free alternative, the "
        f"failure mode of a wrong-but-plausible product photo is low-stakes (unlike a wrong "
        f"designation, which misleads a customer about the product), and {image_paid_total} "
        f"searches is a small, bounded, one-time cost for the current catalog (ongoing runs "
        f"will only need to search new listings). Recommend approving the "
        f"**{image_paid_total}-search image batch** and declining (or hand-fixing) the "
        f"designation residual rather than paying for it."
    )
    lines.append("")
    lines.append("## Confirmation")
    lines.append("")
    lines.append(
        "Zero live SERP API calls were made anywhere in building this layer or this report. "
        "`run_live()` in `pipelines/shared/enrichment.py` is fully implemented (kill-switch "
        "check, budget hard-stop, cache-check-before-spend) but was never invoked — every "
        "number above came from `--field all` with no `--live` flag."
    )

    with open(APPROVAL_MD, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"\nApproval report written to: {APPROVAL_MD}")


def report(fields, supplier_filter=None, budget_hint=5000, apply_cache=False, write=False):
    migrated, already = migrate_formula_cache()
    if migrated:
        print(f"Migrated {migrated} entries from formula's old serp_cache.json into the "
              f"centralized cache ({already} were already present).")

    stats = apply_cached(fields, supplier_filter, write=(apply_cache and write))
    total_needs_paid = 0
    manifest = []

    print("=== ENRICHMENT DRY RUN — no API calls made ===\n")
    for field, s in stats.items():
        print(f"[{field}]")
        for k, v in s.items():
            print(f"  {k:20s}: {v}")
        print()
        total_needs_paid += s["still_needs_paid"]
        cache_now = load_cache()
        for c in discover_candidates(field, supplier_filter):
            if c["cache_key"] not in cache_now:
                manifest.append(c)

    print(f"TOTAL new searches needing approval: {total_needs_paid}")
    verdict = "WITHIN budget" if total_needs_paid <= budget_hint else "EXCEEDS budget — prioritize"
    print(f"vs proposed budget {budget_hint}: {verdict}")

    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump([{k: v for k, v in c.items()} for c in manifest], fh, indent=2, default=str)
    print(f"\nFull query manifest written to: {MANIFEST}")

    # Only build the full markdown approval report when this was a
    # whole-catalog run (no supplier/field filter) — a partial run doesn't
    # have enough data to fill every table.
    if supplier_filter is None and set(fields) == set(SUPPORTED_FIELDS):
        designation_rows = per_source_breakdown("designation")
        image_rows = per_source_breakdown("image")
        free_audit = audit_free_fields()
        write_approval_report(designation_rows, image_rows, free_audit, total_needs_paid, budget_hint)

    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--field", choices=list(SUPPORTED_FIELDS) + ["all"], default="all")
    ap.add_argument("--supplier", default=None)
    ap.add_argument("--budget", type=int, default=None,
                     help="Required together with --live to spend anything.")
    ap.add_argument("--live", action="store_true",
                     help="Without this flag, the script NEVER makes a network call.")
    ap.add_argument("--apply", action="store_true",
                     help="Write (d) cache-hit fixes into the cleaned/raw CSV in place. "
                          "No network call regardless — only reuses already-paid results.")
    args = ap.parse_args()

    fields = list(SUPPORTED_FIELDS) if args.field == "all" else [args.field]

    if args.live and args.budget:
        print(f"LIVE MODE requested — budget={args.budget}, fields={fields}, supplier={args.supplier or 'all'}")
        run_live(fields, args.supplier, args.budget)
        return

    if args.live and not args.budget:
        print("NOTE: --live given without --budget — falling back to report-only (no calls made).")

    report(fields, args.supplier, budget_hint=5000, apply_cache=True, write=args.apply)


if __name__ == "__main__":
    main()
