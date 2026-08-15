# Production Pipeline — Build Checkpoint

Cloud-ready, GraphQL-only weekly sync pipeline for savebigontires.com.
Lives in `savebigontires_client/production/`. Target runtime: Oracle Cloud VM (cron, weekly).

## Stack
- **Neon Postgres** — warehouse (runs, staging, snapshots, audit, validation)
- **Hasura Cloud** — GraphQL API over Neon (`SBOTPipelineData` source); pipeline talks to DB *only* through Hasura (GraphQL + `/v2/query run_sql` for migrations/set-based SQL)
- **Shopify GraphQL Admin API** — replaces ALL REST calls:
  - catalog fetch: `bulkOperationRunQuery` (JSONL export, no pagination/throttle)
  - prices: `productVariantsBulkUpdate`
  - inventory: `inventorySetQuantities` (250 per call, `ignoreCompareQuantity`)
- Secrets in `.env` (python-dotenv) — nothing hardcoded

## Architecture (medallion)
```
raw (supplier CSVs on disk, produced by existing pipelines/<supplier>/)   ← unchanged
  → ingest      supplier_staging (normalized sku/price/qty per run, immutable)
  → catalog     shopify_products mirror (bulk GraphQL export → upsert)
  → merge       merged_snapshot (min price, sum qty per sku — set-based SQL in Neon)
  → validate    hard safety gates (abort before any Shopify write)
  → create      new SKUs → productSet + publish (from Shopify_SEO CSVs), mirror updated
  → sync        diff-based price + inventory push, full audit in sync_actions
```

## Safety gates (validate step — run aborts on error-severity failure)
1. Supplier CSV freshness ≤ MAX_CSV_AGE_DAYS (checked at ingest)
2. Per-supplier minimum row counts
3. Total merged qty change vs last successful run ≤ ±MAX_TOTAL_QTY_DELTA_PCT
4. Zero-out cap: SKUs that would be zeroed ≤ MAX_ZERO_OUT_PCT of catalog
5. Price bounds + per-SKU price swing warnings
6. Sync circuit breaker: abort if failure rate > MAX_SYNC_FAILURE_PCT

## Supplier ingest mappings (verified against real CSVs 2026-07-10)
| supplier | file | sku | price | qty | prefiltered |
|---|---|---|---|---|---|
| formula | `Data/cleaned/cleaned_{type}_*.csv` | `item` | `retail_price` | `available_quantity` | yes |
| northcountry | `Data/cleaned/cleaned_{type}_*.csv` | `sku` | `retail_price` | `available_quantity_related_branches` | yes |
| dttire | `Data/cleaned/cleaned_{type}_*.csv` | `sku` | `cost_price` | `qty_total` | yes |
| emerge | `Data/cleaned/cleaned_tires_*.csv` (tires only) | `sku` | `retail_price` | `available_quantity_related_branches`→`available_quantity` | yes |
| wheelsmart | `Data/raw/wheelmart_tires_*.csv` / `wheelmart_rims_*.csv` | `sku`/`name` split-`:`-last | `retail_price` | `quantity_on_hand` (`20+`→20) | **no — filter qty≥4 at ingest** |

Business rules preserved: $30 markup applied at sync (all types), SKU normalize
`str(int(float(x)))` for numeric SKUs, absent SKUs zeroed (per product type),
product_type map tires→"Vehicle Tires", wheels→"Wheels".

## Files
```
production/
├── run_weekly.py        orchestrator (--dry-run --type --skip --only --from --force --with-scrape)
├── config.py            env-driven settings + supplier mappings + thresholds
├── .env / .env.example  secrets
├── db/migrate.py        migration runner (Hasura run_sql + pg_track_table)
├── db/migrations/001_initial_schema.sql
├── core/hasura.py       Hasura GraphQL client (retries, chunked inserts, run_sql)
├── core/shopify_gql.py  Shopify GraphQL client (cost-aware throttle, bulk ops)
├── core/run_context.py  run/step bookkeeping in DB + file/console logging
└── steps/{ingest,catalog,merge,validate,create_products,sync}.py
```

## Status
- [x] Recon: CSV schemas, Hasura connectivity (source `SBOTPipelineData`), Shopify GraphQL verified
- [x] Schema migration applied to Neon (001), 8 tables tracked in Hasura
- [x] Core clients (core/hasura.py, core/shopify_gql.py)
- [x] Steps (steps/{ingest,catalog,merge,validate,sync}.py)
- [x] Orchestrator (run_weekly.py)
- [x] End-to-end dry-run against real data — run #1, 2026-07-10, SUCCESS (2 min total)
- [x] README + VM/cron deployment notes

