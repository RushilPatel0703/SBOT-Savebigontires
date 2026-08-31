#!/usr/bin/env python3
"""Dashboard-facing driver for the shared enrichment layer.

pipelines/shared/enrichment.py is the engine: candidate discovery, the
centralized cache, filter_image_candidates(), run_live(). This module is the
thin operator-facing wrapper the Streamlit dashboard shells out to. It exists
for two reasons the engine deliberately does not cover.

1. The dashboard needs a MACHINE-READABLE count to put in front of someone
   before they approve spend. enrichment.py's report() prints to stdout and
   writes a markdown approval doc — both written for a human reading a
   terminal. --scan writes .enrich_scan.json instead, and makes no network
   call and no write to any CSV.

2. A live search only fills the CACHE. Getting that result onto the storefront
   takes two more moves: apply it into the cleaned/raw CSV, then re-run the
   supplier's augment step so Data/shopify/Shopify_SEO_*.csv — the file
   "Prepare Upload Files" actually bundles — is rebuilt from the fixed input.
   Run by hand from a terminal, a person does those two steps themselves. From
   a button, the whole chain has to happen or the click spends money and
   changes nothing the client can see.

Modes
-----
  --scan          Read-only. Counts what each field needs. No network, no writes.
  --apply-only    Applies already-paid cache hits, then rebuilds whatever
                  changed. No network.
  --run           The only mode that can spend money. Needs an explicit
                  per-field budget and hard-stops at it. Refuses to start if
                  monitoring/SERP_KILL_SWITCH exists.

Budgets are per field rather than one pooled number on purpose: sharing a pool
lets the image queue (thousands of rows) swallow every search before the
designation queue is reached, which is not what someone approving "200 image +
5 designation" agreed to. "specs" is its own third budget — load index, speed
rating, load range, section width, aspect ratio, rim diameter — bundled into
one field because a row missing its load index is almost always missing its
speed rating too (same source token), so one search commonly fills several of
these for the price of one; see enrichment.py's SPEC_SOURCES for why.

Usage:
    python enrich_runner.py --scan
    python enrich_runner.py --apply-only
    python enrich_runner.py --run --image-budget 412 --designation-budget 0 --specs-budget 665
"""
import argparse
import collections
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import enrichment as E  # noqa: E402  — needs the sys.path line above
import serp_ledger as SL  # noqa: E402

PIPELINES_DIR = os.path.dirname(HERE)
SCAN_FILE     = os.path.join(PIPELINES_DIR, ".enrich_scan.json")
RESULT_FILE   = os.path.join(PIPELINES_DIR, ".enrich_result.json")

_UTF8_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}

# The step that rebuilds Data/shopify/Shopify_SEO_*.csv from a given supplier's
# cleaned/raw CSV — i.e. the step that has to run again once enrichment edits
# that input. Mirrors the STEPS list in each supplier's run_pipeline.py; keyed
# by (supplier, product type) so a fix to one product type never triggers an
# augment for the other, which may have no scraped data at all this run.
#
# A rename on the run_pipeline.py side makes `--only <step>` exit 1 with
# "Unknown step: ...", so a stale entry here fails loudly in the log rather
# than quietly skipping the rebuild.
AUGMENT_STEPS = {
    ("formula_distributors", "tires"):  "augment",
    ("formula_distributors", "wheels"): "augment_wheels",
    ("northcountry",         "tires"):  "augment_tires",
    ("northcountry",         "wheels"): "augment_wheels",
    ("wheelsmart",           "tires"):  "augment_tires",
    ("wheelsmart",           "rims"):   "augment_rims",
    ("emergetires",          "tires"):  "augment_tires",
    ("dttire",               "tires"):  "augment_tires",
    ("dttire",               "wheels"): "augment_wheels",
}


# The nine spec fields a customer actually sees, as labelled in each product's
# description block. They are counted by reading them back OUT of the finished
# Shopify CSV rather than out of the cleaned files, because that block is built
# in augment*.py — downstream of every stage monitoring/gap_report.py measures.
# A field can look fully resolved at the cleaned stage and still ship blank.
SPEC_FIELDS = [
    "Brand", "Style", "Section width", "Aspect ratio", "Rim diameter",
    "Load index", "Speed rating", "Load range", "Designation",
]

