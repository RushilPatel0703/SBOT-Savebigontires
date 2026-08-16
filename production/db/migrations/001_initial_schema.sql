-- 001: initial warehouse schema for the production pipeline.
-- All tables live in public schema (dedicated Neon database) so Hasura root
-- fields stay clean (insert_supplier_staging, pipeline_runs, ...).

-- ── Run bookkeeping ───────────────────────────────────────────────────────────
create table if not exists pipeline_runs (
    run_id       bigint generated always as identity primary key,
    started_at   timestamptz not null default now(),
    finished_at  timestamptz,
    status       text not null default 'running',   -- running | success | failed | aborted
    mode         text not null default 'live',      -- live | dry_run
    triggered_by text,
    product_types text[] not null default '{tires,wheels}',
    stats        jsonb not null default '{}'::jsonb,
    notes        text
);

create table if not exists run_steps (
    id          bigint generated always as identity primary key,
    run_id      bigint not null references pipeline_runs(run_id) on delete cascade,
    step        text not null,
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    status      text not null default 'running',    -- running | success | failed | skipped
    rows_in     integer,
    rows_out    integer,
    error       text,
    details     jsonb not null default '{}'::jsonb
);
create index if not exists run_steps_run_idx on run_steps(run_id);

-- ── Reference ─────────────────────────────────────────────────────────────────
create table if not exists suppliers (
    code   text primary key,
    name   text not null,
    active boolean not null default true
);

insert into suppliers (code, name) values
    ('formula',      'Formula Distributors'),
    ('northcountry', 'North Country Tire'),
    ('wheelsmart',   'WheelMart Canada'),
    ('emerge',       'Emerge Tires'),
    ('dttire',       'DT Tire')
on conflict (code) do nothing;

-- ── Staging: normalized supplier rows, immutable per run ─────────────────────
create table if not exists supplier_staging (
    id           bigint generated always as identity primary key,
    run_id       bigint not null references pipeline_runs(run_id) on delete cascade,
    supplier     text not null references suppliers(code),
    product_type text not null,                      -- tires | wheels
    sku          text not null,
    price        numeric(10,2),
    qty          integer not null default 0,
    brand        text,
    size         text,
    source_file  text,
    loaded_at    timestamptz not null default now()
);
create index if not exists supplier_staging_run_idx on supplier_staging(run_id, product_type);
create index if not exists supplier_staging_sku_idx on supplier_staging(sku);

-- ── Merged snapshot: cross-supplier source of truth per run ──────────────────
create table if not exists merged_snapshot (
    run_id       bigint not null references pipeline_runs(run_id) on delete cascade,
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
    refreshed_at      timestamptz not null default now(),
    last_run_id       bigint
);
create index if not exists shopify_products_type_idx on shopify_products(product_type);

-- ── Audit: every intended/applied change to Shopify ──────────────────────────
create table if not exists sync_actions (
    id         bigint generated always as identity primary key,
    run_id     bigint not null references pipeline_runs(run_id) on delete cascade,
    sku        text not null,
    action     text not null,       -- price_update | inventory_set | inventory_zero
    old_value  text,
    new_value  text,
    status     text not null,       -- applied | dry_run | failed | skipped
    error      text,
    created_at timestamptz not null default now()
);
create index if not exists sync_actions_run_idx on sync_actions(run_id);
create index if not exists sync_actions_sku_idx on sync_actions(sku);

-- ── Validation gate results ───────────────────────────────────────────────────
create table if not exists validation_results (
    id         bigint generated always as identity primary key,
    run_id     bigint not null references pipeline_runs(run_id) on delete cascade,
    check_name text not null,
    severity   text not null default 'error',   -- error | warning
    passed     boolean not null,
    details    jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);
create index if not exists validation_results_run_idx on validation_results(run_id);
