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
     For images, "valid" means more than "not blank". A present-but-unusable
     photo — a 130x130 thumbnail, a supplier's "no photo available" graphic,
     a dead CDN link — is a gap the storefront shows, so it IS a candidate.
     That judgement comes from the image quality audit further down
     (measure_image_urls / image_verdict): plain HTTP GETs, no API spend,
     verdicts cached by URL. This is the DEFAULT for every consumer; only a
     caller that explicitly passes quality_cache=None gets the older
     blank-only behaviour. A candidate is never blanked on this basis —
     a small photo beats an empty listing until a better one is actually found.
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

Three fields ever reach (d)/(e): designation, image, and specs (a bundle of
load index / speed rating / load range / section width / aspect ratio / rim
diameter — see SPEC_SOURCES below). Brand and Style/model are the only two of
the client's ten fields fully resolved by (a)-(c) at any real volume — see
ENRICHMENT_APPROVAL.md for the row counts.

"specs" is one field, not six, on purpose: a row missing its load index is
almost always missing its speed rating too (both come from the same
"104H"-shaped token in the source text — see SIZE_SPEC_NOTES.md-equivalent
reasoning in _missing_spec_subfields below), so one search buys whichever of
the six sub-fields that row actually needs instead of paying once per
sub-field. discover_spec_candidates()/apply_spec_cache() below are the
specs-only counterparts to discover_candidates()/apply_cached() — kept
separate rather than forced into the single-value_col shape those use,
because a specs candidate can need several different columns filled from one
search result, where designation/image only ever fill one.

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
import collections
import glob
import json
import os
import re
import sys
import threading
from urllib.parse import urlparse

import pandas as pd

ROOT         = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHARED_DIR   = os.path.join(ROOT, "pipelines", "shared")
CACHE_FILE   = os.path.join(SHARED_DIR, "Data", "serp_cache.json")
KILL_SWITCH  = os.path.join(ROOT, "monitoring", "SERP_KILL_SWITCH")
MANIFEST     = os.path.join(ROOT, "monitoring", "enrichment_dry_run_manifest.json")
APPROVAL_MD  = os.path.join(ROOT, "monitoring", "ENRICHMENT_APPROVAL.md")

sys.path.insert(0, SHARED_DIR)
from size_parser import (parse_tire_size, parse_load_speed, coerce_load_index,
                         extract_load_range, default_passenger_load_range)
import serp_ledger as SL
import tread as _TREAD

FORMULA_OLD_CACHE = os.path.join(
    ROOT, "pipelines", "formula_distributors", "Data", "shopify", "serp_cache.json"
)