_SPEC_RE  = {f: re.compile(r"<br>" + re.escape(f) + r":\s*(.*?)<br>") for f in SPEC_FIELDS}
_ZERO_ISH = re.compile(r"^0+(?:\.0+)?$")


def _blank_spec(value):
    """A spec that ships as nothing useful.

    "0.0000" counts as blank: Costar writes it for an unknown numeric spec,
    and on a product page it reads as a real measurement rather than as the
    missing value it is.
    """
    s = str(value).strip()
    return (not s
            or s.lower() in ("unknown", "nan", "none", "n/a")
            or bool(_ZERO_ISH.match(s)))


def _latest_shopify_files():
    """Newest Shopify_SEO CSV per supplier per product type — the exact files
    'Prepare Upload Files' would bundle."""
    stamped = re.compile(r"_\d{8}_\d{6}\.csv$")
    found = {}
    for supplier in sorted({sup for sup, _ in AUGMENT_STEPS}):
        pattern = os.path.join(PIPELINES_DIR, supplier, "Data", "shopify", "Shopify_SEO_*.csv")
        groups = {}
        for f in glob.glob(pattern):
            groups.setdefault(stamped.sub("", os.path.basename(f)), []).append(f)
        for name, files in groups.items():
            found[(supplier, name)] = max(files, key=os.path.getmtime)
    return found


def field_completeness():
    """Count blank spec fields in the finished Shopify CSVs, per file.

    Wheel and rim files carry a different block entirely (Wheel Diameter, Bolt
    Pattern, Offset, Centre Bore, Finish), so tire specs are genuinely not
    applicable there. Those files are reported as such instead of being scored
    against fields they were never meant to have — counting them as "missing"
    would invent ~1,200 gaps that do not exist.
    """
    import pandas as pd

    rows = []
    for (supplier, name), path in sorted(_latest_shopify_files().items()):
        try:
            df = pd.read_csv(path, dtype=str, low_memory=False, encoding="utf-8-sig").fillna("")
        except Exception as exc:
            print(f"  WARNING: could not read {os.path.basename(path)}: {exc}")
            continue
        if "Body (HTML)" not in df.columns or df.empty:
            continue
        body = df["Body (HTML)"].astype(str)

        # One probe decides the block type — a tire block always labels a
        # section width, a wheel block never does.
        if not body.head(50).str.contains("Section width:", regex=False).any():
            rows.append(dict(supplier=supplier, file=os.path.basename(path), kind="wheels",
                             total=len(df), missing={}))
            continue

        missing = {}
        for field in SPEC_FIELDS:
            values = body.str.extract(_SPEC_RE[field], expand=False).fillna("")
            missing[field] = int(values.apply(_blank_spec).sum())
        rows.append(dict(supplier=supplier, file=os.path.basename(path), kind="tires",
                         total=len(df), missing=missing))

    tire_rows = [r for r in rows if r["kind"] == "tires"]
    totals = {f: sum(r["missing"][f] for r in tire_rows) for f in SPEC_FIELDS}
    return dict(
        rows=rows,
        totals=totals,
        tire_rows_scanned=sum(r["total"] for r in tire_rows),
        generated=datetime.now().isoformat(timespec="seconds"),
    )


def _write_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)


def _source_files():
    """Latest input CSV per (supplier, type) — the same file enrichment reads
    and writes, and the same one augment*.py will pick up afterwards."""
    found = {}
    for src in E.SOURCES:
        f = E.latest(src["glob"])
        if f:
            found[(src["supplier"], src["type"])] = f
    return found


def _rel(path):
    return os.path.relpath(path, E.ROOT).replace("\\", "/")


# ---------------------------------------------------------------------------
# --scan — read-only
# ---------------------------------------------------------------------------

def _image_urls_on_disk():
    """Every image URL the suppliers currently have on disk, with its row count."""
    import pandas as pd

    urls = []
    for src in E.SOURCES:
        if src["field"] != "image" or not src.get("value_col"):
            continue
        f = E.latest(src["glob"])
        if not f:
            continue
        df = pd.read_csv(f, dtype=str, low_memory=False, encoding="utf-8-sig").fillna("")
        if src["value_col"] in df.columns:
            urls.extend(u for u in df[src["value_col"]] if str(u).strip())
    return urls


