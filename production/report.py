#!/usr/bin/env python3
"""Read-only summaries of past runs, from whichever warehouse backend is configured.

The dashboard renders these after a sync so the client sees what actually
changed rather than a log tail. The same functions are what a cloud run should
send in its "weekly sync finished" notification — the numbers live in the
warehouse either way, so there is one definition of "what did this run do".

Everything here is plain SELECTs through the Warehouse interface, so it works
identically against DuckDB and Hasura/Neon.

Usage (from production/):
    python report.py               # last 5 runs
    python report.py --run 12      # one run in detail
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config


def _wh(warehouse=None):
    if warehouse is not None:
        return warehouse
    from core.warehouse import get_warehouse
    return get_warehouse()


def last_runs(limit=5, warehouse=None):
    """Most recent runs, newest first."""
    wh = _wh(warehouse)
    return wh.select(f"""
        select run_id, started_at, finished_at, status, mode, triggered_by, notes
        from pipeline_runs
        order by run_id desc
        limit {int(limit)};
    """)


def latest_run_id(warehouse=None):
    rows = last_runs(1, warehouse)
    return rows[0]["run_id"] if rows else None


def run_summary(run_id, warehouse=None):
    """Everything worth showing about one run: the run row, its steps, the
    validation gates that did not pass, and what the sync did per action."""
    wh = _wh(warehouse)
    run_id = int(run_id)

    run = wh.select_one(f"""
        select run_id, started_at, finished_at, status, mode, triggered_by, notes
        from pipeline_runs where run_id = {run_id};
    """)
    if not run:
        return None

    steps = wh.select(f"""
        select step, status, rows_in, rows_out, started_at, finished_at, error
        from run_steps where run_id = {run_id} order by id;
    """)

    # status is part of the key: 'applied' vs 'dry_run' vs 'failed' is the whole
    # point of the summary — a preview and a real write must never look alike.
    actions = wh.select(f"""
        select action, status, count(*)::int as n
        from sync_actions where run_id = {run_id}
        group by action, status order by action, status;
    """)

    gates = wh.select(f"""
        select check_name, severity, passed, details
        from validation_results
        where run_id = {run_id} and passed = false
        order by severity, check_name;
    """)

    return {"run": run, "steps": steps, "actions": actions, "failed_gates": gates}


def action_examples(run_id, action, limit=20, warehouse=None):
    """A sample of individual changes, for "show me what it would do"."""
    wh = _wh(warehouse)
    return wh.select(f"""
        select sku, old_value, new_value, status, error
        from sync_actions
        where run_id = {int(run_id)} and action = '{action}'
        order by sku
        limit {int(limit)};
    """)


def main():
    ap = argparse.ArgumentParser(description="Summarise pipeline runs")
    ap.add_argument("--run", type=int, help="Run id (default: most recent)")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.run is None and not args.json:
        rows = last_runs(args.limit)
        if not rows:
            print(f"No runs recorded yet in {config.WAREHOUSE_BACKEND} warehouse.")
            return 0
        print(f"{'run':>5}  {'status':<8} {'mode':<8} {'started':<20} triggered_by")
        for r in rows:
            print(f"{r['run_id']:>5}  {r['status']:<8} {r['mode']:<8} "
                  f"{str(r['started_at'])[:19]:<20} {r['triggered_by'] or ''}")
        print("\nDetail: python report.py --run <id>")
        return 0

    run_id = args.run or latest_run_id()
    if run_id is None:
        print("No runs recorded yet.")
        return 0
    summary = run_summary(run_id)
    if summary is None:
        print(f"No such run: {run_id}")
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return 0

    run = summary["run"]
    print(f"Run #{run['run_id']} — {run['status']} ({run['mode']}), "
          f"started {str(run['started_at'])[:19]}")
    if run.get("notes"):
        print(f"  notes: {run['notes']}")
    print("\nSteps:")
    for s in summary["steps"]:
        print(f"  {s['step']:<18} {s['status']:<8} in={s['rows_in']} out={s['rows_out']}"
              + (f"  ERROR: {str(s['error'])[:120]}" if s["error"] else ""))
    print("\nSync actions:")
    for a in summary["actions"]:
        print(f"  {a['action']:<16} {a['status']:<8} {a['n']}")
    if summary["failed_gates"]:
        print("\nValidation gates not passed:")
        for g in summary["failed_gates"]:
            print(f"  [{g['severity']}] {g['check_name']}: {str(g['details'])[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
