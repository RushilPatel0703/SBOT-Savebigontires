#!/usr/bin/env python3
"""Hasura client — the pipeline's only path to the database.

Two surfaces:
  graphql()  — /v1/graphql for typed inserts/updates/queries (tracked tables)
  run_sql()  — /v2/query for migrations and set-based SQL (merge, diffs, checks)

Both retry with exponential backoff on network errors and 5xx responses.
"""

import json
import time

import requests

import config


class HasuraError(Exception):
    pass


class Hasura:
    def __init__(self):
        self.graphql_url  = config.HASURA_GRAPHQL_ENDPOINT
        self.query_url    = f"{config.HASURA_BASE_URL}/v2/query"
        self.metadata_url = f"{config.HASURA_BASE_URL}/v1/metadata"
        self.source       = config.HASURA_SOURCE
        self.headers = {
            "x-hasura-admin-secret": config.HASURA_ADMIN_SECRET,
            "Content-Type": "application/json",
        }

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
