"""Canonical SKU normalization — the single source of truth for turning a raw
supplier SKU value into the string used as the merge/matching key.

Background: SKUs arrive as strings from CSVs, but a CSV round-trip can turn a
numeric-looking SKU into a float artifact ("1234" -> "1234.0"). The old fix for
that, duplicated across several call sites, was `str(int(float(v)))`. That
round-trip through `int()` silently strips leading zeros, which is wrong: some
suppliers (verified: dttire tires) use leading-zero and non-leading-zero SKUs
for genuinely different products (e.g. "11551" vs "011551"). Collapsing them
onto one key causes merge_suppliers.py to average/med a price across two
different products, sum their quantities together, and drop_duplicates() to
silently delete one of the two real rows.

The correct behaviour: strip a trailing ".0"-style float artifact, but never
touch leading zeros.
    "011551"  -> "011551"   (leading zeros preserved)
    "1234.0"  -> "1234"     (float artifact stripped)
    "1234.00" -> "1234"
    "1234"    -> "1234"     (unchanged)
    "AB-123"  -> "AB-123"   (non-numeric SKUs pass through unchanged, stripped)
"""

import re

_FLOAT_ARTIFACT = re.compile(r"^-?\d+\.0+$")


def normalize_sku(val, split_colon=False):
    """Normalize a raw SKU value into the merge/matching key.

    split_colon: some feeds (WheelMart) carry SKUs as "TIRE:18\" TIRE:2HH2385H1"
    where only the last ':'-segment is the actual SKU (matches what's stored in
    Shopify). Set True for those feeds.
    """
    s = str(val).strip()
    if split_colon and ":" in s:
        s = s.split(":")[-1].strip()
    if _FLOAT_ARTIFACT.match(s):
        s = s.split(".")[0]
    return s

def disambiguate_skus(raw_skus, prefixes):
    """Resolve raw (last-colon-segment) SKUs that collide across differing
    name prefixes into distinct SKUs, in original row order.

    WheelMart lists some models under both a generic 'WHEEL:' listing and a
    'RAC:' branded listing that share the same trailing model code (e.g.
    'WHEEL:22\' WHEEL:L09GB-229512045' and 'RAC: L09GB-229512045') — the
    client confirmed these are different products (different retail/cost,
    sometimes different stock) and must not collapse onto one SKU.

    The first prefix seen for a given raw code keeps the plain, byte-identical
    SKU — this is the row production's existing drop_duplicates(keep="first")
    already lets through today, so whatever is already live in Shopify under
    the old single-segment derivation is unaffected. A raw code repeated
    under a *different* prefix gets that prefix appended
    ('L09GB-229512045-RAC') so it becomes its own product instead of being
    silently dropped. A raw code repeated under the *same* prefix (a true
    duplicate row) still gets the plain SKU, unchanged from today — downstream
    dedup collapses it exactly as before.
    """
    first_prefix_for = {}
    out = []
    for raw, prefix in zip(raw_skus, prefixes):
        established = first_prefix_for.get(raw)
        if established is None:
            first_prefix_for[raw] = prefix
            out.append(raw)
        elif prefix == established:
            out.append(raw)
        else:
            out.append(f"{raw}-{prefix}")
    return out
