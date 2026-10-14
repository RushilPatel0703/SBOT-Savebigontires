"""Supplier cookies live in shared/Data/session_cookies.json, not in scraper source.

The packaged Mac app ships compiled scrapers with no source to patch, so a
refreshed cookie has to reach the next run through this store.
"""
import sys
from pathlib import Path

import pytest

_PIPELINES = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PIPELINES / "shared"))
import cookie_store  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cookie_store, "STORE", tmp_path / "Data" / "session_cookies.json")
    return cookie_store


def test_missing_store_falls_back_to_default(store):
    assert store.get("nc", "literal") == "literal"


def test_put_then_get_round_trips(store):
    store.put("nc", "fresh")
    store.put("wheelsmart", {"PHPSESSID": "abc"})
    assert store.get("nc", "literal") == "fresh"
    assert store.get("wheelsmart", {}) == {"PHPSESSID": "abc"}


def test_unreadable_store_falls_back_to_default(store):
    store.STORE.parent.mkdir(parents=True)
    store.STORE.write_text("{not json", encoding="utf-8")
    assert store.get("formula", "literal") == "literal"


def test_refresh_saves_to_the_store_not_the_scrapers(store, monkeypatch):
    sys.path.insert(0, str(_PIPELINES))
    pytest.importorskip("playwright")
    import refresh_cookies as rc
    monkeypatch.setattr(rc, "cookie_store", store)
    scraper = _PIPELINES / "northcountry" / "scraper_tires.py"
    before = scraper.read_bytes()
    rc.update_costar_files(rc.SUPPLIERS["nc"], "new-cookie")
    rc.update_wheelsmart_files(rc.SUPPLIERS["wheelsmart"], {"_identity": "i", "PHPSESSID": "p"})
    assert store.get("nc") == "new-cookie"
    assert store.get("wheelsmart") == {"_identity": "i", "PHPSESSID": "p"}
    assert scraper.read_bytes() == before


@pytest.mark.parametrize("rel,key", [
    ("formula_distributors/scraper.py", "formula"),
    ("formula_distributors/scraper_wheels.py", "formula"),
    ("northcountry/scraper_tires.py", "nc"),
    ("northcountry/scraper_wheels.py", "nc"),
    ("emergetires/scraper_tires.py", "emerge"),
    ("emergetires/scraper_wheels.py", "emerge"),
    ("wheelsmart/scraper_tires.py", "wheelsmart"),
    ("wheelsmart/scraper_rims.py", "wheelsmart"),
])
def test_every_scraper_reads_the_store(rel, key):
    src = (_PIPELINES / rel).read_text(encoding="utf-8")
    assert f'cookie_store.get("{key}",' in src
