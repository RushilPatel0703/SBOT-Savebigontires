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

    # ── core execute with throttle handling ────────────────────────────────────
    def execute(self, query, variables=None):
        last_err = None
        for attempt in range(config.SHOPIFY_RETRIES):
            try:
                r = requests.post(self.url, headers=self.headers,
                                  json={"query": query, "variables": variables or {}},
                                  timeout=config.SHOPIFY_TIMEOUT)
            except requests.RequestException as e:
                last_err = str(e)
                time.sleep(2 ** attempt)
                continue

            if r.status_code == 429:                      # rare on GraphQL, but honour it
                time.sleep(max(2.0, float(r.headers.get("Retry-After", 2))))
                continue
            if r.status_code >= 500:
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                time.sleep(2 ** attempt)
                continue
            if not r.ok:
                raise ShopifyError(f"HTTP {r.status_code}: {r.text[:500]}")

            body = r.json()
            errors = body.get("errors") or []
            if errors:
                if any(e.get("extensions", {}).get("code") == "THROTTLED" for e in errors):
                    wait = self._throttle_wait(body)
                    if self.log:
                        self.log.debug(f"Shopify throttled — waiting {wait:.1f}s")
                    time.sleep(wait)
                    continue                              # throttling doesn't burn a retry
                raise ShopifyError(f"GraphQL errors: {json.dumps(errors)[:800]}")
            return body["data"]

        raise ShopifyError(f"Shopify unreachable after {config.SHOPIFY_RETRIES} attempts: {last_err}")

    @staticmethod
    def _throttle_wait(body):
        try:
            ts = body["extensions"]["cost"]["throttleStatus"]
            needed = body["extensions"]["cost"]["requestedQueryCost"] - ts["currentlyAvailable"]
            return max(1.0, needed / ts["restoreRate"] + 0.5)
        except (KeyError, TypeError, ZeroDivisionError):
            return 2.0

    # ── locations ──────────────────────────────────────────────────────────────
    def primary_location_id(self):
        data = self.execute("{ locations(first: 1, includeInactive: false) { nodes { id name } } }")
        nodes = data["locations"]["nodes"]
        if not nodes:
            raise ShopifyError("No active locations found in Shopify store.")
        if self.log:
            self.log.info(f"Shopify location: {nodes[0]['name']} ({nodes[0]['id']})")
        return nodes[0]["id"]

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
        start = """
        mutation($query: String!) {
          bulkOperationRunQuery(query: $query) {
            bulkOperation { id status }
            userErrors { field message }
          }
        }"""
        data = self.execute(start, {"query": self.CATALOG_QUERY})
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
            op = self.execute(poll, {"id": op_id})["node"]
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
        })
        errs = data["productVariantsBulkUpdate"]["userErrors"]
        if errs:
            raise ShopifyError(f"price update userErrors: {errs}")

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
        })
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
