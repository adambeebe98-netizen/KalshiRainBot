"""
Live, second-by-second Kalshi market data capture via WebSocket -- runs
continuously, forever, as its own systemd service, separate from bot.py.

WHY A SEPARATE SCRIPT FROM historical_backfill.py: that pulls hourly
candlesticks for markets that have ALREADY settled, reconstructible any
time from Kalshi's own historical archive. This captures genuine
tick-by-tick market activity AS IT HAPPENS -- once a moment passes without
this running, that moment's exact price movement is gone for good. There
is no "backfill" for this data.

MESSAGE ENVELOPE (confirmed live against docs.kalshi.com's own quick-start
guide, Sept 2026): every server->client message is
    {"type": "<channel-or-control-name>", "sid": <int>, "msg": {...}}
Prices inside `msg` use the exact same "<field>_dollars" decimal-STRING
convention already confirmed and fixed for the REST /markets endpoint
(kalshi_client.market_price_cents) -- e.g. "yes_bid_dollars": "0.5500", NOT
a plain "yes_bid": 55 integer-cents field. This script reuses that same
helper rather than re-deriving the parsing logic a second time.

Event-contract (non-margin) ticker/trade messages carry `ts` in Unix
SECONDS -- confirmed distinct from Kalshi's separate margin/perps product,
which uses `ts_ms` instead; don't cross the two conventions.

Subscribed channels: `ticker`, `trade` (public market data; no
additional per-channel auth beyond the authenticated WS session itself),
plus `market_lifecycle_v2` as a best-effort way to pick up newly-opened
markets without a restart. Since I could not directly confirm whether
market_lifecycle_v2 can be subscribed globally (i.e. without already
knowing which market_tickers to ask for) versus scoped only to markets
you already track, this script does NOT rely on it alone: a periodic
re-discovery pass (REDISCOVER_INTERVAL_SECONDS) re-runs the same
series/market discovery bot.py already uses and subscribes to anything
new, independent of whatever market_lifecycle_v2 actually does.

Every message this script cannot confidently parse into the columns
below is still written in full via raw_json -- capturing something
imperfectly-parsed beats silently dropping data we can never get back.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import websockets

from config import SETTINGS
from kalshi_client import KalshiClient, auth_headers, derive_ws_url, load_private_key, market_price_cents
import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("realtime_kalshi_ws")

RECONNECT_BASE_DELAY_SECONDS = 2
RECONNECT_MAX_DELAY_SECONDS = 60
REDISCOVER_INTERVAL_SECONDS = 15 * 60  # safety net independent of market_lifecycle_v2
DATA_CHANNELS = ["ticker", "trade", "market_lifecycle_v2"]

# Channel/type names that are protocol control messages, not market data --
# logged, never written to realtime_ticks.
CONTROL_TYPES = {"subscribed", "unsubscribed", "ok", "error", "ping", "pong"}


@dataclass
class ParsedTick:
    ticker: str
    channel: str
    ts: int
    yes_price_cents: Optional[int]
    yes_bid_cents: Optional[int]
    yes_ask_cents: Optional[int]
    volume: Optional[int]
    open_interest: Optional[int]


def discover_open_market_tickers(kalshi: KalshiClient) -> set[str]:
    """The same series-discovery bot.py already relies on live -- every
    open market across every series matching SETTINGS.discovery_keywords
    (falling back to SETTINGS.series_tickers if discovery itself fails),
    so this listener tracks the same markets the trading bot does without
    a second, separately-maintained city list."""
    series_tickers: set[str] = set(SETTINGS.series_tickers)
    if SETTINGS.auto_discover_series:
        for keyword in SETTINGS.discovery_keywords:
            try:
                series_tickers.update(kalshi.discover_series_tickers(keyword))
            except Exception:
                log.exception("discover_series_tickers(%s) failed", keyword)

    tickers: set[str] = set()
    for series_ticker in series_tickers:
        cursor = None
        for _ in range(20):  # same defensive pagination cap as discover_series_tickers
            try:
                resp = kalshi.get_markets(series_ticker, status="open", cursor=cursor)
            except Exception:
                log.exception("get_markets(%s) failed during discovery", series_ticker)
                break
            for market in resp.get("markets", []):
                t = market.get("ticker")
                if t:
                    tickers.add(t)
            cursor = resp.get("cursor")
            if not cursor:
                break
    return tickers


def _first_price(msg: dict, *field_names: str) -> Optional[int]:
    """Tries each plausible '<field>_dollars' field name in order, since
    the exact field Kalshi uses for a given channel's "last/trade price"
    wasn't confirmable with full certainty from documentation alone --
    unlike yes_bid_dollars/yes_ask_dollars, which ARE directly confirmed.
    Returns the first one present; None if none are. raw_json remains the
    ground truth regardless of whether this guess lands correctly."""
    for name in field_names:
        val = market_price_cents(msg, name)
        if val is not None:
            return val
    return None


def parse_message(raw_message: str, received_ts: int) -> tuple[Optional[ParsedTick], str, dict]:
    """Returns (ParsedTick-or-None, type_str, msg_dict). ParsedTick is None
    for control messages (subscribed/error/etc) or anything missing a
    market_ticker -- those are still logged by the caller, just never
    written as a tick row."""
    data = json.loads(raw_message)
    msg_type = data.get("type", "unknown")
    msg = data.get("msg", {}) or {}

    if msg_type in CONTROL_TYPES:
        return None, msg_type, msg

    ticker = msg.get("market_ticker")
    if not ticker:
        return None, msg_type, msg

    ts = msg.get("ts")
    if not isinstance(ts, (int, float)):
        ts = received_ts  # best-effort fallback -- still captured via raw_json either way

    tick = ParsedTick(
        ticker=ticker,
        channel=msg_type,
        ts=int(ts),
        yes_price_cents=_first_price(msg, "yes_price", "price", "last_price"),
        yes_bid_cents=_first_price(msg, "yes_bid"),
        yes_ask_cents=_first_price(msg, "yes_ask"),
        volume=msg.get("volume") if isinstance(msg.get("volume"), int) else msg.get("count"),
        open_interest=msg.get("open_interest") if isinstance(msg.get("open_interest"), int) else None,
    )
    return tick, msg_type, msg


def is_tracked_series_ticker(market_ticker: str, keywords: tuple[str, ...]) -> bool:
    """CONFIRMED LIVE: Kalshi's market_lifecycle_v2 channel fires for every
    market on the ENTIRE exchange (crypto price ladders, sports, elections,
    everything) regardless of the market_tickers passed alongside it in the
    same subscribe command -- this makes sense in hindsight, since a "new
    market just opened" event inherently can't be scoped to a ticker that
    doesn't exist yet at subscribe time. Confirmed the hard way: a single
    burst of ~25 new KXBTCD (Bitcoin) markets opening at once triggered
    ~25 unrelated subscribe calls in a row, which stalled the event loop
    long enough to starve the connection's own keepalive ping and get us
    disconnected. So this filters client-side using the exact same
    substring-match logic as discover_series_tickers() -- a market ticker
    always starts with its series ticker as a prefix, so this correctly
    catches only OUR weather series, never anything else on the exchange."""
    ticker_lower = market_ticker.lower()
    return any(keyword.lower() in ticker_lower for keyword in keywords)


async def _subscribe(ws, channels: list[str], market_tickers: list[str], next_id: list[int]) -> None:
    if not market_tickers:
        return
    # Kalshi's own docs don't confirm a hard per-subscribe-message ticker
    # cap, but batching defensively avoids sending one enormous frame for
    # ~100+ markets at once.
    BATCH = 200
    for i in range(0, len(market_tickers), BATCH):
        batch = market_tickers[i:i + BATCH]
        cmd = {
            "id": next_id[0],
            "cmd": "subscribe",
            "params": {"channels": channels, "market_tickers": batch},
        }
        next_id[0] += 1
        await ws.send(json.dumps(cmd))


async def run_listener() -> None:
    kalshi = KalshiClient()
    private_key = load_private_key(SETTINGS.kalshi_private_key_path)
    ws_base = derive_ws_url(SETTINGS.kalshi_base_url)
    ws_path = "/trade-api/ws/v2"
    ws_url = ws_base

    reconnect_delay = RECONNECT_BASE_DELAY_SECONDS

    while True:
        try:
            headers = auth_headers(SETTINGS.kalshi_api_key_id, private_key, "GET", ws_path)
            log.info("Connecting to %s", ws_url)
            async with websockets.connect(ws_url, additional_headers=headers) as ws:
                reconnect_delay = RECONNECT_BASE_DELAY_SECONDS  # reset on a successful connect
                next_id = [1]

                tracked = discover_open_market_tickers(kalshi)
                log.info("Discovered %d open markets to track", len(tracked))
                await _subscribe(ws, DATA_CHANNELS, sorted(tracked), next_id)

                last_rediscover = time.time()
                ticks_written = 0
                last_log = time.time()

                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        # No message in 30s is plausible during quiet overnight
                        # hours, not necessarily a dead connection -- the
                        # underlying `websockets` library handles protocol-level
                        # ping/pong keepalive on its own either way.
                        pass
                    else:
                        received_ts = int(time.time())
                        try:
                            tick, msg_type, msg = parse_message(raw, received_ts)
                        except Exception:
                            log.exception("Failed to parse message, storing raw only: %s", raw[:300])
                            continue

                        if msg_type == "error":
                            log.warning("Kalshi WS error message: %s", msg)
                            continue

                        if tick is not None and tick.channel == "market_lifecycle_v2":
                            # Filter BEFORE storing or acting -- see
                            # is_tracked_series_ticker's own docstring for why
                            # this channel needs a client-side filter at all.
                            if not is_tracked_series_ticker(tick.ticker, SETTINGS.discovery_keywords):
                                continue
                            if tick.ticker not in tracked:
                                tracked.add(tick.ticker)
                                await _subscribe(ws, ["ticker", "trade"], [tick.ticker], next_id)
                                log.info("New weather market via lifecycle event, subscribed: %s", tick.ticker)

                        if tick is not None:
                            storage.save_realtime_tick(
                                ticker=tick.ticker, channel=tick.channel, ts=tick.ts,
                                received_ts=received_ts, yes_price_cents=tick.yes_price_cents,
                                yes_bid_cents=tick.yes_bid_cents, yes_ask_cents=tick.yes_ask_cents,
                                volume=tick.volume, open_interest=tick.open_interest, raw_json=raw,
                            )
                            ticks_written += 1

                    now = time.time()
                    if now - last_log > 300:
                        log.info("%d ticks written in the last 5 min, tracking %d markets",
                                  ticks_written, len(tracked))
                        ticks_written = 0
                        last_log = now

                    if now - last_rediscover > REDISCOVER_INTERVAL_SECONDS:
                        fresh = discover_open_market_tickers(kalshi)
                        new_tickers = fresh - tracked
                        if new_tickers:
                            log.info("Re-discovery found %d new markets", len(new_tickers))
                            await _subscribe(ws, ["ticker", "trade"], sorted(new_tickers), next_id)
                            tracked.update(new_tickers)
                        last_rediscover = now

        except Exception:
            log.exception("WebSocket connection lost, reconnecting in %ds", reconnect_delay)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_DELAY_SECONDS)


if __name__ == "__main__":
    storage.init_db()
    asyncio.run(run_listener())
