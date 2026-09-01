#!/usr/bin/env python3
"""Complete the TLS chains that a supplier serves incompletely.

THE BUG THIS FIXES
------------------
order.emergetires.com rotated its certificate on 2026-08-18 (GoDaddy DV,
*.emergetires.com) and the new server config does not send the issuing
intermediate, "GoDaddy TLS Intermediate CA DV - R1v1". A desktop browser hides
that: it reads the "CA Issuers" AIA URL out of the leaf and fetches the missing
link itself. Nothing else here does, so nothing else can build a chain to a
trusted root:

    requests / certifi          SSLError "unable to get local issuer certificate"
    Playwright's Chromium       ERR_CERT_AUTHORITY_INVALID  (no AIA fetching)

Which is why the dashboard's "Refresh All Cookies" reports Emerge FAILED: it
dies inside page.goto(), before it has even seen a login form, and the message
that reaches the log ("goto failed") says nothing about certificates. Both
Emerge scrapers fail the same way on their first CostarApi POST. North Country
and Formula are unaffected — they serve complete chains.

WHAT THIS DOES
--------------
certs/ holds the intermediates suppliers omit. ca_bundle() concatenates them
onto certifi's roots and returns a path to hand `requests` as verify=. That is
certifi PLUS an intermediate — strictly a superset, and not a weakening: the
intermediate itself still has to chain to a root certifi already trusts, or
verification fails exactly as it does today.

Chromium cannot be handed a CA bundle, so ignoring the error is the only lever
Playwright gives us. browser_can_proceed() makes that lever safe to pull: it
completes the handshake here first, with full verification against the bundle
above, and only reports True when the certificate is genuinely valid and merely
under-served. A host with a truly bad certificate — expired, wrong name,
unknown issuer — fails that check, gets no bypass, and is left for the browser
to reject. When Emerge eventually fixes their chain the check still passes,
the bypass stops being needed, and nothing here has to change.
"""

import hashlib
import os
import socket
import ssl
import tempfile
from pathlib import Path

import certifi

CERTS_DIR = Path(__file__).resolve().parent / "certs"

_bundle_path = None
_probe_cache = {}


def ca_bundle() -> str:
    """Path to a CA bundle of certifi's roots plus every PEM in certs/.

    Hand this to requests as verify=. The file is written to the system temp
    dir under a name derived from the inputs, so concurrent scrapers share one
    bundle and a certifi upgrade produces a new one instead of a stale hit.
    """
    global _bundle_path
    if _bundle_path is not None:
        return _bundle_path

    roots = Path(certifi.where()).read_text(encoding="utf-8")
    extras = sorted(CERTS_DIR.glob("*.pem")) if CERTS_DIR.is_dir() else []
    parts = [roots] + [p.read_text(encoding="utf-8") for p in extras]
    blob = "\n".join(parts)

    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    path = Path(tempfile.gettempdir()) / f"sbot_ca_bundle_{digest}.pem"
    if not path.exists():
        # Write-then-rename so a second process never reads a half-written
        # bundle and concludes the world's roots are untrusted.
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(blob, encoding="utf-8")
        os.replace(tmp, path)

    _bundle_path = str(path)
    return _bundle_path


def verify_chain(host: str, port: int = 443, timeout: int = 20, cafile=None):
    """Complete a TLS handshake with `host`, verifying against `cafile`
    (ca_bundle() by default).

    Returns (ok, detail). ok=False means the certificate was rejected — either
    because the chain is incomplete (pass ca_bundle() and it will pass) or
    because it is genuinely bad, which nothing here should paper over.
    """
    try:
        ctx = ssl.create_default_context(cafile=cafile or ca_bundle())
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                return True, tls.version()
    except ssl.SSLCertVerificationError as e:
        return False, f"certificate rejected: {e.verify_message or e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def browser_needs_chain_bypass(host: str) -> bool:
    """True if Playwright should be told to ignore this host's certificate error.

    Both halves have to hold, so the bypass is never wider than the bug:

      1. the host FAILS against certifi's roots alone — this is a host Chromium
         genuinely cannot verify, not one that is already fine;
      2. it PASSES once the shipped intermediates are added — we have proved the
         certificate is valid and merely under-served.

    A host that verifies normally (North Country, Formula) gets no bypass and
    keeps every check Chromium does. A host with a genuinely bad certificate —
    expired, wrong name, unknown issuer — also gets no bypass and is left for
    the browser to reject. Cached per host: one handshake per process.
    """
    if host in _probe_cache:
        return _probe_cache[host]

    plain_ok, _ = verify_chain(host, cafile=certifi.where())
    if plain_ok:
        _probe_cache[host] = False
        return False

    completed_ok, detail = verify_chain(host)
    if completed_ok:
        print(f"  [tls] {host} serves an incomplete chain; verified here against "
              f"certifi + shipped intermediates ({detail}) - allowing the browser through")
    else:
        print(f"  [tls] {host} did NOT verify against certifi + shipped "
              f"intermediates - {detail}")
    _probe_cache[host] = completed_ok
    return completed_ok
