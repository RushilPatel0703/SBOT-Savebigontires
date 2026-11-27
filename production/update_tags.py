#!/usr/bin/env python3
"""Tags-only update from a Handle,Tags CSV (e.g. TAGS_UPDATE_*/tags_update_terrain.csv).

Why not a Shopify CSV import: an import preview showed default variant, price
and status values for columns the file doesn't have, and it is unclear whether
"overwrite" writes them. This script calls tagsAdd / tagsRemove only, so price,
quantity, per-location stock, description and variants cannot be touched.

Scope is terrain only: it adds Terrain_ tags, and retires the old terrain
spellings Type_All-Terrain / Type_Mud-Terrain (replaced by Terrain_, with
Type_All-Season added if that leaves the product with no season tag). Every
other tag (Brand_, Size_, Location_, Type_Light-Truck, season tags that already
exist) is left alone, even where the store differs from the CSV.

    python update_tags.py FILE.csv                 # dry run (default), writes nothing
    python update_tags.py FILE.csv --limit 2 --apply
    python update_tags.py FILE.csv --apply

Run from production/. Targets whatever SHOPIFY_SHOP resolves to; the store is
printed first.
"""
import argparse
import csv
import sys

import config
from core.shopify_gql import ShopifyGQL

LEGACY_TERRAIN = {"Type_All-Terrain", "Type_Mud-Terrain"}
SEASON = {"Type_Winter", "Type_Summer", "Type_All-Summer", "Type_All-Season",
          "Type_All-Weather"}


def plan(have, want):
    """(add, remove) restricted to terrain."""
    have, want = set(have), set(want)
    add = {t for t in want if t.startswith("Terrain_")} - have
    remove = have & LEGACY_TERRAIN
    if remove and not ((have - remove) | add) & SEASON:
        add.add("Type_All-Season")
    return add, remove


def fetch_tags(gql, handles, batch=40):
    """{handle: (product_id, [current tags])} for handles that exist."""
    out = {}
    for i in range(0, len(handles), batch):
        chunk = handles[i:i + batch]
        parts = [f"h{j}: productByIdentifier(identifier: {{handle: $h{j}}}) {{ id tags }}"
                 for j in range(len(chunk))]
        decl = ", ".join(f"$h{j}: String!" for j in range(len(chunk)))
        data = gql.execute(f"query({decl}) {{ {' '.join(parts)} }}",
                           {f"h{j}": h for j, h in enumerate(chunk)},
                           label="update_tags_lookup")
        for j, h in enumerate(chunk):
            node = data.get(f"h{j}")
            if node:
                out[h] = (node["id"], node["tags"])
    return out


def mutate(gql, pid, add, remove):
    for op, tags in (("tagsRemove", remove), ("tagsAdd", add)):
        if not tags:
            continue
        data = gql.execute(
            f"mutation($id: ID!, $tags: [String!]!) {{ {op}(id: $id, tags: $tags) "
            f"{{ userErrors {{ field message }} }} }}",
            {"id": pid, "tags": sorted(tags)}, label=f"update_tags_{op}")
        errs = data[op]["userErrors"]
        if errs:
            raise RuntimeError(f"{op} {pid}: {errs}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    print(f"store: {config.SHOPIFY_SHOP}   mode: {'APPLY' if a.apply else 'dry run'}")
    with open(a.csv, newline="", encoding="utf-8") as f:
        rows = [(r["Handle"], [t.strip() for t in r["Tags"].split(",") if t.strip()])
                for r in csv.DictReader(f)]
    if a.limit:
        rows = rows[:a.limit]

    gql = ShopifyGQL()
    live = fetch_tags(gql, [h for h, _ in rows])
    missing = [h for h, _ in rows if h not in live]
    todo, same = [], 0
    for h, want in rows:
        if h not in live:
            continue
        pid, have = live[h]
        add, remove = plan(have, want)
        if add or remove:
            todo.append((h, pid, add, remove))
        else:
            same += 1
    print(f"rows {len(rows)} | not on store {len(missing)} | already correct {same} | to change {len(todo)}")
    for h, _, add, remove in todo[:5]:
        print(f"  {h}\n    +{sorted(add)}  -{sorted(remove)}")
    if not a.apply:
        print("dry run - nothing written")
        return 0
    done = 0
    for h, pid, add, remove in todo:
        mutate(gql, pid, add, remove)
        done += 1
        if done % 100 == 0:
            print(f"  {done}/{len(todo)}")
    print(f"updated {done} products")
    return 0


if __name__ == "__main__":
    sys.exit(main())
