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
        resp = self._client.request(method, full_path, params=params, json=json_body, headers=headers)
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

    def get_orderbook_levels(self, ticker: str, depth: int = 50) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
        """
        Returns (yes_bid_levels, no_bid_levels), each a list of (price_cents,
        count) sorted BEST PRICE FIRST (i.e. highest first — reversed from
        Kalshi's own ascending order, since that's the order you'd actually
        walk to fill a market order).

        Confirmed against Kalshi's own docs (docs.kalshi.com/getting_started/
        orderbook_responses): the API returns BIDS ONLY, as
        {"orderbook_fp": {"yes_dollars": [[price_dollars_str, count_fp_str], ...],
        "no_dollars": [...]}}, sorted ascending by price. There is no
        separate ask array — an ask on one side is derived from the best
        bid on the OTHER side (yes_ask = 100 - best_no_bid_cents, and
        vice versa). See implied_ask_levels() below for that conversion.
        """
        raw = self.get_orderbook(ticker, depth=depth)
        book = raw.get("orderbook_fp", raw.get("orderbook", {}))

        def parse_levels(raw_levels) -> list[tuple[int, int]]:
            levels = []
            for entry in raw_levels or []:
                price_str, count_str = entry[0], entry[1]
                price_cents = round(float(price_str) * 100)
                count = round(float(count_str))
                levels.append((price_cents, count))
            levels.sort(key=lambda x: x[0], reverse=True)  # best (highest) price first
            return levels

        yes_bids = parse_levels(book.get("yes_dollars"))
        no_bids = parse_levels(book.get("no_dollars"))
        return yes_bids, no_bids

    def get_market_rules_text(self, ticker: str) -> str:
        """
        Kalshi generally publishes rules as a linked PDF/text per market series
        rather than a clean API field. This pulls whatever free-text rules
        field is available on the market object; if it's empty, you'll need
        to fetch the rules PDF URL yourself and pass its text to rules_extractor.
        """
        market = self.get_market(ticker).get("market", {})
        return market.get("rules_primary", "") or market.get("rules_secondary", "") or ""

    def get_series_list(self, limit: int = 200, cursor: Optional[str] = None) -> dict:
        """
        GET /series — browse ALL series templates Kalshi currently has
        listed. Used for auto-discovering every rain series (one per city)
        instead of hardcoding a city list that goes stale the moment Kalshi
        adds or removes a city.
        """
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/series", params=params)

    def discover_series_tickers(self, keyword: str) -> list[str]:
        """
        Paginates through the full series list and returns every ticker
        whose ticker OR title contains `keyword` (case-insensitive) — e.g.
        keyword='rain' finds KXRAIN, KXRAINNYC, KXRAINAUS, etc, whatever
        Kalshi currently has listed, without needing a maintained city list.
        """
        matches = []
        cursor = None
        keyword_lower = keyword.lower()
        for _ in range(20):  # hard cap on pagination loops, just in case
            resp = self.get_series_list(cursor=cursor)
            for series in resp.get("series", []):
                ticker = series.get("ticker", "")
                title = series.get("title", "")
                if keyword_lower in ticker.lower() or keyword_lower in title.lower():
                    matches.append(ticker)
            cursor = resp.get("cursor")
            if not cursor:
                break
        return matches

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
                      time_in_force: str = "good_till_canceled",
                      client_order_id: Optional[str] = None) -> dict:
        """
        Public signature kept as (side: 'yes'/'no', action: 'buy'/'sell',
        price_cents: 1-99) for backward compatibility with existing callers
        (bot.py). Internally translated to Kalshi's CURRENT V2 order schema,
        confirmed against docs.kalshi.com/api-reference/orders/create-order-v2
        on 2026-09-08:

          POST /portfolio/events/orders
          {
            "ticker": ...,
            "side": "bid" | "ask",       # NOT "yes"/"no" — V2 quotes
                                          # everything from the YES leg only:
                                          # bid = buy YES, ask = sell YES
                                          # (selling YES == buying NO at 1-price)
            "count": "<fixed-point string, up to 2 decimals>",
            "price": "<fixed-point dollar string, e.g. '0.5600'>",
            "time_in_force": "good_till_canceled" | "fill_or_kill" | "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross" | "maker",
            "client_order_id": ...
          }

        Response shape also changed: order_id/fill_count/remaining_count/ts_ms
        come back FLAT (no "order" wrapper like the legacy endpoint had) —
        callers reading order["order"]["order_id"] need to read order["order_id"]
        instead (bot.py updated to match).

        The old integer-cents legacy endpoint (/portfolio/orders) still works
        per Kalshi's docs (deprecated no earlier than May 6, 2026, not yet
        removed as of this writing), but their own quick-start guide now
        defaults to V2, so this migrates rather than patching the legacy path.

        yes/no + buy/sell -> bid/ask + YES-leg price, derived as:
          v2_side = 'bid' if (side == 'yes') == (action == 'buy') else 'ask'
          yes_price_cents = price_cents if side == 'yes' else (100 - price_cents)
        (buying NO at P is economically selling YES at 1-P, and vice versa.)

        ⚠️ Schema now matches Kalshi's published docs, but published docs and
        live behavior aren't guaranteed identical — this has NOT been
        confirmed against a real order on the live API. Do not remove the
        order_schema_verified guard (config.SETTINGS.order_schema_verified /
        bot.py's live-mode check) until an actual test order has been placed
        and confirmed to work.
        """
        is_yes = side == "yes"
        is_buy = action == "buy"
        v2_side = "bid" if is_yes == is_buy else "ask"
        yes_price_cents = price_cents if is_yes else (100 - price_cents)

        body = {
            "ticker": ticker,
            "side": v2_side,
            "count": f"{count:.2f}",
            "price": f"{yes_price_cents / 100:.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id or f"bot-{int(time.time()*1000)}",
        }
        return self._request("POST", "/portfolio/events/orders", json_body=body)

    def cancel_order(self, order_id: str) -> dict:
        # V2 path — legacy was /portfolio/orders/{order_id}. Response shape
        # is also flat: {order_id, client_order_id, reduced_by}.
        return self._request("DELETE", f"/portfolio/events/orders/{order_id}")
