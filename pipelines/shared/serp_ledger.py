#!/usr/bin/env python3
"""Append-only ledger of every billable SerpAPI call.

Why this exists
---------------
Reconciling the SerpAPI dashboard against this repo used to be impossible.
The dashboard read 1,469 calls; the caches on disk accounted for 924 distinct
paid questions. The missing 545 were real money with no trace, and no amount
of re-reading the caches could find them, because the caches were never a
record of spend in the first place — they are a record of *answers kept*.

The gap is structural. Every call site in enrichment.py has this shape:

    results = GoogleSearch({...}).get_dict()   # <-- billed here
    ...parse / filter / verify...              # <-- crash window
    cache[key] = entry; save_cache(cache)      # <-- recorded here

The billable event and the durable record are separated by the parse step —
and for images that step calls pick_verified_image(), which does its own
network fetches and is the most failure-prone code in the path. An exception,
a Ctrl-C, or a killed process anywhere in that window burns credit that
leaves nothing behind. `spent` is a local int; it dies with the process.

The fix is ordering, not bookkeeping: record the INTENT to call before the
call goes out, then record the outcome after. Disk then leads the dashboard
instead of trailing it. The worst case is over-counting by one call — if the
process dies between the intent write and the HTTP request — which is the
correct direction to be wrong about money.

Format
------
One JSON object per line (JSONL), appended and fsync'd immediately so a hard
kill cannot lose the tail. Never rewritten, never compacted: this is a
financial record, and the whole point is that it survives everything.

Events:
    opening_balance  calls, note      — spend that predates the ledger
    sent             id, field, key, query, ts
    ok               id, usable, ts   — call returned and parsed cleanly
    error            id, error, ts    — call or parse raised

A `sent` with no matching `ok`/`error` is a call that billed and then died
mid-flight. That number is the thing this module was written to make visible.

Usage:
    python serp_ledger.py                       # print reconciliation
    python serp_ledger.py --opening 1469 --note "SerpAPI dashboard 2026-08-23"
"""
import argparse
import collections
import contextlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone

SHARED_DIR  = os.path.dirname(os.path.abspath(__file__))
LEDGER_FILE = os.path.join(SHARED_DIR, "Data", "serp_ledger.jsonl")

_LOCK = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append(record):
    """Append one record and force it to disk.

    fsync is not paranoia here. Without it the tail of the ledger sits in the
    OS page cache, and the exact scenario this module exists to survive — the
    process dying mid-call — is also the scenario most likely to lose it.
    """
    record.setdefault("ts", _now())
    os.makedirs(os.path.dirname(LEDGER_FILE), exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _LOCK:
        with open(LEDGER_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    return record


class _Call:
    """Handle yielded by billed_call(). Set `.usable` when the result turns
    out to contain a real answer, so yield can be measured per field without
    re-deriving it from the cache later."""

    __slots__ = ("id", "field", "key", "usable")

    def __init__(self, call_id, field, key):
        self.id, self.field, self.key, self.usable = call_id, field, key, False


@contextlib.contextmanager
def billed_call(field, key, query=""):
    """Wrap one SerpAPI search. The `sent` record hits disk BEFORE the body
    runs, so the call is on the books even if nothing below it survives.

    Wrap the parse and verify steps too, not just the HTTP call — they sit
    inside the same billed window, and an exception there costs exactly as
    much as an exception in the request itself.
    """
    call_id = uuid.uuid4().hex[:12]
    _append({"event": "sent", "id": call_id, "field": field,
             "key": key, "query": query})
    handle = _Call(call_id, field, key)
    try:
        yield handle
    except BaseException as exc:  # noqa: BLE001 — KeyboardInterrupt counts too
        _append({"event": "error", "id": call_id,
                 "error": f"{type(exc).__name__}: {exc}"[:300]})
        raise
    _append({"event": "ok", "id": call_id, "usable": bool(handle.usable)})


def read_events(path=None):
    path = path or LEDGER_FILE
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A torn final line means the process died mid-write. The
                # call it represents is still real, so surface it rather than
                # dropping it silently.
                out.append({"event": "torn", "raw": line[:200]})
    return out


def summary(path=None):
    """Reconcile the ledger. Returns totals plus a per-field breakdown."""
    events = read_events(path)
    opening = sum(e.get("calls", 0) for e in events if e.get("event") == "opening_balance")

    sent = {e["id"]: e for e in events if e.get("event") == "sent"}
    resolved, errors, usable = set(), set(), set()
    for e in events:
        if e.get("event") == "ok":
            resolved.add(e.get("id"))
            if e.get("usable"):
                usable.add(e.get("id"))
        elif e.get("event") == "error":
            resolved.add(e.get("id"))
            errors.add(e.get("id"))

    lost = set(sent) - resolved
    torn = sum(1 for e in events if e.get("event") == "torn")

    by_field = collections.defaultdict(
        lambda: dict(sent=0, ok=0, usable=0, error=0, lost=0))
    for cid, e in sent.items():
        row = by_field[e.get("field", "?")]
        row["sent"] += 1
        if cid in errors:
            row["error"] += 1
        elif cid in lost:
            row["lost"] += 1
        else:
            row["ok"] += 1
        if cid in usable:
            row["usable"] += 1

    return dict(opening_balance=opening, sent=len(sent),
                total_billed=opening + len(sent) + torn,
                ok=len(resolved - errors), usable=len(usable),
                errors=len(errors), lost=len(lost), torn=torn,
                by_field=dict(by_field))


def set_opening_balance(calls, note=""):
    """Record spend that predates the ledger, so the running total matches
    the SerpAPI dashboard instead of restarting from zero."""
    return _append({"event": "opening_balance", "calls": int(calls), "note": note})


def format_summary(path=None):
    s = summary(path)
    lines = [
        "SerpAPI ledger - " + (path or LEDGER_FILE),
        "",
        f"  opening balance (pre-ledger spend)  {s['opening_balance']:>6}",
        f"  calls sent since                    {s['sent']:>6}",
        f"  {'-' * 38}",
        f"  TOTAL BILLED (reconciles to SerpAPI){s['total_billed']:>6}",
        "",
        f"  completed cleanly                   {s['ok']:>6}",
        f"    of which returned a usable answer {s['usable']:>6}",
        f"  raised an error after billing       {s['errors']:>6}",
        f"  died mid-call, never recorded       {s['lost']:>6}",
    ]
    if s["torn"]:
        lines.append(f"  torn/unparseable lines              {s['torn']:>6}")
    if s["by_field"]:
        lines += ["", "  by field:",
                  f"    {'field':<14}{'sent':>6}{'usable':>8}{'yield':>8}{'error':>7}{'lost':>6}"]
        for field, r in sorted(s["by_field"].items()):
            pct = (100.0 * r["usable"] / r["sent"]) if r["sent"] else 0.0
            lines.append(f"    {field:<14}{r['sent']:>6}{r['usable']:>8}"
                         f"{pct:>7.1f}%{r['error']:>7}{r['lost']:>6}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--opening", type=int, default=None,
                    help="Record pre-ledger spend read off the SerpAPI dashboard.")
    ap.add_argument("--note", default="", help="Provenance for --opening.")
    ap.add_argument("--path", default=None, help="Ledger file (default: shared/Data).")
    args = ap.parse_args()

    if args.opening is not None:
        set_opening_balance(args.opening, args.note or f"manual entry {_now()}")
        print(f"Recorded opening balance of {args.opening} call(s).")
    print(format_summary(args.path))


if __name__ == "__main__":
    main()
