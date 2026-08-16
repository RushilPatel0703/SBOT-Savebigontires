#!/usr/bin/env python3
"""Costar session-death detection + in-process re-authentication.

ONE shared implementation used by all three Costar-platform scraper pairs:
    northcountry/scraper_tires.py,   northcountry/scraper_wheels.py
    formula_distributors/scraper.py, formula_distributors/scraper_wheels.py
    emergetires/scraper_tires.py,    emergetires/scraper_wheels.py

THE BUG THIS FIXES
------------------
A Costar session that has expired does not raise and does not reliably flip
an error field — Product/Search and Product/ProductInfo both return HTTP 200
with EMPTY result lists. Verified against a real ~21h northcountry run
(2026-08-14 20:03 -> 2026-08-15 17:02): the search phase succeeded (8087
products, mostly served from the zero-terms cache) but the ProductInfo pass
that ran after the session had silently died returned

    Images found : 0 / 8087
    Specs found  : 0 / 8087

and every downstream pipeline step still logged "OK", because nothing in the
scraper treated "0 results with HTTP 200" as failure. A second, independent
sample (stale-cookie run, 2026-08-16 00:38, dashboard_logs/
northcountry_20260816_003841.log) confirms the same signature at the
search-phase level: ALL 17 search terms in a row returned 0 results. The
identical terms, replayed 3 minutes later in the same log directory with a
refreshed cookie (northcountry_20260816_004136.log), returned real, mostly
nonzero counts: 58, 21, 46, 50, 26, 159, 2, 249, 1, 145, 32, 0, 21, 4, 4, 3,
2, 11 — only ONE of those 17 ('39X') is a legitimate zero. That is the
data-grounding for ZERO_STREAK_THRESHOLD below.

DO NOT use `notAvailableMessage` in the ProductInfo response as a death
signal — it is a canned string present on every healthy response too
(confirmed against a live healthy-session baseline; see task notes).

DETECTION STRATEGY
-------------------
Search phase: a consecutive-zero-result streak is only a TRIGGER, never
acted on directly — it is confirmed with a canary re-probe before anything
re-authenticates. A real NC catalog has long *legitimate* zero stretches:
the zero-terms cache exists because ~1000 of 1452 generated search terms (a
synthetic width x ratio / LT / ST grid) never match a real stocked size, and
measuring pipelines/northcountry/Data/raw/zero_terms_tires_cache.json against
SEARCH_TERMS order shows one legitimate run of 980 consecutive cached-zero
terms (the tail of the metric grid plus the entire LT/ST synthetic block),
while every *other* legitimate run in that same cache tops out at 14. A
fixed streak count can't be both short enough to catch a dead session
quickly in the "real" part of the grid and long enough to survive a 980-term
legitimate desert without constantly re-authenticating — so the streak only
triggers a cheap canary check: re-probe the last search term this run saw
return a solid (>=5) result count.
  - Canary still returns results  -> legitimate empty region. Log it, reset
    the streak counter, keep going. No re-auth.
  - Canary is ALSO empty          -> session presumed dead. Re-authenticate.
Resetting (rather than disabling) the streak on a legitimate-empty verdict
means the canary re-fires every ZERO_STREAK_THRESHOLD terms for as long as
the desert continues, which bounds worst-case detection latency to about one
threshold's worth of terms anywhere in the run — not just at the start.

ProductInfo / cost phase: no streak needed. A single batch (up to
PRODUCT_INFO_BATCH_SIZE=50 heterogeneous products spanning many
brands/sizes) returning ZERO images AND ZERO specs is already implausible on
a healthy session — the measured healthy baseline returns 1 image and 32
spec fields for a single guid, so 50 unrelated guids all having neither is a
much stronger signal than any single empty search term. Callers trigger
re-auth immediately on that condition (see all_zero_batch()).

KEEPALIVE
---------
Also re-auth proactively every KEEPALIVE_INTERVAL_S, independent of any
symptom. We know a session can die somewhere inside a ~21h window but have
no telemetry on exactly when — that run had no detection at all, so the
death could have happened anywhere from minute 1 to minute 1260. 3 hours
(~7 proactive refreshes across a 21h run) keeps the session's age, at any
point in the run, to well under a third of the shortest failure window
actually observed, at the cost of a ~10-20s Playwright login roughly every
3 hours — negligible against a run measured in many hours.

FAILURE BUDGET
---------------
Re-auth is capped at MAX_REAUTH_ATTEMPTS per run (shared across search-phase
and ProductInfo-phase triggers — one budget, not one per phase) with
exponential backoff between failed login attempts (60s, 120s, 240s, capped
at 600s). Exhausting the budget raises SessionDeathError.

WHY SessionDeathError SUBCLASSES BaseException, NOT Exception
---------------------------------------------------------------
Every scraper in this repo is threaded with liberal `except Exception as e:
print(...)` around individual sub-requests (rim drilldowns, ProductInfo
batches, etc.) — that's deliberate resilience against flaky HTTP and is not
something this change should have to hunt down and special-case at every one
of the ~15 call sites across 6 files. Modeled on SystemExit/KeyboardInterrupt,
SessionDeathError inherits directly from BaseException so it passes through
every `except Exception` handler untouched and reaches main() (or a
ThreadPoolExecutor future's .result()) to terminate the run with a nonzero
exit code — a run that cannot get data must not exit 0.
"""

