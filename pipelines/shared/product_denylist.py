#!/usr/bin/env python3
"""Individual products the client has decided not to sell, whatever the feeds say.

WHY THIS EXISTS SEPARATELY FROM THE SIZE FILTER
-----------------------------------------------
size_parser.is_consumer_tire_size() already drops whole *classes* of product —
ATV, UTV, golf-cart and heavy-commercial sizes — by reading the size. That rule
is the right shape for the thing it catches: a 275/70R22.5 is a semi-truck tire
no matter who made it, so no list needs maintaining.

It cannot express the other kind of "we don't sell this". From the September
2026 storefront image review, the client marked 59 products for removal. 52 of
them were commercial/trailer fitments the size filter already blocks. The
remaining 7 are ordinary passenger sizes:

    EMRG-80614504  DOUBLE UNICONTROL EV   235/40R19
    EMRG-80614498  WARRIOR Unicontrol EV  245/50ZR18
    EMRG-80447047  WARRIOR AT-PLUS        265/70R16
    EMRG-80416264  DOUBLE DC-32           205/55R17
    EMRG-80601022  DOUBLE R700 EV         215/55R18
    EMRG-80521947  DOUBLE TOUR D06        205/65R15
    4268500        Pirelli P-ZERO SPORT   315/30R22

There is no size, brand or tag these share with each other and not with
products the store does sell — the same suppliers' DOUBLE and WARRIOR lines are
stocked, and Pirelli is a core brand. The only thing that identifies them is
that the client looked at each one and said no. So the rule is a list, and the
list is data rather than code.

BOTH ROADS, ONE LIST
--------------------
The same reasoning as filter_consumer_tires() in upload_bundle.py: there are
two roads to the storefront — production/steps/ingest.py for the weekly sync,
and the hand-uploaded dashboard bundle — and a product excluded on one road but
not the other means the store's contents depend on which button was pressed.
Both call this module.

Wheels are on the list too even though no wheel is currently live, because the
size filter covers tires only: nothing else would stop a denied wheel being
re-created by the next scrape.

KEYING
------
On the SKU as Shopify holds it, which is post-prefix — Emerge's EMRG- is
applied by production/config.SUPPLIERS at ingest and by augment_tires.py on the
bundle road, so both roads present the same string here. Comparison runs
through the canonical normalize_sku so a CSV float artifact ("4268500.0")
cannot silently miss.
"""

import importlib.util
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))

# sku_normalize is loaded by file path, not by name: this module is itself
# loaded by file path from production/steps/ingest.py, where pipelines/shared
# is not on sys.path, so a bare import would work on the dashboard road and
# fail on the production one — precisely the divergence this module exists to
# prevent.
_spec = importlib.util.spec_from_file_location(
    "_denylist_sku_normalize", os.path.join(_HERE, "sku_normalize.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
normalize_sku = _mod.normalize_sku

DENYLIST_PATH = os.path.join(_HERE, "Data", "product_denylist.json")


def load_denylist(path=None):
    """{normalized SKU: reason}. No file at all means an empty denylist.

    A file that exists but will not parse raises, matching markup_overrides.json
    and for the same reason: continuing past it would quietly list the exact
    products the client removed, and a silent re-listing is the failure this
    module is here to prevent.
    """
    path = path or DENYLIST_PATH
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    out = {}
    for entry in doc.get("skus", []):
        sku = normalize_sku(entry.get("sku", ""))
        if sku:
            out[sku] = entry.get("reason", "")
    return out


def denied_flags(skus, denylist=None):
    """Apply the denylist across one feed.

    Returns (keep_flags, hits) where hits is {sku: reason} for what actually
    matched — callers log that, so a denial is visible in the run rather than
    just a smaller row count.

    There is deliberately no runaway guard of the kind consumer_size_filter has.
    That one protects against reading the wrong column, which can drop a whole
    feed; this can only ever drop SKUs someone typed into the file by hand.
    """
    dl = load_denylist() if denylist is None else denylist
    skus = list(skus)
    keep, hits = [], {}
    if not dl:
        return [True] * len(skus), hits
    for raw in skus:
        sku = normalize_sku(raw)
        reason = dl.get(sku)
        if reason is None:
            keep.append(True)
        else:
            keep.append(False)
            hits[sku] = reason
    return keep, hits