def _image_quality_cache(check_images, refresh_images):
    """Measure the supplier images that have never been measured, then return
    the verdict map. Plain HTTP against public CDNs; no SerpAPI credit, ever.

    MEASURING IS THE DEFAULT, and that is the whole point of this function.
    An unmeasured URL is deliberately classified 'unknown' and NOT counted as
    a gap (guessing would inflate the number the client is asked to approve).
    That is the right rule and it has a trap attached: every URL is unmeasured
    until something measures it, so a scan that skipped measuring reported
    almost no image gaps at all. The dashboard runs plain `--scan`, so the
    5,662-row image gap this layer exists to surface was invisible from the
    only screen the client uses — and would go invisible again after every
    scrape, since a scrape rewrites the URLs.

    The cost of the default is the delta, not the catalogue: verdicts are
    cached by URL and deduped across sources (8,986 DT Tire rows share 979
    images), so a warm cache measures nothing and a post-scrape scan measures
    only what actually changed.
    """
    if not (check_images or refresh_images):
        # Explicit --no-check-images: fall back to whatever is already on
        # disk, and say so, because the numbers below are then only as
        # complete as that cache.
        cached = E.load_quality_cache()
        print("Image quality audit: SKIPPED (--no-check-images) — using "
              f"{len(cached)} cached verdict(s); unmeasured images are not "
              f"counted as gaps.")
        return cached or None

    urls = _image_urls_on_disk()
    distinct = set(urls)
    cached = E.load_quality_cache()
    todo = {u for u in distinct
            if not E.judge_image_url(u) and (refresh_images or u not in cached)}
    print(f"Image quality audit: {len(distinct)} distinct URL(s) across "
          f"{len(urls)} row(s); {len(todo)} to measure "
          f"({len(distinct) - len(todo)} already decided)")
    return E.measure_image_urls(urls, refresh=refresh_images)


