#!/usr/bin/env python3
"""Central configuration for the production pipeline.

All secrets come from .env (same directory). Everything else — supplier ingest
mappings, business rules, validation thresholds — is declared here so a run is
fully reproducible from code + env.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

# ── Warehouse backend ──────────────────────────────────────────────────────────
# The run's bookkeeping + set-based SQL layer. Two interchangeable backends
# implement core/warehouse.py's Warehouse interface:
#
#   local   — an embedded DuckDB file (production/Data/warehouse.duckdb). No
#             server, no secrets, runs on the client's laptop behind the
#             dashboard's Sync button.
#   hasura  — Hasura Cloud over Neon Postgres. What the unattended cloud run uses.
#
# The step code (ingest/merge/validate/sync/catalog) is identical either way —
# that is the whole point of the split, so what the client previews locally is
# what the cron run applies. Going to cloud is one env var: SBOT_WAREHOUSE=hasura.
#
# Default is 'local' deliberately. The alternative — inferring 'hasura' from the
# presence of HASURA_* in .env — means a half-configured cloud box silently
# writes to whichever backend it guessed. Naming the backend is cheap; a run
# that priced off the wrong warehouse is not.
WAREHOUSE_BACKEND = os.environ.get("SBOT_WAREHOUSE", "local").strip().lower()

# ── Endpoints & secrets ────────────────────────────────────────────────────────
# Read, never required at import: a local-backend run has no Hasura project at
# all, and the dashboard imports this module just to show which store it targets.
# core/hasura.py raises with a specific message if a Hasura call is attempted
# without these set — see Hasura.__init__.
HASURA_GRAPHQL_ENDPOINT = os.environ.get("HASURA_GRAPHQL_ENDPOINT", "")
HASURA_ADMIN_SECRET     = os.environ.get("HASURA_ADMIN_SECRET", "")
HASURA_SOURCE           = os.environ.get("HASURA_SOURCE", "SBOTPipelineData")
# /v1/graphql → base URL for /v2/query and /v1/metadata
HASURA_BASE_URL         = HASURA_GRAPHQL_ENDPOINT.rsplit("/v1/graphql", 1)[0]

# Shopify credentials resolve from production/.env first, then from the
# SBOT_SHOPIFY_* names the pipelines/ layer and the dashboard already use
# (pipelines/shared/config.py). One store per environment, named in one place:
# without the shared fallback the dashboard could be pointed at the live store
# while this pipeline still wrote to the dev one, and nothing would say so.
# Switching to the live store = set these to the live shop + its Admin API token.
SHOPIFY_SHOP  = os.environ.get("SHOPIFY_SHOP")  or os.environ.get("SBOT_SHOPIFY_SHOP", "")
SHOPIFY_TOKEN = os.environ.get("SHOPIFY_TOKEN") or os.environ.get("SBOT_SHOPIFY_TOKEN", "")
SHOPIFY_API_VERSION = (os.environ.get("SHOPIFY_API_VERSION")
                       or os.environ.get("SBOT_SHOPIFY_API_VERSION", "2026-01"))
SHOPIFY_GRAPHQL_URL = f"https://{SHOPIFY_SHOP}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"

# Shops that are safe to write to without an extra confirmation. Anything else
# is treated as production by the dashboard's Sync button, which then demands an
# explicit "yes, write to the live store" before it will apply anything.
DEV_SHOPS = tuple(s.strip() for s in os.environ.get(
    "SBOT_DEV_SHOPS", "database-testing-2.myshopify.com").split(",") if s.strip())


def is_dev_store() -> bool:
    return SHOPIFY_SHOP in DEV_SHOPS


# ── Paths ──────────────────────────────────────────────────────────────────────
PIPELINES_DIR = HERE.parent / "pipelines"   # existing supplier pipelines (raw layer)
LOGS_DIR      = HERE / "logs"
DATA_DIR      = HERE / "Data"               # local warehouse file lives here
LOCAL_WAREHOUSE_PATH = Path(
    os.environ.get("SBOT_LOCAL_WAREHOUSE", str(DATA_DIR / "warehouse.duckdb")))

# ── Business rules ─────────────────────────────────────────────────────────────
# Per-supplier markup, set by the client before each run (dashboard → "Markup
# per supplier") and stored in pipelines/shared/Data/markup_overrides.json.
# Each supplier is EITHER a dollar amount on cost OR a percentage of cost.
#
# This used to be a second hard-coded copy of the table in
# pipelines/shared/config.py, with a comment asking the next person to keep the
# two in sync. Both now load the same markup.py, so a client edit cannot reach
# the supplier pipelines and miss the warehouse, or the reverse.
#
# Markups differ per supplier, which breaks an assumption the merge step used to
# rely on: with one flat markup, min(cost) and min(cost + markup) always picked
# the same supplier, so markup could safely be added at the very end. They now
# diverge (cost 100 + 46.50 = 146.50 loses to cost 105 + 36.50 = 141.50), so the
# markup is applied per supplier during merge and merged_price is FINAL.
import importlib.util as _ilu

_markup_spec = _ilu.spec_from_file_location(
    "_sbot_markup", str(PIPELINES_DIR / "shared" / "markup.py"))
markup = _ilu.module_from_spec(_markup_spec)
_markup_spec.loader.exec_module(markup)

MarkupConfigError = markup.MarkupConfigError
load_markups      = markup.load
save_markups      = markup.save
describe_markup   = markup.describe
markup_rule_for   = markup.rule_for
sql_price_expr    = markup.sql_price_expr

PRODUCT_TYPES = ("tires", "wheels")
SHOPIFY_PRODUCT_TYPE = {                    # pipeline type → Shopify product_type
    "tires":  "Vehicle Tires",
    "wheels": "Wheels",
}

# ── Supplier ingest mappings ───────────────────────────────────────────────────
# pattern is relative to PIPELINES_DIR; {type} → tires|wheels.
# prefiltered: cleaned CSVs already had the low-qty threshold applied upstream.
# price_cols: ordered candidates, first present column wins — cost first, retail
# last. Feeds scraped before the cost-price migration lack the cost column and
# fall back (with a warning) instead of pricing the feed at 0.
# sku_split_colon: WheelMart SKUs arrive as "TIRE:18\" TIRE:2HH2385H1" — Shopify
# holds only the last ':'-segment, so ingest must match that.
SUPPLIERS = {
    "formula": {
        "types": ("tires", "wheels"),
        "pattern":   "formula_distributors/Data/cleaned/cleaned_{type}_*.csv",
        "seo_pattern": "formula_distributors/Data/shopify/Shopify_SEO_{type}_*.csv",
        "sku_col":   "item",
        "price_cols": ("cost_price", "retail_price"),
        "qty_cols":  ("available_quantity",),
        "prefiltered": True,
    },
    "northcountry": {
        "types": ("tires", "wheels"),
        "pattern":   "northcountry/Data/cleaned/cleaned_{type}_*.csv",
        "seo_pattern": "northcountry/Data/shopify/Shopify_SEO_northcountry_{type}_*.csv",
        "sku_col":   "sku",
        "price_cols": ("cost_price", "retail_price"),
        "qty_cols":  ("available_quantity_related_branches", "available_quantity"),
        "prefiltered": True,
    },
    "dttire": {
        "types": ("tires", "wheels"),
        "pattern":   "dttire/Data/cleaned/cleaned_{type}_*.csv",
        "seo_pattern": "dttire/Data/shopify/Shopify_SEO_dttire_{type}_*.csv",
        "sku_col":   "sku",
        "price_cols": ("cost_price",),   # dttire has always scraped true cost
        "qty_cols":  ("qty_total",),
        "prefiltered": True,
    },
    "emerge": {
        "types": ("tires",),
        "pattern":   "emergetires/Data/cleaned/cleaned_{type}_*.csv",
        "seo_pattern": "emergetires/Data/shopify/Shopify_SEO_emergetires_{type}_*.csv",
        "sku_col":   "sku",
        "price_cols": ("cost_price", "retail_price"),
        "qty_cols":  ("available_quantity_related_branches", "available_quantity"),
        "prefiltered": True,
        # augment prefixes SKUs before Shopify import, so the store (and the SEO
        # files) hold EMRG-… — ingest must match or emerge never syncs
        "sku_prefix": "EMRG-",
    },
    "wheelsmart": {
        "types": ("tires", "wheels"),
        "pattern":   "wheelsmart/Data/raw/wheelmart_{type}_*.csv",
        "seo_pattern": "wheelsmart/Data/shopify/Shopify_SEO_wheelsmart_{type}_*.csv",
        "type_alias": {"tires": "tires", "wheels": "rims"},   # file naming uses 'rims'
        # WheelMart rims collide across WHEEL:/RAC: name prefixes; resolve them
        # instead of letting dedup delete one of the two real products.
        "disambiguate_name_prefix": True,
        "sku_col":   {"tires": "sku", "wheels": "name"},
        # WheelMart's cost price is the "product-price" span scraped out of the
        # description cell — stored as user_group_price. retail_price (tds[4]) is
        # only the supplier's suggested retail.
        "price_cols": ("cost_price", "user_group_price", "retail_price"),
        "qty_cols":  ("quantity_on_hand",),
        "prefiltered": False,
        "min_qty": 4,                        # keep qty >= 4 (matches augment_tires/rims)
        "sku_split_colon": True,
    },
}

# Suppliers whose absence for a given type should FAIL validation (vs. warn).
# northcountry wheels + others may legitimately be missing early on → warn only.
REQUIRED_SUPPLIERS = {
    "tires":  ("formula", "northcountry", "wheelsmart", "emerge", "dttire"),
    "wheels": ("formula", "wheelsmart", "dttire"),
}

# ── Validation thresholds (safety gates) ───────────────────────────────────────
MAX_CSV_AGE_DAYS        = 7      # supplier CSV older than this → that supplier fails freshness
MIN_ROWS_PER_SUPPLIER   = 50     # fewer normalized rows than this → error (scrape likely broke)
MAX_TOTAL_QTY_DELTA_PCT = 40.0   # |Δ total qty| vs last successful run → error
MAX_ZERO_OUT_PCT        = 30.0   # % of catalog that would be zeroed → error
MIN_PRICE               = 20.0   # final (marked-up) price bounds → outside = warning,
MAX_PRICE               = 5000.0 #   and the SKU is skipped at sync
MAX_PRICE_SWING_PCT     = 60.0   # per-SKU price change vs live Shopify price → warning
MAX_SYNC_FAILURE_PCT    = 5.0    # sync circuit breaker: abort remaining pushes
MAX_NEW_PRODUCTS_PER_RUN = 3000  # create_products cap → error (bad merge would flood the store)

# ── API client tuning ──────────────────────────────────────────────────────────
HASURA_INSERT_CHUNK       = 1000  # rows per insert mutation
HASURA_TIMEOUT            = 120   # seconds
HASURA_RETRIES            = 5
SHOPIFY_TIMEOUT           = 90
SHOPIFY_RETRIES           = 5
SHOPIFY_INVENTORY_BATCH   = 250   # inventorySetQuantities hard limit
PRICE_ALIAS_BATCH         = 25    # aliased productVariantsBulkUpdate mutations per request.
                                  # Each alias costs ~10 points against the 2000-point
                                  # bucket, so 25 keeps a request well inside one refill
                                  # while cutting round-trips 25x vs. one call per SKU.
SHOPIFY_BULK_POLL_SECONDS = 5
SHOPIFY_BULK_MAX_WAIT     = 1800  # 30 min for bulk export of ~10k products
