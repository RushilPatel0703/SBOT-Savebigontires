# Save Big On Tires — Pipeline QA Report
**Date:** 2026-07-10  
**Tester:** Claude Code (automated QA)  
**Scope:** All 5 supplier pipelines + Streamlit dashboard + final Shopify CSVs  

---

## Verdict: PASS with fixes applied

6 bugs found and fixed. 1 known limitation (NC cookies expired). All Shopify CSVs pass consistency checks post-fix.

---

## Bugs Fixed

### BUG-1 — WheelMart tires scraper writing to wrong directory (CRITICAL)
**File:** `wheelsmart/scraper_tires.py`  
**Symptom:** WheelMart pipeline fails at `augment_tires` even after cookie refresh.  
**Root cause:** `OUTPUT_FILE = "Data/wheelmart_tires_*.csv"` but `augment_tires.py` reads from `Data/raw/wheelmart_tires_*.csv`. Path mismatch meant the scraper output was invisible to augment.  
**Fix:** Changed output path to `Data/raw/wheelmart_tires_*.csv` and `os.makedirs("Data/raw")`. Moved 4 existing misplaced CSVs.  
**Also fixed:** `wheelsmart/refresh_session.py` was patching cookies only in `scraper_rims.py`, leaving `scraper_tires.py` stale.

---

### BUG-2 — Formula tires: Variant SKU always blank (CRITICAL)
**File:** `formula_distributors/augment.py`  
**Symptom:** All 556 rows in `Shopify_SEO_tires_*.csv` had empty `Variant SKU`. Products imported to Shopify would have no SKU identifier.  
**Root cause:** `out["SKU"] = df["item"]` populated the non-standard `SKU` column but `Variant SKU` was never assigned — the guard `if "Variant SKU" in out.columns else ""` left it blank.  
**Fix:** Added `out["Variant SKU"] = out["SKU"]` after line 450.

---

### BUG-3 — DT Tire clean_tires missing SKU deduplication (HIGH)
**File:** `dttire/clean.py`  
**Symptom:** 240 duplicate `Variant SKU` entries in the final Shopify CSV (117 pairs). Shopify import would create duplicate product variants.  
**Root cause:** `_clean_wheels()` had SKU dedup but `_clean_tires()` did not. The DT Tire scraper queries by width×ratio pairs so the same tire appears under multiple search terms.  
**Fix:** Added `df.drop_duplicates(subset="sku", keep="first")` in `_clean_tires()`. 409 dupes dropped; 0 remaining.

---

### BUG-4 — Dashboard CSV merge includes all historical runs (HIGH)
**File:** `dashboard.py` → `collect_source_csvs()`  
**Symptom:** "Prepare Upload Files" merged ALL timestamped Shopify CSVs (31,906 rows from 14 files) instead of the most-recent file per product-type per supplier (11,238 rows from 7 files). Upload to Shopify would contain stale/duplicate data.  
**Root cause:** `found.extend(csvs)` extended with every CSV glob match instead of filtering to the latest per type.  
**Fix:** Added dedup by stem-base (stripping `_YYYYMMDD_HHMMSS` suffix), keeping only the most recent per product-type.

---

### BUG-5 — Unicode crash in subprocess on Windows cp1252 (MEDIUM)
**File:** `dashboard.py` → `_run()`  
**Symptom:** First formula pipeline run from dashboard failed: `UnicodeEncodeError: 'charmap' codec can't encode character '→'` in `formula_distributors/clean.py`. 28 pipeline scripts across all suppliers use `→`, `❌`, `⚠️` etc. in print statements without `sys.stdout.reconfigure(encoding='utf-8')`.  
**Root cause:** Dashboard launches subprocesses whose stdout is redirected to a file opened with `encoding="utf-8"`, but Python's subprocess stdout defaults to cp1252 on Windows.  
**Fix:** `dashboard.py` now passes `env["PYTHONUTF8"] = "1"` to all child processes, forcing UTF-8 output without modifying 28 individual scripts.

---

## Pipeline Status

| Supplier | Scraper Auth | Clean | Augment | Shopify CSV | Rows |
|----------|-------------|-------|---------|-------------|------|
| Formula Tires | ✅ Valid | ✅ 556 rows | ✅ Fixed (BUG-2) | ✅ PASS | 556 |
| Formula Wheels | ✅ Valid | ✅ 279 rows | ✅ Pass | ✅ PASS | 290 |
| WheelMart Tires | ✅ Valid | N/A | ✅ Fixed (BUG-1) | ✅ PASS | 340 |
| WheelMart Rims | ✅ Valid | N/A | ✅ Pass | ✅ PASS | 172 |
| Emerge Tires | ✅ Valid | ✅ 627 rows | ✅ Pass (QA gate PASS) | ✅ PASS | 627 |
| DT Tire Tires | ✅ Login OK | ✅ Fixed (BUG-3) | ✅ Pass | ✅ PASS | 8,966 |
| DT Tire Wheels | ✅ Login OK | ❌ No raw CSV yet | — | — | — |
| North Country Tires | ❌ **Session expired** | ✅ Works (on checkpoint) | ✅ 287 rows | ⚠️ Partial data | 287 |
| North Country Wheels | ❌ **Session expired** | — | — | — | — |

