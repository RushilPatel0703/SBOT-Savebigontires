#!/usr/bin/env python3
"""Pixel-level verification of candidate product images.

WHY THIS EXISTS
---------------
`enrichment.filter_image_candidates()` ranks candidates using only the metadata
SerpAPI returns: dimensions, title, source. It cannot see the picture. That gap
produced a concrete miss in the client's 5-search test: the Antares INGENS EV
winner came from SimpleTire (a top-tier source in the ranking table) and was a
flat face-on shot of the WHEEL FACE with no tread visible at all - exactly the
framing the client rejected.

Source reputation does not predict framing. This module opens the image.

WHAT IS EXACT vs WHAT IS A HEURISTIC
------------------------------------
Be clear about which is which, because the difference matters when a check
rejects something:

  EXACT (hard gates - a failure is a fact, not a guess)
    * the URL actually fetches and decodes as an image
    * true pixel dimensions, measured rather than taken from SerpAPI metadata
    * the image is not a blank/placeholder (near-uniform frame)

  HEURISTIC (scored, and off by default as a hard reject)
    * `face_on` - whether the shot is a flat face-on view rather than the
      angled tread view the client wants

The face-on heuristic rests on one geometric fact that does hold up: a tire
photographed face-on is an ANNULUS. Its silhouette encloses a hub/bore region
that is not rubber - either background showing through (bare tire) or a wheel
centre. Photographed at an angle with the tread toward camera, the middle of
the silhouette is solid tread rubber. So "is the core of the object solid?"
is a real signal, not a proxy.

Its limits are equally real, and it is calibrated on 10 labelled tire images
from the one approved test batch. On that set `core_bg_frac` separates cleanly
(good: 0.001-0.240, face-on: 0.388 and 0.639) but the margin between the worst
good and the best bad is thin, and n=10 is a small sample. It is therefore a
RANKING DEMOTION by default. Pass strict=True to make it a hard reject once a
larger labelled set justifies that.

THE ACTUAL PAYOFF
-----------------
One paid SerpAPI search returns ~50 candidates. Verification costs no extra
search: when the top candidate fails, fall through to the next. Budget is spent
per PRODUCT, not per attempt.

No SerpAPI calls are made anywhere in this module. Image fetches are plain HTTP
GETs against public CDNs.
"""

import io
import json
import os
import sys
import urllib.request

try:
    import numpy as np
    from PIL import Image
except ImportError:                                    # pragma: no cover
    np = None
    Image = None

MIN_SIDE_PX = 800          # matches enrichment.MIN_SIDE_PX
MAX_FETCH_BYTES = 12 << 20  # 12 MB - a product photo above this is wrong
FETCH_TIMEOUT = 20
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SaveBigOnTires-image-check/1.0"

# Calibrated on the 10 labelled tire images from the approved test batch.
# Good shots sat at 0.001-0.240; the two face-on shots at 0.388 and 0.639.
FACE_ON_CORE_BG = 0.30
# Fraction of the object's bbox short side used as the "core" disc radius.
CORE_RADIUS_FRAC = 0.30
# Distance in RGB space beyond which a pixel is foreground, not background.
BG_DISTANCE = 0.16


class ImageCheckError(Exception):
    """Raised when a candidate cannot be fetched or decoded."""


