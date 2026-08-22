#!/usr/bin/env python3
"""Pick the right image out of a Costar `productImageList`.

ONE shared implementation for all three Costar-platform scraper pairs:
    northcountry/scraper_tires.py,   northcountry/scraper_wheels.py
    formula_distributors/scraper.py, formula_distributors/scraper_wheels.py
    emergetires/scraper_tires.py,    emergetires/scraper_wheels.py

THE BUG THIS FIXES
------------------
Costar returns a LIST of images per product, not one. Measured against a live
North Country session over 300 products spread across the catalogue:

    images per product : {0: 43, 1: 180, 2: 27, 3: 46, 4: 3, 8: 1}
                         -> 26% of products carry more than one image
    imageType values   : LARGE 328, SMALL 64

and the two types are nothing alike. Over 52 multi-image products:

    imageType=LARGE : n=99  min-side 51..2400, median 351
    imageType=SMALL : n=50  min-side 54..400,  median 54     <- 54px

formula_distributors/scraper.py picked with no preference at all:

    for entry in data.get("productImageList", []):
        ...
        images[guid] = url          # last entry wins

so whichever image the API happened to list last became the storefront photo.
Measured against the same 52 products, that rule picks a SMALLER image than
the best available LARGE on 33 of them (63%) and a bigger one on none — i.e.
it was routinely shipping a 54px thumbnail while a 2400px photo sat in the
same response.

northcountry/emergetires guarded with `or imageType == "LARGE"`, which does
keep SMALL from overwriting a LARGE, but among several LARGEs it still takes
whichever came last, and LARGE spans 51..2400px — so that choice was
arbitrary too.

WHY imageType IS NOT ENOUGH
---------------------------
Ranking by imageType removes the systematic error above, but the label itself
is not reliable. Costar's TireGuide-sourced rows in particular mislabel
routinely — this is one real product (Cooper Endeavor Plus 245/60R18,
productGuid cfd2fd46-386f-ec11-8146-00155d060500), every entry measured:

    imageType=LARGE  680e4435-....jpg  ->  200x290    <- what ranking picks
    imageType=SMALL  670e4435-....jpg  ->  400x580    <- twice the size
    imageType=SMALL  690e4435-....jpg  ->   54x54

The label is inverted for that product: the biggest image is announced as
SMALL. Measured over 200 currently-low-res North Country tires, one product
per distinct image:

    imageType=LARGE : n=260  min 51   median 385  max 600
    imageType=SMALL : n= 47  min 54   median  54  max 400

    label-based pick == biggest available : 169  (84%)
    label-based pick <  biggest available :  31  (16%)

So one product in six ships a smaller photo than the supplier actually has,
for free, in the same API response.

WHAT THIS DOES
--------------
Two selection modes, and the caller chooses by whether it passes a cache:

  * No `quality_cache` — rank by label (LARGE > unlabelled > SMALL), ties
    broken by first occurrence so the result is stable across runs rather
    than dependent on API list order. Fetches nothing.

  * With a `quality_cache` (enrichment.load_quality_cache()) — rank by the
    MEASURED short side, because that is the thing actually being chosen and
    the label demonstrably is not it. Known-dead and placeholder images are
    dropped from contention. Ties fall back to label rank, then first
    occurrence.

Measured mode only engages when every candidate for that product has a
measurement; a partial picture is worse than a consistent one, so a single
unmeasured candidate falls the whole product back to label ranking. Callers
warm the cache first with `enrichment.measure_image_urls(candidate_urls(...))`,
which dedupes and persists, so the fetches happen once per image ever rather
than once per scrape.
"""

# Higher wins. An unlabelled type sits between the two: it is not the
# explicit thumbnail SMALL announces itself to be, but it carries none of
# LARGE's promise either.
IMAGE_TYPE_RANK = {"LARGE": 2, "": 1, "SMALL": 0}
_UNKNOWN_RANK = 1


def _rank(image_type):
    return IMAGE_TYPE_RANK.get(str(image_type or "").strip().upper(), _UNKNOWN_RANK)


