#!/usr/bin/env python3
"""Shopify GraphQL Admin API client — replaces every REST call in the old sync.

Capabilities:
  execute()                 — cost-aware query/mutation with THROTTLED backoff
  fetch_catalog()           — full product+variant export via bulk operation (JSONL)
  update_variant_price()    — productVariantsBulkUpdate (per product)
  set_inventory_quantities()— inventorySetQuantities, up to 250 items per call
  primary_location_id()     — first active location
  create_product()          — productSet (synchronous) for new single-variant products
  online_store_publication_id() / publish_product() — sales-channel publishing
    (requires read_publications + write_publications scopes on the app token)
"""

import json
import time

import requests

import config


class ShopifyError(Exception):
    pass


class ShopifyThrottled(Exception):
    pass


class ShopifyGQL:
    def __init__(self, logger=None):
        self.url = config.SHOPIFY_GRAPHQL_URL
        self.headers = {
            "X-Shopify-Access-Token": config.SHOPIFY_TOKEN,
            "Content-Type": "application/json",
        }
        self.log = logger
        self.cost_log = []          # per-request cost/throttle samples
        self.throttle_waits = []    # seconds slept on THROTTLED

    # ── core execute with throttle handling ────────────────────────────────────
    #
    # BUG FIXED: this used `for attempt in range(retries)` with `continue` for
    # BOTH real failures and THROTTLED waits. `continue` still advances the loop
    # variable, so — despite the old comment claiming "throttling doesn't burn a
    # retry" — every throttle wait consumed one of the 5 attempts. A large sync
    # is throttled constantly by design (that is how Shopify's leaky bucket
    # works), so calls died with "Shopify unreachable after 5 attempts" while
    # the server was healthy and had only ever answered THROTTLED.
    #
    # Now: a `while` loop with an explicit counter incremented ONLY on genuine
    # failures (network exception, 5xx, non-THROTTLED GraphQL error). 429 and
    # THROTTLED retry indefinitely, which is what the REST layer in
    # pipelines/shared/config.py already did correctly.
    def execute(self, query, variables=None, label=None):
        last_err = None
        attempt = 0
        while attempt < config.SHOPIFY_RETRIES:
            try:
                r = requests.post(self.url, headers=self.headers,
                                  json={"query": query, "variables": variables or {}},
                                  timeout=config.SHOPIFY_TIMEOUT)
            except requests.RequestException as e:
                last_err = str(e)
                attempt += 1
                time.sleep(2 ** attempt)
                continue

            if r.status_code == 429:                      # rare on GraphQL, but honour it
                time.sleep(max(2.0, float(r.headers.get("Retry-After", 2))))
                continue                                   # doesn't burn a retry attempt
            if r.status_code >= 500:
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                attempt += 1
                time.sleep(2 ** attempt)
                continue
            if not r.ok:
                raise ShopifyError(f"HTTP {r.status_code}: {r.text[:500]}")

            body = r.json()
            self._record_cost(body, label)

            errors = body.get("errors") or []
            if errors:
                if any(e.get("extensions", {}).get("code") == "THROTTLED" for e in errors):
                    wait = self._throttle_wait(body)
                    self.throttle_waits.append(wait)
                    if self.log:
                        self.log.debug(f"Shopify throttled — waiting {wait:.1f}s")
                    time.sleep(wait)
                    continue                              # doesn't burn a retry attempt
                raise ShopifyError(f"GraphQL errors: {json.dumps(errors)[:800]}")
            return body["data"]

        raise ShopifyError(f"Shopify unreachable after {config.SHOPIFY_RETRIES} attempts: {last_err}")

    # ── cost / throttle instrumentation ────────────────────────────────────────
    # Shopify's GraphQL limit is a cost budget, not a request count, so "how
    # close was this run to the ceiling" is the only useful capacity signal
    # before the catalog doubles. Logged at the end of each sync step.
    def _record_cost(self, body, label):
        try:
            cost = body["extensions"]["cost"]
            ts   = cost["throttleStatus"]
            self.cost_log.append({
                "label": label,
                "requested": cost.get("requestedQueryCost"),
                "actual": cost.get("actualQueryCost"),
                "currently_available": ts.get("currentlyAvailable"),
                "restore_rate": ts.get("restoreRate"),
            })
        except (KeyError, TypeError):
            pass

    def cost_summary(self):
        by_label = {}
        for entry in self.cost_log:
            label = entry["label"] or "(unlabeled)"
            agg = by_label.setdefault(label, {"calls": 0, "total_actual": 0.0, "max_actual": 0.0})
            agg["calls"] += 1
            actual = entry["actual"] or 0
            agg["total_actual"] += actual
            agg["max_actual"] = max(agg["max_actual"], actual)
        for agg in by_label.values():
            agg["avg_actual"] = agg["total_actual"] / agg["calls"] if agg["calls"] else 0
        return {
            "by_label": by_label,
            "throttle_wait_count": len(self.throttle_waits),
            "throttle_wait_seconds_total": round(sum(self.throttle_waits), 1),
        }

    def verify_scopes(self):
        """Access scopes on this token — used by preflight to fail early and
        clearly rather than on the first write."""
        data = self.execute(
            "{ currentAppInstallation { accessScopes { handle } } }",
            label="verify_scopes",
        )
        return sorted(s["handle"] for s in data["currentAppInstallation"]["accessScopes"])

    @staticmethod
    def _throttle_wait(body):
        try:
            ts = body["extensions"]["cost"]["throttleStatus"]
            needed = body["extensions"]["cost"]["requestedQueryCost"] - ts["currentlyAvailable"]
            return max(1.0, needed / ts["restoreRate"] + 0.5)
        except (KeyError, TypeError, ZeroDivisionError):
            return 2.0

    # ── locations ──────────────────────────────────────────────────────────────
    #
    # BUG FIXED: this used `locations(first: 1)` and trusted the result order.
    # `locations` has NO ordering guarantee, and the store now has two — asked
    # today, database-testing-2 returns "Kamloops" (isPrimary=false) FIRST and
    # "Shop location" (isPrimary=true) second. Every inventory write would have
    # gone to the wrong, unstocked location: Shopify answers those with
    # ITEM_NOT_STOCKED_AT_LOCATION, so the whole inventory sync fails clean
    # rather than corrupting quantities — but it fails completely, and the
    # circuit breaker is what would have stopped the run.
    #
    # Filter on isPrimary instead, and refuse to guess if the answer is not
    # exactly one location. Picking wrong here is silent and store-wide.
    def primary_location_id(self):
        data = self.execute(
            "{ locations(first: 20, includeInactive: false) { nodes { id name isPrimary } } }",
            label="primary_location_id",
        )
        nodes = data["locations"]["nodes"]
        if not nodes:
            raise ShopifyError("No active locations found in Shopify store.")
        primary = [n for n in nodes if n.get("isPrimary")]
        if not primary:
            raise ShopifyError(
                f"No location with isPrimary=true found among {len(nodes)} active "
                f"location(s): {[n['name'] for n in nodes]} — refusing to guess.")
        if len(primary) > 1:
            raise ShopifyError(
                f"Multiple locations report isPrimary=true: "
                f"{[n['name'] for n in primary]} — ambiguous, refusing to guess.")
        if self.log:
            self.log.info(f"Shopify primary location: {primary[0]['name']} ({primary[0]['id']})")
        return primary[0]["id"]

    # ── bulk catalog export ────────────────────────────────────────────────────
    CATALOG_QUERY = """
    {
      products {
        edges {
          node {
            id
            handle
            title
            productType
            tags
            variants {
              edges {
                node {
                  id
                  sku
                  price
                  inventoryQuantity
                  inventoryItem { id }
                }
              }
            }
          }
        }
      }
    }
    """

    def fetch_catalog(self):
        """Run a bulk operation exporting all products+variants.
        Returns (products, variants): lists of JSONL objects; variant objects
        carry __parentId referencing their product's gid.
        """
        self._ensure_no_running_bulk_query()

        start = """
        mutation($query: String!) {
          bulkOperationRunQuery(query: $query) {
            bulkOperation { id status }
            userErrors { field message }
          }
        }"""
        data = self.execute(start, {"query": self.CATALOG_QUERY},
                            label="bulkOperationRunQuery")
        errs = data["bulkOperationRunQuery"]["userErrors"]
        if errs:
            raise ShopifyError(f"bulkOperationRunQuery userErrors: {errs}")
        op_id = data["bulkOperationRunQuery"]["bulkOperation"]["id"]
        if self.log:
            self.log.info(f"Bulk catalog export started: {op_id}")

        url = self._poll_bulk(op_id)
        if url is None:                                    # empty store
            return [], []

        products, variants = [], []
        with requests.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("__parentId"):
                    variants.append(obj)
                else:
                    products.append(obj)
        if self.log:
            self.log.info(f"Bulk export downloaded: {len(products)} products, {len(variants)} variants")
        return products, variants

    def _ensure_no_running_bulk_query(self):
        """A shop may run only ONE bulk query at a time. A previous run that was
        killed mid-export (the dashboard's Cancel button, a closed laptop) leaves
        one RUNNING server-side, and every later fetch_catalog() would then fail
        to start with no obvious cause. Wait the old one out instead."""
        data = self.execute("{ currentBulkOperation(type: QUERY) { id status } }",
                            label="check_running_bulk_query")
        op = data.get("currentBulkOperation")
        if op and op["status"] == "RUNNING":
            if self.log:
                self.log.warning(
                    f"Prior bulk query {op['id']} still RUNNING — waiting for it to finish.")
            self._poll_bulk(op["id"])

    def _poll_bulk(self, op_id):
        poll = """
        query($id: ID!) {
          node(id: $id) {
            ... on BulkOperation { id status errorCode objectCount url }
          }
        }"""
        waited = 0
        while waited < config.SHOPIFY_BULK_MAX_WAIT:
            time.sleep(config.SHOPIFY_BULK_POLL_SECONDS)
            waited += config.SHOPIFY_BULK_POLL_SECONDS
            op = self.execute(poll, {"id": op_id}, label="poll_bulk")["node"]
            status = op["status"]
            if status == "COMPLETED":
                if self.log:
                    self.log.info(f"Bulk export completed: {op['objectCount']} objects")
                return op["url"]
            if status in ("FAILED", "CANCELED", "EXPIRED"):
                raise ShopifyError(f"Bulk operation {status}: errorCode={op.get('errorCode')}")
        raise ShopifyError(f"Bulk operation timed out after {config.SHOPIFY_BULK_MAX_WAIT}s")

    # ── mutations ──────────────────────────────────────────────────────────────
    PRICE_MUTATION = """
    mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkUpdate(productId: $productId, variants: $variants) {
        userErrors { field message }
      }
    }"""

    def update_variant_price(self, product_gid, variant_gid, price):
        data = self.execute(self.PRICE_MUTATION, {
            "productId": product_gid,
            "variants": [{"id": variant_gid, "price": f"{price:.2f}"}],
        }, label="update_variant_price")
        errs = data["productVariantsBulkUpdate"]["userErrors"]
        if errs:
            raise ShopifyError(f"price update userErrors: {errs}")

    def update_prices_batched(self, items, batch_size=None):
        """items: list of (sku, product_gid, variant_gid, price).
        Returns [(sku, ok, error)] in input order.

        productVariantsBulkUpdate is per-product, so a naive price sync is one
        HTTP round-trip per SKU — a few thousand changed prices then take tens of
        minutes of pure latency, which is the difference between a sync the
        client will wait for and one they will not. GraphQL aliases let one
        request carry many independent mutations, so this sends batch_size of
        them at a time and reads each alias's userErrors separately.

        Each alias still succeeds or fails on its own — unlike
        inventorySetQuantities, there is no all-or-nothing batch here, so one bad
        SKU cannot take out the rest of its batch.
        """
        batch_size = batch_size or config.PRICE_ALIAS_BATCH
        results = []
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            parts, variables = [], {}
            for j, (_sku, product_gid, variant_gid, price) in enumerate(batch):
                variables[f"p{j}"] = product_gid
                variables[f"v{j}"] = [{"id": variant_gid, "price": f"{price:.2f}"}]
                parts.append(
                    f"u{j}: productVariantsBulkUpdate(productId: $p{j}, variants: $v{j}) "
                    f"{{ userErrors {{ field message }} }}")
            var_decls = ", ".join(
                f"$p{j}: ID!, $v{j}: [ProductVariantsBulkInput!]!" for j in range(len(batch)))
            query = f"mutation({var_decls}) {{ {' '.join(parts)} }}"

            try:
                data = self.execute(query, variables, label="update_prices_batched")
            except ShopifyError as e:
                # A transport/throttle failure kills the whole request, so mark
                # every SKU in it failed rather than silently reporting success.
                results.extend((sku, False, str(e)[:500]) for sku, *_ in batch)
                continue
            for j, (sku, *_rest) in enumerate(batch):
                errs = data[f"u{j}"]["userErrors"]
                results.append((sku, False, str(errs)[:500]) if errs else (sku, True, None))
        return results

    INVENTORY_MUTATION = """
    mutation($input: InventorySetQuantitiesInput!) {
      inventorySetQuantities(input: $input) {
        userErrors { field message code }
      }
    }"""

    def set_inventory_quantities(self, location_gid, items):
        """items: list of (inventory_item_gid, qty). Max 250 — caller batches."""
        if len(items) > config.SHOPIFY_INVENTORY_BATCH:
            raise ValueError(f"max {config.SHOPIFY_INVENTORY_BATCH} items per call")
        data = self.execute(self.INVENTORY_MUTATION, {
            "input": {
                "name": "available",
                "reason": "correction",
                "ignoreCompareQuantity": True,
                "quantities": [
                    {"inventoryItemId": gid, "locationId": location_gid, "quantity": int(qty)}
                    for gid, qty in items
                ],
            }
        }, label="set_inventory_quantities")
        errs = data["inventorySetQuantities"]["userErrors"]
        if errs:
            raise ShopifyError(f"inventorySetQuantities userErrors: {errs}")

    # ── product creation ───────────────────────────────────────────────────────
    PRODUCT_SET_MUTATION = """
    mutation($input: ProductSetInput!) {
      productSet(input: $input, synchronous: true) {
        product {
          id
          handle
          variants(first: 1) {
            nodes { id sku price inventoryItem { id } }
          }
        }
        userErrors { field message }
      }
    }"""

    def create_product(self, *, title, description_html, vendor, product_type,
                       tags, handle, sku, price, qty, location_gid,
                       image_url=None, seo_title=None, seo_description=None):
        """Create one single-variant product (tracked inventory, ACTIVE).
        Returns dict with product_id / variant_id / inventory_item_id / handle.
        """
        product_input = {
            "title": title,
            "handle": handle,
            "descriptionHtml": description_html or "",
            "vendor": vendor,
            "productType": product_type,
            "tags": tags,
            "status": "ACTIVE",
            "productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}],
            "variants": [{
                "optionValues": [{"optionName": "Title", "name": "Default Title"}],
                "sku": sku,
                "price": f"{price:.2f}",
                "inventoryItem": {"tracked": True},
                "inventoryQuantities": [{
                    "locationId": location_gid,
                    "name": "available",
                    "quantity": int(qty),
                }],
            }],
        }
        if image_url:
            product_input["files"] = [{"originalSource": image_url, "contentType": "IMAGE"}]
        if seo_title or seo_description:
            product_input["seo"] = {"title": seo_title, "description": seo_description}

        data = self.execute(self.PRODUCT_SET_MUTATION, {"input": product_input})
        result = data["productSet"]
        if result["userErrors"]:
            raise ShopifyError(f"productSet userErrors: {result['userErrors']}")
        product = result["product"]
        variant = product["variants"]["nodes"][0]
        return {
            "product_id": product["id"],
            "variant_id": variant["id"],
            "inventory_item_id": variant["inventoryItem"]["id"],
            "handle": product["handle"],
        }

    # ── sales-channel publishing ───────────────────────────────────────────────
    def online_store_publication_id(self):
        """Publication gid of the Online Store channel, or None if the token
        lacks the read_publications scope (caller decides how to degrade)."""
        try:
            data = self.execute("{ publications(first: 20) { nodes { id name } } }")
        except ShopifyError as e:
            if "read_publications" in str(e) or "ACCESS_DENIED" in str(e):
                return None
            raise
        for node in data["publications"]["nodes"]:
            if node["name"] == "Online Store":
                return node["id"]
        return None

    PUBLISH_MUTATION = """
    mutation($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        userErrors { field message }
      }
    }"""

    def publish_product(self, product_gid, publication_gid):
        data = self.execute(self.PUBLISH_MUTATION, {
            "id": product_gid,
            "input": [{"publicationId": publication_gid}],
        })
        errs = data["publishablePublish"]["userErrors"]
        if errs:
            raise ShopifyError(f"publishablePublish userErrors: {errs}")