## Run #1 dry-run results (2026-07-10)
- catalog: 33,668 products bulk-exported in ~25 s → 18,734 unique SKUs mirrored
  (⚠ 14,934 duplicate-SKU variants in store — first variant wins; investigate later)
- ingest: 17,091 rows / 8 feeds (northcountry wheels missing — warn only, as configured)
- merge: 13,653 tire + 926 wheel SKUs; 2,496 multi-supplier tire SKUs
- validate: all error gates PASS (zero-out 2.1% tires / 21.9% wheels, both < 30% cap);
  warnings: 26 price swings > 60%, 3 unpriced, 949 snapshot SKUs not in store (new products)
- sync preview: 1,966 price updates + 3,655 qty changes + 402 zero-outs = 6,023 audited actions
- warehouse after run: staging 17,091 | snapshot 14,579 | mirror 18,734 | actions 6,023 | validations 31

## Fixes during build
- Hasura ≥ 2.x maps `text[]` insert input to `[String!]`, not `_text` (run_context.py)
- inventorySetQuantities is all-or-nothing per batch → per-item retry fallback added (steps/sync.py)

## create_products step — IMPLEMENTED (no manual imports in cloud architecture)
User directive 2026-07-10: cloud pipeline must be fully hands-off — new products are
created directly via GraphQL; manual Shopify_SEO CSV admin import was only a stopgap
for the Streamlit app. Implemented + dry-run verified (run #3):

- `steps/create_products.py`, runs between validate and sync_prices.
- New SKUs = merged_snapshot(run) minus shopify_products mirror → SEO definition
  looked up by normalized Variant SKU in latest `Shopify_SEO_*.csv` per supplier
  (`seo_pattern` in config.SUPPLIERS, suppliers searched in ingest order, first wins)
  → `productSet` (synchronous: true; title/body/vendor/productType/tags/handle/SEO,
  Image Src → files.originalSource, single Default Title variant, tracked inventory
  with qty at primary location) → `publishablePublish` to Online Store → mirror row
  inserted (sync steps see no diff) + sync_actions action=product_create.
- Price = merged_price + markup (same source of truth as sync_prices, NOT the SEO
  file's Variant Price); out-of-bounds → skipped + audited. No SEO definition →
  summary count only. Gate: MAX_NEW_PRODUCTS_PER_RUN=3000 (validation error + abort).
- ShopifyGQL additions: create_product() (productSet), online_store_publication_id()
  (returns None gracefully on missing scope), publish_product().
- ⚠ **TOKEN SCOPE GAP**: app token lacks `read_publications` + `write_publications`
  (verified via currentAppInstallation.accessScopes). Until added in the app config,
  live create runs make products ACTIVE but NOT visible in the Online Store channel
  (step warns loudly and continues). ADD BOTH SCOPES BEFORE FIRST LIVE RUN.

## Fix: emerge SKU prefix (found via run #2 dry-run)
Emerge's augment adds `EMRG-` to SKUs before Shopify import, so the store holds
`EMRG-…` SKUs (644 products, all qty 0) while cleaned CSVs are unprefixed. Without a
fix, emerge products would NEVER sync and create_products would have made 609
duplicates. Fixed with `"sku_prefix": "EMRG-"` on emerge in config.SUPPLIERS, applied
idempotently at ingest. Run #3 confirms: new SKUs 950→331, no_seo_definition 609→0,
and the 644 store emerge products now match for price/inventory sync.

## Run #3 dry-run results (2026-07-10, full pipeline incl. create_products)
- catalog 18,752 SKUs mirrored | ingest 17,302 rows / 9 feeds (northcountry wheels
  feed now live: 211 rows) | merge 13,671 tires + 1,137 wheels
- all error gates PASS; create_products: 331 previewed, 0 without SEO, 0 failed
- sync preview: 1,956 price updates + 3,695 qty changes + 144 zero-outs

## Next session
1. Add read_publications + write_publications scopes to the Shopify app (user, admin UI)
2. First LIVE run: `python run_weekly.py` (tires+wheels) — watch create_products
   (331 new products) and verify publish; spot-check a few in the store
3. Review the 26 price swings > 60% (validation_results / sync_actions)
4. Investigate 14,934 duplicate SKUs in dev store (likely earlier duplicate imports)

## Deferred / later sessions
- Oracle VM provisioning (user does this) + cron install + Docker Postgres/Hasura swap (connection strings only — code is endpoint-agnostic)
- Scraper cookie auto-refresh on headless VM (playwright); `--with-scrape` exists but scrapers still need valid cookies
- Alerting (email/webhook) on run failure — hook point exists in run_weekly.py finalize
