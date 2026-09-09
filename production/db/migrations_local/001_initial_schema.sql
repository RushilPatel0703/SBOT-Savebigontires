-- 001 (local/DuckDB): the same warehouse schema as db/migrations/001_initial_schema.sql,
-- expressed in the dialect DuckDB accepts. Table names, column names, column
-- order and semantics are deliberately identical, because every SELECT in
-- steps/ is one string run verbatim against both backends.
--
-- Only three things differ, none of which the step SQL can observe:
--
--   1. identity columns  → DuckDB has no `generated always as identity`, so
--      run_id / id come from explicit sequences via a default.
--   2. timestamptz       → plain `timestamp`. DuckDB can only *return* a
--      timestamptz to Python if pytz is installed, and a local bookkeeping
--      clock has no use for a zone the Postgres side isn't storing per-row
--      either (Neon runs UTC). Values are written in local wall-clock time and
--      only ever read back for display.
--   3. jsonb             → `json`. DuckDB's json is a text-backed type; the
--      pipeline only ever writes and reads whole documents (stats, details),
--      never queries inside them, so the binary form buys nothing.
--
-- Foreign keys are kept: DuckDB enforces them, and they are what stops a
-- half-written run from leaving orphaned staging rows behind.

create sequence if not exists pipeline_runs_run_id_seq start 1;
create sequence if not exists run_steps_id_seq start 1;
create sequence if not exists supplier_staging_id_seq start 1;
create sequence if not exists sync_actions_id_seq start 1;
create sequence if not exists validation_results_id_seq start 1;

-- ── Run bookkeeping ───────────────────────────────────────────────────────────
create table if not exists pipeline_runs (
    run_id        bigint primary key default nextval('pipeline_runs_run_id_seq'),
    started_at    timestamp not null default now(),
    finished_at   timestamp,
    status        text not null default 'running',   -- running | success | failed | aborted
    mode          text not null default 'live',      -- live | dry_run
    triggered_by  text,
    product_types text[] not null default ['tires', 'wheels'],
    stats         json not null default '{}',
    notes         text
);

create table if not exists run_steps (
    id          bigint primary key default nextval('run_steps_id_seq'),
    run_id      bigint not null references pipeline_runs(run_id),
    step        text not null,
    started_at  timestamp not null default now(),
    finished_at timestamp,
    status      text not null default 'running',    -- running | success | failed | skipped
    rows_in     integer,
    rows_out    integer,
    error       text,
    details     json not null default '{}'
);

-- ── Reference ─────────────────────────────────────────────────────────────────
create table if not exists suppliers (
    code   text primary key,
    name   text not null,
    active boolean not null default true
);

-- DuckDB has no `on conflict do nothing` for a plain insert..values with no
-- index specified, so seed only what is missing.
insert into suppliers (code, name)
select * from (values
    ('formula',      'Formula Distributors'),
    ('northcountry', 'North Country Tire'),
    ('wheelsmart',   'WheelMart Canada'),
    ('emerge',       'Emerge Tires'),
    ('dttire',       'DT Tire')
) as seed(code, name)
where seed.code not in (select code from suppliers);

-- ── Staging: normalized supplier rows, immutable per run ─────────────────────
create table if not exists supplier_staging (
    id           bigint primary key default nextval('supplier_staging_id_seq'),
    run_id       bigint not null references pipeline_runs(run_id),
    supplier     text not null,
    product_type text not null,                      -- tires | wheels
    sku          text not null,
    price        numeric(10,2),
    qty          integer not null default 0,
    brand        text,
    size         text,
    source_file  text,
    loaded_at    timestamp not null default now()
);

-- ── Merged snapshot: cross-supplier source of truth per run ──────────────────
create table if not exists merged_snapshot (
    run_id       bigint not null references pipeline_runs(run_id),
    product_type text not null,
    sku          text not null,
    merged_price numeric(10,2),                     -- FINAL selling price: min(cost + that
                                                    -- supplier's markup). Markup is per supplier,
                                                    -- so it is applied inside the min() at merge —
                                                    -- do NOT add markup again downstream.
    merged_qty   integer not null,
    sources      text not null,
    primary key (run_id, product_type, sku)
);

-- ── Shopify catalog mirror (replaces sku_map.json / handle_map.json) ──────────
create table if not exists shopify_products (
    sku               text primary key,
    product_id        text not null,
    variant_id        text not null,
    inventory_item_id text not null,
    handle            text,
    title             text,
    product_type      text,
    tags              text,
    current_price     numeric(10,2),
    current_qty       integer,
    refreshed_at      timestamp not null default now(),
    last_run_id       bigint
);

-- ── Audit: every intended/applied change to Shopify ──────────────────────────
create table if not exists sync_actions (
    id         bigint primary key default nextval('sync_actions_id_seq'),
    run_id     bigint not null references pipeline_runs(run_id),
    sku        text not null,
    action     text not null,       -- price_update | inventory_set | inventory_zero
    old_value  text,
    new_value  text,
    status     text not null,       -- applied | dry_run | failed | skipped
    error      text,
    created_at timestamp not null default now()
);

-- ── Validation gate results ───────────────────────────────────────────────────
create table if not exists validation_results (
    id         bigint primary key default nextval('validation_results_id_seq'),
    run_id     bigint not null references pipeline_runs(run_id),
    check_name text not null,
    severity   text not null default 'error',   -- error | warning
    passed     boolean not null,
    details    json not null default '{}',
    created_at timestamp not null default now()
);