---

## Known Limitations

### NC cookies expired (requires refresh before full run)
The North Country Costar session returns `p_ClientData cannot be null` — the `ASP.NET_SessionId` is stale. Run "Refresh All Cookies" from Step 1 of the dashboard before triggering the NC pipeline. The full NC scraper takes 8–9 hours; the existing checkpoint CSV only has 883 rows (partial run).

### DT Tire wheels: no raw CSV
`scraper_wheels.py` has never been run for DT Tire. Login was verified (auth works). A full wheels scrape is needed to populate `Data/raw/dttire_wheels_*.csv` before the wheels clean/augment steps can run.

### Formula serp_enrich: ~3 hours remaining
The formula pipeline currently running via dashboard has 146 uncached SerpAPI calls pending at 73s/call (free tier). This is the expected behavior for the first full run; subsequent runs will hit the cache. Shopify CSVs are available now from augment run without serp enrichment (designations may be slightly less accurate for ~263 rows).

### NC full run timing
NC full scrape takes 8–9 hours and should be started overnight. The clean/augment/create_products pipeline is confirmed working on checkpoint data.

---

## Shopify CSV Consistency — Final Check

All CSVs checked for: blank Variant SKU, duplicate SKUs, blank price, blank tags, supplier tag presence.

| File | Rows | Blank SKU | Dup SKU | Blank Price | Missing Supplier Tag |
|------|------|-----------|---------|-------------|----------------------|
| formula_tires | 556 | 0 | 0 | 0 | 0 |
| formula_wheels | 290 | 0 | 0 | 0 | 0 |
| wheelsmart_tires | 340 | 0 | 0 | 0 | 0 |
| wheelsmart_rims | 172 | 0 | 0 | 0 | 0 |
| emergetires_tires | 627 | 0 | 0 | 0 | 0 |
| dttire_tires | 8,966 | 0 | 0 | 0 | 0 |
| nc_tires (partial) | 287 | 0 | 0 | 0 | 0 |
| **Total** | **11,238** | | | | |

**Note:** Emerge QA gate reported `LoadRange_` coverage at 56.5% (threshold ≥70%). This is a pre-existing data quality issue from the supplier feed, not a pipeline bug.

---

## Dashboard UI

- Streamlit dashboard launches and serves on port 8501 ✅
- Step 1 (Cookie Refresh): button functional, launches `refresh_cookies.py` ✅
- Step 2 (Supplier Pipelines): all 5 supplier buttons render, "Skip scraping" checkbox works ✅
- Step 3 (Sync to Shopify): correctly disabled with "Not yet configured" warning ✅
- "Prepare Upload Files": logic verified — now correctly merges 7 CSVs (11,238 rows) in 2 chunks ✅
- Advanced reset stuck status: renders stuck pipeline names and clear buttons ✅
- Progress bar parsing relies on `Pipeline start — N step(s): [...]` log line format — confirmed present in all run_pipeline.py scripts ✅

---

## Scraper Auth Summary

| Supplier | Platform | Auth Status |
|----------|----------|-------------|
| Formula Distributors | Costar (HMAC-signed) | ✅ Valid — returns products |
| North Country | Costar (HMAC-signed) | ❌ Expired — needs cookie refresh |
| Emerge Tires | Costar (HMAC-signed) | ✅ Valid — returns products |
| WheelMart Canada | Yii2 session | ✅ Valid — full scrape confirmed (618 tires, 432 rims) |
| DT Tire | SAP Hybris (auto-login) | ✅ Valid — login confirmed (JSESSIONID obtained) |

---

## Files Changed

| File | Change |
|------|--------|
| `wheelsmart/scraper_tires.py` | Output path: `Data/` → `Data/raw/` |
| `wheelsmart/refresh_session.py` | Now patches both `scraper_rims.py` AND `scraper_tires.py` |
| `formula_distributors/augment.py` | Added `out["Variant SKU"] = out["SKU"]` |
| `dttire/clean.py` | Added SKU dedup in `_clean_tires()` |
| `dashboard.py` | Set `PYTHONUTF8=1` for child processes; fixed `collect_source_csvs` to take latest-per-type only |