def scan(check_images=True, refresh_images=False):
    """Count what enrichment could still fix, and write it as JSON.

    Zero SerpAPI calls and zero writes to any CSV: every number comes from
    reading the cleaned/raw files already on disk and comparing them against
    the cache of results already paid for.

    With check_images, the images the suppliers DID give us are downloaded and
    measured, and the unusable ones (too small, placeholder, dead link) join
    the blank ones in `needs_search` — so the approval total reflects what the
    storefront actually shows rather than merely whether a column is empty.
    Those fetches are plain HTTP against public CDNs; no API credit is spent.
    """
    migrated, already = E.migrate_formula_cache()
    if migrated:
        print(f"Migrated {migrated} entr(ies) from formula's original cache "
              f"into the centralized one ({already} already present).")

    quality = _image_quality_cache(check_images, refresh_images)

    fields = {}
    all_paid_keys = set()
    for field in E.SUPPORTED_FIELDS:
        rows = []
        qc = quality if field == "image" else None
        for (supplier, ptype), r in sorted(E.per_source_breakdown(field, quality_cache=qc).items()):
            rows.append(dict(
                supplier=supplier, type=ptype,
                missing=r["missing"],
                peer_fixed=r.get("peer_fix", 0),
                cache_fixed=r["cache_hit"],
                needs_search=r["needs_paid"],
                dead_end=r.get("dead_end", 0),
                writable=r["writable"],
                by_verdict=dict(r.get("by_verdict") or {}),
                searches=len(r.get("paid_keys") or ()),
                _keys=r.get("paid_keys") or set(),
            ))
        # Rows from a report-only source have no column to write an answer
        # into, so a search bought for them is money for a result that can
        # never ship. They stay visible as a known gap but are excluded from
        # anything anyone is asked to approve — run_live() skips them too.
        #
        # dead_end (specs only): every sub-field a row needs has already been
        # searched for and come back empty. Distinct from needs_search — a
        # dead-end row is NOT offered for spend again, because doing so would
        # buy the identical question a second time for an answer that isn't
        # out there. See enrichment.apply_spec_cache's docstring.
        # A cache key is shared across suppliers as well as across rows (the
        # same tire model is carried by more than one supplier), so the billed
        # count is the union, not the sum, of the per-source key sets.
        field_keys = set().union(*(r["_keys"] for r in rows if r["writable"])) if rows else set()
        fields[field] = dict(
            rows=[{k: v for k, v in r.items() if k != "_keys"} for r in rows],
            missing=sum(r["missing"] for r in rows),
            cache_hit=sum(r["cache_fixed"] for r in rows),
            # Free from a photo the catalogue already owns — a fix, not a
            # purchase. See enrichment.build_peer_image_index().
            peer_fixed=sum(r["peer_fixed"] for r in rows),
            needs_paid=sum(r["needs_search"] for r in rows if r["writable"]),
            searches=len(field_keys),
            report_only=sum(r["needs_search"] for r in rows if not r["writable"]),
            dead_end=sum(r["dead_end"] for r in rows),
        )
        all_paid_keys |= field_keys

    payload = dict(
        generated=datetime.now().isoformat(timespec="seconds"),
        kill_switch=os.path.exists(E.KILL_SWITCH),
        completeness=field_completeness(),
        fields=fields,
        total_needs_paid=sum(f["needs_paid"] for f in fields.values()),
        total_searches=len(all_paid_keys),
        total_cache_hit=sum(f["cache_hit"] for f in fields.values()),
        total_peer_fixed=sum(f["peer_fixed"] for f in fields.values()),
        sources=[
            dict(supplier=s, type=t, file=_rel(f),
                 mtime=datetime.fromtimestamp(os.path.getmtime(f)).isoformat(timespec="seconds"))
            for (s, t), f in sorted(_source_files().items())
        ],
    )
    _write_json(SCAN_FILE, payload)

    print("\n=== ENRICHMENT SCAN — no API calls, no files changed ===")
    for field, f in fields.items():
        # rows-affected and searches-billed are different numbers: one search
        # answers every row sharing its cache key.
        billed = (f"{f['searches']} search(es) buy {f['needs_paid']} row(s)"
                  if f["searches"] != f["needs_paid"]
                  else f"needs-a-paid-search={f['needs_paid']}")
        print(f"[{field}] missing={f['missing']}  free-from-cache={f['cache_hit']}  "
              + (f"free-from-a-photo-we-already-have={f['peer_fixed']}  "
                 if f["peer_fixed"] else "")
              + f"{billed}"
              + (f"  (+{f['report_only']} report-only, never bought)" if f["report_only"] else "")
              + (f"  (+{f['dead_end']} already tried, no match found — won't be re-bought)"
                 if f["dead_end"] else ""))
        # For images, say WHY each row is a gap. "blank column" and "shipped a
        # 130px thumbnail" are both gaps but they are not the same problem,
        # and only one of them is visible without measuring the pixels.
        if field == "image":
            merged = collections.Counter()
            for r in f["rows"]:
                merged.update(r.get("by_verdict") or {})
            if merged:
                label = {"missing": "no image url", "placeholder": "placeholder graphic",
                         "low_res": f"below {E.IMAGE_MIN_SIDE_PX}px", "unusable": "dead/undecodable link"}
                detail = ", ".join(f"{label.get(k, k)}={v}" for k, v in merged.most_common())
                print(f"          by cause: {detail}")
    print(f"\nTotal needing approval: {payload['total_searches']} search(es), "
          f"filling {payload['total_needs_paid']} row(s)")
    free_now = payload["total_cache_hit"] + payload["total_peer_fixed"]
    print(f"Free fixes ready to apply: {free_now} row(s)"
          + (f" ({payload['total_cache_hit']} from already-paid searches, "
             f"{payload['total_peer_fixed']} from photos the catalogue already owns)"
             if payload["total_peer_fixed"] else ""))

    comp = payload["completeness"]
    if comp["tire_rows_scanned"]:
        print(f"\nSpec-block completeness across {comp['tire_rows_scanned']} tire row(s) "
              f"in the finished Shopify CSVs:")
        for field in SPEC_FIELDS:
            n = comp["totals"][field]
            print(f"  {field:16s} {n:>6d} blank" + ("" if n else "   ok"))
    if payload["kill_switch"]:
        print(f"KILL SWITCH ACTIVE at {E.KILL_SWITCH} — live runs are blocked.")
    print(f"Scan written to: {SCAN_FILE}")
    return payload


# ---------------------------------------------------------------------------
# Shared tail: land cached results in the CSVs, rebuild what changed
# ---------------------------------------------------------------------------

