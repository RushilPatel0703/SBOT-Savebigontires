# Build spec — automated cloud pipeline

**Status:** decided 2026-09-26, for build and deploy the following week.
**Audience:** whoever implements this, starting with no prior context.

This document is self-contained. It states what to build, what already exists, the
facts that will bite you, and what is still undecided. Everything here was either
read out of the repo or measured — where a number appears, it came from a real run
or a real lookup, not an estimate.

---

## 1. The goal

`production/run_weekly.py` has never been deployed anywhere. It works — it has
completed full dry runs against real data — but it only ever runs when a human
clicks a button in a Streamlit dashboard. The job is to put it on a server, on
cron, unattended.

**The Streamlit dashboard's role changes.** It stops being the primary path and
becomes the **manual switch for outages**: when the automated run fails or the
warehouse is unreachable, a human opens the dashboard and gets data out by hand.
That has a direct design consequence, in §6.2 — do not remove the local DuckDB
backend.

**What stays manual regardless:** supplier cookie refresh (§7.4) and the client's
Shopify CSV import path in the dashboard.

---

## 2. Architecture

```
  suppliers (5 portals)
        │  Playwright + session cookies, ~10 h  (§7.1)
        ▼
  ┌─────────────────────────────────────┐
  │  Vultr Seattle VPS                  │
  │  scrapers + run_weekly.py, on cron  │
  └───────────┬──────────────┬──────────┘
              │              │
   prices, inventory,     warehouse tables,
   new products          full per-SKU audit
              │              │
              ▼              ▼
     Shopify Admin      Supabase Postgres
      GraphQL API        schema `pipeline`
              ▲              │
              │              │  read-only view, PHASE 2
   reads today │              ▼
              └────────  Shop OS  (schema `public`, unchanged)
                         staff quoting at physical locations
```

**One run writes two places.** Steps 5–7 push each change to Shopify *and* record
it in Postgres in the same pass, so the store and the audit trail cannot disagree
about what happened. This is not a chain — Postgres does not feed Shopify.

**Shopify is also read.** Step 1 (`catalog`) bulk-exports the live catalog into
the `shopify_products` mirror. Steps 5–7 write back. The relationship is
bidirectional.

**The Shop OS does not move and is not modified.** It is the client's quoting app,
live at physical locations, on Supabase, almost certainly coupled to Supabase
Auth + RLS + PostgREST. We add a schema beside it; we change nothing it owns.

---

## 3. Infrastructure

| Service | Spec | Cost (USD/mo) |
|---|---|---|
| **Vultr, Seattle** | Regular Performance, 2 vCPU / 4 GB / 80 GB SSD / 3 TB | $20 |
| Vultr automatic backups | 20% of plan — the box holds caches that cost hours and money to rebuild | $4 |
| **Supabase Pro** | Upgrade the client's **existing** project. Do not create a new one | $25 |
| Healthchecks.io | Dead-man's switch on the cron | free |
| | **Total** | **$49** |

Conditional, only if a test says so: Supabase IPv4 add-on (~$4) if the pooler will
not serve a session-mode connection; an R2/B2 bucket (<$1) for weekly `pg_dump`.

**Seattle** because the suppliers are western — see §7.2. Do not host in Europe.

**Why a flat-rate VPS and not a container platform or hyperscaler:** runs take ~10
hours and the scraper caches must survive between runs. That rules out Railway,
Render, Fly, Lambda, Cloud Run Jobs and Spot instances — per-minute billing is the
wrong model for 40+ hours a month, and ephemeral disks destroy the caches. AWS and
GCP work but cost 2–3x for a single cron box that uses nothing they sell.

### Supabase layout

- Pipeline tables go in a **separate `pipeline` schema**, never `public`.
  Supabase exposes only `public` through PostgREST by default, so the warehouse
  will not accidentally become a REST API.
- The pipeline connects as a **dedicated Postgres role** with rights on
  `pipeline` only — not `service_role`, not `postgres`.
- Use a **session-mode** connection. The pipeline does bulk inserts and set-based
  SQL; transaction-mode pooling will not serve it. Verify this early; Supabase's
  direct connection is IPv6-only on most tiers.
- **Do not put supplier cost data in `public`.** If the Shop OS has permissive RLS
  and ships an anon key to store terminals, `public` is effectively readable by
  anyone with devtools.

