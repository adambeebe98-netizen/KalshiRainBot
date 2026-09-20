"""Probe the live Kalshi API surface, and diff it against this client.

The published spec at readme.io serves HTML, not JSON, so it cannot be
diffed. Probing the production API directly is better evidence anyway:
it reflects what the exchange actually answers today rather than what
the docs last said.

Every candidate below is requested once. A 200 means it exists and is
public; 401/403 means it exists behind auth; 404 means it does not.
Rate limiting (429) is retried, since a 429 is not an answer.

The output column that matters is the one listing endpoints that
EXIST, return data, and are never called by this bot -- because that is
the set of things missing from the archive for no reason other than
nobody having asked.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from kalshi_client import KalshiClient

kalshi = KalshiClient()
CLIENT = Path("/root/kalshi_weather_bot/kalshi_client.py")

# A real ticker/series to fill path parameters with.
TICKER = "KXRAINNYC-26APR07-T0"
SERIES = "KXRAINNYC"
EVENT = "KXRAINNYC-26APR07"

CANDIDATES = [
    # market data
    "/markets",
    f"/markets/{TICKER}",
    f"/markets/{TICKER}/orderbook",
    "/markets/trades",
    "/events",
    f"/events/{EVENT}",
    "/series",
    f"/series/{SERIES}",
    f"/series/{SERIES}/markets/{TICKER}/candlesticks",
    # historical mirrors
    "/historical/markets",
    f"/historical/markets/{TICKER}",
    f"/historical/markets/{TICKER}/candlesticks",
    "/historical/trades",
    "/historical/events",
    "/historical/series",
    # exchange metadata
    "/exchange/status",
    "/exchange/schedule",
    "/exchange/user_data_timestamp",
    # structure
    "/milestones",
    "/structured_targets",
    "/multivariate_event_collections",
    # account (auth)
    "/portfolio/balance",
    "/portfolio/positions",
    "/portfolio/orders",
    "/portfolio/fills",
    "/portfolio/settlements",
    "/portfolio/resting_order_total_value",
    "/communications/quotes",
    "/communications/rfqs",
]


def probe(path: str):
    for attempt in range(4):
        try:
            resp = kalshi._request("GET", path, params={"limit": 1})
            return 200, resp
        except Exception as exc:
            msg = str(exc)
            code = 0
            m = re.search(r"error (\d{3})", msg)
            if m:
                code = int(m.group(1))
            if code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            return code or -1, msg[:60]
    return 429, "rate limited"


src = CLIENT.read_text(encoding="utf-8")
called = set(re.findall(r'_request\(\s*"[A-Z]+",\s*f?"([^"]+)"', src))
called_norm = {re.sub(r"\{[^}]*\}", "{}", c).rstrip("/") for c in called}


def is_called(path: str) -> bool:
    norm = path
    for value in (TICKER, SERIES, EVENT):
        norm = norm.replace(value, "{}")
    return norm in called_norm


print(f"{'endpoint':<52} {'status':<9} {'called':<7} payload")
print("-" * 96)

never_called = []
for path in CANDIDATES:
    code, body = probe(path)
    if code == 200:
        status = "OK"
        keys = list(body)[:4] if isinstance(body, dict) else type(body).__name__
        payload = str(keys)
    elif code in (401, 403):
        status = "AUTH"
        payload = "(exists, needs auth)"
    elif code == 404:
        status = "404"
        payload = ""
    else:
        status = str(code)
        payload = str(body)[:40]

    mark = "yes" if is_called(path) else "NO"
    print(f"{path:<52} {status:<9} {mark:<7} {payload}")

    if code == 200 and mark == "NO":
        never_called.append(path)
    time.sleep(0.25)

print("\n" + "=" * 96)
print("EXISTS, RETURNS DATA, NEVER CALLED BY THIS BOT")
print("=" * 96)
for p in never_called:
    print(f"   {p}")
print(f"\n   {len(never_called)} endpoints")
