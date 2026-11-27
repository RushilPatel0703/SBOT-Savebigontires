# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Inventory pipeline for savebigontires.com (Shopify). It scrapes five supplier portals (Formula, North Country, Emerge, WheelMart, DT Tire — all small self-hosted Costar WebOrder sites), cleans the feeds, builds Shopify import CSVs, and syncs prices/inventory to the store. Python 3.10+, no build step, no linter config.

Two halves:

- **`pipelines/`**: the per-supplier scrape → clean → enrich → augment chain, plus the Streamlit dashboard (`dashboard.py`) that drives it by hand.
- **`production/`**: `run_weekly.py`, the warehouse-backed GraphQL sync that is meant to run unattended on cron. It reads the supplier CSVs that `pipelines/` produces.

The dashboard is now the **manual fallback**; `production/run_weekly.py` is the intended automated path. `AUTOMATION_BUILD_SPEC.md` is the current build plan for deploying it (a Vultr Seattle VPS plus a Supabase Postgres `pipeline` schema). Read it before touching `production/`.

## Commands

```bash
pip install -r requirements.txt && playwright install chromium

# Dashboard (launch_dashboard.{bat,sh,command} do this). Must run from pipelines/,
# because .streamlit/config.toml is read from the working directory.
cd pipelines && python -m streamlit run dashboard.py

# Tests. Run from pipelines/, where Streamlit is stubbed in conftest.py and nothing hits the network.
cd pipelines && python -m pytest tests/ -v
cd pipelines && python -m pytest tests/test_markup.py -v
cd pipelines && python -m pytest tests/test_markup.py::test_name -v

# One supplier pipeline. Run it from that supplier's own directory, since scripts use relative Data/ and logs/ paths.
cd pipelines/northcountry && python run_pipeline.py --skip scraper_tires   # also --only, --from, --dry-run
cd pipelines && python run_all.py --skip-scraping --suppliers formula nc

# Production sync. Always run --dry-run first.
cd production && python preflight.py
cd production && python run_weekly.py --dry-run
cd production && python report.py --run N
```

`production/` modules do a bare `import config`, so run them from `production/`. `pipelines/tests/test_warehouse.py` puts `production/` first on `sys.path` to get at `core.local_warehouse`.

## Architecture

### Supplier pipelines (`pipelines/<supplier>/`)

Each supplier's `run_pipeline.py` is a list of `(step_name, script, args)` run as **subprocesses** in order, with `--skip/--only/--from` composing over that list. The typical chain is:

`scraper_* → clean.py → ../shared/enrichment.py --apply → augment_*.py → create_products.py`

- `Data/raw/` → `Data/cleaned/` → `Data/shopify/Shopify_SEO_*.csv`. The CSVs are gitignored, but the JSON caches under `Data/` are tracked.
- `pipelines/shared/` is reached via `sys.path` inserts, not as a package. It holds the one-and-only definitions of markup (`markup.py`), SKU normalization (`sku_normalize.py::normalize_sku`), SerpAPI enrichment (`enrichment.py`, `serp_ledger.py`), size parsing and image-quality checks. **Do not add a second markup or SKU normalizer.** Duplicate normalizers have already mispriced real SKUs.
- **Two exclusion mechanisms, applied on both roads to the storefront** (`production/steps/ingest.py` for the sync, `pipelines/shared/upload_bundle.py` for the hand-uploaded bundle). Excluding on one road only means the store's contents depend on which button was pressed. `size_parser.is_consumer_tire_size()` drops whole *classes* by size — ATV, UTV, golf-cart, heavy-commercial, ST trailer. `product_denylist.py` + `Data/product_denylist.json` drops *named* products the client struck off, keyed on the SKU as Shopify holds it (Emerge entries carry the `EMRG-` prefix). Anything a size rule can express belongs in the size filter, not the denylist.
- Step names differ per supplier (`scraper` vs `scraper_tires`, `create_tires` vs `create_products`). `dashboard.py` and `run_all.py` each hardcode per-supplier `run_args`/`skip_args`, and `tests/test_pipeline_args.py` guards them. If you rename a step, update all three.
- The `augment_*.py` step builds the customer-facing spec block. Gap audits (`monitoring/gap_report.py`) measure raw/cleaned only, so a field can be "complete" upstream and still ship blank. Costar writes unknown numerics as `"0.0000"`, which is truthy, so `spec or fallback` keeps the placeholder.
- North Country dominates runtime (~8 of ~10 hours). Its 1,452-term search grid is only survivable because `northcountry/Data/raw/zero_terms_*_cache.json` skips dead terms. `MAX_WORKERS = 1` is deliberate because the server times out under concurrency.
- Emerge adds an `EMRG-` SKU prefix in augment. Cleaned CSVs are unprefixed, and `production/config.SUPPLIERS` re-applies the prefix at ingest.

### Paid enrichment (SerpAPI)

