# Save Big On Tires — Inventory Pipeline

Automated scraping, cleaning, and Shopify sync for 5 tire/wheel suppliers.

---

## One-Time Setup

### Windows

#### 1. Install Python 3.10 or newer
Download from python.org. During install, check **"Add Python to PATH"**.

#### 2. Install dependencies
Open a terminal in this folder and run:
```
pip install -r requirements.txt
playwright install chromium
```

---

### macOS

#### 1. Install Python 3.10 or newer
The recommended way is via [Homebrew](https://brew.sh):
```bash
brew install python@3.12
```
Or download from python.org and run the installer.

#### 2. (Recommended) Create a virtual environment
```bash
cd /path/to/savebigontires_client
python3 -m venv venv
source venv/bin/activate
```

#### 3. Install dependencies
```bash
pip install -r requirements.txt
playwright install chromium
```

#### 4. Make the launch script executable (first time only)
Executable permissions don't survive being zipped on Windows and unzipped on Mac,
so this step is required even though the file already looks like a script:
```bash
chmod +x launch_dashboard.command
```

### 3. Verify credentials
Open `pipelines/shared/config.py` and confirm:
- `SHOP` — your Shopify store URL
- `TOKEN` — your Shopify Admin API token
- `SERPAPI_KEY` — your SerpAPI key (used for Step 3's image/designation/spec enrichment)

All supplier login credentials are already embedded in the scraper scripts.
If you need to update them, see the **Cookie Refresh** section below.

---

## Running the Pipeline

**Windows:** Double-click **`launch_dashboard.bat`**  
**macOS:** Double-click **`launch_dashboard.command`** (or run `bash launch_dashboard.sh` in Terminal — same script, either works)

A browser tab opens automatically at `http://localhost:8501`.

The dashboard walks you through 4 steps:

### Step 1 — Refresh Cookies
Logs into all supplier portals and updates session credentials.
Run this first, every time you start a pipeline run.

### Step 2 — Run Supplier Pipelines
Run each of the 5 suppliers. Each pipeline:
- Scrapes inventory from the supplier's website
- Cleans and filters the data (removes low-stock items)
- Builds Shopify-ready CSV files in `pipelines/<supplier>/Data/shopify/`

Check **"Skip scraping"** if you want to reuse data from a recent scrape
and only re-run the clean + augment steps.

**Expected run times:**
| Supplier | Tires | Wheels |
|---|---|---|
| Formula Tires | ~30 min | ~20 min |
| North Country | 8–9 hours | ~1 hour |
| Emerge Tires | ~15 hours | — |
| WheelMart | ~1 hour | ~30 min |
| DT Tire | ~1 hour | ~30 min |

Pipelines run in the background — you can close the browser tab and they
keep running as long as the terminal window stays open.

### Step 3 — Data Enrichment (optional)
Fills in product images, tire designations, and tire specs that no supplier
feed provides and free parsing couldn't recover.

Once all five pipelines finish, the dashboard automatically counts how many
searches would be needed and shows the number **before** anything is spent.
Nothing is bought until you review that count and approve it.

- **Free fixes** reuse results already paid for on an earlier run. Applying
  them makes no network call and costs nothing.
- **Paid searches** run against the client's SerpAPI key, in three independent
  budgets so approving one never lets it eat another's:
  - **Images** — the field worth buying by default; nothing free produces a photo.
  - **Designations** — off by default. Historically only 4 of 253 paid searches
    (1.6%) returned anything useful, so those few rows are cheaper to fix by hand.
  - **Specs** — load index, speed rating, load range, section width, aspect
    ratio, rim diameter. Bundled into one budget because a row missing its
    load index is almost always missing its speed rating too (both come from
    the same source token, e.g. "104H"), so one search commonly fills several
    of these for the price of one.

  Set a cap per field, type `ENRICH` to confirm, then click **Run Enrichment**.
  Each budget hard-stops at its own cap.

After searching, the run writes the results into the cleaned/raw CSVs and
automatically re-runs `augment` for whichever suppliers changed, so the
Shopify CSVs in Step 4 carry the fix.

To block paid searches entirely, create an empty file at
`monitoring/SERP_KILL_SWITCH` — the run refuses to start while it exists.

**Product spec fields.** The same panel reports completeness for the nine
fields shown in each product's description block — Brand, Style, Section
width, Aspect ratio, Rim diameter, Load index, Speed rating, Load range,
Designation. Brand, Style, Section width, Aspect ratio, Rim diameter and
Designation are filled in for free by parsing the supplier feed; a gap in
those is a parsing shortfall worth reporting, not something more spend fixes.
Load index, Speed rating and Load range are exactly what the **Specs** budget
above searches for once free parsing comes up empty.

The counts are read back out of the finished `Shopify_SEO_*.csv` files, which
is deliberate: that block is built in `augment*.py`, downstream of every stage
`monitoring/gap_report.py` measures, so a field can look complete at the
cleaned stage and still ship blank. Wheels and rims carry a different block
(Wheel Diameter, Bolt Pattern, Offset, Centre Bore, Finish) and are not scored
against tire specs. A blank count includes `0.0000` placeholders — on a product
page those read as a real measurement rather than as the missing value they are.

### Step 4 — Upload New Products to Shopify (when needed)
Merges every supplier's `Shopify_SEO_*.csv` into upload-ready files, split
under Shopify's 15 MB import limit, in a dated folder under `upload_ready/`.

Only needed when suppliers have new SKUs that aren't in Shopify yet.

---

## Adding New Products to Shopify

When a supplier has new products (SKUs that don't exist in Shopify yet), a
price/inventory sync alone won't create them. You need to:

1. After Step 2 (and Step 3, if you want images filled in), click
   **Prepare Upload Files** in Step 4
2. Go to **Shopify Admin → Products → Import** and upload the generated
   files from `upload_ready/`, in order
3. Then run the price/inventory sync (`pipelines/merged/run_sync.py`)

For regular bi-weekly syncs where no new products are being added,
skip the import and go straight to the sync.

---

## Cookie Refresh (When Scrapers Return Auth Errors)

Supplier session cookies expire every few weeks. The dashboard's
"Refresh All Cookies" button handles this automatically using saved credentials.

If automatic refresh fails for a supplier, use the manual Tampermonkey scripts
in `tools/tampermonkey/` — install the script for that supplier in Chrome,
visit their website, and the cookie is copied to your clipboard.

| Supplier | Website |
|---|---|
| Formula | weborder.formuladistributors.com |
| North Country | online.northcountrytire.ca |
| Emerge Tires | order.emergetires.com |
| WheelMart | wheelmartcanada.ca |
| DT Tire | dttirehub.ca |

---

## Folder Structure

```
savebigontires_client/
├── launch_dashboard.bat       ← double-click to start (Windows)
├── launch_dashboard.command   ← double-click to start (macOS)
├── launch_dashboard.sh        ← same script, run via `bash launch_dashboard.sh` (macOS / Linux)
├── requirements.txt           ← pip install -r requirements.txt
├── pipelines/
│   ├── dashboard.py           ← Streamlit UI
│   ├── refresh_cookies.py     ← auto cookie refresh (all suppliers)
│   ├── run_all.py             ← CLI: run everything at once
│   ├── shared/                ← Shopify API helpers, SKU map builder,
│   │                            enrichment layer (Step 3) + its cache
│   ├── formula_distributors/  ← Formula pipeline
│   ├── northcountry/          ← North Country pipeline
│   ├── wheelsmart/            ← WheelMart Canada pipeline
│   ├── emergetires/           ← Emerge Tires pipeline
│   ├── dttire/                ← DT Tire pipeline
│   └── merged/                ← Price + inventory sync to Shopify
└── theme_files/               ← Shopify Liquid/CSS/JS (manual upload via Shopify admin)
```

Generated data is written to each supplier's `Data/` subdirectory during pipeline runs.
Logs are written to each supplier's `logs/` subdirectory.

---

## Updating Shopify Store Credentials

To switch to a different Shopify store, edit `pipelines/shared/config.py`:

```python
SHOP        = "your-store.myshopify.com"
TOKEN       = "shpua_xxxxxxxxxxxxxxxxxxxx"
API_VERSION = "2026-01"
```

To get a new Admin API token: Shopify Admin → Settings → Apps and Sales Channels →
Develop Apps → Create App → Configure Admin API Scopes (read/write products + inventory)
→ Install App → copy the Admin API access token.

---

## Theme Files

`theme_files/` contains Shopify Liquid templates, CSS, and JavaScript.
**All theme changes require manual upload via Shopify Admin → Online Store → Themes → Edit Code.**
The API cannot upload theme files to this store.

Filename convention: `snippets_search-result.liquid` maps to `snippets/search-result.liquid` in Shopify.