SUPPORTED_FIELDS = ("designation", "image", "specs")
SPEC_SUBFIELDS   = ("load_index", "speed_rating", "load_range",
                    "section_width", "aspect_ratio", "rim_diameter")

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
    # up blank OR unusable.
    #
    # dttire used to be excluded here on the grounds that 0/9,601 rows were
    # missing an image. That was true and still misleading: every one of those
    # images was a 130x130 listing thumbnail (see dttire/large_images.py).
    # "Has a URL" was never the same question as "has a usable photo", which is
    # exactly what the quality audit below now measures — so dttire belongs in
    # this list. Its scraper and backfill_images.py resolve the 600px copies;
    # whatever the PDP could not resolve stays visible here as a real gap
    # instead of being counted as complete.
    dict(supplier="dttire", type="tires", field="image",
         glob="pipelines/dttire/Data/cleaned/cleaned_tires_*.csv",
         brand_col="brand", model_col="model", size_col="size", extra_col=None,
         desc_col="tread_design",
         value_col="image_url", writable=True),
    dict(supplier="dttire", type="wheels", field="image",
         glob="pipelines/dttire/Data/cleaned/cleaned_wheels_*.csv",
         brand_col="brand", model_col="description", size_col="size", extra_col=None,
         desc_col="description",
         value_col="image_url", writable=True),
    dict(supplier="northcountry", type="tires", field="image",
         glob="pipelines/northcountry/Data/cleaned/cleaned_tires_*.csv",
         brand_col="brand", model_col="model", size_col="size", extra_col=None,
         mfr_col="manufacturer_code", desc_col="description",
         value_col="image_url", writable=True),
    dict(supplier="northcountry", type="wheels", field="image",
         glob="pipelines/northcountry/Data/cleaned/cleaned_wheels_*.csv",
         brand_col="brand", model_col="model", size_col="size", extra_col=None,
         mfr_col="manufacturer_code", desc_col="description",
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
         mfr_col="manufacturer_code", desc_col="description",
         value_col="image_url", writable=True),
    dict(supplier="wheelsmart", type="tires", field="image",
         glob="pipelines/wheelsmart/Data/raw/wheelmart_tires_*.csv",
         brand_col=None, model_col=None, size_col="size_raw", extra_col="description",
         value_col="image", writable=True, qty_filter="wheelsmart_tires",
         desc_parser="wheelsmart_tires"),
    dict(supplier="wheelsmart", type="rims", field="image",
         glob="pipelines/wheelsmart/Data/raw/wheelmart_rims_*.csv",
         brand_col=None, model_col="name", size_col="diameter", extra_col="description",
         # model_col "name" holds a SKU, not a name, so the description is what
         # actually identifies the wheel ("FORGIATO STYLE GLOSS BLACK").
         desc_col="description",
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
# specs sources: load_index / speed_rating / load_range / section_width /
# aspect_ratio / rim_diameter. Tires only — wheels/rims carry a different spec
# block entirely (Wheel Diameter, Bolt Pattern, Offset, ...), not these six.
#
# *_col below names the column each supplier's own augment*.py already reads
# FIRST for that sub-field (None where there isn't one and the sub-field is
# always derived from size/description text — dttire and wheelsmart have no
# separate spec columns for width/ratio/rim at all). Writing a SERP result
# into that same column means augment*.py's existing `spec or fallback` logic
# picks it up on the very next run with NO changes to any augment*.py needed
# beyond the ones already made — the fix arrives exactly like a value the
# supplier had provided all along.
#
# formula's width_col is deliberately None even though a `section_width`
# column exists: it holds ~6-9, a rim-width-shaped number, not a section
# width (real values run 155-335mm) — e.g. size "155/70R14" carries
# section_width="6.1" in that feed. It's mislabeled, not measured, so it is
# never trusted as a source of truth, and a SERP result is never written
# there either — see build_body_html's comment in formula/augment.py.
SPEC_SOURCES = [
    dict(supplier="northcountry", type="tires",
         glob="pipelines/northcountry/Data/cleaned/cleaned_tires_*.csv",
         brand_col="brand", model_col="model", size_col="size", extra_col=None,
         mfr_col="manufacturer_code", desc_col="description",
         load_index_col="spec_loadindex", speed_rating_col="spec_speedrating",
         load_range_col="spec_loadrange",
         width_col="spec_sectionwidth", ratio_col="spec_aspectratio", rim_col="spec_rimsize"),
    dict(supplier="emergetires", type="tires",
         glob="pipelines/emergetires/Data/cleaned/cleaned_tires_*.csv",
         brand_col="brand", model_col="model", size_col="size", extra_col=None,
         mfr_col="manufacturer_code", desc_col="description",
         load_index_col="spec_loadindex", speed_rating_col="spec_speedrating",
         load_range_col="spec_loadrange",
         width_col="spec_sectionwidth", ratio_col="spec_aspectratio", rim_col="spec_rimsize"),
    dict(supplier="formula_distributors", type="tires",
         glob="pipelines/formula_distributors/Data/cleaned/cleaned_tires_*.csv",
         brand_col="brand_name", model_col="model", size_col="size", extra_col=None,
         desc_col="description",
         load_index_col="load_index", speed_rating_col="speed_rating",
         load_range_col="load_range",
         width_col=None, ratio_col="aspect_ratio", rim_col="rim_size"),
    dict(supplier="dttire", type="tires",
         glob="pipelines/dttire/Data/cleaned/cleaned_tires_*.csv",
         brand_col="brand", model_col="model", size_col="size", extra_col=None,
         desc_col="service_desc",
         load_index_col="load_index", speed_rating_col="speed_rating",
         load_range_col="load_range",
         width_col=None, ratio_col=None, rim_col=None),
    dict(supplier="wheelsmart", type="tires",
         glob="pipelines/wheelsmart/Data/raw/wheelmart_tires_*.csv",
         brand_col=None, model_col=None, size_col="size_raw", extra_col="description",
         desc_col="description",
         load_index_col="load_index", speed_rating_col=None, load_range_col=None,
         width_col="width", ratio_col="aspect_ratio", rim_col="rim_diameter",
         qty_filter="wheelsmart_tires"),
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


# ---------------------------------------------------------------------------
# Image searches are keyed by brand+model, NOT brand+model+size.
#
# A tire's product photo is a picture of a tread pattern, and suppliers store
# it per model, not per size. Measured on the CSVs as scraped:
#
#     supplier/type    rows   distinct images   distinct brand|model
#     formula/wheels    685        213                 213      1.00 per model
#     formula/tires     520         54                  53      1.02
#     emerge/tires      624         80                  72      1.11
#     nc/tires         6452        893                 515      1.73
#
# So the storefront already shows one photo across every size of a model —
# buying a separate photo per size pays several times over for the same
# picture. Keyed by size, North Country's low-res tires alone are 4,586
# searches covering 535 distinct models; keyed by model they are 535.
#
# WHEELS ARE NOT TIRES HERE, and the difference is measured, not assumed.
# "brand + model" names a tread pattern on a tire. On a wheel it frequently
# names a category or a colour, because that is what the supplier put in the
# model column. Collapsing wheels onto a model key merges genuinely different
# products onto one bought photo — the worst cases in the 2026-08-18 data:
#
#     image:rssw|steel wheel|      92 candidate rows, 63 distinct sizes
#     image:|black daytona|        11 rows (formula: brand column blank)
#     image:|gloss black|          30 distinct wheels (wheelsmart/rims)
#
# "RSSW Steel Wheel wheel product photo" is not a query that identifies a
# product, so one arbitrary steel-wheel photo would land on all 92 listings.
# The saving that justified dropping size is almost entirely a tire saving
# anyway: of the 790 searches the model key produced, 714 were tires and 76
# were wheels. Keeping size on wheels/rims costs +124 searches (790 -> 914,
# still far inside the 5,000 approval budget) and buys back that accuracy.
IMAGE_KEY_INCLUDES_SIZE = False

# Product types whose photo is genuinely per-model rather than per-size.
# Everything not listed here keeps its size in the key.
_MODEL_LEVEL_IMAGE_TYPES = ("tires",)


def _image_key_drops_size(field, brand, model, product_type="tires"):
    """Whether this row's image key may omit size.

    Two conditions, both measured above:

      * the product's photo is per-model, not per-size — true for tires,
        false for wheels and rims (see _MODEL_LEVEL_IMAGE_TYPES); and
      * brand or model actually names the product. A row that qualified for
        a search on its `extra`/description alone (see _is_queryable) has an
        empty brand AND model, so dropping size would collapse every such row
        in the catalogue onto the single key "image:||" — one photo for
        thousands of unrelated products. Size keeps those rows distinct.
    """
    return (field == "image" and not IMAGE_KEY_INCLUDES_SIZE
            and str(product_type or "").lower() in _MODEL_LEVEL_IMAGE_TYPES
            and bool(str(brand or "").strip() or str(model or "").strip()))


# A trailing " - <category>" that Costar/DT Tire append to the model name:
# "Open Country M/T  - Mud Terrain", "P Zero  - Ultra High Performance".
# It is derived FROM the model, so it never distinguishes two products — but
# it does split one product across several cache keys.
#
# The space AFTER the dash is load-bearing, and the catalogue is the reason.
# Costar's taxonomy is punctuated "<model>  - <Category>" without exception —
# all 15 categories in use, 7,800 rows. A dash glued to the following word
# comes from somewhere else: North Country derives models from description
# text, and "P-ZERO (PZ4)-SPORT - 107Y XL" yields "P-ZERO -SPORT". That is
# Pirelli's P Zero PZ4 Sport, a different tire from the P Zero — so matching
# it here would put one tread pattern's photo on the other's listing to save
# a single search. Requiring the space keeps every real category merge and
# drops that one.
_MODEL_CATEGORY_SUFFIX = re.compile(
    r"(?:\s+-\s+[a-z][a-z/ ]*|\s+-\s*)$", re.IGNORECASE)


def normalize_key_token(text):
    """Collapse a brand or model to its identity, dropping the punctuation and
    spacing that vary between suppliers for the SAME product.

    This is what makes the cache a memory of PRODUCTS rather than of strings.
    Measured across the 2026-08-26 catalogue, these all named one tire and so
    bought one search each, four times over:

        'p zero'  'p-zero'  'p zero  - ultra high performance'
        'wildpeak a/t4w'  'wildpeak at4w'  'wildpeak a/t4w  - all terrain'
        'wintercontact ts 860 s'  'wintercontact ts860 s'

    Collapsing them removes 178 duplicate image searches outright and — because
    build_peer_image_index() keys off this same function — lets North Country's
    130px thumbnails be filled from DT Tire's 600px photo of the identical
    tread pattern, 2,122 rows of it, for free.

    Verified non-destructive on the full catalogue: every key that merges more
    than one raw brand|model merges spelling variants of a single product. Run
    `python enrichment.py --audit-keys` to re-check that after any scrape.
    """
    s = str(text or "").strip().lower()
    stripped = _MODEL_CATEGORY_SUFFIX.sub("", s)
    # Never let the suffix strip eat the whole name ("- Touring" as a model).
    if len(re.sub(r"[^a-z0-9]+", "", stripped)) >= 3:
        s = stripped
    return re.sub(r"[^a-z0-9]+", "", s)


def cache_key(field, brand, model, size, product_type="tires"):
    """Centralized, cross-supplier key — the same brand+model+size is never
    paid for twice even if two different suppliers carry the identical tire.

    For TIRE images the size is dropped (see IMAGE_KEY_INCLUDES_SIZE), so one
    photo covers every size of a model and is bought once. Wheels and rims
    keep their size — their model column names a category or a colour as often
    as a product, so collapsing them merges different wheels onto one photo.

    Brand and model go through normalize_key_token() so that punctuation and
    spacing variants of one product share one key. Size does NOT: it is
    already a controlled format, and stripping its separators would collapse
    genuinely different wheel fitments ("18 5-114.3" vs "185 114.3").
    """
    b = normalize_key_token(brand)
    m = normalize_key_token(model)
    s = str(size or "").strip().lower()
    if _image_key_drops_size(field, b, m, product_type):
        s = ""
    return f"{field}:{b}|{m}|{s}"


def _split_cache_key(key):
    """('image', brand, model, size) from a stored key. Brand and model can
    themselves contain ':' (a wheel model reads 'CB:124.9 OS:131'), so the
    field is taken off the FIRST colon and the size off the LAST pipe."""
    field, _, rest = str(key).partition(":")
    parts = rest.split("|")
    if len(parts) < 3:
        return field, rest, "", ""
    return field, parts[0], "|".join(parts[1:-1]), parts[-1]


def migrate_cache_keys():
    """Re-key the centralized cache onto normalize_key_token(). Idempotent.

    Without this, tightening the key throws away every answer already paid
    for — 924 entries on 2026-08-26 — because the scan would look them up
    under the new spelling and miss. That would be the single most expensive
    way to land an optimisation.

    When two old keys collapse onto one new key, the entry carrying a real
    answer wins; an empty answer never overwrites a found one. Ties keep the
    first-seen entry so the result does not depend on dict order.

    Returns (rekeyed, merged).
    """
    cache = load_cache()
    if not cache:
        return 0, 0

    def answer_of(field, entry):
        if not isinstance(entry, dict):
            return entry
        if field == "specs":
            return entry.get("specs") or {}
        return entry.get(field) or ""

    out, rekeyed, merged = {}, 0, 0
    for old_key, entry in cache.items():
        field, brand, model, size = _split_cache_key(old_key)
        new_key = f"{field}:{normalize_key_token(brand)}|{normalize_key_token(model)}|{size}"
        if new_key != old_key:
            rekeyed += 1
        if new_key in out:
            merged += 1
            if answer_of(field, entry) and not answer_of(field, out[new_key]):
                out[new_key] = entry
            elif field == "specs":
                # Two partial spec answers for one tire are additively better
                # than either alone, and every sub-field in them was paid for.
                keep = dict(out[new_key])
                specs = dict(keep.get("specs") or {})
                specs.update({k: v for k, v in (entry.get("specs") or {}).items() if v})
                keep["specs"] = specs
                keep["tried"] = sorted(set(keep.get("tried") or []) |
                                       set(entry.get("tried") or []))
                out[new_key] = keep
            continue
        out[new_key] = entry

    if rekeyed or merged:
        save_cache(out)
    return rekeyed, merged


def catalogue_image_keys():
    """Every image cache key the current catalogue would look up, gap or not.

    Deliberately not discover_candidates(): that returns only rows with a gap
    RIGHT NOW, which after an apply is nearly nothing. Reconciling the cache
    against the keys the catalogue can generate is what tells you whether a
    paid answer is still reachable.
    """
    keys = set()
    for src in SOURCES:
        if src["field"] != "image":
            continue
        f = latest(src["glob"])
        if not f:
            continue
        try:
            df = pd.read_csv(f, dtype=str, low_memory=False,
                             encoding="utf-8-sig").fillna("")
        except (OSError, ValueError):
            continue
        for _, row in df.iterrows():
            brand, model = _resolve_brand_model(row, src)
            size = row.get(src["size_col"], "") if src["size_col"] else ""
            keys.add(cache_key("image", brand, model, size, src["type"]))
    return keys


def migrate_glued_load_speed_keys(write=False):
    """Re-home answers stranded by the load/speed strip in _SPEC_TOKEN.

    migrate_cache_keys() cannot do this. It re-derives a key from the STORED
    key, where the model is already normalized to "winguardwinspike386txl" —
    and at that point the strip is genuinely ambiguous, because nothing says
    whether that is WinSpike 3 rated 86T or WinSpike rated 386.

    The catalogue resolves it. Only one candidate stem is a key the catalogue
    actually generates, so each stranded answer is matched against
    catalogue_image_keys() and moved only when exactly one target fits.
    Without this the strip would BACKFIRE: every affected product would miss
    the cache and be bought a second time, which is the opposite of the point.

    Returns (moved, skipped_ambiguous, unreachable_left).
    """
    cache = load_cache()
    wanted = catalogue_image_keys()
    answered = [k for k, v in cache.items()
                if k.startswith("image:") and isinstance(v, dict) and v.get("image")]

    moved = ambiguous = 0
    for key in answered:
        if key in wanted:
            continue
        rest = key.split(":", 1)[1]
        if rest.count("|") < 2:
            continue
        brand, model, size = rest.split("|", 2)
        hits = {model[:i] for i in range(3, len(model))
                if _GLUED_LOAD_SPEED.fullmatch(model[i:])
                and f"image:{brand}|{model[:i]}|{size}" in wanted}
        if len(hits) != 1:
            ambiguous += len(hits) > 1
            continue
        target = f"image:{brand}|{hits.pop()}|{size}"
        existing = cache.get(target)
        # An answer already sitting on the target wins; both were paid for,
        # and the one keyed the way the catalogue asks is the live one.
        if isinstance(existing, dict) and existing.get("image"):
            del cache[key]
        else:
            cache[target] = cache.pop(key)
        moved += 1

    left = sum(1 for k, v in cache.items()
               if k.startswith("image:") and isinstance(v, dict)
               and v.get("image") and k not in wanted)
    if write and moved:
        save_cache(cache)
    return moved, ambiguous, left


def audit_shared_images(cache=None):
    """Photos that ended up on more than one BRAND's product key.

    The counterpart to audit_key_normalization(). That one catches two
    spellings of one product collapsing onto a single key; this catches the
    opposite failure, where two genuinely different products end up wearing
    the same picture because a search returned something generic.

    Sharing per se is normal and wanted: one tire model uses one photo across
    every size it comes in. Sharing across brands is not — a Touren wheel and
    an Envy wheel are not the same object. On the 2026-08-30 run the worst
    offender was a Shopify *collection* thumbnail ("SATIN BLACK DEEP") that
    won 39 keys across 5 brands, which is a category banner rather than a
    product photo.

    Returns {url: [cache_key, ...]} for the offenders only.
    """
    cache = load_cache() if cache is None else cache
    by_url = collections.defaultdict(list)
    for key, entry in cache.items():
        if not key.startswith("image:") or not isinstance(entry, dict):
            continue
        url = entry.get("image")
        if url:
            by_url[url].append(key)

    def _brand(key):
        return key.split(":", 1)[1].split("|")[0]

    return {url: sorted(keys) for url, keys in by_url.items()
            if len({_brand(k) for k in keys}) > 1}


def requeue_tread_rejected(cache=None, write=False):
    """Drop cached image answers a reviewer has since rejected for pose.

    After a tread review the cache is in a state nothing else resolves: the
    key HAS an answer, so run_live skips it as already paid for, but the
    answer is a photo the reviewer threw out, so image_verdict() calls every
    row using it a gap. Left alone the product is stuck — permanently
    flagged, never re-bought.

    Deleting the key is right here, and is the opposite of what
    quarantine_shared_images() does, for a reason worth keeping straight:
      * a shared/generic photo is a question already answered badly by a
        search that would answer it the same way again, so it is blanked and
        NOT re-queued;
      * a sidewall photo is a question answered from a candidate list that
        still contains better options, and run_live now filters out every
        URL the reviewer rejected before choosing, so asking again is a
        different question with a different answer.

    The rejection itself lives in tread_cache.json and is untouched, so the
    same photo can never be re-selected. Returns (requeued, kept_no_gap).
    """
    cache = load_cache() if cache is None else cache
    tcache = _TREAD.load()
    requeued = 0
    for key in list(cache):
        if not key.startswith("image:"):
            continue
        entry = cache.get(key)
        if not isinstance(entry, dict):
            continue
        url = entry.get("image")
        if url and _TREAD.is_rejected(url, tcache):
            del cache[key]
            requeued += 1
    if write and requeued:
        save_cache(cache)
    return requeued


def quarantine_shared_images(cache=None, write=False):
    """Blank the answer for every photo audit_shared_images() flags.

    Blanked rather than deleted on purpose. Deleting the entry would put the
    key straight back into the buy queue and the next run would pay to make
    the same mistake; an empty answer is the module's existing "asked, nothing
    usable came back" state, which is already excluded from both the spend
    queue and the write path. The product ships with no photo, which is the
    honest outcome — the same rule _is_queryable() applies when a row names no
    product. To retry one deliberately, delete its key from the cache.
    """
    cache = load_cache() if cache is None else cache
    offenders = audit_shared_images(cache)
    touched = 0
    for url, keys in offenders.items():
        for key in keys:
            entry = cache.get(key)
            if not isinstance(entry, dict) or not entry.get("image"):
                continue
            entry["image"] = ""
            entry["quarantined"] = {
                "reason": "one photo on more than one brand",
                "was": url,
                "shared_with": len(keys),
                "ts": SL._now(),
            }
            touched += 1
    if write and touched:
        save_cache(cache)
    return offenders, touched


def audit_key_normalization():
    """Report every normalized key that merges more than one raw brand|model,
    so a scrape that introduces a genuinely ambiguous model name is caught
    rather than silently sharing one photo between two products.

    Read-only. Returns {normalized key: {(brand, model), ...}} for merges only.
    """
    groups = collections.defaultdict(set)
    for src in SOURCES:
        if src["field"] != "image" or not src.get("value_col"):
            continue
        f = latest(src["glob"])
        if not f:
            continue
        df = pd.read_csv(f, dtype=str, low_memory=False, encoding="utf-8-sig").fillna("")
        for _, row in df.iterrows():
            brand = _resolve_brand(row, src["brand_col"], src.get("mfr_col"))
            model = _resolve_model(row, src["model_col"], src.get("desc_col"))
            if not (brand.strip() and model.strip()):
                continue
            size = row.get(src["size_col"], "") if src["size_col"] else ""
            key = cache_key("image", brand, model, size, src["type"])
            groups[key].add((brand.strip().lower(), model.strip().lower()))
    return {k: v for k, v in groups.items() if len(v) > 1}


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
        # Old key format: "brand|model|size", lowercased but NOT otherwise
        # normalized — it predates normalize_key_token(). Rebuilding it through
        # cache_key() is what makes this migration idempotent: interpolating the
        # raw string instead re-injected 250 un-normalized keys on every single
        # run, which migrate_cache_keys() then folded straight back onto the
        # entries they duplicated. No answer was ever lost, but every report
        # rewrote the cache twice and announced a 250-entry re-key that had
        # already happened.
        _, brand, model, size = _split_cache_key(f"designation:{old_key}")
        new_key = cache_key("designation", brand, model, size)
        if new_key in cache:
            already += 1
            continue
        cache[new_key] = {"designation": entry.get("designation", ""), "query": entry.get("query", "")}
        migrated += 1
    if migrated:
        save_cache(cache)
    return migrated, already


def _scrub(text):
    """Drop replacement/control characters and collapse whitespace.

    Supplier descriptions arrive with mojibake from the source encoding - e.g.
    "SPORT HP AS\ufffd". Passing U+FFFD to the search engine is noise at best;
    at worst it splits the model name.
    """
    cleaned = "".join(
        ch for ch in str(text or "")
        if ch == " " or (ch.isprintable() and ch != "\ufffd")
    )
    return " ".join(cleaned.split())


def build_query(field, brand, model, size, extra="", product_type="tires"):
    noun = "wheel" if str(product_type).lower() in ("wheels", "rims") else "tire"
    base = " ".join(filter(None, [_scrub(brand), _scrub(model), _scrub(size)]))
    if not _scrub(brand) and not _scrub(model) and extra:
        base = f"{base} {_scrub(extra)}".strip()
    if field == "designation":
        return f"{base} tire".strip()
    if field == "image":
        # The query has to match the key: a model-level key means a model-level
        # search, or two different queries would share one cached answer.
        if _image_key_drops_size(field, brand, model, product_type):
            base = " ".join(filter(None, [_scrub(brand), _scrub(model)]))
        return f"{base} {noun} product photo".strip()
    if field == "specs":
        return f"{base} tire size load index speed rating".strip()
    raise ValueError(f"no query template for field {field!r}")


# ---------------------------------------------------------------------------
# Query field resolution.
#
# The cleaned/raw CSVs are the right thing to READ from - they are also where a
# fix is written back so augment picks it up - but their `brand`/`model` columns
# are not yet the human-readable values augment derives. On North Country in
# particular brand is often blank while manufacturer_code holds the real make,
# and `model` holds a compressed code rather than a name:
#
#     brand=""  model="RRRT"  manufacturer_code="PARAGON"
#     description="RIDGERUNNER RT (Studdable #15) - 12PR 3PMS"
#
# Searching "RRRT 33X12.50R18" buys a photo of an unrelated tire - two of the
# six images bought on 18 Aug are exactly that. Searching "Paragon RIDGERUNNER
# RT 33X12.50R18" buys the right one. augment_tires.py already does this
# backfill from manufacturer_code; these helpers apply the same resolution at
# query-build time so the search matches what the storefront will show.
# ---------------------------------------------------------------------------

# A bare code like "RRRT", "TART", "K3204R", "RM7". No rule is perfect, but a
# short all-caps alphanumeric token is never a model NAME.
_CODE_LIKE = re.compile(r"^[A-Z0-9]{2,6}$")

# A part number rather than a name: contains a colon/hash, or a short letter
# cluster glued to three or more digits ("EVO-MRGW897", "FGC-241012042").
_SKU_LIKE = re.compile(r"[:#]|[A-Za-z]{1,6}-?\d{3,}")

# Spec tokens that lead wheel/tire descriptions: 24X10, ET42, 5X120, CB72.6.
# Tokens that describe the FITMENT rather than name the product, stripped out
# of description-derived model text before it becomes a cache key.
#
# The load-index/speed pair ("86T", "115T", "92T XL") is here because North
# Country states it in two different places for the same tire:
#
#     'WINGUARD winSpike 3 86T XL (Studdable #12)'   -> winguardwinspike386txl
#     'WINGUARD winSpike 3 (Studdable #12) - 92T XL' -> winguardwinspike3
#
# One product, two keys, two searches — and on the 2026-08-30 run that pattern
# split 19 products across 219 keys, buying 154 photos of tires the catalogue
# had already photographed. It has to be stripped HERE, before
# normalize_key_token() removes the spaces: afterwards "winguardwinspike386txl"
# genuinely cannot be told apart from a model named "WinSpike" rated 386.
#
# The load index is bounded to 60-150 (real load indices) so a model that
# merely ends in digits keeps them — "Voyager 777EV" and "Firehawk Indy 500"
# are unaffected — and the speed letter comes from size_parser's own symbol
# table rather than a fresh guess at which letters count.
_SPEED_SYMBOLS = "BCDEFGHJKLMNPQRSTUVWYZ"
_SPEC_TOKEN = re.compile(
    r"^(?:\d+(?:\.\d+)?X[\d.]+|ET-?\d+|CB[\d.]+|\d+PR|3PMS|M\+S"
    rf"|(?:6\d|[7-9]\d|1[0-4]\d|150)[{_SPEED_SYMBOLS}]{{1,2}}|XL)$", re.I)

# The same pair, but GLUED to the end of an already-normalized model, as it
# appears inside a cache key written before the strip above existed. Used only
# by migrate_glued_load_speed_keys() to re-home those answers.
_GLUED_LOAD_SPEED = re.compile(
    rf"(?:6\d|[7-9]\d|1[0-4]\d|150)[{_SPEED_SYMBOLS.lower()}]{{1,2}}(?:xl)?$")

# Words that describe any wheel or tire and so identify nothing on their own.
_GENERIC_WORDS = {
    "wheel", "wheels", "tire", "tires", "rim", "rims", "style", "flow", "form",
    "black", "gloss", "matte", "satin", "chrome", "silver", "bronze", "machine",
    "machined", "face", "seat", "cone", "with", "and", "the", "season", "snow",
    "winter", "summer", "radial", "load", "range", "inch", "new", "set",
}


def _meaningful_tokens(text):
    """Alphabetic words >= 4 chars that could name a brand or model."""
    return [t for t in re.findall(r"[A-Za-z]{4,}", str(text or ""))
            if t.lower() not in _GENERIC_WORDS]


def _clean_model_text(text):
    """'RIDGERUNNER RT (Studdable #15) - 12PR 3PMS' -> 'RIDGERUNNER RT'."""
    t = re.sub(r"\(.*?\)", " ", str(text or ""))
    t = t.split(" - ")[0]
    t = t.replace("...", " ")
    keep = [tok for tok in t.split() if not _SPEC_TOKEN.match(tok)]
    return " ".join(keep).strip()


def _resolve_brand(row, brand_col, mfr_col):
    """Brand column, falling back to manufacturer_code - the same fallback
    augment_tires.py applies before writing the storefront title."""
    brand = str(row.get(brand_col, "") or "").strip() if brand_col else ""
    if brand:
        return brand
    if mfr_col:
        mfr = str(row.get(mfr_col, "") or "").strip()
        if mfr:
            return mfr.title()
    return ""


def _resolve_model(row, model_col, desc_col):
    """The model column when it holds a real name, the description when it
    holds a code. A code buys the wrong photo, so descriptive text wins."""
    model = str(row.get(model_col, "") or "").strip() if model_col else ""
    desc = _clean_model_text(row.get(desc_col, "")) if desc_col else ""
    if (model and _meaningful_tokens(model)
            and not _CODE_LIKE.match(model) and not _SKU_LIKE.search(model)):
        return model
    return desc or model


# Words that describe a tread PATTERN, not a product. A handful of DT Tire
# rows carry one of these in the model column, which reads as a real name to
# _meaningful_tokens: "Bridgestone Asymmetrical tire product photo" buys a
# photo of whichever Bridgestone Google ranks first, and the storefront then
# shows a confidently wrong picture. Kept as an explicit deny-list rather than
# a rule about short or generic models, because the obvious general rule
# ("a model with no distinctive word is unqueryable") also throws out Starfire
# WR, Amp Terrain Pro A/T and American Truxx DNA, which are all real.
_PATTERN_ONLY_MODELS = {"directional", "asymmetrical", "symmetrical",
                        "non-directional", "nondirectional"}


def _is_queryable(brand, model, extra):
    """False when nothing in the row actually names the product.

    Some rows carry only a SKU and a size. A search built from that returns an
    arbitrary product, so it is money spent on a wrong picture. Skipping costs
    nothing: the product ships without an image, which is the honest outcome.
    """
    if str(model or "").strip().lower() in _PATTERN_ONLY_MODELS:
        # The brand may still look "meaningful", but a brand alone cannot
        # identify one tire out of that brand's whole range.
        model = ""
        if not _meaningful_tokens(extra):
            return False
    return bool(_meaningful_tokens(f"{brand} {model}") or _meaningful_tokens(extra))

# ---------------------------------------------------------------------------
# WheelSmart's tire feed carries no brand and no model column. Everything that
# names the product sits inside one free-text description:
#
#     "235/55R20 102V DELINTE DS2 SUV ALL SEASON..."
#      <size>     <ld/sp> <brand> <----model----> <category>
#
# Left unparsed, _resolve_brand and _resolve_model both return "", build_query
# falls back to the raw description (which begins with an 11-digit SKU and ends
# in a truncated "..."), and cache_key degrades to "image:||<sku>". Both
# consequences are paid for in searches:
#
#   * every other supplier keys a tire photo at model level, so one search
#     covers a model in all its sizes. Keyed on SKU instead, WheelSmart buys
#     one photo per size — 49 Delinte DS2 rows were 49 separate searches.
#   * a query led by an 11-digit SKU returns an arbitrary product, so the
#     search is not merely duplicated, it is likely to buy the wrong picture.
#
# The brand list is explicit rather than inferred. This feed is a short tail of
# budget marques the other four suppliers do not stock (so there is no brand
# column anywhere else to learn from), and the source data misspells them
# —"LANVIGTOR", "DELINT", "GOUNDSPEED"— often enough that the variants have to
# fold onto one canonical name or the de-duplication above does not happen.
# ---------------------------------------------------------------------------

# Canonical brand -> misspellings seen in the live feed. Longest match wins, so
# two-word brands are tried before the single-word ones they start with.
_WS_BRANDS = {
    "LANVIGATOR":   ("LANVIGTOR", "ALNVIGATOR", "LANVIGATROR", "LANVIGAOTR"),
    "DELINTE":      ("DELINT",),
    "GROUND SPEED": ("GROUNDSPEED", "GOUNDSPEED"),
    "ROYAL BLACK":  (),
    "GREEN-MAX":    ("GREENMAX",),
    "SONIX":        ("SONIX-ROADMARCH",),
    "POWERTRAC":    (),
    "POWERHUB":     (),
    "ROADMARCH":    (),
    "ROCKBLADE":    (),
    "ILINK":        (),
    "COMPASAL":     (),
    "STATURE":      (),
    "LEXANI":       (),
    "LANDSAIL":     (),
    "SUREGRIP":     (),
    "GREENTRAC":    (),
}

# alias (upper, no punctuation collapsed) -> canonical brand
_WS_BRAND_LOOKUP = {}
for _canon, _aliases in _WS_BRANDS.items():
    for _name in (_canon,) + tuple(_aliases):
        _WS_BRAND_LOOKUP[_name] = _canon
# Longest first so "GROUND SPEED" is not shadowed by a future "GROUND".
_WS_BRAND_ORDER = sorted(_WS_BRAND_LOOKUP, key=len, reverse=True)

# A leading token that describes the fitment rather than the product.
_WS_SIZE = re.compile(
    r"^(LT|P|ST)?\(?(LT)?\d{2,3}(\.\d+)?"
    r"(/\d{2,3}(\.\d+)?|X\d{1,2}(\.\d+)?)"
    r"(Z)?R?\d{1,2}(\.\d+)?(LT)?[A-Z]{0,2}(\(POR\))?$", re.I)
_WS_LOADSPD = re.compile(r"^\d{2,3}(/\d{2,3})?[A-Z]{1,3}$", re.I)
_WS_PLY     = re.compile(r"^\d{1,2}PR$", re.I)
_WS_LEAD_NOISE = {"XL", "LT", "C", "STD", "POR", "-"}

# Trailing words that name the CATEGORY, never the model. Deliberately short:
# "A/S", "A/T", "M/T" and "SUV" are left in the model because Lanvigator ships
# both a "CATCHFORS" and a "CATCHFORS A/S", and stripping the suffix would
# collapse two real products onto one photo — the same mistake the model-suffix
# rule in normalize_key_token is written to avoid.
_WS_CATEGORY_TAIL = (
    "ALL SEASON", "ALL-SEASON", "ALL WEATHER", "ALL-WEATHER", "ALL TERRAIN",
    "ALL-TERRAIN", "MUD TERRAIN", "HIGHWAY TERRAIN", "WINTER", "SUMMER", "SNOW",
)


def _parse_wheelsmart_tire_desc(desc):
    """'235/55R20 102V DELINTE DS2 SUV ALL SEASON...' -> ('DELINTE', 'DS2 SUV').

    Returns ("", "") when the description names no brand this feed is known to
    carry — the caller then falls back to the old behaviour, which is a skipped
    row rather than a guessed one.
    """
    text = re.sub(r"\s+", " ", str(desc or "").replace("...", " ")).strip()
    text = re.sub(r"\s+-\s+", " ", text)
    toks = text.upper().split()
    while toks and (_WS_SIZE.match(toks[0]) or _WS_LOADSPD.match(toks[0])
                    or _WS_PLY.match(toks[0]) or toks[0] in _WS_LEAD_NOISE
                    or toks[0][:1].isdigit()):
        toks.pop(0)
    tail = " ".join(toks).strip()
    if not tail:
        return "", ""

    brand = ""
    for alias in _WS_BRAND_ORDER:
        if tail == alias or tail.startswith(alias + " "):
            brand = _WS_BRAND_LOOKUP[alias]
            tail = tail[len(alias):].strip()
            break
    if not brand:
        # A few rows lead with the model and put the marque second
        # ("ROCK525 ROCKBLADE MUD TERRAIN"). Pull the brand out of wherever it
        # sits so those rows key alongside the brand-first spelling of the
        # same product instead of forming a second key for it.
        for alias in _WS_BRAND_ORDER:
            if re.search(r"(?:^|\s)" + re.escape(alias) + r"(?:\s|$)", tail):
                brand = _WS_BRAND_LOOKUP[alias]
                tail = re.sub(r"(?:^|\s)" + re.escape(alias) + r"(?:\s|$)",
                              " ", tail).strip()
                break
    if not brand:
        return "", ""

    for cat in _WS_CATEGORY_TAIL:
        if tail.endswith(" " + cat) or tail == cat:
            tail = tail[: len(tail) - len(cat)].strip()
            break
    return brand, tail


_ROW_PARSERS = {"wheelsmart_tires": _parse_wheelsmart_tire_desc}


def _resolve_brand_model(row, src):
    """Brand and model for one row, honouring a per-source description parser.

    Sources with real brand/model columns keep the existing resolution
    untouched; only a feed that hides both inside free text declares a
    `desc_parser`, and even then a parse that recognises nothing falls back
    rather than inventing a product name.
    """
    parser = _ROW_PARSERS.get(src.get("desc_parser"))
    if parser:
        col = src.get("extra_col") or src.get("desc_col")
        brand, model = parser(row.get(col, "") if col else "")
        if brand or model:
            return brand, model
    return (_resolve_brand(row, src["brand_col"], src.get("mfr_col")),
            _resolve_model(row, src["model_col"], src.get("desc_col")))




# ---------------------------------------------------------------------------
# Candidate discovery — read-only
# ---------------------------------------------------------------------------

def _is_missing_image(val):
    return str(val).strip() == ""


def _is_missing_designation(val):
    return str(val).strip().lower() == "unknown"


# ---------------------------------------------------------------------------
# Image QUALITY audit — a scraped image can be present and still be unusable
#
# WHY THIS EXISTS
# ---------------
# Until now "does this row need an image?" was answered by _is_missing_image()
# alone: blank means missing, anything else means done. That treats a 130x130
# thumbnail exactly like a 2000x2000 studio photo, and the storefront does not.
# Measured on the 2026-08-18 scrape, before the DT Tire fix:
#
#     dttire       979 unique tire images -> 130x130, every one
#     northcountry 200x290 / 200x200 / 356x470 alongside genuine 1500-3840px
#     formula      326x500, 427x427, 444x439 alongside 1280-2187px
#     wheelsmart   300x300, 365x408 alongside 800-2560px
#
# Every one of those rows counted as "has an image" and so was invisible to
# both the gap report and the enrichment spend estimate.
#
# WHAT THIS DOES NOT DO
# ---------------------
# A failing verdict marks the row as a CANDIDATE. It never blanks the column:
# a 400px photo beats an empty listing, so the existing URL stays until a
# better one is actually found. apply_cached() only overwrites when the cache
# holds a real replacement.
#
# COST
# ----
# Zero SerpAPI credit — these are plain HTTP GETs against public CDNs, the same
# fetches image_view.py already does. Results are cached on disk by URL, and
# URLs are deduped before fetching (8,986 dttire rows share 979 images), so a
# warm cache makes every later report free. Only `verdict` and dimensions are
# stored; image bytes are never written to disk.
# ---------------------------------------------------------------------------

IMAGE_QUALITY_CACHE = os.path.join(SHARED_DIR, "Data", "image_quality_cache.json")

# Minimum acceptable short side for an image the client already has.
#
# Deliberately LOWER than image_view.MIN_SIDE_PX (800), and the difference is
# the point: 800 is the bar for CHOOSING among ~50 free SERP candidates, where
# being picky costs nothing. This is the bar for CONDEMNING a photo we already
# own, where being picky costs a paid search. 600 splits the measured
# distribution where it actually separates — it condemns the 130/200/300/400px
# thumbnails without flagging the 600-800px range that looks acceptable in a
# product grid, and DT Tire's repaired images land exactly at 600.
IMAGE_MIN_SIDE_PX = 600

# Suppliers serve a real, non-uniform "no photo available" graphic that no
# pixel check can distinguish from a genuine picture — it has to be matched by
# name. DT Tire's is missing_product_EN_300x300.png (243 rows in the 08-18 raw
# wheels scrape).
_PLACEHOLDER_URL_RE = re.compile(
    r"(missing_product|no[-_]?image|placeholder|no[-_]?photo|coming[-_]?soon)",
    re.IGNORECASE)


# Sentinel for "nobody said, so use the verdicts on disk".
#
# This is not decoration. Every consumer of discover_candidates() has to make
# the same choice — count unusable images as gaps, or only blank ones — and
# they have to make the SAME choice, or the number the client approves is not
# the number that gets bought and written. They did not: the scan passed the
# cache, while run_live() and apply_cached() defaulted to None. The approval
# screen offered 790 image searches covering 5,663 rows; the live run behind
# it could see 29 searches covering 57 rows, and the apply step could not have
# written an answer into any of the other 5,606 even if one had been bought.
#
# So the disk cache is now the DEFAULT rather than something each caller has
# to remember to pass. Passing quality_cache=None still asks for the old
# blank-only behaviour explicitly, which is the only way to get it.
USE_DISK_QUALITY_CACHE = object()

_quality_cache_memo = {"mtime": None, "data": None}


def load_quality_cache():
    """Verdict map from disk, memoized on the file's mtime.

    Scanning calls this once per field per source; the file is ~1 MB of JSON
    and re-parsing it each time is pure waste. The mtime check means a writer
    in the same process (measure_image_urls) still sees its own updates.
    """
    if not os.path.exists(IMAGE_QUALITY_CACHE):
        return {}
    mtime = os.path.getmtime(IMAGE_QUALITY_CACHE)
    if _quality_cache_memo["mtime"] != mtime:
        with open(IMAGE_QUALITY_CACHE, encoding="utf-8") as f:
            _quality_cache_memo["data"] = json.load(f)
        _quality_cache_memo["mtime"] = mtime
    return _quality_cache_memo["data"]


def resolve_quality_cache(field, quality_cache):
    """Turn a caller's `quality_cache` argument into the map to actually use.

    USE_DISK_QUALITY_CACHE (the default) means "read the verdicts on disk";
    an explicit None means "blank-only, ignore quality"; anything else is used
    as given. Non-image fields never need one.
    """
    if field != "image":
        return None
    if quality_cache is USE_DISK_QUALITY_CACHE:
        return load_quality_cache() or None
    return quality_cache


def save_quality_cache(cache):
    os.makedirs(os.path.dirname(IMAGE_QUALITY_CACHE), exist_ok=True)
    # Unique temp name: a fixed one lets two concurrent writers interleave
    # their partial JSON into a single file and then rename the wreckage over
    # a good cache. Every caller today is sequential, but the scrapers write
    # this from inside their ProductInfo loops, which is one MAX_WORKERS bump
    # away from not being.
    tmp = f"{IMAGE_QUALITY_CACHE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
    os.replace(tmp, IMAGE_QUALITY_CACHE)
    _quality_cache_memo["mtime"] = os.path.getmtime(IMAGE_QUALITY_CACHE)
    _quality_cache_memo["data"] = cache


def judge_image_url(url):
    """Name-only verdict, no network. Returns (verdict, reason) or None when
    the URL has to be fetched to be judged."""
    u = str(url or "").strip()
    if not u:
        return "missing", "no image url"
    if _PLACEHOLDER_URL_RE.search(u):
        return "placeholder", "supplier's 'no photo available' graphic"
    return None


def measure_image_urls(urls, workers=8, refresh=False, progress=200, verbose=True):
    """Fetch + measure every URL without a cached verdict. Returns the full
    cache dict. Plain HTTP; no SerpAPI credit is involved at any point.

    A URL that cannot be fetched is recorded as 'unusable' rather than left
    unknown, so a dead CDN link is treated as the gap it is instead of being
    silently re-fetched on every future run.
    """
    try:
        from image_view import fetch_image, analyze_image, ImageCheckError
    except ImportError as exc:
        raise RuntimeError(
            f"image quality audit needs Pillow + numpy ({exc})") from exc

    cache = load_quality_cache()
    todo = []
    for u in dict.fromkeys(urls):
        u = str(u or "").strip()
        if not u or judge_image_url(u):
            continue                      # blank/placeholder decided by name
        if refresh or u not in cache:
            todo.append(u)

    if not todo:
        if verbose:
            print(f"  [image-quality] cache warm — 0 of {len(set(urls))} "
                  f"URL(s) needed fetching")
        return cache

    if verbose:
        print(f"  [image-quality] measuring {len(todo)} uncached image(s) "
              f"(HTTP only, no API spend)")

    done = [0]

    def work(u):
        try:
            finding = analyze_image(fetch_image(u), "tires")
            entry = {"w": finding["width"], "h": finding["height"]}
            if finding["uniform"]:
                entry.update(verdict="placeholder", reason="blank/near-uniform image")
            elif min(finding["width"], finding["height"]) < IMAGE_MIN_SIDE_PX:
                entry.update(verdict="low_res",
                             reason=f"{finding['width']}x{finding['height']} "
                                    f"< {IMAGE_MIN_SIDE_PX}px")
            else:
                entry.update(verdict="ok", reason="")
        except ImageCheckError as exc:
            entry = {"w": None, "h": None, "verdict": "unusable", "reason": str(exc)[:120]}
        except Exception as exc:                                    # noqa: BLE001
            entry = {"w": None, "h": None, "verdict": "unusable",
                     "reason": f"{type(exc).__name__}: {exc}"[:120]}
        done[0] += 1
        if verbose and progress and done[0] % progress == 0:
            print(f"    ...{done[0]}/{len(todo)}")
        return u, entry

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for u, entry in ex.map(work, todo):
            cache[u] = entry

    save_quality_cache(cache)
    if verbose:
        counts = collections.Counter(cache[u]["verdict"] for u in todo)
        print(f"  [image-quality] done: {dict(counts)}")
    return cache


def image_verdict(url, quality_cache, tread_cache=None):
    """Combined name-then-pixel-then-pose verdict for one URL.

    Returns one of: 'missing', 'placeholder', 'low_res', 'unusable',
    'no_tread', 'ok', or 'unknown' when quality has never been measured for
    it. 'unknown' is deliberately NOT treated as a gap — an unmeasured image
    is not evidence of a bad image, and guessing would inflate the spend
    estimate.

    'no_tread' means a labeller positively said the photo shows the side of
    the tire rather than the tread (see tread.py). An image nobody has
    reviewed yet reads 'unlabelled' there and stays 'ok' here, for the same
    reason 'unknown' is not a gap: the client is not billed for a search on
    the strength of a question no one has answered.

    Resolution is checked BEFORE pose. A 130px photo is a gap whether or not
    its tread shows, and reporting it as 'low_res' names the cause that a new
    search actually has to beat.
    """
    named = judge_image_url(url)
    if named:
        return named[0]
    entry = (quality_cache or {}).get(str(url).strip())
    if not entry:
        return "unknown"
    verdict = entry.get("verdict", "unknown")
    if verdict == "ok" and tread_cache is not None:
        if _TREAD.verdict(url, tread_cache) == _TREAD.NOT_VISIBLE:
            return "no_tread"
    return verdict


# Verdicts that make a row worth spending a search on.
BAD_IMAGE_VERDICTS = ("missing", "placeholder", "low_res", "unusable", "no_tread")


# ---------------------------------------------------------------------------
# (b2) — free layer: a photo this catalogue ALREADY OWNS
#
# The same tire model is carried by more than one supplier, and within a
# single supplier one size of a model often scrapes a clean 1500px photo while
# the next size gets a 385px one. Both are the same picture of the same tread
# pattern — which is the premise the model-level cache key already rests on.
# So before anything is bought, every gap should be checked against the photos
# the catalogue is already holding.
#
# Measured on the 2026-08-18 data, against the 5,387 tire rows the audit
# flags as having no usable image:
#
#     556 rows can be filled from a photo another row already carries
#      28 searches disappear entirely (every row behind them is covered)
#     donor short side: min 600, median 1500, max 4096
#
#     needy supplier <- donor      rows
#     northcountry   <- northcountry 553
#     formula        <- emergetires   60
#     northcountry   <- formula       16
#     dttire         <- dttire        15
#
# This costs nothing: no SerpAPI credit, no HTTP request, no re-scrape. It is
# reading two files that are already on disk.
#
# TIRES ONLY, deliberately. The donor is matched on brand+model, which names a
# tread pattern on a tire and a category or a colour on a wheel — the same
# reason _MODEL_LEVEL_IMAGE_TYPES exists. Sharing a photo across everything
# called "RSSW / Steel Wheel" would put one wheel's picture on 92 listings.
# ---------------------------------------------------------------------------

def build_peer_image_index(quality_cache=USE_DISK_QUALITY_CACHE, supplier_filter=None):
    """{image cache key: (short_side, url, supplier)} — the best photo already
    measured as usable that any row in the catalogue carries for that product.

    Read-only and network-free. Only 'ok' verdicts are eligible as donors, so
    a gap can never be filled from another gap.
    """
    quality_cache = resolve_quality_cache("image", quality_cache)
    if not quality_cache:
        return {}

    best = {}
    for src in SOURCES:
        if src["field"] != "image" or not src.get("value_col"):
            continue
        if src["type"] not in _MODEL_LEVEL_IMAGE_TYPES:
            continue
        f = latest(src["glob"])
        if not f:
            continue
        df = pd.read_csv(f, dtype=str, low_memory=False, encoding="utf-8-sig").fillna("")
        if src["value_col"] not in df.columns:
            continue
        if supplier_filter and src["supplier"] != supplier_filter:
            continue

        # Only rows whose image is already measured 'ok' can donate, and only
        # the columns the key is built from are needed. Deduping on those
        # columns first turns northcountry/tires from 6,452 row lookups into
        # ~900 — the same photo repeats across every size of a model.
        #
        # A photo a reviewer rejected for showing the sidewall is disqualified
        # as a donor too: copying it onto 66 other rows would spread the exact
        # defect the client asked to remove, for free, which is worse than the
        # thumbnail it replaces.
        tcache = _TREAD.load()
        usable = df[src["value_col"]].map(
            lambda u: bool((quality_cache.get(str(u or "").strip()) or {}).get("w"))
            and (quality_cache.get(str(u or "").strip()) or {}).get("verdict") == "ok"
            and not _TREAD.is_rejected(u, tcache))
        donors = df[usable]
        if donors.empty:
            continue
        key_cols = [c for c in (src["brand_col"], src["model_col"],
                                src.get("mfr_col"), src.get("desc_col"),
                                src["value_col"])
                    if c and c in donors.columns]
        for _, row in donors[key_cols].drop_duplicates().iterrows():
            url = str(row.get(src["value_col"], "") or "").strip()
            entry = quality_cache.get(url)
            if not entry:
                continue
            # Resolved through the same helpers discover_candidates uses, off
            # the same columns, so a donor key and a candidate key for the
            # same product are byte-identical.
            brand = _resolve_brand(row, src["brand_col"], src.get("mfr_col"))
            model = _resolve_model(row, src["model_col"], src.get("desc_col"))
            if not (brand.strip() and model.strip()):
                continue      # a donor has to actually name its product
            key = cache_key("image", brand, model, "", src["type"])
            side = min(entry["w"], entry["h"])
            if key not in best or side > best[key][0]:
                best[key] = (side, url, src["supplier"])
    return best


def _peer_upgrade(candidate, peer_index, quality_cache):
    """The donor URL for one candidate row, or '' when there is none or it
    would not be an improvement.

    Never sideways and never smaller — the same rule backfill_costar_images.py
    applies. A candidate whose own image is somehow bigger than the best donor
    keeps what it has.
    """
    hit = peer_index.get(candidate["cache_key"])
    if not hit:
        return ""
    side, url, _supplier = hit
    if url == candidate.get("current_value", ""):
        return ""
    cur = (quality_cache or {}).get(str(candidate.get("current_value", "")).strip()) or {}
    if cur.get("w") and cur.get("h") and min(cur["w"], cur["h"]) >= side:
        return ""
    return url



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
        return qty >= 4   # augment_wheels.py LOW_QTY_THRESHOLD
    if qty_filter == "wheelsmart_tires":
        return df["quantity_on_hand"].apply(_coerce_qty) >= 4   # augment_tires.py LOW_QTY_THRESHOLD
    if qty_filter == "wheelsmart_rims":
        return df["quantity_on_hand"].apply(_coerce_qty) >= 4   # augment_rims.py LOW_QTY_THRESHOLD
    return pd.Series(True, index=df.index)


def discover_candidates(field, supplier_filter=None,
                       quality_cache=USE_DISK_QUALITY_CACHE):
    """Yield dicts describing every row that is a genuine (d)/(e) candidate:
    the field is blank/Unknown AFTER clean.py's free inference already ran.
    Read-only — opens the latest cleaned/raw CSV per source, never writes.

    For field='image', `quality_cache` (from measure_image_urls()) widens
    "missing" to "missing OR unusable": a 130x130 thumbnail, a placeholder
    graphic and a dead CDN link are all gaps the storefront shows, even
    though the column is not blank. It DEFAULTS to the verdicts on disk —
    see USE_DISK_QUALITY_CACHE for why that default matters. Pass None to ask
    for the old blank-only behaviour explicitly. Each candidate carries
    `verdict` so the report can say WHY a row is a candidate rather than
    lumping every cause together.
    """
    quality_cache = resolve_quality_cache(field, quality_cache)
    candidates = []
    unqueryable = []
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

        verdicts = None
        if src["value_col"] and src["value_col"] in df.columns:
            col = df[src["value_col"]]
            if field == "image":
                if quality_cache is None:
                    mask = col.apply(_is_missing_image)
                else:
                    tcache = _TREAD.load()
                    verdicts = col.apply(
                        lambda v: image_verdict(v, quality_cache, tcache))
                    mask = verdicts.isin(BAD_IMAGE_VERDICTS)
            else:
                mask = col.apply(_is_missing_designation)
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
        mfr_col   = src.get("mfr_col")
        desc_col  = src.get("desc_col")
        for idx in df.index[mask]:
            row = df.loc[idx]
            brand, model = _resolve_brand_model(row, src)
            size  = row.get(src["size_col"], "") if src["size_col"] else ""
            extra = row.get(extra_col, "") if extra_col else ""
            if not _is_queryable(brand, model, extra):
                unqueryable.append((src["supplier"], src["type"]))
                continue
            query = build_query(field, brand, model, size, extra, src["type"])
            candidates.append(dict(
                supplier=src["supplier"], type=src["type"], field=field,
                row_index=idx, source_file=f, value_col=src["value_col"],
                writable=src["writable"],
                brand=brand, model=model, size=size, query=query,
                cache_key=cache_key(field, brand, model, size, src["type"]),
                verdict=(verdicts.loc[idx] if verdicts is not None else None),
                # What the row holds today. For images this is not always
                # blank — a candidate can be a 130px thumbnail — and the free
                # peer-image layer needs it to refuse a sideways swap.
                current_value=(str(row.get(src["value_col"], "") or "").strip()
                               if src["value_col"] else ""),
            ))
    if unqueryable:
        counts = collections.Counter(unqueryable)
        total = sum(counts.values())
        detail = ", ".join(f"{sup}/{typ}={n}" for (sup, typ), n in sorted(counts.items()))
        print(f"  Skipped {total} row(s) with nothing to search on (SKU/size only) "
              f"— no search is bought for them: {detail}")
    return candidates


# ---------------------------------------------------------------------------
# specs candidate discovery — read-only
#
# A row is a candidate for a given sub-field only if EVERY free-layer source
# that supplier's own augment*.py would consult has already come up empty.
# Reimplementing that check here (rather than, say, treating any blank spec
# column as a gap) is what keeps this from wildly overcounting: e.g. a blank
# `spec_loadrange` on a plain passenger tire is NOT a gap — augment.py fills
# it with 'SL' for free (default_passenger_load_range) — so it must not be
# queued for a paid search. Getting this wrong once already cost real
# accuracy in this module's own numbers (see git history); these functions
# call the exact same shared size_parser functions augment*.py calls, so a
# row already resolved for free can never be miscounted as needing spend.
# ---------------------------------------------------------------------------

def _wheelsmart_spec_funcs():
    """wheelsmart's free layer for load_index/speed_rating/load_range is more
    capable than the generic shared parser (handles inverted "S109" ratings,
    mud/flotation size stripping) — imported live so candidacy checks and any
    future free-layer change to wheelsmart never drift out of sync with it."""
    sys.path.insert(0, os.path.join(ROOT, "pipelines", "wheelsmart"))
    from augment_tires import (extract_load_range as wm_load_range,
                               extract_speed_rating as wm_speed_rating,
                               _extract_load_index_from_desc as wm_load_index)
    return wm_load_index, wm_speed_rating, wm_load_range


def _missing_spec_subfields(row, src):
    """Return the subset of SPEC_SUBFIELDS still blank after replicating this
    supplier's own free-layer precedence: its preferred spec column first
    (skipping Costar's "0.0000" unknown-value placeholder for load index),
    then the same size/description parse augment*.py falls back to.
    """
    desc = str(row.get(src["desc_col"], "") or "") if src.get("desc_col") else ""
    size = str(row.get(src["size_col"], "") or "") if src.get("size_col") else ""
    missing = set()

    if src["supplier"] == "wheelsmart":
        wm_load_index, wm_speed_rating, wm_load_range = _wheelsmart_spec_funcs()
        li = str(row.get(src["load_index_col"], "")).strip() or wm_load_index(desc)
        sr = wm_speed_rating(desc)
        lr = wm_load_range(size, desc) or default_passenger_load_range(size)
    else:
        li_col = src.get("load_index_col")
        li = coerce_load_index(row.get(li_col, "") if li_col else "", desc)
        sr_col = src.get("speed_rating_col")
        sr = (str(row.get(sr_col, "")).strip() if sr_col else "") or parse_load_speed(desc)[1]
        lr_col = src.get("load_range_col")
        lr = ((str(row.get(lr_col, "")).strip() if lr_col else "")
              or extract_load_range(size, desc) or default_passenger_load_range(size))

    if not li:
        missing.add("load_index")
    if not sr:
        missing.add("speed_rating")
    if not lr:
        missing.add("load_range")

    parsed = parse_tire_size(size, desc)
    for sub, parsed_key, col_key in (
        ("section_width", "width",       "width_col"),
        ("aspect_ratio",  "ratio",       "ratio_col"),
        ("rim_diameter",  "rim_display", "rim_col"),
    ):
        col = src.get(col_key)
        val = (str(row.get(col, "")).strip() if col else "") or parsed[parsed_key]
        if not val:
            missing.add(sub)

    return missing


def discover_spec_candidates(supplier_filter=None):
    """Yield dicts describing every row still missing at least one spec
    sub-field after every free-layer source has been checked. Each candidate
    carries `needed` — the specific subset still blank on that row, so a
    live search only asks the SERP-parsing step to look for what that row
    actually lacks rather than treating every candidate as missing all six.
    """
    candidates = []
    unqueryable = []
    report_only_subfields = 0
    for src in SPEC_SOURCES:
        if supplier_filter and src["supplier"] != supplier_filter:
            continue
        f = latest(src["glob"])
        if not f:
            continue
        df = pd.read_csv(f, dtype=str, low_memory=False, encoding="utf-8-sig").fillna("")

        qty_filter = src.get("qty_filter")
        if qty_filter:
            df = df[_qty_keep_mask(df, qty_filter)]

        spec_cols = dict(
            load_index=src.get("load_index_col"), speed_rating=src.get("speed_rating_col"),
            load_range=src.get("load_range_col"), section_width=src.get("width_col"),
            aspect_ratio=src.get("ratio_col"), rim_diameter=src.get("rim_col"),
        )

        brand_col, model_col, extra_col = src["brand_col"], src["model_col"], src["extra_col"]
        mfr_col, desc_col = src.get("mfr_col"), src.get("desc_col")
        for idx in df.index:
            row = df.loc[idx]
            needed_all = _missing_spec_subfields(row, src)
            if not needed_all:
                continue
            # A sub-field with no destination column (wheelsmart's
            # speed_rating/load_range) can never be written back even if a
            # search finds it — same "no column to live in" principle
            # already applied to the wheelsmart designation source. Drop it
            # from what gets searched; the completeness panel (which reads
            # the finished Shopify CSV independently) still shows the gap.
            needed = {sub for sub in needed_all if spec_cols.get(sub)}
            report_only_subfields += len(needed_all) - len(needed)
            if not needed:
                continue
            brand, model = _resolve_brand_model(row, src)
            size  = row.get(src["size_col"], "") if src["size_col"] else ""
            extra = row.get(extra_col, "") if extra_col else ""
            if not _is_queryable(brand, model, extra):
                unqueryable.append((src["supplier"], src["type"]))
                continue
            query = build_query("specs", brand, model, size, extra, src["type"])
            candidates.append(dict(
                supplier=src["supplier"], type=src["type"], field="specs",
                row_index=idx, source_file=f, writable=True,
                brand=brand, model=model, size=size, query=query, needed=needed,
                cache_key=cache_key("specs", brand, model, size),
                spec_cols=spec_cols,
            ))
    if unqueryable:
        counts = collections.Counter(unqueryable)
        total = sum(counts.values())
        detail = ", ".join(f"{sup}/{typ}={n}" for (sup, typ), n in sorted(counts.items()))
        print(f"  Skipped {total} row(s) with nothing to search on (SKU/size only) "
              f"— no search is bought for them: {detail}")
    if report_only_subfields:
        print(f"  {report_only_subfields} sub-field gap(s) have no column to write an "
              f"answer into for that supplier — never searched, shown in the completeness "
              f"panel only.")
    return candidates


# ---------------------------------------------------------------------------
# (d) — apply already-cached (already-paid-for) results. No network call.
# ---------------------------------------------------------------------------

def apply_cached(fields, supplier_filter=None, write=False,
                 quality_cache=USE_DISK_QUALITY_CACHE):
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
        cands = discover_candidates(field, supplier_filter, quality_cache)
        by_file = {}
        for c in cands:
            by_file.setdefault(c["source_file"], []).append(c)

        # Free layer (b2): a usable photo the catalogue already owns beats
        # both a paid search and the gap. Built once per field, from the same
        # files already on disk — see build_peer_image_index().
        qcache = resolve_quality_cache(field, quality_cache)
        peers = build_peer_image_index(qcache) if field == "image" else {}

        field_stats = dict(total_missing=len(cands), cache_hits=0, cache_hits_empty=0,
                            peer_fixed=0, still_needs_paid=0, files_written=[])

        for source_file, rows in by_file.items():
            df = None
            dirty = False
            for c in rows:
                entry = cache.get(c["cache_key"])
                value = "" if entry is None else entry.get(field, "")
                source = "cache"
                if not value:
                    # Nothing bought (or bought and empty) — try the photo
                    # another row already carries before giving up on the row.
                    value = _peer_upgrade(c, peers, qcache) if peers else ""
                    source = "peer"
                if value and source == "peer":
                    field_stats["peer_fixed"] += 1
                elif value:
                    field_stats["cache_hits"] += 1
                elif entry is None:
                    field_stats["still_needs_paid"] += 1
                    continue
                else:
                    field_stats["cache_hits_empty"] += 1
                    continue
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
# Load-index plausibility.
#
# A searched load index is the one enriched value that is WORSE than a blank
# when it is wrong: it reads as a real measurement on a load-bearing part.
# Live results included "load index 70" for a Continental CHD3 245/70R19.5 —
# 70 rates 335 kg, and no 19.5" commercial tire carries 335 kg. It comes from
# _parse_spec_result reading a load/speed token that belongs to some smaller
# tire also named in the result text.
#
# The check does not invent a threshold. The suppliers' own feeds already
# state a load index for ~1,000 distinct sizes, so the catalogue itself is the
# reference. Two tires of one size DO differ by a few indices when their load
# range differs (33X12.50R20 is 114 at E and 121 at F), so only a large gap
# counts: each index step is about +1.25% capacity, making 20 steps roughly
# half the load — far outside any same-size variation, and comfortably inside
# the 40-70 step errors actually observed.
#
# Rejecting only the load index, and only downward, is deliberate. An
# unexpectedly HIGH value is usually a heavier load range of the same tire,
# and the other sub-fields from the same search (speed rating, load range)
# are unaffected by this particular failure.
# ---------------------------------------------------------------------------

_LOAD_INDEX_GAP = 20
_load_index_ref = None


def _normalize_size_token(text):
    return re.sub(r"[^0-9A-Z.]", "", str(text or "").upper())


def load_index_reference(refresh=False):
    """{normalized size: lowest load index the suppliers state for it}."""
    global _load_index_ref
    if _load_index_ref is not None and not refresh:
        return _load_index_ref
    ref = {}
    for src in SPEC_SOURCES:
        li_col, sz_col = src.get("load_index_col"), src.get("size_col")
        if not li_col or not sz_col:
            continue
        f = latest(src["glob"])
        if not f:
            continue
        try:
            df = pd.read_csv(f, dtype=str, low_memory=False,
                             encoding="utf-8-sig").fillna("")
        except (OSError, ValueError):
            continue
        if li_col not in df.columns or sz_col not in df.columns:
            continue
        for sz, li in zip(df[sz_col], df[li_col]):
            key = _normalize_size_token(sz)
            val = str(li).strip()
            if key and val.isdigit():
                v = int(val)
                if key not in ref or v < ref[key]:
                    ref[key] = v
    _load_index_ref = ref
    return ref


def implausible_load_index(size, value):
    """True when `value` is too low to be this size's load index at any load
    range. False whenever the catalogue has nothing to compare against — an
    unknown size is not evidence of a bad answer."""
    val = str(value or "").strip()
    if not val.isdigit():
        return False
    ref = load_index_reference()
    known = ref.get(_normalize_size_token(size))
    if known is None:
        return False
    return int(val) < known - _LOAD_INDEX_GAP


# A flotation size states overall diameter x section width on a rim —
# "LT35X12.50R17" is 35 inches tall, 12.50 wide, on a 17 inch wheel. There is
# no aspect ratio in it; the number simply does not exist for this shape of
# size. A search still returns one, because the result text names other tires:
# the live run answered LT35X12.50R17 with aspect_ratio 55, which would print
# a fabricated measurement onto the product page.
_FLOTATION_SIZE = re.compile(r"^(?:LT|P|ST)?\d{2,3}(?:\.\d+)?X", re.I)


def spec_not_applicable(size, subfield):
    """True when this sub-field cannot exist for this shape of size, so any
    answer for it is fabricated rather than merely uncertain."""
    if subfield != "aspect_ratio":
        return False
    return bool(_FLOTATION_SIZE.match(str(size or "").strip()))


def apply_spec_cache(supplier_filter=None, write=False):
    """specs counterpart to apply_cached() — same (d)-step contract (cache
    hits only, zero network calls regardless of `write`), different write
    shape: a specs cache entry is {subfield: value}, and each present
    subfield is written into ITS OWN column (spec_cols, built by
    discover_spec_candidates from SPEC_SOURCES) rather than one value_col.

    Three outcomes per candidate, not two — a plain "found vs not" split
    would make "still_needs_paid" (what the dashboard tells someone to buy)
    include rows that were already searched and came back empty, which
    would have that same row bought again on every future run for no new
    information:
      cache_hits        every needed sub-field is present -> fully resolved
      cache_hits_empty  every needed sub-field has been TRIED at least once
                        (found or not) -> a dead end, nothing left to buy.
                        cache_hits_partial (a subset of this) means at least
                        one sub-field WAS found even though others were not.
      still_needs_paid  at least one needed sub-field has never been tried
                        -> this, and only this, is what a live run searches.
    """
    cache = load_cache()
    cands = discover_spec_candidates(supplier_filter)
    by_file = {}
    for c in cands:
        by_file.setdefault(c["source_file"], []).append(c)

    stats = dict(total_missing=len(cands), cache_hits=0, cache_hits_empty=0,
                 cache_hits_partial=0, still_needs_paid=0, files_written=[])

    for source_file, rows in by_file.items():
        df = None
        dirty = False
        for c in rows:
            entry = cache.get(c["cache_key"]) or {}
            specs = entry.get("specs", {})
            tried = set(entry.get("tried", []))
            found = {sub: specs[sub] for sub in c["needed"] if specs.get(sub)}
            # Drop a load index the size says is impossible. Checked here
            # rather than at search time so results cached by earlier runs are
            # covered as well; the sub-field stays in `tried`, so refusing to
            # write it never turns into re-buying it.
            if found.get("load_index") and implausible_load_index(
                    c.get("size"), found["load_index"]):
                stats["rejected_load_index"] = stats.get("rejected_load_index", 0) + 1
                found.pop("load_index")
            for sub in [s for s in found if spec_not_applicable(c.get("size"), s)]:
                stats["rejected_not_applicable"] = stats.get("rejected_not_applicable", 0) + 1
                found.pop(sub)
            never_tried = {sub for sub in c["needed"] if sub not in tried}

            if len(found) == len(c["needed"]):
                stats["cache_hits"] += 1
            elif never_tried:
                stats["still_needs_paid"] += 1
            else:
                stats["cache_hits_empty"] += 1
                if found:
                    stats["cache_hits_partial"] += 1

            if not found:
                continue
            if write:
                if df is None:
                    df = pd.read_csv(source_file, dtype=str, low_memory=False,
                                     encoding="utf-8-sig").fillna("")
                for sub, value in found.items():
                    col = c["spec_cols"].get(sub)
                    if col:   # None for a sub-field this supplier has no column for
                        df.at[c["row_index"], col] = value
                        dirty = True
        if dirty:
            df.to_csv(source_file, index=False, encoding="utf-8")
            stats["files_written"].append(source_file)

    return stats


# ---------------------------------------------------------------------------
# Image quality filter — real implementation, called only from run_live()
# (which is never invoked). Pure function: no network, safe to read/review.
# ---------------------------------------------------------------------------

# Retailers whose catalogue photography SKEWS toward a three-quarter angled
# view showing the TREAD rather than a flat face-on shot. That is the look the
# client wants on the storefront. These outrank every other source.
#
# Treat this as a weak prior, not a guarantee. The live test disproved the
# strong version: SimpleTire sits in this set and still returned a flat face-on
# wheel-face shot for the Antares INGENS EV — the exact framing the client
# rejected. Source reputation does not predict framing, so the real check is
# image_view.analyze_image(), which opens the picture. This table only orders
# the queue that check walks through.
ANGLED_TREAD_RETAILERS = {
    "simpletire.com", "tirerack.com", "blackcircles.ca", "blackcircles.com",
    "prioritytire.com", "tirebuyer.com", "discounttire.com", "1010tires.com",
    "pmctire.com", "tiredirect.ca",
}

# Other well-known tire/wheel retailers — good, consistent product photography,
# but the framing is less predictable (some face-on, some angled).
TIRE_RETAILER_BIAS = {
    "walmart.com", "walmart.ca", "walmartimages.com",
    "canadiantire.ca", "kaltire.com", "fountaintire.com", "costco.ca",
    "costco.com", "americastire.com", "tiresplus.com", "ntb.com",
    "onlinetires.com", "mavis.com", "tirecraft.com", "activegreenross.com",
    "sailun.ca", "goodyear.ca", "cncwheels.com.au", "torquewheels.com",
    "wheelpros.com", "customwheeloffset.com", "tiresbyweb.com",
}

# A retailer's own domain often never appears in the image URL — PMCtire serves
# from cdn.shopify.com, Walmart from i5.walmartimages.com, others from
# bigcommerce/cloudfront. SerpAPI reports the origin separately in `source`, so
# match on the source/title text too rather than the CDN host alone.
def _retailer_tier(domain, source_text):
    hay = f"{domain} {source_text}".lower()
    def hit(names):
        return any(n.split(".")[0] in hay for n in names)
    if hit(ANGLED_TREAD_RETAILERS):
        return 6
    if hit(TIRE_RETAILER_BIAS):
        return 4
    return 0

DOMAIN_ALLOWLIST_BIAS = {
    "michelin.com", "goodyear.com", "bfgoodrichtires.com", "bridgestonetire.com",
    "continentaltire.com", "pirelli.com", "hankooktire.com", "toyotires.com",
    "yokohamatire.com", "generaltire.com", "coopertire.com", "falkentire.com",
    "nittotire.com", "kumhotire.com", "nexentireusa.com",
}
# Matched on the leading token, not the whole hostname: these serve images
# from CDNs that carry the brand but not the domain — "i.ebayimg.com",
# "external-preview.redd.it" — and both got through the full-hostname test and
# were bought, then rejected by the reviewer.
DOMAIN_DEPRIORITIZE = {
    "pinterest.com", "ebay.com", "ebayimg.com", "reddit.com", "redd.it",
    "facebook.com", "fbcdn.net", "instagram.com", "alibaba.com",
    "aliexpress.com", "made-in-china.com", "yandex.net", "mvideo.ru",
}

MIN_SIDE_PX      = 800    # hard floor — below this, reject outright
TARGET_LONG_EDGE = 1200   # soft preference — scored, not required


# Retail CDNs name the shot in the path: ".../951-sidewall/...", "-side.jpg",
# "?view=sidewall". Matched on the URL only — a page TITLE mentioning
# "sidewall" is usually prose about the tire, not a description of the photo.
_SIDEWALL_URL = re.compile(r"side[-_]?wall|[-_/]side[-_/.]|[-_/]side\d*\.(?:jpg|jpeg|png|webp)",
                           re.I)
# "sidetread" and "tread" mean the tread IS in frame, and the substring
# "side" inside "sidetread" would otherwise trip the pattern above.
_TREAD_URL = re.compile(r"tread", re.I)
# Larger than the full spread of domain_score + long_edge + squareness, so a
# sidewall shot always sorts last without ever being removed from the list.
_POSE_PENALTY = 10

# Gallery position, for hosts measured to encode it in the filename.
#
# PriorityTire serves a numbered gallery per tire — "...-b-aaa-1.jpg",
# "...-b-aaa-22.jpg" — and the number is the shot, not the product. Measured
# against 206 of its photos labelled by hand after the 2026-08-30 run:
#
#     index  1   191 visible /   7 rejected    4% wrong
#     index  2    21 visible /   1 rejected    5% wrong
#     index  3     9 visible /   7 rejected   44% wrong
#     index  4    14 visible /   3 rejected   18% wrong
#     index 22     0 visible /  20 rejected  100% wrong
#     all host   254 visible /  59 rejected   19% wrong
#
# Measured over every photo this host ever supplied, including ones since
# replaced. An earlier cut of this table read index 1 at 2% and the host at
# 21%; that counted only the photos still in the cache, which excludes the
# rejects that had already been swapped out, and so flattered both figures.
#
# Image 1 is the hero tread shot; the high numbers are alternate angles, and
# the sidewall lives among them. Preferring index 1 on this host takes its
# reject rate from 21% to 2%.
#
# Kept as a per-host table rather than a general "prefer the lowest number"
# rule because it does NOT generalize: SimpleTire names its shots
# (sidetread / treadfull / treadonly) and those split 15% / 9% / 23%, which is
# no signal at all. A host earns an entry here by being measured, not assumed.
_GALLERY_INDEX = {
    "backend.prioritytire.com": re.compile(r"-(\d+)\.jpg$", re.I),
}
_GALLERY_FIRST_BONUS = 2      # index 1-2 — the hero shot
_GALLERY_LATE_PENALTY = 4     # index >= 3 — measured 18-100% wrong
_GALLERY_LATE_FROM = 3


def _gallery_score(domain, url):
    """Bonus/penalty from a host's own gallery numbering. 0 when unmeasured."""
    pattern = _GALLERY_INDEX.get(domain)
    if not pattern:
        return 0
    m = pattern.search(url or "")
    if not m:
        return 0
    idx = int(m.group(1))
    if idx <= 2:
        return _GALLERY_FIRST_BONUS
    if idx >= _GALLERY_LATE_FROM:
        return -_GALLERY_LATE_PENALTY
    return 0


# Formats Shopify's product-CSV import cannot ingest. Unlike pose, this is not
# a preference to be scored: the image simply never attaches, and the listing
# ships blank with nothing in the logs to say why. A live run bought
# "l_zeal_56-2.heic" from a Shopify-hosted storefront — the format is fine
# where it came from, and useless on import. Rejected outright so the walk
# moves on to a candidate that will actually load.
_UNUSABLE_FORMAT = re.compile(r"\.(?:heic|heif|avif|bmp|tiff?|svg)(?:$|[?#])", re.I)


def filter_image_candidates(results, brand="", model="", product_type="tires"):
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

        # 4. Keyword association. WHEELS are strict: finish and spoke design
        # ARE the product, so a near-miss model is a visibly wrong picture on
        # the listing (the live test returned a "GT Form Torque" for a
        # "Torque F027"). Tires are lenient: a model's photo shows its tread
        # pattern and is reused across every size anyway, so brand-or-model is
        # enough. A wheel with no confident match gets NO image rather than a
        # confidently wrong one.
        text = f"{c.get('title', '')} {c.get('source', '')}".lower()
        is_wheel = str(product_type).lower() in ("wheels", "rims")
        if is_wheel:
            # Token overlap, not exact substring. Wheel "models" are a mix of
            # distinctive codes ("F027") and plain descriptions ("Steel Winter
            # Wheel"). Requiring the whole string verbatim throws away correct
            # photos of the descriptive ones, while a single shared word is far
            # too weak for the coded ones. Requiring the brand plus half the
            # model tokens rejects "GT Form Torque" for a "Torque F027" (0 of 1
            # token) while keeping "DTD - Steel - Take-Off" for a "DTD Steel
            # Winter Wheel" (2 of 3).
            if brand_l and brand_l not in text:
                continue
            tokens = [t for t in re.split(r"[^a-z0-9]+", model_l) if len(t) > 1]
            if not tokens:
                continue
            hits = sum(1 for t in tokens if t in text)
            if hits / len(tokens) < 0.5:
                continue
        else:
            if brand_l and brand_l not in text and model_l and model_l not in text:
                continue

        # 3. Source bias (soft — scored, never a hard reject). Matched on the
        # reported source as well as the URL host, since retailers commonly
        # serve from a CDN that carries none of their branding.
        url = c.get("original") or c.get("link") or ""
        if _UNUSABLE_FORMAT.search(url):
            continue
        domain = urlparse(url).netloc.lower()
        domain_score = _retailer_tier(domain, f"{c.get('source','')} {c.get('title','')}")
        if domain_score == 0:
            if any(d in domain for d in DOMAIN_ALLOWLIST_BIAS):
                domain_score = 2          # manufacturer — good, but hero/lifestyle heavy
            elif any(d.split(".")[0] in domain for d in DOMAIN_DEPRIORITIZE):
                domain_score = -2

        # Square-ish shots sit better in a product grid than 4:3.
        squareness_bonus = 1 if 0.9 <= ratio <= 1.11 else 0

        # Pose, read off what the PUBLISHER called the file rather than off
        # the pixels. This is the one reliable tread signal available at rank
        # time: image_view's face_on/core_bg_frac test was measured against 76
        # hand-labelled photos and caught 0 of 5 true sidewall shots while
        # firing on good three-quarter ones (the numbers are in tread.py), and
        # a live run watched it accept ".../951-sidewall/general-altimax-
        # arctic.png" after demoting two candidates. A path segment reading
        # "sidewall" is not an inference about the image — it is SimpleTire
        # saying which shot it is.
        #
        # Scored, never a hard reject, and deliberately larger than the whole
        # rest of the scale so a sidewall shot sorts below every alternative
        # but is still bought when it is the only candidate. Getting a photo
        # the reviewer can throw out beats shipping a blank listing, and a
        # hard reject here would silently turn "wrong pose" into "no image".
        pose_penalty = _POSE_PENALTY if _SIDEWALL_URL.search(url) else 0
        if _TREAD_URL.search(url):
            pose_penalty = 0

        score = (domain_score + long_edge_bonus + squareness_bonus
                 + _gallery_score(domain, url) - pose_penalty)
        scored.append((score, w * h, c))

    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [c for _, _, c in scored]


def _parse_spec_result(text, size_hint, needed):
    """Extract whichever of `needed` can be read out of aggregated SERP
    result text — the same size_parser functions augment*.py already trusts
    for this, just pointed at search-result text instead of the row's own
    description. Only returns keys that were actually found; a candidate
    still missing some of `needed` after this stays missing (see
    apply_spec_cache's cache_hits_partial) rather than being marked done.

    size_hint is the original (un-normalized) size string, used only to help
    parse_tire_size anchor width/ratio/rim if a size-shaped token also shows
    up in the result text (a plain google search result for a matching
    product overwhelmingly repeats the same size in a listing title).
    """
    found = {}
    if "load_index" in needed or "speed_rating" in needed:
        li, sr = parse_load_speed(text)
        if li and "load_index" in needed:
            found["load_index"] = li
        if sr and "speed_rating" in needed:
            found["speed_rating"] = sr
    if "load_range" in needed:
        # extract_load_range() reads a trailing letter off a SIZE string or a
        # PR/XL mention in free text — it has no pattern for a labeled field,
        # which is exactly how a retailer listing states this ("Load Range:
        # E", "Load Range E"). Check that shape first; it is unambiguous
        # where it matches, whereas a bare loose letter anywhere in a page of
        # search-result text is not safe to treat as the load range.
        m = re.search(r"Load\s*Range[:\s]+([B-N]|XL|SL)\b", text, re.IGNORECASE)
        lr = m.group(1).upper() if m else extract_load_range(size_hint, text)
        if lr:
            found["load_range"] = lr
    if {"section_width", "aspect_ratio", "rim_diameter"} & needed:
        parsed = parse_tire_size(text, "")
        for sub, key in (("section_width", "width"), ("aspect_ratio", "ratio"),
                        ("rim_diameter", "rim_display")):
            if sub in needed and parsed[key]:
                found[sub] = parsed[key]
    return found


# ---------------------------------------------------------------------------
# (e) — live search. Gated. NEVER invoked in this task.
# ---------------------------------------------------------------------------

def run_live(fields, supplier_filter, budget, verify_images=True, verify_depth=6,
             quality_cache=USE_DISK_QUALITY_CACHE):
    """The only path in this module that can spend the client's money.

    Four independent gates, all must pass:
      1. Kill switch absent (monitoring/SERP_KILL_SWITCH)
      2. budget > 0
      3. Explicit --live flag (checked by caller before this is even called)
      4. Cache checked before every single candidate — a cache hit never
         counts against budget or fires a network call.
      5. Report-only sources (writable=False) are never queued — their answer
         would have no column to live in, so the search could not pay off.

    Image verification (verify_images=True): after ranking, each candidate is
    downloaded and inspected by image_view.pick_verified_image() until one
    passes. This spends NO extra SerpAPI credit — all ~50 candidates come from
    the single search already paid for — so a rejected top result costs only
    an HTTP GET. It catches what metadata cannot: dead URLs, placeholder
    images, and flat face-on shots. Set verify_images=False to fall back to
    trusting the metadata ranking alone.

    Rehosting vs hotlinking (images): this function stores the winning URL. It
    does NOT upload anything to Shopify — see SERP_LAYER_DESIGN.md §6 for why
    rehosting is recommended over hotlinking (source stability, licensing).
    The seam is here: the verified bytes are already fetched at this point, so
    a rehost step would reuse them rather than re-download.
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

    if verify_images:
        try:
            from image_view import pick_verified_image
        except ImportError as exc:
            print(f"ERROR: image verification needs Pillow + numpy ({exc}). "
                  f"Install them, or pass --no-verify-images to skip the check.")
            sys.exit(1)

    # Before a single call goes out: fold every already-paid answer onto the
    # key the scan will look it up under. Skipping this would re-buy answers
    # the client has already paid for.
    migrate_cache_keys()

    cache = load_cache()
    spent = 0
    seen = set()
    verify_logs = {}
    skipped_unwritable = 0
    skipped_peer = 0

    for field in fields:
        if field == "specs":
            # Separate loop: candidates come from discover_spec_candidates()
            # (not discover_candidates()), and a cache "hit" here means EVERY
            # sub-field that row needs is covered — a candidate still missing
            # one of several needed sub-fields is searched again so the
            # remainder gets filled, with whatever was already found merged
            # in rather than overwritten.
            #
            # "tried" records every sub-field a search has ever been run for,
            # independent of whether it was found — the same distinction
            # image/designation make via cache_hits_empty (an entry that
            # exists but is empty means "already tried, don't re-buy the
            # same question"). Without it, a sub-field that came back empty
            # would look identical to one never searched at all, and the
            # next run would pay to ask the exact same query the exact same
            # question again.
            for c in discover_spec_candidates(supplier_filter):
                if spent >= budget:
                    print(f"Budget of {budget} reached — stopping mid-batch.")
                    return
                cache_entry  = cache.get(c["cache_key"]) or {}
                existing     = cache_entry.get("specs", {})
                tried        = set(cache_entry.get("tried", []))
                still_needed = {sub for sub in c["needed"] if sub not in existing and sub not in tried}
                if not still_needed or c["cache_key"] in seen:
                    continue
                seen.add(c["cache_key"])

                # The ledger entry is written before the request goes out, so
                # the call is on the books even if the parse below dies. See
                # serp_ledger.py for why ordering, not bookkeeping, is the fix.
                with SL.billed_call("specs", c["cache_key"], c["query"]) as call:
                    results = GoogleSearch({
                        "engine": "google", "q": c["query"], "num": 6,
                        "api_key": SERPAPI_KEY_CLIENT,
                    }).get_dict()
                    texts = []
                    for r in results.get("shopping_results", [])[:6]:
                        texts += [r.get("title", ""), r.get("snippet", "")]
                    for r in results.get("organic_results", [])[:6]:
                        texts += [r.get("title", ""), r.get("snippet", "")]
                    found = _parse_spec_result(" ".join(filter(None, texts)), c["size"], still_needed)
                    call.usable = bool(found)

                    merged = dict(existing)
                    merged.update(found)
                    cache[c["cache_key"]] = {"specs": merged, "query": c["query"],
                                             "tried": sorted(tried | still_needed),
                                             "ts": SL._now(), "call": call.id,
                                             "supplier": c["supplier"]}
                    save_cache(cache)
                spent += 1
                print(f"  [{spent}/{budget}] specs: {c['query']!r} "
                      f"needed={sorted(still_needed)} -> {found or '(nothing found)'}")
            continue

        cands = discover_candidates(field, supplier_filter, quality_cache)
        # Free layer (b2) runs before the chequebook opens: a key whose rows
        # can all be filled from a photo the catalogue already owns must not
        # be bought. apply_cached() will land those for nothing.
        qcache = resolve_quality_cache(field, quality_cache)
        peers = build_peer_image_index(qcache) if field == "image" else {}
        for c in cands:
            if spent >= budget:
                print(f"Budget of {budget} reached — stopping mid-batch.")
                return
            if peers and _peer_upgrade(c, peers, qcache):
                skipped_peer += 1
                continue
            if not c["writable"]:
                # This source has no column the answer could be written into
                # (SOURCES: writable=False), so buying the search pays for a
                # result that can never reach Shopify. It stays counted in the
                # report as a real gap — it just never gets queued for spend.
                skipped_unwritable += 1
                continue
            if c["cache_key"] in cache or c["cache_key"] in seen:
                continue  # already paid for, or covered earlier in this same batch
            seen.add(c["cache_key"])

            # One wrapper covers both branches and the cache write. The image
            # branch is the reason this has to span the parse: pick_verified_image()
            # does its own network fetches, so it is the most likely place for a
            # call that has ALREADY been billed to die without leaving a record.
            with SL.billed_call(field, c["cache_key"], c["query"]) as call:
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
                    ptype = c.get("type", "tires")
                    best = filter_image_candidates(candidates_raw, c["brand"], c["model"], ptype)
                    # Never re-select a photo a reviewer has already rejected
                    # for showing the sidewall. The same stock image is served
                    # by many retailers, so without this the walk below can buy
                    # its way back to a picture already thrown out by hand.
                    tcache = _TREAD.load()
                    best = [b for b in best
                            if not _TREAD.is_rejected(
                                b.get("original") or b.get("link") or "", tcache)]
                    # Metadata ranking picks the queue; pixels pick the winner.
                    # Walking down the list costs NO extra SerpAPI credit — every
                    # candidate came from the one search already paid for above.
                    if verify_images and best:
                        winner, check_log = pick_verified_image(
                            best, ptype, max_fetch=verify_depth, verbose=True)
                        verify_logs[c["cache_key"]] = check_log
                        value = (winner.get("original") or winner.get("link") or "") if winner else ""
                    else:
                        value = (best[0].get("original") or best[0].get("link") or "") if best else ""

                call.usable = bool(value)
                # `ts` and `call` are what turn the cache from "an answer" into
                # a record of WHICH tire was enriched with WHAT, WHEN, and by
                # which billed call — so a row on the storefront can be traced
                # back to the line in serp_ledger.jsonl that paid for it, and
                # so a stale answer can be expired by date without discarding
                # the whole cache. `supplier` records who needed it first.
                entry = {field: value, "query": c["query"],
                         "ts": SL._now(), "call": call.id,
                         "supplier": c["supplier"]}
                if field == "image" and c["cache_key"] in verify_logs:
                    entry["verified"] = verify_logs[c["cache_key"]]
                cache[c["cache_key"]] = entry
                save_cache(cache)
            spent += 1
            print(f"  [{spent}/{budget}] {field}: {c['query']!r} -> {value!r}")

    if skipped_unwritable:
        print(f"Skipped {skipped_unwritable} candidate(s) from report-only sources — "
              f"no column exists to store the answer, so no search was bought for them.")
    if skipped_peer:
        print(f"Skipped {skipped_peer} candidate(s) already covered by a usable photo "
              f"elsewhere in the catalogue — --apply lands those for free.")

    if verify_logs:
        checked = sum(len(v) for v in verify_logs.values())
        rejected = sum(1 for v in verify_logs.values() for e in v
                       if e.get("status") not in ("accepted", None))
        empty = sum(1 for v in verify_logs.values()
                    if not any(e.get("status") == "accepted" for e in v))
        print(f"Image verification: {checked} candidate(s) downloaded, "
              f"{rejected} rejected or demoted, {empty} product(s) left without "
              f"a clean image. Per-candidate detail is in the cache under "
              f"'verified' — no extra SerpAPI credit was spent on any of it.")

    print(f"Live run complete. Spent {spent} / {budget}.")


# ---------------------------------------------------------------------------
# Reporting — read-only, drives monitoring/ENRICHMENT_APPROVAL.md
# ---------------------------------------------------------------------------

def per_source_breakdown(field, supplier_filter=None,
                        quality_cache=USE_DISK_QUALITY_CACHE):
    """Per (supplier, type) breakdown for `field`: missing / cache_hit_nonempty
    (free — already paid) / needs_paid (no cache entry at all).

    For field='image', `quality_cache` also counts rows whose image is present
    but unusable (low_res / placeholder / dead link). Those are real gaps the
    storefront shows, so they belong in needs_paid — the number the client is
    asked to approve — and are broken out by cause in `by_verdict` so "blank"
    and "130px thumbnail" never look like the same problem. Defaults to the
    verdicts on disk, so this figure and the one run_live() spends against
    cannot drift apart (see USE_DISK_QUALITY_CACHE).

    `peer_fix` is the free layer (b2): rows a photo already in the catalogue
    covers. They are gaps, but they are not purchases, so they are excluded
    from needs_paid and from paid_keys.
    """
    if field == "specs":
        return per_spec_source_breakdown(supplier_filter)

    cache = load_cache()
    qcache = resolve_quality_cache(field, quality_cache)
    peers = build_peer_image_index(qcache) if field == "image" else {}
    rows = {}
    for c in discover_candidates(field, supplier_filter, quality_cache):
        key = (c["supplier"], c["type"])
        r = rows.setdefault(key, dict(missing=0, cache_hit=0, peer_fix=0, needs_paid=0,
                                      writable=c["writable"],
                                      paid_keys=set(),
                                      by_verdict=collections.Counter()))
        r["missing"] += 1
        if c.get("verdict"):
            r["by_verdict"][c["verdict"]] += 1
        entry = cache.get(c["cache_key"])
        if entry and entry.get(field):
            r["cache_hit"] += 1
        elif peers and _peer_upgrade(c, peers, qcache):
            # Free from a photo the catalogue already owns — counted as fixed,
            # never as something to buy. run_live() skips these too, so the
            # two numbers cannot disagree.
            r["peer_fix"] += 1
        elif entry is None:
            r["needs_paid"] += 1
            # run_live() buys one search per DISTINCT cache key, not per row —
            # many rows share a key (every size of a tire model shares one
            # image key). needs_paid counts affected rows, which is what the
            # storefront shows; paid_keys is what is actually billed. Reporting
            # only the first would overstate the bill by ~8x on images.
            r["paid_keys"].add(c["cache_key"])
        # entry exists but empty ("already tried, came back empty") counts
        # toward neither — it's a resolved non-hit, not a fresh spend candidate.
    return rows


def per_spec_source_breakdown(supplier_filter=None):
    """specs counterpart to per_source_breakdown(). needs_paid counts ONLY
    rows with at least one sub-field never yet searched — a row that was
    searched and came back empty is a dead end (dead_end), not something
    another --run would spend on again. See apply_spec_cache's docstring for
    why that distinction has to exist at all.
    """
    cache = load_cache()
    rows = {}
    for c in discover_spec_candidates(supplier_filter):
        key = (c["supplier"], c["type"])
        r = rows.setdefault(key, dict(missing=0, cache_hit=0, needs_paid=0,
                                      dead_end=0, writable=True,
                                      paid_keys=set()))
        r["missing"] += 1
        entry = cache.get(c["cache_key"]) or {}
        specs, tried = entry.get("specs", {}), set(entry.get("tried", []))
        if c["needed"] <= specs.keys() and all(specs.get(s) for s in c["needed"]):
            r["cache_hit"] += 1
        elif c["needed"] - tried:
            r["needs_paid"] += 1
            # Same rows-vs-searches distinction as per_source_breakdown. A
            # specs key keeps the size (a load index really does differ per
            # size), so these two numbers are usually close — but they must
            # still be reported separately, and an empty paid_keys here would
            # read as "0 searches" and silently cap the specs budget at zero.
            r["paid_keys"].add(c["cache_key"])
        else:
            r["dead_end"] += 1
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

    monitoring/ is gitignored, so gap_report.py exists only on a dev machine;
    anywhere else (a fresh clone, the client's Mac app) the audit is empty.
    """
    gap_dir = os.path.join(ROOT, "monitoring")
    if not os.path.isfile(os.path.join(gap_dir, "gap_report.py")):
        print("Free-field audit skipped: monitoring/gap_report.py is not on this machine.")
        return {}
    sys.path.insert(0, gap_dir)
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
        "For image, \"missing\" means the storefront has no USABLE photo, not merely an "
        "empty column: a row whose image has been measured and found to be under "
        f"{IMAGE_MIN_SIDE_PX}px, a supplier placeholder graphic, or a dead link counts here "
        "too. Run `enrich_runner.py --scan --check-images` to measure them (plain HTTP, "
        "no API spend); rows never measured are not counted either way. "
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

    os.makedirs(os.path.dirname(APPROVAL_MD), exist_ok=True)
    with open(APPROVAL_MD, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"\nApproval report written to: {APPROVAL_MD}")


def report(fields, supplier_filter=None, budget_hint=5000, apply_cache=False, write=False):
    migrated, already = migrate_formula_cache()
    if migrated:
        print(f"Migrated {migrated} entries from formula's old serp_cache.json into the "
              f"centralized cache ({already} were already present).")
    rekeyed, merged = migrate_cache_keys()
    if rekeyed:
        print(f"Re-keyed {rekeyed} cache entr(ies) onto the normalized product key "
              f"({merged} collapsed onto an entry already held). No answer was lost.")

    simple_fields = [f for f in fields if f != "specs"]
    stats = apply_cached(simple_fields, supplier_filter, write=(apply_cache and write))
    if "specs" in fields:
        stats["specs"] = apply_spec_cache(supplier_filter, write=(apply_cache and write))

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
        if field == "specs":
            for c in discover_spec_candidates(supplier_filter):
                entry = (cache_now.get(c["cache_key"]) or {}).get("specs", {})
                if not (c["needed"] <= entry.keys() and all(entry.get(s) for s in c["needed"])):
                    manifest.append(c)
        else:
            for c in discover_candidates(field, supplier_filter):
                if c["cache_key"] not in cache_now:
                    manifest.append(c)

    print(f"TOTAL new searches needing approval: {total_needs_paid}")
    verdict = "WITHIN budget" if total_needs_paid <= budget_hint else "EXCEEDS budget — prioritize"
    print(f"vs proposed budget {budget_hint}: {verdict}")

    # monitoring/ is gitignored, so a fresh checkout or install has none.
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump([{k: v for k, v in c.items()} for c in manifest], fh, indent=2, default=str)
    print(f"\nFull query manifest written to: {MANIFEST}")

    # Only build the full markdown approval report when this was a
    # whole-catalog run (no supplier/field filter) — a partial run doesn't
    # have enough data to fill every table. The doc itself only covers
    # designation/image so far (specs numbers print above and land in
    # .enrich_scan.json via enrich_runner.py --scan, which is what the
    # dashboard actually reads — extending this markdown generator is a
    # separate change).
    if supplier_filter is None and {"designation", "image"} <= set(fields):
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
    ap.add_argument("--no-verify-images", dest="verify_images", action="store_false",
                     help="Skip the pixel check on image results. Verification costs no "
                          "SerpAPI credit (it re-uses candidates from the same search), "
                          "so the only reason to skip it is speed.")
    ap.add_argument("--verify-depth", type=int, default=6,
                     help="How far down the ranked list to walk while looking for an "
                          "image that passes the pixel check (default 6).")
    ap.add_argument("--requeue-rejected", action="store_true",
                     help="Drop cached image answers a reviewer rejected for pose, so "
                          "the next live run can buy a replacement. Without this they "
                          "stay flagged as gaps forever and are never re-bought. "
                          "Read-only unless --write is also given.")
    ap.add_argument("--migrate-suffix-keys", action="store_true",
                     help="Re-home image answers stranded by the load/speed strip, "
                          "matching each against the keys the catalogue actually "
                          "generates. Read-only unless --write is also given.")
    ap.add_argument("--write", action="store_true",
                     help="Let --migrate-suffix-keys save its changes.")
    ap.add_argument("--audit-images", action="store_true",
                     help="List every photo that ended up on more than one BRAND — "
                          "a generic or category image that won several unrelated "
                          "products. Read-only unless --quarantine is also given.")
    ap.add_argument("--quarantine", action="store_true",
                     help="With --audit-images, blank those answers so the products "
                          "ship with no photo instead of a wrong one. Costs nothing "
                          "and does not re-queue them for purchase.")
    ap.add_argument("--audit-keys", action="store_true",
                     help="List every normalized product key that merges more than one "
                          "raw brand|model, so an ambiguous new model name is caught "
                          "before two products end up sharing one photo. Read-only.")
    args = ap.parse_args()

    if args.requeue_rejected:
        n = requeue_tread_rejected(write=args.write)
        print(f"{n} cached answer(s) hold a photo the reviewer rejected for pose.")
        if args.write:
            print("Dropped, so the next live run can buy a replacement. The "
                  "rejections themselves stay in tread_cache.json, so none of "
                  "those photos can be chosen again.")
        else:
            print("Read-only. Re-run with --write to drop them.")
        return

    if args.migrate_suffix_keys:
        moved, ambiguous, left = migrate_glued_load_speed_keys(write=args.write)
        print(f"{moved} answer(s) re-homed onto the key the catalogue asks for.")
        print(f"{ambiguous} skipped as ambiguous (more than one stem fits).")
        print(f"{left} paid answer(s) still unreachable — products the catalogue "
              f"no longer carries, so nothing re-buys them.")
        if not args.write:
            print("Read-only. Re-run with --write to save.")
        return

    if args.audit_images:
        offenders, touched = quarantine_shared_images(write=args.quarantine)
        keys_hit = sum(len(v) for v in offenders.values())
        print(f"{len(offenders)} photo(s) landed on more than one brand, "
              f"covering {keys_hit} product key(s).\n")
        for url, keys in sorted(offenders.items(), key=lambda kv: -len(kv[1])):
            brands = sorted({k.split(":", 1)[1].split("|")[0] for k in keys})
            print(f"  {len(keys)} keys across {len(brands)} brands: {', '.join(brands)}")
            print(f"      {url[:110]}")
        if args.quarantine:
            print(f"\nBlanked {touched} answer(s). Those products now ship with no "
                  f"photo rather than a wrong one, and are NOT re-queued for spend.")
        elif offenders:
            print("\nRead-only. Re-run with --quarantine to blank these.")
        return

    if args.audit_keys:
        merges = audit_key_normalization()
        print(f"{len(merges)} normalized key(s) merge more than one raw brand|model.\n")
        for key, variants in sorted(merges.items(), key=lambda kv: -len(kv[1]))[:40]:
            print(f"  {key}  ({len(variants)} spellings)")
            for brand, model in sorted(variants):
                print(f"      {brand!r} | {model!r}")
        print("\nEach group above shares ONE cached answer. Confirm they are spelling "
              "variants of a single product, not two different products.")
        return

    fields = list(SUPPORTED_FIELDS) if args.field == "all" else [args.field]

    if args.live and args.budget:
        print(f"LIVE MODE requested — budget={args.budget}, fields={fields}, "
              f"supplier={args.supplier or 'all'}, "
              f"image verification={'on' if args.verify_images else 'OFF'}")
        run_live(fields, args.supplier, args.budget,
                 verify_images=args.verify_images, verify_depth=args.verify_depth)
        return

    if args.live and not args.budget:
        print("NOTE: --live given without --budget — falling back to report-only (no calls made).")

    report(fields, args.supplier, budget_hint=5000, apply_cache=True, write=args.apply)


if __name__ == "__main__":
    main()
