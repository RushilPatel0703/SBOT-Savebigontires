#!/usr/bin/env python3
"""Admin API access token for the store, static or minted on demand.

Two ways to authenticate, chosen by what production/.env holds:

  SHOPIFY_TOKEN                               a fixed Admin API token (the dev
                                              store's legacy custom app)
  SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET   a Dev Dashboard app installed on
                                              the store; exchanged for a token
                                              with the client-credentials grant

Client-credentials tokens live 24 hours (expires_in = 86399). A sync can start
near the end of one token's life, so the token is renewed a margin before
expiry, and again whenever Shopify answers 401 (revoked or rotated early).

The token is held in memory only. Nothing is written to disk, so a crashed run
leaves no live credential behind; the next run simply mints a new one. The
grant only works when the app and the store belong to the same Shopify
organization.
"""

import threading
import time

import requests

import config

RENEW_MARGIN_SECONDS = 600   # renew 10 min before Shopify says it expires


class ShopifyAuthError(Exception):
    pass


class TokenProvider:
    def __init__(self, shop=None, client_id=None, client_secret=None, static_token=None):
        self.shop = shop or config.SHOPIFY_SHOP
        self.client_id = client_id if client_id is not None else config.SHOPIFY_CLIENT_ID
        self.client_secret = (client_secret if client_secret is not None
                              else config.SHOPIFY_CLIENT_SECRET)
        self.static_token = static_token if static_token is not None else config.SHOPIFY_TOKEN
        self._token = None
        self._expires_at = 0.0
        self.scopes = None
        self._lock = threading.Lock()

    @property
    def uses_client_credentials(self):
        return bool(self.client_id and self.client_secret)

    @property
    def configured(self):
        return bool(self.shop) and (self.uses_client_credentials or bool(self.static_token))

    def get(self):
        if not self.uses_client_credentials:
            if not self.static_token:
                raise ShopifyAuthError(
                    "No Shopify credentials: set SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET "
                    "or SHOPIFY_TOKEN in production/.env.")
            return self.static_token
        with self._lock:
            if self._token is None or time.time() >= self._expires_at - RENEW_MARGIN_SECONDS:
                self._mint()
            return self._token

    def invalidate(self):
        """Forget the current token so the next get() mints a fresh one.
        Returns False when there is nothing to renew (static token)."""
        if not self.uses_client_credentials:
            return False
        with self._lock:
            self._token = None
            self._expires_at = 0.0
        return True

    def _mint(self):
        url = f"https://{self.shop}/admin/oauth/access_token"
        try:
            r = requests.post(url, data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }, timeout=config.SHOPIFY_TIMEOUT)
        except requests.RequestException as e:
            raise ShopifyAuthError(f"Token request to {self.shop} failed: {e}") from e
        if not r.ok:
            # The body is an error message, never a token, so it is safe to show.
            raise ShopifyAuthError(
                f"Token request to {self.shop} refused (HTTP {r.status_code}): {r.text[:300]}. "
                "Check the app is installed on this store and belongs to the same "
                "organization, and that the client ID/secret are current.")
        body = r.json()
        self._token = body["access_token"]
        self._expires_at = time.time() + float(body.get("expires_in", 86399))
        self.scopes = body.get("scope")


_default = None


def default_provider():
    """One provider per process, so every ShopifyGQL shares one token."""
    global _default
    if _default is None:
        _default = TokenProvider()
    return _default
