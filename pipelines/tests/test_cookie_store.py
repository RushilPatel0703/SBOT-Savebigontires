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


def test_put_never_erases_other_suppliers_when_store_is_unreadable(store):
    """A store that exists but can't be read must stop the write, not be
    replaced by a one-key store that drops every other supplier's cookie."""
    store.STORE.parent.mkdir(parents=True)
    store.STORE.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        store.put("wheelsmart", {"PHPSESSID": "abc"})
    assert store.STORE.read_text(encoding="utf-8") == "{not json"
    assert list(store.STORE.parent.iterdir()) == [store.STORE]     # no temp file left


def test_put_retries_a_briefly_locked_store(store, monkeypatch):
    """Windows: another process holding the file open makes os.replace raise
    PermissionError for a moment. The write must wait it out, not fail."""
    store.put("nc", "old")
    real_replace, calls = store.os.replace, []

    def flaky_replace(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("in use")
        real_replace(src, dst)

    monkeypatch.setattr(store.os, "replace", flaky_replace)
    monkeypatch.setattr(store, "_RETRY_SLEEP", 0)
    store.put("formula", "fresh")
    assert store.get("nc") == "old" and store.get("formula") == "fresh"
    assert list(store.STORE.parent.iterdir()) == [store.STORE]


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
