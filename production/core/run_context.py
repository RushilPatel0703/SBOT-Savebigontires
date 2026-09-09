#!/usr/bin/env python3
"""RunContext — one object per pipeline run.

Owns:
  - the pipeline_runs row (created at start, finalized at end)
  - per-step bookkeeping in run_steps (timings, row counts, errors)
  - a logger writing to console + logs/run_<ts>.log

DB bookkeeping goes through whichever warehouse backend is configured (see
core/warehouse.py) — Hasura/Neon in the cloud, an embedded DuckDB file behind
the dashboard's Sync button. If a bookkeeping write fails the run continues (the
file log is the fallback record) — but data-path writes elsewhere are never
best-effort.
"""

import getpass
import json
import logging
import socket
from contextlib import contextmanager
from datetime import datetime

import config
from core.warehouse import get_warehouse


def make_logger(name):
    config.LOGS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = config.LOGS_DIR / f"{name}_{ts}.log"

    logger = logging.getLogger(f"{name}_{ts}")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        fh.setFormatter(fmt)
        ch.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(ch)
    logger.info(f"Log file: {log_file}")
    return logger


class StepFailed(Exception):
    """Raised inside a step to mark it failed and stop the run."""


class RunContext:
    def __init__(self, mode, product_types, warehouse=None, logger=None):
        self.mode          = mode                       # live | dry_run
        self.product_types = list(product_types)
        self.log           = logger or make_logger("run")
        self.wh            = warehouse or get_warehouse(logger=self.log)
        self.stats         = {}

        self.log.info(f"Warehouse: {self.wh.describe()}")
        self.run_id = self.wh.start_run(
            mode=mode,
            triggered_by=f"{getpass.getuser()}@{socket.gethostname()}",
            product_types=self.product_types,
        )
        self.log.info(f"Run #{self.run_id} started — mode={mode}, types={self.product_types}")

    # Steps written against the old Hasura-only context still say ctx.hasura.
    # It is the same object; the name just stopped being accurate once a run
    # could be backed by DuckDB.
    @property
    def hasura(self):
        return self.wh

    # ── step bookkeeping ───────────────────────────────────────────────────────
    @contextmanager
    def step(self, name):
        rec = _StepRecord(self, name)
        self.log.info("=" * 60)
        self.log.info(f"STEP: {name}")
        self.log.info("=" * 60)
        try:
            yield rec
        except Exception as e:
            rec.finish("failed", error=str(e)[:2000])
            raise
        else:
            rec.finish("success")

    def _insert_step(self, name):
        return self.wh.insert_step(self.run_id, name)

    def _update_step(self, step_id, status, rows_in, rows_out, error, details):
        self.wh.update_step(step_id, status, rows_in, rows_out, error, details)

    # ── validation + audit writers ─────────────────────────────────────────────
    def record_validation(self, check_name, passed, severity="error", details=None):
        marker = "PASS" if passed else ("WARN" if severity == "warning" else "FAIL")
        line = f"[{marker}] {check_name}"
        if details:
            line += f" — {json.dumps(details, default=str)[:300]}"
        (self.log.info if passed else
         self.log.warning if severity == "warning" else self.log.error)(line)
        try:
            self.wh.insert_many("validation_results", [{
                "run_id": self.run_id, "check_name": check_name,
                "severity": severity, "passed": passed, "details": details or {},
            }])
        except Exception as e:
            self.log.warning(f"validation_results write failed: {e}")

    def record_sync_actions(self, actions):
        """actions: list of dicts with sku/action/old_value/new_value/status/error."""
        if not actions:
            return
        for a in actions:
            a["run_id"] = self.run_id
        self.wh.insert_many("sync_actions", actions)

    # ── finalize ───────────────────────────────────────────────────────────────
    def finalize(self, status, notes=None):
        self.wh.finalize_run(self.run_id, status, self.stats, notes)
        self.log.info(f"Run #{self.run_id} finished — status={status}")
        self.log.info(f"Stats: {json.dumps(self.stats, default=str)}")


class _StepRecord:
    def __init__(self, ctx, name):
        self.ctx      = ctx
        self.name     = name
        self.rows_in  = None
        self.rows_out = None
        self.details  = {}
        self._id      = ctx._insert_step(name)

    def finish(self, status, error=None):
        self.ctx._update_step(self._id, status, self.rows_in, self.rows_out, error, self.details)
        if status == "success":
            self.ctx.stats[self.name] = {
                "rows_in": self.rows_in, "rows_out": self.rows_out, **self.details,
            }
