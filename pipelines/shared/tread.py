#!/usr/bin/env python3
"""Per-image memory of whether the TREAD is facing the camera.

WHY THIS IS A SEPARATE LAYER
----------------------------
The client's rule is not about pixels: "the tire tread must be visible rather
than the side of the tire — like a donut figure, we don't want that shot."
That is a judgement about camera pose, and the existing pixel heuristic in
image_view.py cannot make it. Measured on 117 supplier photos sampled from the
2026-08-26 catalogue and labelled by eye:

    image_view.analyze_image(...)['face_on'], the incumbent detector
      caught  0 of 5  true donut shots (bare tire, tread not presented)
      fired on 6 of 11 wheel-face-dominant shots
      ALSO fired on plainly good three-quarter shots whose wheel happened to
      be black on a black backdrop — core_bg_frac 0.68-0.74 on tread-forward
      photos of a Pirelli Scorpion and a Michelin Pilot Sport

The failure is structural, not a bad threshold. `core_bg_frac` asks "is the
middle of the object BACKGROUND-COLOURED", which answers a different question:
a black alloy wheel on a black backdrop scores as a hole, and a donut whose
centre shows the inner sidewall does not. Candidate replacements were measured
on the same 117 images — enclosed-hole fraction (with a flatness guard so a
spoked wheel is not mistaken for a backdrop), silhouette aspect, solidity, and
inner-vs-outer edge energy. None separated the classes: GOOD and WHEELFACE
overlap completely on every one of them.

So this module does NOT invent another heuristic. It stores a LABEL, whatever
produced it, keyed by image URL, and makes that label permanent. Labelling is
the expensive step; doing it twice for the same photo is the waste worth
engineering away — the same principle serp_cache.json already applies to
searches.

WHERE LABELS COME FROM
----------------------
Any of these, recorded with its provenance so they are never confused:

  human     a person reviewed a contact sheet (`--sheets`, then `--import`)
  model     a vision model classified it (`--import`, source=model)
  heuristic image_view's face_on, imported as a weak prior — never trusted
            as a rejection on its own

A URL is labelled ONCE. 6,452 North Country rows share 520 distinct photos, so
labelling is priced per PICTURE, not per row — and a photo already labelled in
a previous scrape is never looked at again.

NOTHING HERE SPENDS SERPAPI CREDIT. Sheet building is plain HTTP against the
same CDNs the quality audit already fetches.
"""
import argparse
import collections
import csv
import io
import json
import os
import threading
from datetime import datetime, timezone

SHARED_DIR  = os.path.dirname(os.path.abspath(__file__))
TREAD_CACHE = os.path.join(SHARED_DIR, "Data", "tread_cache.json")

# The three labels. `unknown` is stored explicitly (rather than left absent) so
# "a reviewer looked and could not tell" is distinguishable from "never looked".
VISIBLE, NOT_VISIBLE, UNKNOWN = "visible", "not_visible", "unknown"
LABELS = (VISIBLE, NOT_VISIBLE, UNKNOWN)

# Provenance ranking: a human label is never overwritten by a machine one.
SOURCE_RANK = {"human": 3, "model": 2, "heuristic": 1}

_LOCK = threading.Lock()
_memo = {"mtime": None, "data": None}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path=TREAD_CACHE):
    """Labels from disk, memoized on mtime (the file is read once per scan)."""
    if not os.path.exists(path):
        return {}
    mtime = os.path.getmtime(path)
    if _memo["mtime"] != mtime or _memo["data"] is None:
        with open(path, encoding="utf-8") as f:
            _memo["data"] = json.load(f)
        _memo["mtime"] = mtime
    return _memo["data"]


