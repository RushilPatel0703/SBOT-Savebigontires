"""
Guards on the SerpAPI spend ledger (pipelines/shared/serp_ledger.py).

The ledger exists because the caches could never reconcile against the SerpAPI
dashboard: 1,469 calls billed, 924 distinct paid questions on disk, 545 gone
without a trace. The caches record answers kept, not money spent, and the two
diverge every time a call bills and then dies before its result is written.

So the property under test is an ORDERING property, not an accounting one:
the `sent` record must reach disk *before* the request goes out. Everything
else here follows from that. If a test in this file starts failing, assume
the ledger has gone back to undercounting the bill.

Nothing in this file makes a network call.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_PIPELINES_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_PIPELINES_DIR / "shared"))

import serp_ledger as SL  # noqa: E402


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    """Point the module at a throwaway ledger so tests never touch the real
    financial record under pipelines/shared/Data/."""
    path = tmp_path / "Data" / "serp_ledger.jsonl"
    monkeypatch.setattr(SL, "LEDGER_FILE", str(path))
    return path


def _events(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# The ordering property — the whole reason this module exists
# ---------------------------------------------------------------------------

def test_sent_is_on_disk_before_the_body_runs(ledger):
    """If the intent record were written after the call, a crash inside the
    call would lose it — which is exactly how 545 calls went missing."""
    seen_from_inside = None

    with SL.billed_call("image", "image:brand|model|", "a query"):
        # Simulates the instant the HTTP request is in flight: read the file
        # from inside the billed window and the call must already be there.
        seen_from_inside = _events(ledger)

    assert len(seen_from_inside) == 1
    assert seen_from_inside[0]["event"] == "sent"
    assert seen_from_inside[0]["field"] == "image"
    assert seen_from_inside[0]["query"] == "a query"


def test_hard_kill_mid_call_still_counts(tmp_path):
    """os._exit() bypasses except/finally entirely — the closest thing to a
    real process kill. The call must still be billed on disk."""
    path = tmp_path / "l.jsonl"
    child = (
        "import os, sys, serp_ledger as SL\n"
        "SL.LEDGER_FILE = sys.argv[1]\n"
        "with SL.billed_call('image', 'image:k|m|', 'q'):\n"
        "    os._exit(9)\n"
    )
    rc = subprocess.run([sys.executable, "-c", child, str(path)],
                        cwd=str(_PIPELINES_DIR / "shared")).returncode
    assert rc == 9

    s = SL.summary(str(path))
    assert s["sent"] == 1
    assert s["lost"] == 1, "a call that billed then died must not vanish"
    assert s["total_billed"] == 1


def test_exception_is_recorded_and_reraised(ledger):
    with pytest.raises(ValueError):
        with SL.billed_call("image", "image:p|q|", "q"):
            raise ValueError("boom inside pick_verified_image")

    s = SL.summary(str(ledger))
    assert s["sent"] == 1
    assert s["errors"] == 1
    assert s["lost"] == 0, "an error is resolved, not lost"
    assert s["total_billed"] == 1


def test_keyboard_interrupt_is_recorded(ledger):
    """Ctrl-C during a long run is the most likely real-world interruption,
    and BaseException means a plain `except Exception` would have missed it."""
    with pytest.raises(KeyboardInterrupt):
        with SL.billed_call("specs", "specs:a|b|c", "q"):
            raise KeyboardInterrupt

    assert SL.summary(str(ledger))["errors"] == 1


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------

def test_opening_balance_reconciles_to_the_dashboard(ledger):
    SL.set_opening_balance(1469, "dashboard reading")
    with SL.billed_call("specs", "specs:a|b|c", "q") as c:
        c.usable = True

    s = SL.summary(str(ledger))
    assert s["opening_balance"] == 1469
    assert s["sent"] == 1
    assert s["total_billed"] == 1470


def test_usable_defaults_to_false(ledger):
    """A call whose result parsed to nothing still cost money. It must count
    as billed, and must not be credited as a usable answer."""
    with SL.billed_call("designation", "designation:x|y|z", "q"):
        pass

    s = SL.summary(str(ledger))
    assert s["total_billed"] == 1
    assert s["usable"] == 0
    assert s["by_field"]["designation"]["usable"] == 0


def test_per_field_yield_breakdown(ledger):
    for i in range(3):
        with SL.billed_call("specs", f"specs:{i}", "q") as c:
            c.usable = i < 2
    with SL.billed_call("image", "image:a|b|", "q") as c:
        c.usable = False

    s = SL.summary(str(ledger))
    assert s["by_field"]["specs"] == dict(sent=3, ok=3, usable=2, error=0, lost=0)
    assert s["by_field"]["image"]["usable"] == 0


def test_torn_final_line_still_counts_as_billed(ledger):
    """A half-written line means the process died mid-append. The call it
    represents was still real money, so it must not be silently dropped."""
    with SL.billed_call("specs", "specs:a|b|c", "q") as c:
        c.usable = True
    os.makedirs(os.path.dirname(str(ledger)), exist_ok=True)
    with open(str(ledger), "a", encoding="utf-8") as f:
        f.write('{"event": "sent", "id": "trunc')  # no newline, no closing brace

    s = SL.summary(str(ledger))
    assert s["torn"] == 1
    assert s["total_billed"] == 2, "torn line is a real call, not a rounding error"


def test_missing_ledger_reads_as_zero(tmp_path):
    s = SL.summary(str(tmp_path / "nope.jsonl"))
    assert s["total_billed"] == 0
    assert s["by_field"] == {}


def test_ledger_is_append_only(ledger):
    """Nothing in the module may rewrite history — earlier lines must survive
    byte-for-byte once written."""
    SL.set_opening_balance(100, "first")
    first = Path(ledger).read_text(encoding="utf-8")
    for i in range(5):
        with SL.billed_call("specs", f"specs:{i}", "q"):
            pass

    assert Path(ledger).read_text(encoding="utf-8").startswith(first)
    assert SL.summary(str(ledger))["total_billed"] == 105


def test_format_summary_runs_on_an_empty_ledger(ledger):
    assert "TOTAL BILLED" in SL.format_summary(str(ledger))