import sys
import threading
import time
from pathlib import Path

SHARED_DIR = Path(__file__).resolve().parent
PIPELINES_DIR = SHARED_DIR.parent

ZERO_STREAK_THRESHOLD = 25          # search phase — see module docstring
KEEPALIVE_INTERVAL_S = 3 * 60 * 60  # 3h — see module docstring
MAX_REAUTH_ATTEMPTS = 4
_CANARY_MIN_COUNT = 5               # only anchor the canary on a "solid" result


class SessionDeathError(BaseException):
    """Raised when the Costar session cannot be recovered. Deliberately NOT
    an Exception subclass — see module docstring. Let this propagate to
    main() uncaught, or catch it there to print a clean message before
    sys.exit(1). Never swallow it with a bare `except Exception`."""


def _load_refresh_module():
    """Import pipelines/refresh_cookies.py lazily — it imports playwright,
    which a normal (no-reauth-needed) scrape run should not be required to
    have installed just to import this module."""
    sys.path.insert(0, str(PIPELINES_DIR))
    import refresh_cookies  # noqa: E402
    return refresh_cookies


def _reauth_sync(supplier_key):
    """Run refresh_cookies.login_costar() to completion and return the new
    COOKIE_STRING, or None on failure. Safe to call from a worker thread —
    asyncio.run() opens its own event loop per call."""
    import asyncio
    rc = _load_refresh_module()
    supplier_cfg = rc.SUPPLIERS[supplier_key]
    return asyncio.run(rc.login_costar(supplier_cfg))


def all_zero_batch(*counts):
    """True if every count in a ProductInfo/cost batch is zero — the
    batch-level trigger described in the module docstring. Used as:
        if guids and all_zero_batch(len(guid_to_image), len(guid_to_specs)):
    """
    return all(c == 0 for c in counts)


