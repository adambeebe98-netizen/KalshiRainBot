"""
Minimal Kalshi REST client with RSA-PSS request signing.

Kalshi's auth scheme (per their docs as of 2026):
  - You generate an RSA keypair, upload the PUBLIC key to Kalshi, and keep
    the PRIVATE key locally.
  - Every request is signed by concatenating:
        timestamp_ms + HTTP_METHOD + request_path
    and signing that string with your private key using RSA-PSS
    (MGF1/SHA-256), then base64-encoding the signature.
  - Three headers go on every authenticated request:
        KALSHI-ACCESS-KEY        (your key ID)
        KALSHI-ACCESS-TIMESTAMP  (ms since epoch, matching the signed string)
        KALSHI-ACCESS-SIGNATURE  (the base64 signature)

Verify this against https://docs.kalshi.com/getting_started/api_keys before
trusting it with real credentials — API vendors do change signing details.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import SETTINGS


class KalshiAuthError(RuntimeError):
    pass


class KalshiClient:
    def __init__(self, base_url: str | None = None, api_key_id: str | None = None,
                 private_key_path: str | None = None):
        self.base_url = (base_url or SETTINGS.kalshi_base_url).rstrip("/")
        self.api_key_id = api_key_id or SETTINGS.kalshi_api_key_id
        key_path = private_key_path or SETTINGS.kalshi_private_key_path

        if not self.api_key_id or not key_path:
            raise KalshiAuthError(
                "Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY_PATH. "
                "Set them in your .env file."
            )

        with open(key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)

        self._client = httpx.Client(base_url=self.base_url, timeout=15.0)

    # ---------- signing ----------

    def _sign(self, method: str, path: str, timestamp_ms: str) -> str:
        message = f"{timestamp_ms}{method}{path}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(method.upper(), path, ts),
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, params: dict | None = None,
                 json_body: dict | None = None) -> dict:
        # The path used in the signature must match exactly what's sent on the wire,
        # including the /trade-api/v2 prefix — adjust here if Kalshi changes this.
        full_path = path if path.startswith("/trade-api") else f"/trade-api/v2{path}"
        headers = self._headers(method, full_path)
        resp = self._client.request(method, path, params=params, json=json_body, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"Kalshi API error {resp.status_code} on {method} {path}: {resp.text}")
        return resp.json()

    # ---------- market data ----------

    def get_markets(self, series_ticker: str, status: str = "open", limit: int = 100,
                     cursor: Optional[str] = None) -> dict:
        params = {"series_ticker": series_ticker, "status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/markets", params=params)

    def get_market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}")

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    def get_market_rules_text(self, ticker: str) -> str:
        """
        Kalshi generally publishes rules as a linked PDF/text per market series
        rather than a clean API field. This pulls whatever free-text rules
        field is available on the market object; if it's empty, you'll need
        to fetch the rules PDF URL yourself and pass its text to rules_extractor.
        """
        market = self.get_market(ticker).get("market", {})
        return market.get("rules_primary", "") or market.get("rules_secondary", "") or ""

    def get_market_settlement(self, ticker: str) -> tuple[bool, Optional[str]]:
        """
        Returns (is_settled, result) where result is 'yes' or 'no' once known.
        Kalshi marks a settled market's status as 'finalized' or 'settled'
        (naming has varied) and sets a 'result' field to 'yes'/'no'. Verify
        the exact field names against current docs/API responses — this
        checks a couple of likely variants defensively rather than assuming one.
        """
        market = self.get_market(ticker).get("market", {})
        status = (market.get("status") or "").lower()
        result = market.get("result")
        settled = status in ("finalized", "settled", "closed") and result in ("yes", "no")
        return settled, result

    # ---------- account ----------

    def get_balance(self) -> dict:
        return self._request("GET", "/portfolio/balance")

    def get_positions(self) -> dict:
        return self._request("GET", "/portfolio/positions")

    # ---------- trading ----------

    def create_order(self, ticker: str, side: str, action: str, count: int,
                      price_cents: int, order_type: str = "limit",
                      client_order_id: Optional[str] = None) -> dict:
        """
        side: 'yes' or 'no'
        action: 'buy' or 'sell'
        price_cents: limit price in cents (1-99)
        """
        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
            "yes_price" if side == "yes" else "no_price": price_cents,
            "client_order_id": client_order_id or f"bot-{int(time.time()*1000)}",
        }
        return self._request("POST", "/portfolio/orders", json_body=body)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/orders/{order_id}")