---

## 4. What already exists

```
production/
├── run_weekly.py     orchestrator: --dry-run --type --skip --only --from --force --with-scrape
├── config.py         thresholds + SUPPLIERS ingest mappings. All tunables live here
├── preflight.py      read-only readiness check (store reachable, token scoped, feeds fresh)
├── report.py         run history: report.py --run N
├── core/warehouse.py      the interface every step talks to  ← implement against this
├── core/hasura.py         Hasura/Neon backend                ← DELETE (§6.1)
├── core/local_warehouse.py  embedded DuckDB backend          ← KEEP (§6.2)
├── core/shopify_gql.py    Shopify GraphQL client, cost-aware throttle, bulk ops
├── core/run_context.py    run/step bookkeeping
├── db/migrations/001_initial_schema.sql
└── steps/{catalog,ingest,merge,validate,create_products,sync}.py

pipelines/
├── run_all.py        runs the 5 supplier pipelines. --suppliers <names> --skip-scraping
├── dashboard.py      Streamlit. Step 5 shells out to run_weekly.py --skip create_products
├── refresh_cookies.py
├── shared/markup.py         THE single markup definition. Do not add a second
├── shared/sku_normalize.py  normalize_sku()
└── <supplier>/       scraper + clean + augment per supplier
```

**Warehouse tables** (`001_initial_schema.sql`): `pipeline_runs`, `run_steps`,
`suppliers`, `supplier_staging`, `merged_snapshot`, `shopify_products`,
`sync_actions`, `validation_results`.

**Pipeline steps, in order:**
`catalog → ingest → merge → validate → create_products → sync_prices → sync_inventory`

Exit 0 = success. Non-zero = failed or aborted, nothing written. Cron-friendly.

**Proven working.** Dry run #3 (2026-07-10): 18,752 SKUs mirrored, 17,302 rows
ingested across 9 feeds, 13,671 tire + 1,137 wheel SKUs merged, all error gates
passed, 331 new products previewed, 1,956 price updates + 3,695 qty changes + 144
zero-outs. Whole pipeline, ~2 minutes. **No live run has ever happened.**

---

## 5. Build tasks, in order

### 5.1 Fix the zero-out safety gate — do this first

`config.SHOPIFY_PRODUCT_TYPE` maps `tires → "Vehicle Tires"`. The actual store has
`"Tires"` (7,465 rows) and `""` (5,979). **Nothing matches**, so:

- `sync_inventory`'s zero-out pass finds 0 rows — a product suppliers dropped
  keeps its stock forever.
- `validate.py`'s `zero_out_cap` gate counts an empty set and records **PASS**.

A green gate here means "measured nothing", not "checked and fine". This is the
protection against a broken scrape zeroing the store, and it is currently inert.

**Acceptance:** `select product_type, count(*) from shopify_products group by 1`
reconciles with what the gate measures, and the gate reports a non-zero
denominator on a real run. Check the live store separately — it may differ from
dev.

### 5.2 Postgres warehouse backend

Add `core/postgres_warehouse.py` implementing the `Warehouse` interface with
psycopg, selected by `SBOT_WAREHOUSE=postgres`.

This is a port to the **native** target, not a new dialect: the step SQL was
originally written for Neon Postgres, so it runs essentially unchanged. The DuckDB
backend is the one carrying a translation shim (`local_warehouse._translate()`).

The six non-SQL methods (`start_run`, `insert_step`, `update_step`,
`finalize_run`, `upsert_shopify_products`, `delete_stale_shopify_products`) exist
only because Hasura reached them through generated GraphQL mutations. On plain
Postgres they collapse into ordinary INSERT/UPDATE.

**Rules:** backend-specific code goes in the backend file, never as a branch
inside a step. The merge rules (per-supplier markup inside `min()`) and the safety
gates must not exist in two copies — this repo has been bitten twice by exactly
that.

**Acceptance:** `pipelines/tests/test_warehouse.py` passes against the new
backend, and a `--dry-run` produces the same merged row counts as the DuckDB
backend on the same inputs.

### 5.3 Delete Hasura

