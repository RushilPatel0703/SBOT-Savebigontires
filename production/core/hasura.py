#!/usr/bin/env python3
"""Hasura client — the cloud warehouse backend (Hasura Cloud over Neon Postgres).

Two surfaces:
  graphql()  — /v1/graphql for typed inserts/updates/queries (tracked tables)
  run_sql()  — /v2/query for migrations and set-based SQL (merge, diffs, checks)

Both retry with exponential backoff on network errors and 5xx responses.

Implements core/warehouse.py::Warehouse, so the pipeline steps run against this
or against the embedded DuckDB backend without knowing which. The bookkeeping
and shopify_products methods at the bottom hold GraphQL that used to sit inline
in core/run_context.py and steps/catalog.py; it is unchanged, only relocated so
those two files stop being Hasura-specific.
"""

import json
import time

import requests

import config
from core.warehouse import Warehouse, WarehouseError


class HasuraError(Exception):
    pass


class Hasura(Warehouse):
    name = "hasura"

    def __init__(self, logger=None):
        self.graphql_url  = config.HASURA_GRAPHQL_ENDPOINT
        self.query_url    = f"{config.HASURA_BASE_URL}/v2/query"
        self.metadata_url = f"{config.HASURA_BASE_URL}/v1/metadata"
        self.source       = config.HASURA_SOURCE
        self.log          = logger
        self.headers = {
            "x-hasura-admin-secret": config.HASURA_ADMIN_SECRET,
            "Content-Type": "application/json",
        }

    # ── warehouse interface: identity + readiness ──────────────────────────────
    def describe(self):
        host = self.graphql_url.split("//", 1)[-1].split("/", 1)[0] or "(unset)"
        return f"Hasura/Neon ({host})"

    def check_ready(self):
        if not config.HASURA_GRAPHQL_ENDPOINT or not config.HASURA_ADMIN_SECRET:
            raise WarehouseError(
                "SBOT_WAREHOUSE=hasura but HASURA_GRAPHQL_ENDPOINT / "
                "HASURA_ADMIN_SECRET are not set in production/.env.")
        try:
            self.graphql("{ __typename }")
        except HasuraError as e:
            raise WarehouseError(f"Hasura is not reachable: {str(e)[:300]}") from None

    def migrate(self):
        """Schema for this backend is applied by db/migrate.py (it also tracks
        tables in Hasura metadata, which has no local equivalent)."""
        from db import migrate as _migrate   # noqa: PLC0415
        _migrate.main()

    # ── low-level ──────────────────────────────────────────────────────────────
    def _post(self, url, payload):
        last_err = None
        for attempt in range(config.HASURA_RETRIES):
            try:
                r = requests.post(url, headers=self.headers, json=payload,
                                  timeout=config.HASURA_TIMEOUT)
            except requests.RequestException as e:
                last_err = str(e)
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 500:
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                time.sleep(2 ** attempt)
                continue
            if not r.ok:
                raise HasuraError(f"HTTP {r.status_code} on {url}: {r.text[:500]}")
            return r.json()
        raise HasuraError(f"Hasura unreachable after {config.HASURA_RETRIES} attempts: {last_err}")

    # ── GraphQL ────────────────────────────────────────────────────────────────
    def graphql(self, query, variables=None):
        body = self._post(self.graphql_url, {"query": query, "variables": variables or {}})
        if body.get("errors"):
            raise HasuraError(f"GraphQL errors: {json.dumps(body['errors'])[:800]}")
        return body["data"]

    def insert_many(self, table, objects, chunk=None):
        """Chunked insert into a tracked table. Returns total affected rows."""
        chunk = chunk or config.HASURA_INSERT_CHUNK
        mutation = f"""
        mutation Insert($objects: [{table}_insert_input!]!) {{
            insert_{table}(objects: $objects) {{ affected_rows }}
        }}"""
        total = 0
        for i in range(0, len(objects), chunk):
            data = self.graphql(mutation, {"objects": objects[i:i + chunk]})
            total += data[f"insert_{table}"]["affected_rows"]
        return total

    # ── SQL over /v2/query ─────────────────────────────────────────────────────
    def run_sql(self, sql, read_only=False):
        """Run raw SQL against the Neon source. Returns Hasura result payload:
        {"result_type": "TuplesOk", "result": [[col, ...], [val, ...], ...]}
        """
        return self._post(self.query_url, {
            "type": "run_sql",
            "args": {"source": self.source, "sql": sql, "read_only": read_only},
        })

    def select(self, sql):
        """run_sql SELECT → list of dicts."""
        payload = self.run_sql(sql, read_only=True)
        rows = payload.get("result") or []
        if len(rows) < 2:
            return []
        header = rows[0]
        return [dict(zip(header, r)) for r in rows[1:]]

    def select_one(self, sql):
        rows = self.select(sql)
        return rows[0] if rows else None

    # ── warehouse interface: run bookkeeping ───────────────────────────────────
    # Relocated verbatim from core/run_context.py. Bookkeeping failures are
    # logged and swallowed (the file log is the fallback record); the run
    # itself continues, matching the previous behaviour exactly.
    def start_run(self, mode, triggered_by, product_types):
        return self.graphql(
            """
            mutation($mode: String!, $by: String, $types: [String!]!) {
              insert_pipeline_runs_one(object: {
                mode: $mode, triggered_by: $by, product_types: $types
              }) { run_id started_at }
            }
            """,
            {"mode": mode, "by": triggered_by, "types": list(product_types)},
        )["insert_pipeline_runs_one"]["run_id"]

    def insert_step(self, run_id, step):
        try:
            return self.graphql(
                """
                mutation($run_id: bigint!, $step: String!) {
                  insert_run_steps_one(object: {run_id: $run_id, step: $step}) { id }
                }""",
                {"run_id": run_id, "step": step},
            )["insert_run_steps_one"]["id"]
        except HasuraError as e:
            if self.log:
                self.log.warning(f"run_steps bookkeeping insert failed: {e}")
            return None

    def update_step(self, step_id, status, rows_in, rows_out, error, details):
        if step_id is None:
            return
        try:
            self.graphql(
                """
                mutation($id: bigint!, $set: run_steps_set_input!) {
                  update_run_steps_by_pk(pk_columns: {id: $id}, _set: $set) { id }
                }""",
                {"id": step_id, "set": {
                    "status": status, "finished_at": "now()",
                    "rows_in": rows_in, "rows_out": rows_out,
                    "error": error, "details": details or {},
                }},
            )
        except HasuraError as e:
            if self.log:
                self.log.warning(f"run_steps bookkeeping update failed: {e}")

    def finalize_run(self, run_id, status, stats, notes):
        try:
            self.graphql(
                """
                mutation($id: bigint!, $set: pipeline_runs_set_input!) {
                  update_pipeline_runs_by_pk(pk_columns: {run_id: $id}, _set: $set) { run_id }
                }""",
                {"id": run_id, "set": {
                    "status": status, "finished_at": "now()",
                    "stats": stats, "notes": notes,
                }},
            )
        except HasuraError as e:
            if self.log:
                self.log.warning(f"pipeline_runs finalize write failed: {e}")

    # ── warehouse interface: shopify_products mirror ───────────────────────────
    # Relocated verbatim from steps/catalog.py.
    def upsert_shopify_products(self, rows, chunk=None):
        mutation = """
        mutation($objects: [shopify_products_insert_input!]!) {
          insert_shopify_products(
            objects: $objects,
            on_conflict: {
              constraint: shopify_products_pkey,
              update_columns: [product_id, variant_id, inventory_item_id, handle,
                               title, product_type, tags, current_price, current_qty,
                               refreshed_at, last_run_id]
            }
          ) { affected_rows }
        }"""
        chunk = chunk or config.HASURA_INSERT_CHUNK
        upserted = 0
        for i in range(0, len(rows), chunk):
            data = self.graphql(mutation, {"objects": rows[i:i + chunk]})
            upserted += data["insert_shopify_products"]["affected_rows"]
        return upserted

    def delete_stale_shopify_products(self, run_id):
        return self.graphql(
            """
            mutation($run_id: bigint!) {
              delete_shopify_products(where: {last_run_id: {_neq: $run_id}}) { affected_rows }
            }""",
            {"run_id": run_id},
        )["delete_shopify_products"]["affected_rows"]

    # ── Metadata ───────────────────────────────────────────────────────────────
    def track_table(self, table, schema="public"):
        payload = {
            "type": "pg_track_table",
            "args": {"source": self.source, "table": {"schema": schema, "name": table}},
        }
        try:
            self._post(self.metadata_url, payload)
            return "tracked"
        except HasuraError as e:
            if "already-tracked" in str(e) or "already tracked" in str(e):
                return "already tracked"
            raise


def sql_str(value):
    """Escape a python value as a SQL literal (single-quoted)."""
    if value is None:
        return "null"
    return "'" + str(value).replace("'", "''") + "'"