`shared/enrichment.py --apply` (run inside every pipeline) only applies cached, already-paid results, with zero network calls. Paid searches only happen through the dashboard's Step 3, which has separate per-field budget caps and a typed confirmation. They are logged in `shared/Data/serp_ledger.jsonl`, and `monitoring/SERP_KILL_SWITCH` blocks them. Never run a paid search without explicit approval for that run.

### Production sync (`production/`)

Steps: `catalog → ingest → merge → validate → create_products → sync_prices → sync_inventory`.

- **Warehouse backend** is chosen by `SBOT_WAREHOUSE` behind `core/warehouse.py::get_warehouse`. Values: `local` is embedded DuckDB at `production/Data/warehouse.duckdb` and is what the dashboard uses. `hasura` is Neon, which is dead and slated for deletion in favour of a new `postgres` backend. Step SQL is Postgres dialect everywhere, and `local_warehouse.py` translates it for DuckDB. **Keep the DuckDB backend**, because it is the offline escape hatch when the cloud warehouse is down.
- `merge` applies markup inside `min()`, so `merged_snapshot.merged_price` is the final selling price. Nothing downstream may add markup. The client sets markup in the dashboard, which writes `pipelines/shared/Data/markup_overrides.json`. A file that exists but won't parse is a deliberate hard failure.
- `validate` gates (freshness, row minimums, ±40% qty delta, 30% zero-out cap, price bounds) abort before any write. `--force` overrides them. All thresholds live at the top of `production/config.py`.
- **Known inert gate:** `config.SHOPIFY_PRODUCT_TYPE` maps tires to `"Vehicle Tires"`, but the store uses `"Tires"`/blank, so the zero-out pass and its cap gate measure an empty set and report PASS (spec §5.1).
- Shopify access is GraphQL only (`core/shopify_gql.py`), using bulk ops for catalog export and `inventorySetQuantities` for absolute, replay-safe quantities.
- When checking a completeness or approval figure, call the executing function and compare it with the reported number. The reporting layer and the acting layer are separate code paths here and have disagreed before.

### Mac app (`macapp/`)

The client runs the dashboard as a sealed PyInstaller app, "Save Big On Tires.app". `macapp/build.py` (macOS only; `--stage-only` works anywhere) stages `pipelines/` into `macapp/build/payload/` as sourceless `.pyc`. It takes tracked plus untracked-but-not-ignored files, so a new module only needs to exist, not be committed. It then freezes from `SaveBigOnTires.spec`, runs a self-test, a DT Tire e2e on `fixtures/` and a browser UI smoke (`ui_smoke.py`), and zips the result into `macapp/dist/SaveBigOnTires-mac.zip`. CI does the same on `macos-14` when the `mac-app` branch gets a push (`.github/workflows/build-mac.yml`).

```bash
pip install -r requirements.txt -r macapp/requirements-build.txt && python -m playwright install chromium
python macapp/build.py              # full build + tests; --skip-ui to skip the browser smoke
python macapp/build.py --stage-only && python macapp/sbot_app.py --sbot-serve   # unfrozen, fast iteration
```

- `sbot_app.py` is one executable with four roles chosen by argv: launch (no args), `--sbot-serve`, `<script>.py` (every pipeline step re-enters the binary as `sys.executable`), and `--sbot-selftest`.
- At runtime the workspace is `~/SaveBigOnTires/` (`SBOT_HOME` overrides it): `.engine/` holds the payload and its `Data/`, and `Uploads/` holds the import files. Logs go to `.engine/app_logs/launcher.log` and `server.log`. Under `Data/`, files are seeded once and then belong to the client, except those in `ALWAYS_UPDATE`.
- The build blanks the developer-only secrets (`DEV_ONLY_SECRETS`) in `shared/config.py` and refuses a payload that contains source or `session_cookies.json`. `production/` and duckdb are not shipped, and the dashboard hides the Sync tab under `SBOT_CLIENT_BUILD=1`.

### Other directories

- `theme_files/` is Shopify Liquid/CSS/JS. It can't be deployed via API and is uploaded manually in the theme editor. The filename `snippets_foo.liquid` maps to `snippets/foo.liquid`.
- `tools/tampermonkey/` holds the manual cookie-grab scripts, for when `pipelines/refresh_cookies.py` fails. Emerge's `refresh_session.py` needs a headed browser.
- `monitoring/` is **gitignored**. Analysis written there never reaches a branch, so put durable findings in commits or module docstrings.
- The README's `pipelines/merged/` (old REST sync) is gone. `production/run_weekly.py` replaced it.

## Credentials

`pipelines/shared/config.py` reads `SBOT_SHOPIFY_SHOP`/`SBOT_SHOPIFY_TOKEN`/SerpAPI keys from env, with fallback defaults pointing at the **dev** store `database-testing-2.myshopify.com`. `production/` reads `production/.env` (see `.env.example`). `preflight.py` reports whether the resolved store is dev or live.