# Verdicts that take an image out of contention when something else is
# available: 'unusable' is a dead or undecodable link, 'placeholder' is a
# blank/near-uniform graphic. Neither is worth choosing over a real photo,
# however flattering its imageType.
_REJECT_VERDICTS = ("unusable", "placeholder")


def entry_url(entry):
    """The URL on one productImageList entry, or ''."""
    return (entry.get("url") or entry.get("imageUrl") or "").strip()


def candidate_urls(image_list):
    """Every distinct image URL in a productImageList, in first-seen order.

    This is what a caller feeds to enrichment.measure_image_urls() to warm the
    quality cache before picking — every candidate, not just the one label
    ranking would have chosen.
    """
    return list(dict.fromkeys(
        u for e in (image_list or []) if (u := entry_url(e))))


def _measured_side(url, quality_cache):
    """Measured short side for `url`, or None if it was never measured or the
    measurement failed. A rejected verdict returns None too — a dead link has
    no size worth ranking."""
    entry = (quality_cache or {}).get(url)
    if not entry or entry.get("verdict") in _REJECT_VERDICTS:
        return None
    w, h = entry.get("w"), entry.get("h")
    if not w or not h:
        return None
    return min(w, h)


def pick_best_image(entries, quality_cache=None):
    """Best image URL from one product's productImageList entries, or ''.

    `entries` is any iterable of dicts shaped like Costar's productImageList
    rows (keys used: 'url' or 'imageUrl', and 'imageType'). Entries without a
    usable URL are ignored.

    With `quality_cache`, ranks by measured pixels instead of by imageType —
    see the module docstring for why the label cannot be trusted on its own.
    """
    candidates = [(i, u, e) for i, e in enumerate(entries or [])
                  if (u := entry_url(e))]
    if not candidates:
        return ""

    if quality_cache:
        sides = {u: _measured_side(u, quality_cache) for _, u, _ in candidates}
        # Every candidate measured (and none rejected) => rank on real pixels.
        # One unmeasured candidate and we cannot tell whether it is the best
        # one, so fall back rather than choose on half the evidence.
        if all(s is not None for s in sides.values()):
            return max(candidates,
                       key=lambda c: (sides[c[1]],
                                      _rank(c[2].get("imageType")),
                                      -c[0]))[1]
        # Partial knowledge: still drop the candidates we KNOW are dead or
        # blank, then rank the rest by label. Dropping a known-bad image is
        # safe on any amount of evidence; ranking on it is not.
        known_bad = {u for _, u, _ in candidates
                     if ((quality_cache.get(u) or {}).get("verdict")
                         in _REJECT_VERDICTS)}
        if known_bad and len(known_bad) < len(candidates):
            candidates = [c for c in candidates if c[1] not in known_bad]

    # -i so that, at equal rank, the earliest entry wins.
    return max(candidates, key=lambda c: (_rank(c[2].get("imageType")), -c[0]))[1]


def build_image_map(image_list, quality_cache=None):
    """Group a whole `productImageList` by productGuid and pick the best image
    for each. Returns {guid: url}, omitting products with no usable image.
    """
    grouped = {}
    for e in image_list or []:
        guid = e.get("productGuid", "")
        if guid:
            grouped.setdefault(guid, []).append(e)
    out = {}
    for guid, entries in grouped.items():
        url = pick_best_image(entries, quality_cache)
        if url:
            out[guid] = url
    return out


def measure_and_build_image_map(image_list, verbose=False, workers=8):
    """build_image_map() with the quality cache warmed first.

    Convenience for scrapers: measures every candidate image once (cached
    across runs in enrichment's image_quality_cache.json), then picks by real
    pixels. Falls back to label ranking if measuring is unavailable for any
    reason — a scrape must never fail because Pillow is missing or a CDN is
    slow, it just gets the older, weaker choice.
    """
    urls = candidate_urls(image_list)
    cache = None
    if urls:
        try:
            import enrichment
            cache = enrichment.measure_image_urls(
                urls, workers=workers, verbose=verbose, progress=0)
        except Exception as exc:                                    # noqa: BLE001
            print(f"  [images] WARN could not measure ({exc}); "
                  f"falling back to imageType ranking")
            cache = None
    return build_image_map(image_list, cache)
