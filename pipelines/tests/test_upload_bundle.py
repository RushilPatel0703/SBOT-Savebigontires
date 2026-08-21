"""Regression tests for the Handle-uniqueness safety net in upload_bundle.py.

Real-world trigger: an empty Shopify store rejected 24 rows across the 20 Aug
2026 bundle with "ignored because handle X already exists" -- on an empty
store, that error can only mean two rows in the SAME import batch collided on
Handle. Investigation found 19 such pairs, all with distinct SKUs (often
distinct prices too), so none were true duplicate rows -- each pair described
two separately-purchasable products that happened to slugify identically:
a "(Y)" vs "Y" speed-rating notation, a "P-ZERO" vs "P ZERO" spacing
difference in the model name, and Nexen SKU variants with no textual
difference in the displayed fields at all. Three unrelated root causes, so
the fix isn't a slugify() patch -- it's a uniqueness pass over the merged
bundle that can't miss a case regardless of why the text collided.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
import upload_bundle as UB  # noqa: E402


def _df(rows):
    return pd.DataFrame(rows)


def test_no_collisions_leaves_handles_untouched():
    df = _df([
        {"Handle": "a", "Variant SKU": "111"},
        {"Handle": "b", "Variant SKU": "222"},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 0
    assert list(out["Handle"]) == ["a", "b"]


def test_first_occurrence_keeps_original_handle():
    df = _df([
        {"Handle": "same", "Variant SKU": "AAA111"},
        {"Handle": "same", "Variant SKU": "BBB222"},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 1
    assert out.loc[0, "Handle"] == "same"
    assert out.loc[1, "Handle"] != "same"


def test_disambiguated_handle_is_traceable_to_sku():
    df = _df([
        {"Handle": "bridgestone-potenza-24535r20-95y-xl", "Variant SKU": "013731"},
        {"Handle": "bridgestone-potenza-24535r20-95y-xl", "Variant SKU": "013367"},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 1
    assert out.loc[1, "Handle"] == "bridgestone-potenza-24535r20-95y-xl-013367"


def test_three_way_collision_all_become_unique():
    df = _df([
        {"Handle": "x", "Variant SKU": "111"},
        {"Handle": "x", "Variant SKU": "222"},
        {"Handle": "x", "Variant SKU": "333"},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 2
    assert out["Handle"].nunique() == 3


def test_result_has_zero_duplicate_handles_regardless_of_group_count():
    df = _df([
        {"Handle": "a", "Variant SKU": "1"},
        {"Handle": "a", "Variant SKU": "2"},
        {"Handle": "b", "Variant SKU": "3"},
        {"Handle": "b", "Variant SKU": "4"},
        {"Handle": "b", "Variant SKU": "5"},
        {"Handle": "c", "Variant SKU": "6"},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 3
    assert not out["Handle"].duplicated().any()


def test_falls_back_to_dup_suffix_when_sku_column_missing():
    df = _df([
        {"Handle": "same"},
        {"Handle": "same"},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 1
    assert out.loc[1, "Handle"] == "same-dup"


def test_falls_back_to_dup_suffix_when_sku_is_blank():
    df = _df([
        {"Handle": "same", "Variant SKU": "111"},
        {"Handle": "same", "Variant SKU": ""},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 1
    assert out.loc[1, "Handle"] == "same-dup"


def test_suffix_collision_falls_back_to_numeric_disambiguator():
    # Two different SKUs that happen to produce the identical cleaned suffix.
    df = _df([
        {"Handle": "same", "Variant SKU": "ABC-123"},
        {"Handle": "same", "Variant SKU": "abc123"},
        {"Handle": "same", "Variant SKU": "  abc123  "},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 2
    assert out["Handle"].nunique() == 3


def test_no_handle_column_is_a_noop():
    df = _df([{"Variant SKU": "111"}, {"Variant SKU": "222"}])
    out, fixed = UB._dedupe_handles(df)
    assert fixed == 0
    assert list(out.columns) == ["Variant SKU"]


def test_row_count_and_non_handle_data_are_unchanged():
    df = _df([
        {"Handle": "same", "Variant SKU": "111", "Variant Price": "9.99"},
        {"Handle": "same", "Variant SKU": "222", "Variant Price": "19.99"},
    ])
    out, fixed = UB._dedupe_handles(df)
    assert len(out) == 2
    assert list(out["Variant Price"]) == ["9.99", "19.99"]
    assert list(out["Variant SKU"]) == ["111", "222"]
