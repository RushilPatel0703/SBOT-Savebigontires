#!/usr/bin/env python3
"""SerpAPI Designation Enrichment
Queries Google Shopping results for tires whose designation was inferred only
from UTQG/speed-rating heuristics (Layer 4/5) or is still Unknown.
Results are cached locally so each product is queried at most once.

Usage:
    python serp_enrich.py               # enrich + update cleaned CSV
    python serp_enrich.py --dry-run     # preview what would be queried
    python serp_enrich.py --reset-cache # clear cache and re-query everything

Requirements:
    pip install google-search-results
"""

import sys
import json
import os
import glob
import time
import argparse
import pandas as pd
from config import SERPAPI_KEY, find_latest
from tire_designation_inference import _keyword_lookup

try:
    from serpapi import GoogleSearch
except ImportError:
    print("ERROR: Run 'pip install google-search-results' first.")
    sys.exit(1)

CACHE_FILE   = "Data/shopify/serp_cache.json"
RATE_DELAY   = 73    # seconds between API calls — free tier cap is 50/hour (1 per 72s)

# Designation reasons that warrant a SerpAPI lookup, in priority order
PRIORITY_ORDER = ["Unknown", "Layer 5", "Layer 4"]


def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def cache_key(row):
    brand = str(row.get("brand_name", "")).strip().lower()
    model = str(row.get("model", "")).strip().lower()
    size  = str(row.get("size", "")).strip().lower()
    return f"{brand}|{model}|{size}"


def needs_enrichment(row):
    if str(row.get("designation", "")).strip() == "Unknown":
        return True
    reason = str(row.get("designation_reason", ""))
    return any(layer in reason for layer in PRIORITY_ORDER)


def priority_score(row):
    """Lower number = higher priority (enriched first)."""
    desig  = str(row.get("designation", "")).strip()
    reason = str(row.get("designation_reason", ""))
    if desig == "Unknown":
        return 0
    if "Layer 4" in reason:   # weakest — null UTQG guess or treadwear threshold
        return 1
    if "Layer 5" in reason:   # speed rating is at least a real spec value
        return 2
    return 3


def build_query(row):
    brand = str(row.get("brand_name", "")).strip()
    model = str(row.get("model", "")).strip()
    size  = str(row.get("size", "")).strip()
    return f"{brand} {model} {size} tire".strip()


def extract_designation(results):
    """Pull all text from shopping + organic results and run keyword lookup."""
    texts = []

    for r in results.get("shopping_results", [])[:6]:
        texts.append(r.get("title", ""))
        texts.append(r.get("snippet", ""))
        for ext in r.get("extensions", []):
            texts.append(str(ext))

    kg = results.get("knowledge_graph", {})
    texts.extend([kg.get("title", ""), kg.get("type", ""), kg.get("description", "")])

    for r in results.get("organic_results", [])[:4]:
        texts.append(r.get("title", ""))
        texts.append(r.get("snippet", ""))

    combined = " ".join(filter(None, texts))
    return _keyword_lookup(combined)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",     action="store_true", help="Preview without making API calls")
    parser.add_argument("--reset-cache", action="store_true", help="Clear cache before running")
    args = parser.parse_args()

    if not SERPAPI_KEY and not args.dry_run:
        print("ERROR: Set SERPAPI_KEY in config.py first.")
        sys.exit(1)

    input_file = find_latest("Data/cleaned/cleaned_tires_*.csv")
    if not input_file:
        print("ERROR: No cleaned_tires_*.csv found. Run clean.py --type tires first.")
        sys.exit(1)
    print(f"Input : {input_file}")

    df = pd.read_csv(input_file, dtype=str).fillna("")
    print(f"Rows  : {len(df)}")

    cache = {} if args.reset_cache else load_cache()
    if args.reset_cache:
        print("Cache cleared.")

    candidates = df[df.apply(needs_enrichment, axis=1)].copy()
    candidates["_priority"] = candidates.apply(priority_score, axis=1)
    candidates = candidates.sort_values("_priority")
    print(f"Rows needing enrichment : {len(candidates)}")
    print(f"  Unknown               : {(candidates['_priority'] == 0).sum()}")
    print(f"  Layer 4 (UTQG guess)  : {(candidates['_priority'] == 1).sum()}")
    print(f"  Layer 5 (speed rating): {(candidates['_priority'] == 2).sum()}")

    if len(candidates) == 0:
        print("Nothing to enrich.")
        return

    # Split into cache hits vs. needs API call
    cache_hits    = candidates[candidates.apply(lambda r: cache_key(r) in cache, axis=1)]
    needs_api     = candidates[candidates.apply(lambda r: cache_key(r) not in cache, axis=1)]
    print(f"  Cache hits            : {len(cache_hits)}")
    print(f"  API calls needed      : {len(needs_api)}")

    if args.dry_run:
        print("\n[DRY RUN] Would query (in priority order):")
        for _, row in needs_api.iterrows():
            print(f"  [{row['_priority']}] {build_query(row)}  ({row['designation']} — {row['designation_reason']})")
        print(f"\n[DRY RUN] Would apply {len(cache_hits)} cached results.")
        return

    resolved = 0
    still_unknown = 0

    # Apply cache hits first
    for idx, row in cache_hits.iterrows():
        entry = cache[cache_key(row)]
        desig = entry.get("designation", "")
        if desig:
            df.at[idx, "designation"]        = desig
            df.at[idx, "designation_reason"] = f"SerpAPI (cached): {entry.get('query', '')}"
            resolved += 1
        else:
            df.at[idx, "designation"]        = "Unknown"
            df.at[idx, "designation_reason"] = f"SerpAPI (cached, no match): {entry.get('query', '')}"
            still_unknown += 1

    # Query SerpAPI for the rest
    api_calls = 0
    for idx, row in needs_api.iterrows():
        query = build_query(row)
        try:
            results = GoogleSearch({
                "engine":  "google",
                "q":       query,
                "num":     6,
                "api_key": SERPAPI_KEY,
            }).get_dict()

            desig = extract_designation(results)
            key   = cache_key(row)

            cache[key] = {"designation": desig, "query": query}
            save_cache(cache)
            api_calls += 1

            if desig:
                df.at[idx, "designation"]        = desig
                df.at[idx, "designation_reason"] = f"SerpAPI: {query}"
                resolved += 1
            else:
                df.at[idx, "designation"]        = "Unknown"
                df.at[idx, "designation_reason"] = f"SerpAPI (no match): {query}"
                still_unknown += 1

            time.sleep(RATE_DELAY)

        except Exception as e:
            err = str(e).lower()
            if "run out of searches" in err or "out of searches" in err or "quota" in err:
                print(f"  Monthly quota exhausted after {api_calls} API calls. Remaining rows deferred to next run.")
                break
            print(f"  ERROR: {query} — {e}")
            still_unknown += 1

    df.to_csv(input_file, index=False)

    print(f"\nCache hits applied : {len(cache_hits)}")
    print(f"API calls made     : {api_calls}")
    print(f"Newly resolved     : {resolved}")
    print(f"Still unresolved   : {still_unknown}")
    print(f"\nDesignation breakdown:\n{df['designation'].value_counts(dropna=False).to_string()}")
    print(f"\nUpdated: {input_file}")


if __name__ == "__main__":
    main()
