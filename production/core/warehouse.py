#!/usr/bin/env python3
"""The warehouse interface every pipeline step talks to.

There are two implementations and the steps cannot tell them apart:

    core/hasura.py::Hasura          — Hasura Cloud over Neon Postgres (cloud cron)
    core/local_warehouse.py::LocalWarehouse — embedded DuckDB file (dashboard button)

Why this exists
---------------
merge/validate/sync hold the business rules — per-supplier markup inside the
min(), the zero-out cap, the price-swing gate, the diff that decides what
actually gets written to Shopify. Those rules must not have a second
implementation, because a second implementation drifts: the repo has already
been bitten by normalize_sku existing twice and by the markup table existing
twice, and both times the copies diverged silently and mispriced real SKUs.

So the split is at the *storage* layer, not the logic layer. Every SELECT in
steps/ is one string that runs verbatim against both backends (DuckDB speaks
enough Postgres for all of it — see pipelines/tests/test_warehouse.py, which
asserts exactly that). What differs per backend is only how rows are inserted
and how run bookkeeping is recorded, which is what this interface covers.

The four methods that are NOT plain SQL — start_run/insert_step/update_step/
finalize_run, plus the two shopify_products mirror writers — exist because
Hasura reaches those through its auto-generated GraphQL mutations rather than
through /v2/query. They used to be inline GraphQL in run_context.py and
steps/catalog.py, which is what tied those two files to Hasura specifically.
"""

import config


class WarehouseError(Exception):
    """Backend-independent failure talking to the warehouse."""


class Warehouse:
    """Interface only — see the two concrete backends for behaviour.

    A backend must be safe to construct without touching the network, so the
    dashboard can ask which store/warehouse a run *would* use without starting
    one.
    """

    name = "abstract"

    # ── description ────────────────────────────────────────────────────────────
    def describe(self):
        """Short human label for logs and the dashboard, e.g. 'DuckDB (local)'."""
        raise NotImplementedError

    def check_ready(self):
        """Raise WarehouseError if this backend cannot serve a run right now.

        Called by preflight so a misconfigured warehouse is reported before any
        Shopify call, rather than halfway through a run.
        """
        raise NotImplementedError

    def migrate(self):
        """Apply the schema this backend needs. Idempotent."""
        raise NotImplementedError

    # ── SQL ────────────────────────────────────────────────────────────────────
    def run_sql(self, sql, read_only=False):
        raise NotImplementedError

    def select(self, sql):
        """SELECT → list of dicts."""
        raise NotImplementedError

    def select_one(self, sql):
        rows = self.select(sql)
        return rows[0] if rows else None

    def insert_many(self, table, objects, chunk=None):
        """Chunked insert. Returns total rows inserted."""
        raise NotImplementedError

    # ── run bookkeeping ────────────────────────────────────────────────────────
    def start_run(self, mode, triggered_by, product_types):
        """Create the pipeline_runs row. Returns its run_id."""
        raise NotImplementedError

    def insert_step(self, run_id, step):
        """Create a run_steps row. Returns its id, or None if bookkeeping failed
        (bookkeeping is best-effort — the file log is the fallback record — but
        data-path writes never are)."""
        raise NotImplementedError

    def update_step(self, step_id, status, rows_in, rows_out, error, details):
        raise NotImplementedError

    def finalize_run(self, run_id, status, stats, notes):
        raise NotImplementedError

    # ── shopify_products mirror ────────────────────────────────────────────────
    def upsert_shopify_products(self, rows, chunk=None):
        """Upsert mirror rows keyed on sku. Returns rows affected."""
        raise NotImplementedError

    def delete_stale_shopify_products(self, run_id):
        """Drop mirror rows not refreshed by this run. Returns rows deleted."""
        raise NotImplementedError


def get_warehouse(logger=None, backend=None):
    """Construct the configured backend.

    backend defaults to config.WAREHOUSE_BACKEND (env SBOT_WAREHOUSE), which is
    'local' unless explicitly set — see the note in config.py for why this is
    not inferred from whether HASURA_* happens to be present.
    """
    backend = (backend or config.WAREHOUSE_BACKEND).strip().lower()

    if backend == "local":
        from core.local_warehouse import LocalWarehouse
        return LocalWarehouse(logger=logger)
    if backend == "hasura":
        from core.hasura import Hasura
        return Hasura(logger=logger)

    raise WarehouseError(
        f"Unknown warehouse backend {backend!r} (from SBOT_WAREHOUSE). "
        f"Expected 'local' (embedded DuckDB) or 'hasura' (Hasura Cloud/Neon)."
    )
