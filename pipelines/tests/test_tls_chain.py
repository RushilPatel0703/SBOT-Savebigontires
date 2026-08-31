"""Tests for shared/tls_chain.py — completing the chain Emerge under-serves.

The bug: order.emergetires.com does not send its issuing intermediate, so
`refresh_cookies.py --supplier emerge` died in page.goto() with
ERR_CERT_AUTHORITY_INVALID and the dashboard's "Refresh All Cookies" reported
Emerge FAILED.

The fix hands `requests` a completed CA bundle and lets Playwright skip the
certificate error — and the whole safety of that second half rests on
browser_needs_chain_bypass() staying narrow. These tests are offline: they pin
the decision logic, not the live certificate, so they keep holding after Emerge
eventually fixes their server.
"""

import ssl
import sys
from pathlib import Path

import certifi

_PIPELINES_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PIPELINES_DIR / "shared"))

import tls_chain as T  # noqa: E402


# ── ca_bundle ────────────────────────────────────────────────────────────────

def test_bundle_is_a_superset_of_certifi():
    """Never a weakening: every root certifi trusts is still trusted."""
    bundle = Path(T.ca_bundle()).read_text(encoding="utf-8")
    roots = Path(certifi.where()).read_text(encoding="utf-8")
    assert roots in bundle


def test_bundle_carries_the_shipped_intermediates():
    bundle = Path(T.ca_bundle()).read_text(encoding="utf-8")
    shipped = sorted(T.CERTS_DIR.glob("*.pem"))
    assert shipped, "certs/ should ship at least the GoDaddy DV intermediate"
    for pem in shipped:
        # Compare on the base64 body — the header comments are not in the bundle
        # verbatim, but the certificate itself must be.
        body = "".join(
            line for line in pem.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.startswith("#")
        )
        assert body.strip() in bundle


def test_bundle_loads_as_a_real_trust_store():
    """A malformed concatenation would fail here rather than at scrape time."""
    ctx = ssl.create_default_context(cafile=T.ca_bundle())
    assert ctx.cert_store_stats()["x509_ca"] > 100


def test_bundle_path_is_stable_within_a_process():
    assert T.ca_bundle() == T.ca_bundle()


# ── browser_needs_chain_bypass ───────────────────────────────────────────────

def _stub_verify(monkeypatch, plain_ok, completed_ok):
    """Stub verify_chain: first arg cafile=certifi is the 'plain' probe."""
    def fake(host, port=443, timeout=20, cafile=None):
        if cafile == certifi.where():
            return plain_ok, "plain"
        return completed_ok, "completed"
    monkeypatch.setattr(T, "verify_chain", fake)
    monkeypatch.setattr(T, "_probe_cache", {})


def test_no_bypass_when_the_host_already_verifies(monkeypatch):
    """North Country and Formula serve complete chains — they must keep every
    check Chromium does, not inherit Emerge's workaround."""
    _stub_verify(monkeypatch, plain_ok=True, completed_ok=True)
    assert T.browser_needs_chain_bypass("online.northcountrytire.ca") is False


def test_bypass_only_for_an_incomplete_but_valid_chain(monkeypatch):
    """Emerge: fails on certifi alone, passes once the intermediate is added."""
    _stub_verify(monkeypatch, plain_ok=False, completed_ok=True)
    assert T.browser_needs_chain_bypass("order.emergetires.com") is True


def test_no_bypass_for_a_genuinely_bad_certificate(monkeypatch):
    """Expired, wrong-host, unknown-issuer: the browser must still reject it.
    This is the test that keeps the fix from becoming a blanket TLS opt-out."""
    _stub_verify(monkeypatch, plain_ok=False, completed_ok=False)
    assert T.browser_needs_chain_bypass("expired.example.com") is False


def test_probe_is_cached_per_host(monkeypatch):
    """One handshake per process, not one per browser launch — reauth can fire
    every 3h across a many-hour run (see costar_session.KEEPALIVE_INTERVAL_S)."""
    calls = []

    def fake(host, port=443, timeout=20, cafile=None):
        calls.append(cafile)
        return (cafile != certifi.where()), "x"

    monkeypatch.setattr(T, "verify_chain", fake)
    monkeypatch.setattr(T, "_probe_cache", {})

    assert T.browser_needs_chain_bypass("order.emergetires.com") is True
    assert T.browser_needs_chain_bypass("order.emergetires.com") is True
    assert len(calls) == 2  # plain + completed, once — not four probes


# ── wiring ───────────────────────────────────────────────────────────────────

def test_emerge_scrapers_pass_the_bundle_to_requests():
    """A `verify=` that gets dropped in a future edit puts the SSLError back."""
    for name in ("scraper_tires.py", "scraper_wheels.py"):
        src = (_PIPELINES_DIR / "emergetires" / name).read_text(encoding="utf-8")
        assert "CA_BUNDLE = ca_bundle()" in src.replace("CA_BUNDLE    =", "CA_BUNDLE =")
        assert "verify=CA_BUNDLE" in src, f"{name} posts without the completed bundle"


def test_refresh_cookies_scopes_the_bypass_per_host():
    """Guards against someone simplifying this to ignore_https_errors=True."""
    src = (_PIPELINES_DIR / "refresh_cookies.py").read_text(encoding="utf-8")
    assert "ignore_https_errors=ignore_https_errors" in src
    assert "browser_needs_chain_bypass(host)" in src
