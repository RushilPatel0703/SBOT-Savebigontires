#!/usr/bin/env python3
"""Embedded DuckDB warehouse — the backend behind the dashboard's Sync button.

Same interface as core/hasura.py (see core/warehouse.py), so ingest, merge,
validate, catalog and sync run against this without a single conditional. The
client gets the real pipeline — the real markup, the real safety gates, the real
diff — on a laptop with no database server and no secrets.

Everything lands in one file (production/Data/warehouse.duckdb) which is also
the run history: pipeline_runs, run_steps, sync_actions and validation_results
accumulate exactly as they do in Neon, so "what did last Tuesday's sync change?"
is answerable locally too. That history is what validate.py's qty-delta gate
compares against, so the file is worth keeping between runs rather than
treating as a scratch cache.

Dialect notes
-------------
DuckDB accepts the step SQL as written — filter (where …), string_agg(distinct …
order by …), is distinct from, ::numeric, CTEs, correlated subqueries, on
conflict do update — with exactly one exception, handled in _translate():

    ::float   DuckDB reads this as FLOAT4 (single precision); Postgres's `float`
              is float8. Left alone, `round(merged_price,2)::float` came back as
              128.10000610351562 instead of 128.1, which reached the sync_actions
              audit trail as the recorded new value. Rewritten to ::double,
              which is what Postgres means by float, so both backends record the
              same number.

Anything else that ever needs translating belongs in _translate() with a note,
never in a branch inside a step.
"""

import json
import re
import threading
from datetime import datetime
from pathlib import Path

import config
from core.warehouse import Warehouse, WarehouseError

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations_local"

# `::float` but not `::float4` / `::float8` — see the dialect note above.
_FLOAT_CAST = re.compile(r"::\s*float(?![0-9a-zA-Z_])", re.IGNORECASE)

# Hasura accepts the literal string "now()" as a timestamp value in an insert;
# a DuckDB parameter cannot be a SQL function call, so the same sentinel is
# resolved to a real timestamp on the way in. Callers (steps/catalog.py) pass it.
_NOW_SENTINEL = "now()"


def _translate(sql):
    return _FLOAT_CAST.sub("::double", sql)