def save(cache, path=TREAD_CACHE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    _memo["mtime"] = os.path.getmtime(path)
    _memo["data"] = cache


def record(url, label, source, note="", cache=None, path=TREAD_CACHE):
    """Write one label. A weaker source never overwrites a stronger one, so
    re-running a model over a set a person already reviewed is a no-op rather
    than a regression. Returns True when the label was actually stored."""
    if label not in LABELS:
        raise ValueError(f"label must be one of {LABELS}, got {label!r}")
    url = str(url or "").strip()
    if not url:
        return False
    own = cache is None
    cache = load(path) if own else cache
    with _LOCK:
        prev = cache.get(url)
        if prev and SOURCE_RANK.get(source, 0) < SOURCE_RANK.get(prev.get("source"), 0):
            return False
        cache[url] = {"tread": label, "source": source, "ts": _now(),
                      **({"note": note} if note else {})}
    if own:
        save(cache, path)
    return True


def verdict(url, cache=None):
    """'visible' / 'not_visible' / 'unknown' / 'unlabelled' for one URL.

    'unlabelled' is deliberately distinct from 'unknown': an unreviewed photo
    is not evidence of a bad photo, and counting it as a gap would inflate the
    spend estimate by the entire catalogue on the first run.
    """
    cache = load() if cache is None else cache
    entry = cache.get(str(url or "").strip())
    if not entry:
        return "unlabelled"
    return entry.get("tread", UNKNOWN)


def is_rejected(url, cache=None):
    """True only when a labeller positively said the tread is not shown."""
    return verdict(url, cache) == NOT_VISIBLE


def coverage(urls, cache=None):
    """(labelled, unlabelled, Counter of labels) over the URLs given."""
    cache = load() if cache is None else cache
    counts = collections.Counter(verdict(u, cache) for u in dict.fromkeys(urls))
    unlabelled = counts.pop("unlabelled", 0)
    return sum(counts.values()), unlabelled, counts


# ---------------------------------------------------------------------------
# Review sheets — contact sheets a person labels in bulk.
# ---------------------------------------------------------------------------

def build_sheets(urls, out_dir, per_sheet=40, cols=8, cell=170, only_unlabelled=True):
    """Write numbered contact sheets plus an index CSV to fill in.

    The CSV is the thing that comes back: one row per image, `label` blank,
    ready for a reviewer to type visible/not_visible. `--import` reads it.
    Returns (sheet paths, index csv path, number of images).
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:                                  # pragma: no cover
        raise RuntimeError(f"review sheets need Pillow ({exc})") from exc
    from image_view import fetch_image
    import concurrent.futures

    cache = load()
    todo = [u for u in dict.fromkeys(str(x or "").strip() for x in urls) if u]
    if only_unlabelled:
        todo = [u for u in todo if verdict(u, cache) == "unlabelled"]
    if not todo:
        return [], None, 0

    os.makedirs(out_dir, exist_ok=True)

    def grab(u):
        try:
            return u, fetch_image(u)
        except Exception:                                       # noqa: BLE001
            return u, None

    fetched = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        for u, data in ex.map(grab, todo):
            if data:
                fetched[u] = data

    ordered = [u for u in todo if u in fetched]
    sheets = []
    for start in range(0, len(ordered), per_sheet):
        chunk = ordered[start:start + per_sheet]
        rows = (len(chunk) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cell, rows * (cell + 18)), "white")
        draw = ImageDraw.Draw(sheet)
        for i, url in enumerate(chunk):
            try:
                im = Image.open(io.BytesIO(fetched[url])).convert("RGB")
            except Exception:                                   # noqa: BLE001
                continue
            im.thumbnail((cell - 8, cell - 8))
            x, y = (i % cols) * cell, (i // cols) * (cell + 18)
            sheet.paste(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
            draw.text((x + 4, y + cell + 3), f"#{start + i}", fill="black")
        path = os.path.join(out_dir, f"tread_sheet_{start // per_sheet:03d}.png")
        sheet.save(path)
        sheets.append(path)

    index = os.path.join(out_dir, "tread_labels.csv")
    with open(index, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["idx", "label", "url"])
        for i, url in enumerate(ordered):
            w.writerow([i, "", url])
    return sheets, index, len(ordered)


def import_labels(csv_path, source="human", path=TREAD_CACHE):
    """Read back a filled-in index CSV. Blank labels are skipped, so a
    partially reviewed sheet can be imported and finished later.

    Accepts the shorthand a reviewer actually types: v/y/ok/good -> visible,
    n/x/bad/donut/faceon -> not_visible, ?/u -> unknown.
    """
    alias = {"v": VISIBLE, "y": VISIBLE, "ok": VISIBLE, "good": VISIBLE,
             "visible": VISIBLE, "tread": VISIBLE,
             "n": NOT_VISIBLE, "x": NOT_VISIBLE, "bad": NOT_VISIBLE,
             "donut": NOT_VISIBLE, "faceon": NOT_VISIBLE, "face_on": NOT_VISIBLE,
             "sidewall": NOT_VISIBLE, "not_visible": NOT_VISIBLE,
             "?": UNKNOWN, "u": UNKNOWN, "unknown": UNKNOWN}
    cache = load(path)
    stored = skipped = unrecognized = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            url = (row.get("url") or "").strip()
            raw = (row.get("label") or "").strip().lower()
            if not url or not raw:
                skipped += 1
                continue
            label = alias.get(raw)
            if label is None:
                unrecognized += 1
                continue
            if record(url, label, source, cache=cache, path=path):
                stored += 1
    save(cache, path)
    return stored, skipped, unrecognized


def import_heuristic_prior(quality_urls, path=TREAD_CACHE):
    """Seed the cache from image_view's face_on flag as a WEAK prior.

    Recorded with source='heuristic' so it ranks below any human or model
    label and is never used as a rejection on its own — its measured miss rate
    (0 of 5 true donuts) is exactly why. Its value is ordering: reviewing the
    photos it flags first surfaces bad shots sooner than random order does.
    """
    from image_view import fetch_image, analyze_image
    import concurrent.futures

    cache = load(path)
    todo = [u for u in dict.fromkeys(quality_urls) if verdict(u, cache) == "unlabelled"]

    def work(u):
        try:
            return u, analyze_image(fetch_image(u), "tires").get("face_on")
        except Exception:                                       # noqa: BLE001
            return u, None

    n = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for u, face_on in ex.map(work, todo):
            if face_on is None:
                continue
            if record(u, NOT_VISIBLE if face_on else VISIBLE, "heuristic",
                      note="image_view.face_on", cache=cache, path=path):
                n += 1
    save(cache, path)
    return n


def format_summary(path=TREAD_CACHE):
    cache = load(path)
    by_label = collections.Counter(e.get("tread") for e in cache.values())
    by_source = collections.Counter(e.get("source") for e in cache.values())
    lines = [f"Tread label memory — {path}", "",
             f"  images labelled                     {len(cache):>6}"]
    for label in LABELS:
        lines.append(f"    {label:<32}{by_label.get(label, 0):>6}")
    lines.append("")
    lines.append("  by provenance:")
    for src in sorted(by_source, key=lambda s: -SOURCE_RANK.get(s, 0)):
        lines.append(f"    {str(src):<32}{by_source[src]:>6}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sheets", metavar="DIR",
                    help="Build review contact sheets for every unlabelled tire "
                         "image in the catalogue, plus a CSV to fill in.")
    ap.add_argument("--all", action="store_true",
                    help="With --sheets: include images that are already labelled.")
    ap.add_argument("--import", dest="import_csv", metavar="CSV",
                    help="Import a filled-in label CSV.")
    ap.add_argument("--source", default="human", choices=sorted(SOURCE_RANK),
                    help="Provenance for --import (default human).")
    ap.add_argument("--seed-heuristic", action="store_true",
                    help="Seed weak priors from image_view.face_on (free, HTTP only).")
    args = ap.parse_args()

    if args.import_csv:
        stored, skipped, bad = import_labels(args.import_csv, args.source)
        print(f"Imported {stored} label(s); {skipped} row(s) blank, {bad} unrecognized.")

    if args.sheets or args.seed_heuristic:
        import enrichment as E
        qcache = E.load_quality_cache()
        urls = []
        for src in E.SOURCES:
            if src["field"] != "image" or src["type"] != "tires" or not src.get("value_col"):
                continue
            f = E.latest(src["glob"])
            if not f:
                continue
            import pandas as pd
            df = pd.read_csv(f, dtype=str, low_memory=False, encoding="utf-8-sig").fillna("")
            if src["value_col"] not in df.columns:
                continue
            urls += [u for u in df[src["value_col"]].unique()
                     if str(u).strip() and E.image_verdict(str(u).strip(), qcache) == "ok"]
        urls = list(dict.fromkeys(urls))
        print(f"{len(urls)} distinct tire photo(s) currently pass the resolution gate.")

        if args.seed_heuristic:
            n = import_heuristic_prior(urls)
            print(f"Seeded {n} weak heuristic prior(s).")
        if args.sheets:
            sheets, index, n = build_sheets(urls, args.sheets,
                                            only_unlabelled=not args.all)
            if not n:
                print("Nothing to review — every image already carries a label.")
            else:
                print(f"Wrote {len(sheets)} sheet(s) covering {n} image(s) in {args.sheets}")
                print(f"Fill in the `label` column of {index}, then:")
                print(f"  python tread.py --import {index}")

    print()
    print(format_summary())


if __name__ == "__main__":
    main()
