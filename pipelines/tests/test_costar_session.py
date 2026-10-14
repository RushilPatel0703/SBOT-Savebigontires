"""CostarSessionGuard re-auth budget. Logins are stubbed; nothing hits the network."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
import costar_session  # noqa: E402
from costar_session import CostarSessionGuard, SessionDeathError  # noqa: E402


@pytest.fixture
def guard(monkeypatch):
    monkeypatch.setattr(costar_session.time, "sleep", lambda s: None)
    monkeypatch.setattr(CostarSessionGuard, "_persist", lambda self, c: None)
    return CostarSessionGuard("nc", "cookie-0", __file__)


def _logins(monkeypatch, results):
    """Each login returns the next item of `results` (None = failed login)."""
    it = iter(results)
    monkeypatch.setattr(costar_session, "_reauth_sync", lambda key: next(it))


def test_successful_reauths_do_not_spend_the_failure_budget(guard, monkeypatch):
    # The 2026-10-09 NC run: 4 symptom re-auths that all succeeded, then the 3h
    # keepalive raised "re-auth failed after 4 attempts" without trying to log in.
    _logins(monkeypatch, [f"cookie-{i}" for i in range(1, 7)])
    for _ in range(4):
        guard.reauth(guard.cookie, "zero-streak")
    assert guard.reauth(guard.cookie, "keepalive", proactive=True) == "cookie-5"


def test_failure_budget_counts_consecutive_failures_and_resets_on_success(guard, monkeypatch):
    _logins(monkeypatch, [None, None, None, "cookie-1", None, None, None, "cookie-2"])
    assert guard.reauth(guard.cookie, "first") == "cookie-1"
    assert guard.reauth(guard.cookie, "second") == "cookie-2"


def test_four_consecutive_failures_raise(guard, monkeypatch):
    _logins(monkeypatch, [None, None, None, None])
    with pytest.raises(SessionDeathError, match="4 consecutive attempts"):
        guard.reauth(guard.cookie, "dead")


def test_keepalives_are_never_capped(guard, monkeypatch):
    guard.max_symptom_reauths = 2
    _logins(monkeypatch, [f"cookie-{i}" for i in range(1, 51)])
    for _ in range(50):
        guard.reauth(guard.cookie, "keepalive", proactive=True)


def test_symptom_reauth_loop_is_capped(guard, monkeypatch):
    guard.max_symptom_reauths = 3
    _logins(monkeypatch, [f"cookie-{i}" for i in range(1, 10)])
    for _ in range(3):
        guard.reauth(guard.cookie, "zero-streak")
    with pytest.raises(SessionDeathError, match="kept presenting as dead"):
        guard.reauth(guard.cookie, "zero-streak")


def test_stale_cookie_returns_current_without_logging_in(guard, monkeypatch):
    _logins(monkeypatch, [])  # any login attempt would raise StopIteration
    assert guard.reauth("some-older-cookie", "zero-streak") == "cookie-0"