Remove `core/hasura.py`, the `SBOT_WAREHOUSE=hasura` branch, `db/migrate.py`'s
Hasura-specific paths (`run_sql` over HTTP, `pg_track_table`), and the
`HASURA_*` env vars. It only ever wrapped Postgres in GraphQL and the project has
been unreachable since 2026-09-07. This is a net simplification.

### 5.4 Migrate schema into `pipeline`

Apply `001_initial_schema.sql` into the `pipeline` schema on Supabase. Create the
dedicated role. Confirm the schema is **not** in Supabase's exposed-schemas list.

### 5.5 Provision the box

Debian, Python venv, `playwright install chromium --with-deps`, xvfb (see §7.4).
No Docker, no Coolify — for one cron job those are layers that can fail without
earning anything.

### 5.6 Move the caches off the git checkout

**This will break weekly if you skip it.** The scrapers write to JSON files that
are **git-tracked** (`pipelines/*/Data/**/*.json` is deliberately not ignored —
they are expensive to rebuild and are versioned on purpose). If you deploy by
`git pull`, the scraper's own writes conflict with every pull.

Put them on a persistent path outside the checkout — symlink or an env-configured
data dir. Affected: `zero_terms_*_cache.json`, `serp_cache.json`,
`image_quality_cache.json`, `serp_ledger.jsonl`.

### 5.7 Cron, one entry per supplier

`run_all.py` walks the five suppliers **sequentially with no retry and no
resume**. If one dies at hour eight, its feed goes stale, the freshness gate trips,
and the entire sync aborts — one bad portal costs the week. `--suppliers` already
exists, so this is config, not code.

```cron
# North Country is ~8 of the 10 hours — give it its own window
0 20 * * 5  cd /opt/sbot/pipelines && python run_all.py --suppliers nc            >> logs/nc.log 2>&1
0 20 * * 6  cd /opt/sbot/pipelines && python run_all.py --suppliers formula wheelsmart emerge dttire >> logs/others.log 2>&1
0  8 * * 0  cd /opt/sbot/production && python run_weekly.py                        >> logs/sync.log 2>&1
```

The sync itself is minutes, not hours. It is not part of the ten.

### 5.8 Alerting

Wire a Healthchecks.io ping into `run_weekly.py`'s finalize hook (the hook point
already exists). A weekly unattended job that silently stops firing is the failure
that goes unnoticed for a month.

### 5.9 Off-box backup

Supabase Pro keeps daily backups for **7 days**. This codebase's characteristic
bug hides for longer than that — a bad spec shipped on 2,433 rows before anyone
noticed. Pipeline data is reconstructible per `run_id`, but the Shop OS's quote
data is not. Weekly `pg_dump` to R2/B2, under $1/mo. Cheaper and better suited
than Supabase's PITR add-on.

---

## 6. Two things not to do

### 6.1 Do not port Hasura
See §5.3. Delete it.

### 6.2 Do not remove `core/local_warehouse.py`

The DuckDB backend is the dashboard's **offline escape hatch**, and that matters
more now that the dashboard is the designated outage switch. If the outage *is*
Supabase being unreachable, a dashboard pointed at Postgres is equally dead. Keep
DuckDB as the local fallback so the manual path survives a warehouse outage.

