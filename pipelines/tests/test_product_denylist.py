"""Tests for the per-product denylist (shared/product_denylist.py).

The denylist is the only thing standing between a re-scrape and re-creating a
product the client has struck off by name, so what is worth pinning is the
ways it could silently stop matching: a float artifact from a CSV round-trip,
a shipped file that no longer parses, and the two roads to the storefront
disagreeing about which SKU string they are comparing.
"""
import json
import sys
from pathlib import Path

import pytest

SHARED = Path(__file__).resolve().parent.parent / "shared"
sys.path.insert(0, str(SHARED))
from product_denylist import (DENYLIST_PATH, denied_flags,  # noqa: E402
                              load_denylist)


def test_shipped_denylist_parses():
    """The file in the repo must load — an unparseable one hard-fails a run."""
    dl = load_denylist()
    assert dl, "shipped denylist is empty; every entry would be re-listed"
    assert all(isinstance(reason, str) for reason in dl.values())


def test_shipped_denylist_covers_the_reviewed_skus():
    """The 2026-09-30 image review's passenger-size removals.

    These are the ones no size rule can express, which is the whole reason the
    denylist exists — if one drops out of the file it comes straight back into
    the store on the next scrape.
    """
    dl = load_denylist()
    for sku in ("EMRG-80614504", "EMRG-80614498", "EMRG-80447047",
                "EMRG-80416264", "EMRG-80601022", "EMRG-80521947", "4268500"):
        assert sku in dl, f"{sku} fell out of the denylist"


def test_wheels_are_covered():
    """The size filter is tires-only, so wheels are denied here or nowhere."""
    dl = load_denylist()
    assert "KR183-2860BM38" in dl
    assert "KM69198512735" in dl


def test_denies_and_keeps():
    dl = {"EMRG-1": "no", "4268500": "no"}
    keep, hits = denied_flags(["EMRG-1", "OTHER", "4268500", ""], dl)
    assert keep == [False, True, False, True]
    assert set(hits) == {"EMRG-1", "4268500"}


def test_float_artifact_still_matches():
    """A numeric SKU that round-tripped through a CSV reads as '4268500.0'.

    Matching on the raw string would silently miss it and re-list the product,
    which is why comparison goes through the canonical normalize_sku.
    """
    keep, hits = denied_flags(["4268500.0"], {"4268500": "no"})
    assert keep == [False]
    assert "4268500" in hits


def test_empty_denylist_keeps_everything():
    keep, hits = denied_flags(["A", "B"], {})
    assert keep == [True, True]
    assert hits == {}


def test_missing_file_is_an_empty_denylist(tmp_path):
    assert load_denylist(tmp_path / "nope.json") == {}


def test_unparseable_file_raises(tmp_path):
    """Deliberate hard failure, matching markup_overrides.json.

    Degrading to "deny nothing" would quietly put the removed products back on
    the storefront, which is the exact outcome this module prevents.
    """
    bad = tmp_path / "product_denylist.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_denylist(bad)


def test_both_roads_use_the_same_keys():
    """ingest.py keys on _sku (post-EMRG-prefix); upload_bundle.py on the
    'Variant SKU' column, which augment_tires.py has already prefixed. The
    denylist file must therefore be written in the prefixed form, or it would
    match on neither road.
    """
    doc = json.loads(Path(DENYLIST_PATH).read_text(encoding="utf-8"))
    for entry in doc["skus"]:
        if entry.get("supplier") == "emergetires":
            assert entry["sku"].startswith("EMRG-"), (
                f"{entry['sku']} is missing the prefix the store holds it under")