def _apply_and_rebuild(fields):
    """Write cache hits into the cleaned/raw CSVs, then re-run augment for
    exactly the (supplier, type) pairs whose file actually changed.

    Rebuilding only what changed is not just a speed win: re-running augment
    for a supplier whose wheels were never scraped this cycle fails on a
    missing input, turning a successful enrichment into a failed run.
    """
    print("ENRICH PHASE: apply — writing cached results into the cleaned/raw CSVs")
    simple_fields = [f for f in fields if f != "specs"]
    stats = E.apply_cached(simple_fields, None, write=True) if simple_fields else {}
    if "specs" in fields:
        stats["specs"] = E.apply_spec_cache(None, write=True)
    for field, s in stats.items():
        hits = s["cache_hits"] + s.get("cache_hits_partial", 0) + s.get("peer_fixed", 0)
        print(f"  {field}: {hits} row(s) fixed from cache, "
              f"{s['still_needs_paid']} still unsearched, "
              f"{len(s['files_written'])} file(s) rewritten")

    written = {os.path.abspath(p) for s in stats.values() for p in s["files_written"]}
    targets = set()
    for (supplier, ptype), f in _source_files().items():
        if os.path.abspath(f) in written:
            targets.add((supplier, ptype))

    if not targets:
        print("ENRICH PHASE: augment — skipped, no CSV changed. The Shopify CSVs "
              "already match their inputs.")
        return stats, []

    failures = []
    for supplier, ptype in sorted(targets):
        step = AUGMENT_STEPS.get((supplier, ptype))
        if step is None:
            print(f"  WARNING: no augment step mapped for {supplier}/{ptype} — its "
                  f"Shopify CSV was NOT rebuilt and will not carry the fix.")
            failures.append(f"{supplier}/{ptype}: no augment step mapped")
            continue
        print(f"ENRICH PHASE: augment ({supplier} / {step})")
        rc = subprocess.run(
            [sys.executable, "run_pipeline.py", "--only", step],
            cwd=os.path.join(PIPELINES_DIR, supplier), env=_UTF8_ENV,
        ).returncode
        if rc != 0:
            print(f"  ERROR: {supplier} --only {step} exited {rc} — its Shopify CSV "
                  f"does not carry the fix.")
            failures.append(f"{supplier}/{step}: exit {rc}")
    return stats, failures


def _finish(mode, stats, failures, **extra):
    result = dict(
        mode=mode,
        finished=datetime.now().isoformat(timespec="seconds"),
        fields={f: {k: v for k, v in s.items() if k != "files_written"}
                for f, s in stats.items()},
        files_written=sorted({_rel(p) for s in stats.values() for p in s["files_written"]}),
        failures=failures,
        ledger=SL.summary(),
        **extra,
    )
    _write_json(RESULT_FILE, result)

    # Re-scan so the dashboard's counts reflect what just happened instead of
    # still showing the pre-run numbers.
    print("ENRICH PHASE: rescan")
    scan()

    if failures:
        print(f"\nENRICH FINISHED WITH {len(failures)} PROBLEM(S): " + "; ".join(failures))
        return 1
    print("\nENRICH COMPLETE — run 'Prepare Upload Files' to bundle the refreshed CSVs.")
    return 0


# ---------------------------------------------------------------------------
# --apply-only — free, no network
# ---------------------------------------------------------------------------

def apply_only():
    print("ENRICH START — applying results already paid for. No searches, no network calls.")
    stats, failures = _apply_and_rebuild(list(E.SUPPORTED_FIELDS))
    return _finish("apply-only", stats, failures, approved=0)


# ---------------------------------------------------------------------------
# --run — the only path that spends the client's money
# ---------------------------------------------------------------------------