`SBOT_WAREHOUSE` should end up with two values: `postgres` (the cron, on the box)
and `local` (the dashboard's offline mode).

---

## 7. Facts that will bite you

### 7.1 The ten hours are almost entirely North Country

`pipelines/northcountry/scraper_tires.py` builds a cartesian search grid:

| Segment | Count |
|---|---|
| Metric (59 widths × 16 ratios) | 944 |
| LT-prefix (35 × 9) | 315 |
| ST trailer (25 × 7) | 175 |
| Flotation | 18 |
| **Total grid** | **1,452** |

Every term hitting the 75-result API cap re-searches across 22 rim diameters plus
LT and speed-letter drilldowns, so one capped term fans out into dozens of calls.

`MAX_WORKERS = 1` is **load-bearing**, not an oversight — the comment states the
server cannot handle concurrent load without timing out. This cannot be
parallelized away.

**The zero-terms cache is the only reason it is 10 hours and not 40.**
`zero_terms_tires_cache.json` held 1,139 known-dead terms as of 2026-09-26,
cutting the run to ~313 productive terms. Lose it and every run is an effective
`--full` rebuild that will not fit a weekly window. See §5.6.

`brand_sweep.py` runs unconditionally as a coverage net, so a stale zero-terms
cache is not a correctness risk.

### 7.2 Where the suppliers actually are

Measured 2026-09-26 by DNS + ARIN RDAP + IP geolocation:

| Supplier | Host | Location |
|---|---|---|
| North Country (~8 h) | Shaw business line, own /29, self-hosted | **Edmonton, AB** |
| Emerge | **Telus HSIA — consumer broadband** | Surrey / Vancouver, BC |
| WheelMart | **Shaw broadband** | Vancouver, BC |
| Formula | PrivateSystems Networks | Tukwila, WA |
| DT Tire | Azure `canadacentral` | Toronto |

**None of the five sits behind a CDN, WAF or bot-management product.** Every
domain resolves straight to an origin IP. These are small self-hosted Costar
WebOrder installations — the vendor markets it as maintainable without "expensive
IT support". There is also almost certainly **no IP allowlist**, since the scraper
works today from a dynamic residential IP.

### 7.3 Judge hosting on tail latency, not average

`api_post` uses `timeout=(10, 90)` with 4 retries and exponential backoff. **One
call crossing the 90-second read timeout costs ~45x a normal call** — up to ~6
minutes for a single search. Mean round-trip differences are noise; the tail is
what turns minutes into hours, against servers already documented as
timeout-prone. This is why Europe was ruled out and the Pacific Northwest chosen.

### 7.4 Cookie refresh needs a real browser

`pipelines/emergetires/refresh_session.py:38` hardcodes `headless=False` because
the Costar SPA only fires its login API on a genuine click. Use xvfb in the
container. **Expect this to be the first thing that breaks.** It is the least
automated link in the chain and remains a manual risk on a headless box.

### 7.5 Emerge SKUs carry a prefix

Emerge's augment step adds `EMRG-` before Shopify import, so the store holds
`EMRG-…` SKUs while cleaned CSVs are unprefixed. `config.SUPPLIERS` handles this
with `"sku_prefix": "EMRG-"`, applied idempotently at ingest. Without it, Emerge
products never sync and `create_products` makes ~609 duplicates.

### 7.6 Join keys

Numeric-looking SKUs normalize as `str(int(float(x)))` — `"12345.0"` → `"12345"`.
`pipelines/shared/sku_normalize.py::normalize_sku` is the single definition. Do
not add a second. A normalizer existing in two places has already mispriced real
SKUs here.

### 7.7 Markup has exactly one home

`pipelines/shared/markup.py`, fed by `markup_overrides.json` which the client
edits in the dashboard. Applied in `merge` **inside** `min()`, so
`merged_snapshot.merged_price` is the FINAL selling price. Nothing downstream may
add markup again. A markup file that exists but will not parse is a hard failure,
deliberately — pricing off a stale table is worse than stopping.

---

## 8. Safety gates

`validate` aborts the run before **either** write if an error-severity gate fails.

1. Supplier CSV freshness ≤ `MAX_CSV_AGE_DAYS` (checked at ingest)
2. Per-supplier minimum row counts
3. Total merged qty change vs last successful run ≤ ±`MAX_TOTAL_QTY_DELTA_PCT` (40%)
4. Zero-out cap ≤ `MAX_ZERO_OUT_PCT` of catalog (30%) — **currently inert, §5.1**
5. Price bounds + per-SKU swing warnings
6. Sync circuit breaker: abort if failure rate > `MAX_SYNC_FAILURE_PCT`

`--force` overrides after human review. All thresholds live at the top of
`config.py`; change them there, not in step code.

**When checking any completeness or approval figure, call the executing function
directly and diff it against the reported number.** The layer that reports a gap
and the layer that acts on it are separate code paths here and have silently
disagreed three times.

---

## 9. Open decisions — needed before first live run

| # | Decision | Why it blocks |
|---|---|---|
| 1 | **Dev store or live store**, and an Admin API token for it | Everything points at `database-testing-2`. Token needs `read_products`, `write_products`, `read_inventory`, `write_inventory`, `read_locations` |
| 2 | Add **`read_publications` + `write_publications`** to the Shopify app | Without them `create_products` makes products ACTIVE but invisible in the Online Store channel. The step warns and continues |
| 3 | **May SerpAPI enrichment run inside the cron?** | Metered quota needs per-run approval by standing rule; a cron cannot ask. Either exclude enrichment, or give it a hard budget cap + alerting and treat the cap as the approval. An enrichment step has already been wired into a default step list once and spent searches silently |
| 4 | ~~Does the Shop OS write inventory to Shopify?~~ **RESOLVED 2026-09-28: No.** | Verified audit found no active Shop OS catalog writer to Shopify. Shopify order intake is webhook-driven; the owner commerce function reports `SHOPIFY_SYNC_ENGINE_NOT_ENABLED`. No two-writer conflict on inventory fields. |
| 5 | ~~Supabase project region + session-mode connection~~ **PARTIALLY RESOLVED 2026-09-28.** | Region is `ca-central-1`, Postgres 17.6, database ~215 MB. Session-mode pooler support was **not tested** by the audit (read-only scope) — still verify before build task 5.4. |

Items 4 and 5 were answered by the Shop OS integration brief (`SHOPOS_INTEGRATION_BRIEF.md`) — verified audit response 2026-09-28.

### 9.1 New blocker from the Shop OS audit — fix before adding more cost data

**`shopify-demo-smoke-public`**, a Supabase Edge Function in the Shop OS's project, has JWT
verification disabled, uses service-role access internally, allows public CORS, and returns
supplier `wholesale_cost` / `dealer_cost` in its JSON response. That is live, unauthenticated
exposure of what the business pays its suppliers. Lock down or retire that function **before**
task 5.4 (migrating `001_initial_schema.sql` into the shared `pipeline` schema) — the whole
point of the `pipeline` schema / dedicated-role isolation in §3 is defeated if an unrelated
public endpoint on the same project can read cost data straight out of service-role access.

### 9.2 New facts for Phase 2 (§11) — not a blocker for this build, but will shape the view

- Shop OS's tire identity is `(provider, product_key)`, not normalized SKU. The `pipeline`
  schema's `live_pricing` view will need an explicit normalized-SKU ↔ `(provider,
  product_key)` bridge — Shop OS cannot join on SKU alone as things stand.
- Shop OS already applies its own markup/freight/levy/tax on top of whatever it reads. Phase 2
  must expose pipeline cost and pipeline markup as **separate** fields (not just the blended
  `merged_snapshot.merged_price`), or Shop OS's markup stacks on top of ours — the exact
  double-markup failure mode §7.7 already guards against internally.
- No table-name collisions in `public`; `pipeline` schema does not exist yet. §3's plan to
  keep pipeline tables out of `public` is unaffected.

---

## 10. How to know it worked

**Before anything else — the IP test.** Stand the box up, run one scraper against
one supplier. Blocking is unlikely (§7.2), so what you are really checking is that
the **session survives the whole run**, not just authenticates. Watch
`CostarSessionGuard`'s re-auth log lines. A cookie that works for forty minutes and
then dies looks like success at first and costs you the run.

Then, in order:

1. `python preflight.py` — store reachable, token scoped, feeds fresh, location unambiguous
2. `python run_weekly.py --dry-run` — full preview, nothing written
3. Compare merged row counts against the §4 dry-run #3 figures. Large divergence means something upstream changed
4. Confirm gate 4 now reports a **non-zero denominator** (§5.1)
5. One live run, watched. Then `report.py --run N` and
   `select action, count(*) from pipeline.sync_actions where run_id = N group by 1`
6. Spot-check a handful of SKUs in the store against `merged_snapshot`

---

## 11. Deliberately out of scope

**Phase 2 — the Shop OS reading a `live_pricing` view — is not part of this
build.** Deploy the pipeline writing into a `pipeline` schema that nothing reads
yet. Let it run once or twice. Watch the numbers. Only then wire quoting to it.

The reason is blast radius: today a bad run means wrong prices on the storefront.
Once quoting depends on pipeline data, a bad run means **staff cannot sell**. Do
not connect those until the gates have been proven against real data — which, per
§5.1, they have not been.

Also out of scope: fixing the Shop OS's own bugs, automating cookie refresh, and
any change to the dashboard beyond keeping its offline path working (§6.2).
