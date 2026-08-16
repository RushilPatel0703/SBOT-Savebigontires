#!/usr/bin/env python3
"""DEPRECATED — retired in favour of the shared enrichment layer.

This was formula's original, formula-only SerpAPI designation enrichment
step. It has been replaced by pipelines/shared/enrichment.py, which:
  - covers all five suppliers (not just formula), not five copies of this file
  - covers both designation AND image (this script only ever did designation)
  - defaults to a report-only dry run; a separate --apply flag applies
    already-cached (already-paid) fixes with zero network calls; a separate
    --live --budget N is required to spend anything at all
  - reuses this file's own cache rather than re-paying for the same query

See monitoring/SERP_LAYER_DESIGN.md for the design and
monitoring/ENRICHMENT_APPROVAL.md for the current free-vs-paid numbers.

This file is kept only as a pointer (not deleted) in case anything still
invokes `python serp_enrich.py` out of habit — it makes NO network calls and
performs NO action beyond printing where to go instead. run_pipeline.py no
longer references this script at all (see the 'enrich' step).

Data/shopify/serp_cache.json (253 already-paid-for designation queries) is
UNTOUCHED by this file and remains on disk exactly as before — it has been
migrated (copied, not moved) into the centralized cache at
pipelines/shared/Data/serp_cache.json by enrichment.py's migrate_formula_cache().
"""
import sys

MESSAGE = """\
serp_enrich.py is deprecated and no longer does anything.

Use the shared enrichment layer instead, from pipelines/shared/:
    python ../shared/enrichment.py --supplier formula_distributors            # report only
    python ../shared/enrichment.py --supplier formula_distributors --apply    # apply cache hits (free)
    python ../shared/enrichment.py --live --budget N                         # LIVE — requires approval

Your original cache (Data/shopify/serp_cache.json) has been preserved and
migrated into pipelines/shared/Data/serp_cache.json — nothing was lost.
"""


def main():
    print(MESSAGE)
    sys.exit(0)


if __name__ == "__main__":
    main()