def run(image_budget, designation_budget, specs_budget=0, verify_images=True, verify_depth=6):
    if os.path.exists(E.KILL_SWITCH):
        print(f"ABORT: kill switch present at {E.KILL_SWITCH}. Delete it to re-enable live runs.")
        return 1

    budgets = [("image", max(0, image_budget)), ("designation", max(0, designation_budget)),
               ("specs", max(0, specs_budget))]
    approved = sum(b for _, b in budgets)
    if approved <= 0:
        print("ABORT: no budget approved — nothing to search for.")
        return 1

    before = SL.summary()
    print(f"ENRICH START — {approved} search(es) approved (image={budgets[0][1]}, "
          f"designation={budgets[1][1]}, specs={budgets[2][1]}) against the client's "
          f"PAID SerpAPI key.")
    print(f"  ledger before this run: {before['total_billed']} call(s) billed to date; "
          f"this run may add up to {approved}, for a ceiling of "
          f"{before['total_billed'] + approved}.")

    searched = []
    for field, budget in budgets:
        if budget <= 0:
            print(f"ENRICH PHASE: search ({field}) — skipped, 0 approved")
            continue
        print(f"ENRICH PHASE: search ({field}) — hard stop at {budget}")
        searched.append(field)
        try:
            E.run_live([field], None, budget,
                       verify_images=verify_images, verify_depth=verify_depth)
        except SystemExit as exc:
            # run_live() exits on a missing serpapi package or key. Every result
            # is written to the cache the moment it comes back, so whatever was
            # already bought is safe on disk — carry on to the apply phase and
            # land it rather than stranding paid-for results in the cache.
            print(f"  ERROR: live {field} search stopped early (exit {exc.code}). "
                  f"Anything already bought is cached and will still be applied.")

    after = SL.summary()
    print(f"\nENRICH PHASE: reconcile — {after['total_billed'] - before['total_billed']} "
          f"call(s) billed this run, {after['total_billed']} total to date. "
          f"Compare against the SerpAPI dashboard; the ledger should now match it "
          f"or lead it by at most one.")
    print(SL.format_summary())

    # Apply only the fields that were actually searched. A run approved for
    # images should not silently rewrite designation/specs columns nobody
    # asked about — that is what --apply-only is for.
    stats, failures = _apply_and_rebuild(searched)
    return _finish("run", stats, failures, approved=approved,
                   image_budget=budgets[0][1], designation_budget=budgets[1][1],
                   specs_budget=budgets[2][1])


def main():
    ap = argparse.ArgumentParser(
        description="Dashboard driver for the shared enrichment layer.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scan", action="store_true",
                      help="Read-only count of what needs enriching. No network, no writes.")
    mode.add_argument("--apply-only", action="store_true",
                      help="Apply already-paid cache hits and rebuild. No network.")
    mode.add_argument("--run", action="store_true",
                      help="LIVE. Spends searches, then applies and rebuilds.")
    mode.add_argument("--ledger", action="store_true",
                      help="Print the SerpAPI spend ledger and exit. Reconciles "
                           "against the SerpAPI dashboard. No network, no writes.")
    ap.add_argument("--image-budget", type=int, default=0,
                    help="Hard cap on image searches (--run only).")
    ap.add_argument("--designation-budget", type=int, default=0,
                    help="Hard cap on designation searches (--run only).")
    ap.add_argument("--specs-budget", type=int, default=0,
                    help="Hard cap on specs searches — load index, speed rating, "
                         "load range, section width, aspect ratio, rim diameter "
                         "(--run only). One search can fill several of these at "
                         "once for the same row.")
    ap.add_argument("--no-verify-images", dest="verify_images", action="store_false",
                    help="Skip the pixel check on image results. Verification costs no "
                         "SerpAPI credit, so the only reason to skip it is speed.")
    ap.add_argument("--verify-depth", type=int, default=6,
                    help="How far down the ranked list to look for an image that "
                         "passes the pixel check (default 6).")
    ap.add_argument("--check-images", action="store_true", default=None,
                    help="Measure the images the suppliers actually gave us and count "
                         "the unusable ones (130px thumbnails, placeholder graphics, "
                         "dead links) as gaps. This is now the DEFAULT for --scan; the "
                         "flag is kept so existing commands and docs keep working. "
                         "Downloads images over plain HTTP — no SerpAPI credit — and "
                         "caches every verdict by URL, so only URLs new since the last "
                         "scan are ever fetched.")
    ap.add_argument("--no-check-images", dest="check_images", action="store_false",
                    help="Do NOT measure anything; score images only against verdicts "
                         "already cached. Faster, and wrong after a scrape — a URL "
                         "nobody has measured is not counted as a gap, so this "
                         "under-reports exactly the images a fresh scrape just changed.")
    ap.add_argument("--recheck-images", action="store_true",
                    help="Re-measure URLs already in the cache (use when a CDN has "
                         "replaced the image behind a URL that did not change).")
    args = ap.parse_args()

    if args.ledger:
        print(SL.format_summary())
        return 0
    if args.scan:
        scan(check_images=(True if args.check_images is None else args.check_images)
                          or args.recheck_images,
             refresh_images=args.recheck_images)
        return 0
    if args.apply_only:
        return apply_only()
    return run(args.image_budget, args.designation_budget, args.specs_budget,
               args.verify_images, args.verify_depth)


if __name__ == "__main__":
    sys.exit(main())
