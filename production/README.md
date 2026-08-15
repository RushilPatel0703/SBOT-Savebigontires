# Production Pipeline (GraphQL / Cloud)

The production-ready weekly sync for savebigontires.com. Replaces the old REST
sync (`pipelines/merged/` + `pipelines/shared/build_sku_map.py`) with a
GraphQL-only, warehouse-backed pipeline designed for unattended runs.

```
supplier CSVs (existing pipelines/<supplier>/ scrape+clean — the raw layer)
      │  ingest
      ▼
Neon Postgres  ◄── Hasura GraphQL (the only DB access path)
  supplier_staging → merged_snapshot → validation gates → diff vs shopify_products
      │  sync (only changed values)
      ▼
Shopify GraphQL Admin API
  • catalog:   bulkOperationRunQuery (full product export, JSONL — no REST pagination)
  • creation:  productSet + publishablePublish (new SKUs, from Shopify_SEO CSVs)
  • prices:    productVariantsBulkUpdate
  • inventory: inventorySetQuantities (250 per call)
```

## Why this design
- **Diff-based sync.** The old sync PUT every SKU with a 0.5 s sleep (~4 h for 25k
  SKUs). Here the merged snapshot is joined against a live catalog mirror in SQL
  and only differences are pushed — a steady-state week is minutes, not hours.
- **Safety gates before any write.** A broken scrape must never zero the store.
  The validate step aborts the run (exit 1, nothing written) if feeds are stale,
  suspiciously small, total qty swings > 40 %, or > 30 % of the catalog would be
  zeroed. `--force` overrides after human review.
- **Full audit.** Every run, step, validation check, and per-SKU change (old →
  new value, applied/failed/dry_run) is recorded in the warehouse. "What changed
  last Tuesday and why" is one SQL query away.
- **Idempotent + resumable.** Staging and snapshots are immutable per `run_id`;
  re-running a step re-computes from the same inputs. `inventorySetQuantities`
  sets absolute values, so replays are safe.

## Setup
```bash
cd production
pip install -r requirements.txt
cp .env.example .env        # fill in Hasura + Shopify credentials
python db/migrate.py        # apply schema to Neon + track tables in Hasura
```

## Running
```bash
python run_weekly.py --dry-run          # ALWAYS first: preview all changes
python run_weekly.py                    # live run
python run_weekly.py --type tires       # tires only
python run_weekly.py --with-scrape      # scrape suppliers first (pipelines/run_all.py)
python run_weekly.py --only validate --force   # re-check gates / override
```
Steps: `catalog → ingest → merge → validate → create_products → sync_prices → sync_inventory`
(`--skip`, `--only`, `--from` compose as in the old pipelines).

Exit code 0 = success; non-zero = failed/aborted (cron-friendly).

## Weekly cron on the Oracle VM
```cron
# Sundays 02:00 — scrape everything, then validated sync
0 2 * * 0  cd /opt/savebigontires/production && /usr/bin/python3 run_weekly.py --with-scrape >> logs/cron.log 2>&1
```
Notes for the VM build-out:
- Scrapers need Playwright Chromium (`playwright install chromium --with-deps`)
  and fresh supplier cookies (`pipelines/refresh_cookies.py`). Cookie refresh on a
  headless VM is the one remaining manual risk — see BUILD_CHECKPOINT.md.
- When Postgres/Hasura move from Neon/Hasura-Cloud to Docker on the VM, only
  `.env` changes. No code changes.
- New products ARE created by this pipeline: SKUs in the merged snapshot but not
  in the store are created via `productSet` from the latest `Shopify_SEO_*.csv`
  per supplier and published to the Online Store channel, capped at
  `MAX_NEW_PRODUCTS_PER_RUN`. Publishing requires the `read_publications` +
  `write_publications` scopes on the app token — without them products are
  created ACTIVE but not visible in the Online Store channel (the step warns).
  SKUs with no SEO definition are counted in `create_products.no_seo_definition`
  and left for the next run's fresh SEO files.

## Operational queries (Hasura console or run_sql)
```sql
-- last runs
select run_id, started_at, status, mode, stats from pipeline_runs order by run_id desc limit 5;

-- why did run N abort?
select check_name, severity, passed, details from validation_results where run_id = N and not passed;

-- everything that changed in run N
select action, count(*), count(*) filter (where status='failed') as failed
from sync_actions where run_id = N group by action;

-- price history of one SKU
select run_id, action, old_value, new_value, status, created_at
from sync_actions where sku = 'XXXX' order by id desc;
```

## Housekeeping
Neon free tier is 0.5 GB. `supplier_staging` grows ~30–50k rows/week; prune old
runs occasionally (keeps audit + snapshots for recent history):
```sql
delete from pipeline_runs where started_at < now() - interval '90 days';
-- cascades to run_steps, supplier_staging, merged_snapshot, sync_actions, validation_results
```

## Thresholds
All tunables live at the top of `config.py` (markup, freshness, qty-delta,
zero-out cap, price bounds, circuit breaker). Change them there — not in step code.