def fetch_image(url, timeout=FETCH_TIMEOUT, max_bytes=MAX_FETCH_BYTES):
    """GET `url` and return raw bytes. Raises ImageCheckError on any failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
    except Exception as exc:                            # noqa: BLE001 - any network failure is the same outcome
        raise ImageCheckError(f"fetch failed: {type(exc).__name__}: {exc}") from exc
    if not data:
        raise ImageCheckError("fetch returned 0 bytes")
    if len(data) > max_bytes:
        raise ImageCheckError(f"image exceeds {max_bytes} bytes")
    return data


def _segment(arr):
    """Split an HxWx3 float array into (foreground mask, background sd).

    Background is estimated from a border ring rather than the four corners.
    The corner-only estimate breaks on dark studio backdrops, which is how the
    face-on Antares shot slipped through the first calibration pass: its black
    background made every corner read as 'object'.
    """
    h, w, _ = arr.shape
    b = max(2, min(h, w) // 25)
    ring = np.concatenate([
        arr[:b].reshape(-1, 3), arr[-b:].reshape(-1, 3),
        arr[:, :b].reshape(-1, 3), arr[:, -b:].reshape(-1, 3),
    ])
    bg = np.median(ring, axis=0)
    bg_sd = float(ring.std(axis=0).mean())
    dist = np.linalg.norm(arr - bg, axis=2)
    return dist > BG_DISTANCE, bg_sd


def analyze_image(data, product_type="tires", max_side=256):
    """Measure one image. Returns a dict of findings; never raises on content.

    Keys: ok, reasons (list of hard-gate failures), width, height, face_on,
    core_bg_frac, fill, bg_sd, uniform.
    """
    if Image is None or np is None:
        raise ImageCheckError("Pillow and numpy are required for image analysis")
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        im = im.convert("RGB")
    except Exception as exc:                            # noqa: BLE001
        raise ImageCheckError(f"decode failed: {type(exc).__name__}: {exc}") from exc

    out = {
        "width": im.width, "height": im.height,
        "ok": True, "reasons": [], "face_on": False,
        "core_bg_frac": None, "fill": None, "bg_sd": None, "uniform": False,
    }

    # --- EXACT GATE: true dimensions -------------------------------------
    if min(im.width, im.height) < MIN_SIDE_PX:
        out["ok"] = False
        out["reasons"].append(f"too small: {im.width}x{im.height} < {MIN_SIDE_PX}px")

    small = im.copy()
    small.thumbnail((max_side, max_side))
    arr = np.asarray(small).astype(np.float32) / 255.0

    # --- EXACT GATE: blank / placeholder ---------------------------------
    if float(arr.std()) < 0.02:
        out["ok"] = False
        out["uniform"] = True
        out["reasons"].append("blank or placeholder image (near-uniform)")
        return out

    mask, bg_sd = _segment(arr)
    out["bg_sd"] = round(bg_sd, 4)
    out["fill"] = round(float(mask.mean()), 4)
    if mask.sum() < 200:
        out["ok"] = False
        out["reasons"].append("no product region found against background")
        return out

    # --- HEURISTIC: face-on (annulus) detection --------------------------
    h, w = mask.shape
    ys, xs = np.nonzero(mask)
    cy, cx = ys.mean(), xs.mean()
    bh = ys.max() - ys.min() + 1
    bw = xs.max() - xs.min() + 1
    radius = CORE_RADIUS_FRAC * min(bh, bw)
    yy, xx = np.mgrid[0:h, 0:w]
    core = ((yy - cy) ** 2 + (xx - cx) ** 2) < radius ** 2
    core_n = int(core.sum())
    core_bg = round(float((core & ~mask).sum() / core_n), 4) if core_n else 0.0
    out["core_bg_frac"] = core_bg

    # Only tires are judged on this. A wheel photographed face-on is CORRECT -
    # the spoke design and finish are the product - and a wheel's hub bore
    # legitimately reads as a hole. Applying the tire rule to wheels would
    # reject exactly the right pictures.
    if str(product_type).lower() not in ("wheels", "rims"):
        out["face_on"] = core_bg > FACE_ON_CORE_BG

    return out


def score_adjustment(finding):
    """Ranking penalty derived from the heuristic checks (never a rejection)."""
    return -5 if finding.get("face_on") else 0


def pick_verified_image(ranked, product_type="tires", max_fetch=6,
                        strict=False, fetcher=fetch_image, verbose=False):
    """Walk `ranked` best-first, fetch and check each, return the best survivor.

    `ranked` is filter_image_candidates() output. Returns (winner, log) where
    winner is the chosen candidate dict (or None) and log is a per-attempt
    record suitable for the approval report.

    Costs zero SerpAPI credits: every candidate here came from one search that
    was already paid for. `max_fetch` bounds wall-clock, not budget.

    With strict=False (default) a face-on shot is demoted but still usable as a
    last resort - better a face-on photo than an empty listing. With strict=True
    it is dropped outright.
    """
    log = []
    passed, demoted = [], []

    for cand in (ranked or [])[:max_fetch]:
        url = cand.get("original") or cand.get("link") or ""
        entry = {"url": url, "domain": cand.get("source", ""), "status": None}
        if not url:
            entry["status"] = "no url"
            log.append(entry)
            continue
        try:
            data = fetcher(url)
            finding = analyze_image(data, product_type)
        except ImageCheckError as exc:
            entry["status"] = f"unusable: {exc}"
            log.append(entry)
            if verbose:
                print(f"    reject {url[:70]}: {exc}")
            continue

        entry.update({
            "width": finding["width"], "height": finding["height"],
            "core_bg_frac": finding["core_bg_frac"], "face_on": finding["face_on"],
        })
        if not finding["ok"]:
            entry["status"] = "; ".join(finding["reasons"])
            log.append(entry)
            if verbose:
                print(f"    reject {url[:70]}: {entry['status']}")
            continue
        if finding["face_on"]:
            entry["status"] = "demoted: face-on, tread not visible"
            demoted.append(cand)
            log.append(entry)
            if verbose:
                print(f"    demote {url[:70]}: face-on (core_bg={finding['core_bg_frac']})")
            continue

        entry["status"] = "accepted"
        log.append(entry)
        passed.append(cand)
        if verbose:
            print(f"    accept {url[:70]}")
        break                                   # best-first: first pass wins

    if passed:
        return passed[0], log
    if demoted and not strict:
        return demoted[0], log
    return None, log


# ---------------------------------------------------------------------------
# Self-test - runs against a local folder of images, no network, no API spend.
# ---------------------------------------------------------------------------

def _selftest(folder):
    """Analyze every image in `folder`; print a table. Expects index.json with
    at least {file, type} entries, else infers type from the filename."""
    index_path = os.path.join(folder, "index.json")
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as fh:
            rows = json.load(fh)
    else:
        rows = [{"file": f, "type": "tires"} for f in sorted(os.listdir(folder))
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]

    print(f"{'file':30s} {'type':6s} {'dims':>11s} {'core_bg':>8s} {'face_on':>8s}  status")
    print("-" * 88)
    for r in rows:
        path = os.path.join(folder, r["file"])
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as fh:
                finding = analyze_image(fh.read(), r.get("type", "tires"))
        except ImageCheckError as exc:
            print(f"{r['file']:30s} {'-':6s} {'-':>11s} {'-':>8s} {'-':>8s}  UNUSABLE: {exc}")
            continue
        dims = f"{finding['width']}x{finding['height']}"
        cb = finding["core_bg_frac"]
        status = "ok" if finding["ok"] and not finding["face_on"] else (
            "FACE-ON" if finding["face_on"] else "; ".join(finding["reasons"]))
        print(f"{r['file']:30s} {r.get('type','tires')[:6]:6s} {dims:>11s} "
              f"{cb if cb is not None else '-':>8} {str(finding['face_on']):>8s}  {status}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        _selftest(sys.argv[2])
    else:
        print(__doc__)
        print("Usage: python image_view.py --selftest <folder-of-images>")
