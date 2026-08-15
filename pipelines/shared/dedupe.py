"""Disambiguate duplicate product titles so every row gets a unique Shopify handle.

Several suppliers produce identical Title text for physically distinct SKUs
(e.g. two offsets of the same wheel that aren't reflected in the title). Since
`Handle` is slugified from Title, unresolved duplicates collapse multiple
products onto one Shopify handle, which the importer rejects as
"variant already exists" / "ignored because handle already exists".
"""
import pandas as pd


def dedupe_titles(titles, skus):
    """Return a new Series: any Title shared by more than one row gets a
    SKU-derived suffix appended on the 2nd+ occurrence so every row is unique.
    The first occurrence of a duplicated title is left unchanged.
    """
    titles = pd.Series(titles).copy()
    skus = pd.Series(skus)
    skus.index = titles.index

    counts = titles.value_counts()
    dup_set = set(counts[counts > 1].index)
    if not dup_set:
        return titles

    seen = set()
    for i in titles.index:
        t = titles.at[i]
        if t not in dup_set:
            continue
        if t not in seen:
            seen.add(t)
            continue
        suffix = str(skus.at[i]).split("-")[-1].strip()
        new_t = f"{t} {suffix}" if suffix else t
        base_new_t = new_t
        n = 2
        while new_t in seen:
            new_t = f"{base_new_t}-{n}"
            n += 1
        titles.at[i] = new_t
        seen.add(new_t)
    return titles