class CostarSessionGuard:
    """One instance per scraper process. Tracks search-phase zero-result
    streaks, does canary verification, and drives in-process
    re-authentication (with on-disk persistence via refresh_cookies.py so
    the NEXT run also starts from a fresh cookie)."""

    def __init__(self, supplier_key, cookie, scraper_file,
                 zero_streak_threshold=ZERO_STREAK_THRESHOLD,
                 keepalive_interval_s=KEEPALIVE_INTERVAL_S,
                 max_reauth_attempts=MAX_REAUTH_ATTEMPTS):
        self.supplier_key = supplier_key
        self.cookie = cookie
        self.scraper_file = Path(scraper_file)
        self.zero_streak_threshold = zero_streak_threshold
        self.keepalive_interval_s = keepalive_interval_s
        self.max_reauth_attempts = max_reauth_attempts

        self._lock = threading.Lock()
        self._zero_streak = 0
        self._last_healthy_term = None
        self._reauth_count = 0
        self._last_reauth_ts = time.monotonic()

    # ---- search-phase signal ----------------------------------------------
    def note_search_result(self, term, count):
        """Call after every Product/Search (including drilldown sub-queries).
        Returns True once the streak crosses the threshold — caller should
        canary-verify (see scraper _check_session_health() helpers) before
        deciding whether to re-auth."""
        with self._lock:
            if count > 0:
                self._zero_streak = 0
                if count >= _CANARY_MIN_COUNT:
                    self._last_healthy_term = term
                return False
            self._zero_streak += 1
            return self._zero_streak >= self.zero_streak_threshold

    def canary_term(self):
        with self._lock:
            return self._last_healthy_term

    def reset_streak(self):
        with self._lock:
            self._zero_streak = 0

    # ---- keepalive ----------------------------------------------------------
    def keepalive_due(self):
        return (time.monotonic() - self._last_reauth_ts) >= self.keepalive_interval_s

    # ---- re-auth --------------------------------------------------------------
    def reauth(self, bad_cookie, reason):
        """Re-authenticate. If `bad_cookie` no longer matches the guard's
        current cookie, someone else already refreshed it (avoids duplicate
        Playwright logins when multiple worker threads hit the same dead
        session at once) — return the current cookie with no network call.

        Otherwise loops internally (own backoff) until either a fresh cookie
        is obtained or MAX_REAUTH_ATTEMPTS is exhausted, in which case this
        raises SessionDeathError. Never returns a still-bad cookie — callers
        can assume "returned normally" means "safe to retry now".
        """
        with self._lock:
            if bad_cookie is not None and self.cookie != bad_cookie:
                return self.cookie  # already refreshed by another caller/thread

            print(f"  [session] suspected session death ({self.supplier_key}) — {reason}")
            while self._reauth_count < self.max_reauth_attempts:
                self._reauth_count += 1
                attempt = self._reauth_count
                print(f"  [session] re-auth attempt {attempt}/{self.max_reauth_attempts}"
                      f" ({self.supplier_key})...")
                try:
                    new_cookie = _reauth_sync(self.supplier_key)
                except Exception as e:
                    new_cookie = None
                    print(f"  [session] re-auth attempt {attempt} raised: {e}")

                if new_cookie:
                    self.cookie = new_cookie
                    self._persist(new_cookie)
                    self._zero_streak = 0
                    self._last_reauth_ts = time.monotonic()
                    print(f"  [session] re-auth OK on attempt {attempt} — cookie "
                          f"refreshed in-process and persisted to disk")
                    return new_cookie

                if self._reauth_count < self.max_reauth_attempts:
                    wait = min(60 * (2 ** (attempt - 1)), 600)
                    print(f"  [session] re-auth attempt {attempt} FAILED — "
                          f"backing off {wait}s before retrying")
                    time.sleep(wait)

            raise SessionDeathError(
                f"[{self.supplier_key}] re-auth failed after "
                f"{self.max_reauth_attempts} attempts — {reason}. "
                f"A run that cannot get data must not exit 0."
            )

    def _persist(self, cookie):
        """Patch COOKIE_STRING back into every file refresh_cookies.py knows
        about for this supplier (not just the currently-running script), so
        the NEXT run also starts from a fresh cookie."""
        try:
            rc = _load_refresh_module()
            supplier_cfg = rc.SUPPLIERS[self.supplier_key]
            rc.update_costar_files(supplier_cfg, cookie)
        except Exception as e:
            print(f"  [session] WARNING: failed to persist refreshed cookie to disk: {e}")