class LocalWarehouse(Warehouse):
    name = "local"

    def __init__(self, logger=None, path=None):
        self.path = Path(path or config.LOCAL_WAREHOUSE_PATH)
        self.log  = logger
        self._con = None
        # DuckDB connections are not thread-safe; the dashboard's status thread
        # never touches the warehouse, but guard anyway so a future caller can't
        # corrupt a write by accident.
        self._lock = threading.RLock()

    # ── connection ─────────────────────────────────────────────────────────────
    @property
    def con(self):
        if self._con is None:
            try:
                import duckdb  # noqa: PLC0415
            except ImportError:
                raise WarehouseError(
                    "The local warehouse needs duckdb. Install it with "
                    "`pip install duckdb` (it is in requirements.txt), or set "
                    "SBOT_WAREHOUSE=hasura to use the cloud warehouse instead."
                ) from None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._con = duckdb.connect(str(self.path))
            except Exception as e:
                # DuckDB takes an exclusive lock on the file. The usual cause is
                # a second sync already running, which is worth saying plainly —
                # two concurrent syncs would fight over the same store anyway.
                raise WarehouseError(
                    f"Could not open the local warehouse at {self.path}: {e}. "
                    f"If a sync is already running, wait for it to finish."
                ) from None
        return self._con

    def close(self):
        if self._con is not None:
            self._con.close()
            self._con = None

    # ── identity + readiness ───────────────────────────────────────────────────
    def describe(self):
        return f"DuckDB (local file: {self.path.name})"

    def check_ready(self):
        self.migrate()          # cheap, idempotent, and proves the file is writable

    def migrate(self):
        with self._lock:
            for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
                # DDL here is all `create ... if not exists` / guarded seeds, so
                # replaying every file on every open is both correct and the
                # simplest thing that keeps a long-lived file up to date.
                self.con.execute(sql_file.read_text(encoding="utf-8"))

    # ── SQL ────────────────────────────────────────────────────────────────────
    def run_sql(self, sql, read_only=False):
        with self._lock:
            self.con.execute(_translate(sql))
        return {"result_type": "CommandOk", "result": None}

    def select(self, sql):
        with self._lock:
            cur = self.con.execute(_translate(sql))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def select_one(self, sql):
        rows = self.select(sql)
        return rows[0] if rows else None

    # ── inserts ────────────────────────────────────────────────────────────────
    @staticmethod
    def _coerce(value):
        """Python value → something DuckDB can bind as a parameter."""
        if value == _NOW_SENTINEL:
            return datetime.now()
        if isinstance(value, (dict, list, tuple)):
            # json columns (stats, details) and text[] (product_types). A list
            # bound to a text[] column works natively, so only dicts need
            # encoding; tuples are normalised to lists for the array case.
            if isinstance(value, dict):
                return json.dumps(value, default=str)
            return list(value)
        return value

    def insert_many(self, table, objects, chunk=None):
        if not objects:
            return 0
        chunk = chunk or config.HASURA_INSERT_CHUNK
        # Union of keys, not the first row's keys: a caller that omits an
        # optional column on some rows (brand/size on a feed without them)
        # would otherwise silently write those rows against the wrong columns.
        cols = list(dict.fromkeys(k for obj in objects for k in obj))
        placeholders = ", ".join("?" for _ in cols)
        sql = (f"insert into {table} ({', '.join(cols)}) "
               f"values ({placeholders})")

        total = 0
        with self._lock:
            for i in range(0, len(objects), chunk):
                batch = objects[i:i + chunk]
                rows = [[self._coerce(obj.get(c)) for c in cols] for obj in batch]
                self.con.executemany(sql, rows)
                total += len(rows)
        return total

    # ── run bookkeeping ────────────────────────────────────────────────────────
    def start_run(self, mode, triggered_by, product_types):
        with self._lock:
            row = self.con.execute(
                "insert into pipeline_runs (mode, triggered_by, product_types) "
                "values (?, ?, ?) returning run_id",
                [mode, triggered_by, list(product_types)],
            ).fetchone()
        return int(row[0])

    def insert_step(self, run_id, step):
        try:
            with self._lock:
                row = self.con.execute(
                    "insert into run_steps (run_id, step) values (?, ?) returning id",
                    [run_id, step],
                ).fetchone()
            return int(row[0])
        except Exception as e:
            if self.log:
                self.log.warning(f"run_steps bookkeeping insert failed: {e}")
            return None

    def update_step(self, step_id, status, rows_in, rows_out, error, details):
        if step_id is None:
            return
        try:
            with self._lock:
                self.con.execute(
                    "update run_steps set status = ?, finished_at = ?, rows_in = ?, "
                    "rows_out = ?, error = ?, details = ? where id = ?",
                    [status, datetime.now(), rows_in, rows_out, error,
                     json.dumps(details or {}, default=str), step_id],
                )
        except Exception as e:
            if self.log:
                self.log.warning(f"run_steps bookkeeping update failed: {e}")

    def finalize_run(self, run_id, status, stats, notes):
        try:
            with self._lock:
                self.con.execute(
                    "update pipeline_runs set status = ?, finished_at = ?, stats = ?, "
                    "notes = ? where run_id = ?",
                    [status, datetime.now(), json.dumps(stats or {}, default=str),
                     notes, run_id],
                )
        except Exception as e:
            if self.log:
                self.log.warning(f"pipeline_runs finalize write failed: {e}")

    # ── shopify_products mirror ────────────────────────────────────────────────
    # Column list matches the Hasura backend's update_columns exactly: sku is the
    # conflict key and is never in the SET.
    _MIRROR_COLS = ["sku", "product_id", "variant_id", "inventory_item_id", "handle",
                    "title", "product_type", "tags", "current_price", "current_qty",
                    "refreshed_at", "last_run_id"]

    def upsert_shopify_products(self, rows, chunk=None):
        if not rows:
            return 0
        chunk = chunk or config.HASURA_INSERT_CHUNK
        cols = self._MIRROR_COLS
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "sku")
        sql = (f"insert into shopify_products ({', '.join(cols)}) "
               f"values ({', '.join('?' for _ in cols)}) "
               f"on conflict (sku) do update set {updates}")

        total = 0
        with self._lock:
            for i in range(0, len(rows), chunk):
                batch = rows[i:i + chunk]
                self.con.executemany(
                    sql, [[self._coerce(r.get(c)) for c in cols] for r in batch])
                total += len(batch)
        return total

    def delete_stale_shopify_products(self, run_id):
        with self._lock:
            before = self.con.execute(
                "select count(*) from shopify_products").fetchone()[0]
            # Plain `!=`, NOT `is distinct from`: on a null last_run_id `!=`
            # evaluates to null and the row survives, which is exactly what
            # Hasura's `_neq` does. catalog.py always writes last_run_id so the
            # case should never arise — but the two backends must not disagree
            # about it if it ever does.
            self.con.execute(
                "delete from shopify_products where last_run_id != ?", [run_id])
            after = self.con.execute(
                "select count(*) from shopify_products").fetchone()[0]
        return int(before - after)
