#!/usr/bin/env python3
"""Apply SQL migrations to Neon through Hasura and track tables in Hasura metadata.

Idempotent: applied migrations are recorded in schema_migrations; tracking an
already-tracked table is ignored.

Usage (from production/):
    python db/migrate.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.hasura import Hasura

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

TRACKED_TABLES = [
    "pipeline_runs",
    "run_steps",
    "suppliers",
    "supplier_staging",
    "merged_snapshot",
    "shopify_products",
    "sync_actions",
    "validation_results",
]


def main():
    hasura = Hasura()

    hasura.run_sql(
        """
        create table if not exists schema_migrations (
            version    text primary key,
            applied_at timestamptz not null default now()
        );
        """
    )

    applied = {
        row[0]
        for row in hasura.run_sql("select version from schema_migrations;", read_only=True)["result"][1:]
    }

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.stem
        if version in applied:
            print(f"skip    {version} (already applied)")
            continue
        print(f"apply   {version} ...")
        sql = path.read_text(encoding="utf-8")
        hasura.run_sql(sql)
        hasura.run_sql(f"insert into schema_migrations (version) values ('{version}');")
        print(f"ok      {version}")

    print("tracking tables in Hasura metadata ...")
    for table in TRACKED_TABLES:
        result = hasura.track_table(table)
        print(f"  {table}: {result}")

    print("done.")


if __name__ == "__main__":
    main()
