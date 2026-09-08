#!/usr/bin/env bash
# One-shot setup + deploy script for the Kalshi Weather Bot.
# Run this on a small Linux server (see README-DEPLOY.md for how to get one).
# It writes all the code, installs dependencies, asks for your credentials,
# and starts the bot running in the background in PAPER mode.
set -e

APP_DIR="$HOME/kalshi_weather_bot"
echo "Installing into $APP_DIR ..."
mkdir -p "$APP_DIR"
cd "$APP_DIR"

echo "Writing project files..."
cat > config.py << 'PYEOF_CONFIG_PY'
"""
Central configuration. Everything here is read from environment variables
so no secrets ever live in code. Copy .env.example to .env and fill it in.
"""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val else default


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


# Risk mode presets — a shorthand for the three risk knobs below. Explicit
# MIN_EDGE_CENTS / MAX_POSITION_PCT / MAX_DAILY_LOSS_PCT env vars, if set,
# always override whatever the preset supplies.
_RISK_PRESETS = {
    "conservative": {"min_edge_cents": 10, "max_position_pct": 0.015, "max_daily_loss_pct": 0.04},
    "balanced":     {"min_edge_cents": 6,  "max_position_pct": 0.03,  "max_daily_loss_pct": 0.06},
    "aggressive":   {"min_edge_cents": 3,  "max_position_pct": 0.05,  "max_daily_loss_pct": 0.10},
}
_RISK_MODE = os.getenv("RISK_MODE", "balanced").strip().lower()
_PRESET = _RISK_PRESETS.get(_RISK_MODE, _RISK_PRESETS["balanced"])


@dataclass(frozen=True)
class Settings:
    # --- Kalshi API ---
    # NOTE: Kalshi has used a few different API hostnames over time
    # (trading-api.kalshi.com, api.elections.kalshi.com, external-api.kalshi.com).
    # Confirm the current one at https://docs.kalshi.com before running, and
    # set it here rather than trusting this default blindly.
    kalshi_base_url: str = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
    kalshi_api_key_id: str = os.getenv("KALSHI_API_KEY_ID", "")
    kalshi_private_key_path: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")

    # --- Anthropic API (used only to parse market rules text into structured fields) ---
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")

    # --- Trading mode ---
    # LIVE_TRADING must be explicitly "true" AND --live must be passed on the
    # command line for real orders to ever be sent. Belt and suspenders.
    live_trading_enabled: bool = _bool("LIVE_TRADING", False)
    # Separate, explicit second flag required for live trading to start with
    # no human at a terminal to type the confirmation phrase (e.g. under
    # systemd). Defaults to false — headless live trading is opt-in twice over.
    live_confirmed_headless: bool = _bool("LIVE_TRADING_CONFIRMED", False)

    # --- Risk mode ---
    # 'conservative' | 'balanced' | 'aggressive' — sets the three risk knobs
    # below to a sane preset. Falls back to 'balanced' on an unrecognized value.
    risk_mode: str = _RISK_MODE if _RISK_MODE in _RISK_PRESETS else "balanced"

    # --- Bankroll / risk controls ---
    starting_bankroll_cents: int = _int("STARTING_BANKROLL_CENTS", 50_000)  # $500 default
    max_position_pct: float = _float("MAX_POSITION_PCT", _PRESET["max_position_pct"])
    max_daily_loss_pct: float = _float("MAX_DAILY_LOSS_PCT", _PRESET["max_daily_loss_pct"])
    min_edge_cents: int = _int("MIN_EDGE_CENTS", _PRESET["min_edge_cents"])
    min_contract_price_cents: int = _int("MIN_CONTRACT_PRICE_CENTS", 2)
    max_contract_price_cents: int = _int("MAX_CONTRACT_PRICE_CENTS", 90)
    max_open_positions: int = _int("MAX_OPEN_POSITIONS", 15)

    # --- Loop timing ---
    poll_interval_seconds: int = _int("POLL_INTERVAL_SECONDS", 300)  # 5 min between scan cycles
    rules_cache_path: str = os.getenv("RULES_CACHE_PATH", "rules_cache.json")
    db_path: str = os.getenv("DB_PATH", "bot_state.db")

    # --- Market scope ---
    # Kalshi weather series tickers to scan, e.g. rain/precip series for specific cities.
    # Keep this list explicit rather than scanning all markets — precision over coverage.
    series_tickers: tuple = tuple(
        t.strip() for t in os.getenv(
            "SERIES_TICKERS",
            "KXRAIN,KXHIGHTEMP"
        ).split(",") if t.strip()
    )


SETTINGS = Settings()

PYEOF_CONFIG_PY

cat > kalshi_client.py << 'PYEOF_KALSHI_CLIENT_PY'
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

PYEOF_KALSHI_CLIENT_PY

cat > weather_data.py << 'PYEOF_WEATHER_DATA_PY'
"""
Pulls weather data from api.weather.gov (free, no key required).

The whole point of this module is to answer the SAME question the Kalshi
contract will be graded on — precipitation at a specific station — not
"is it raining somewhere in the metro area." Station precision is the edge.

api.weather.gov gives NWS data. Some Kalshi rain contracts settle on
The Weather Company instead of raw NWS — rules_extractor.py records which
source a given market actually uses so you know when NWS data is only a
proxy/estimate rather than the literal settlement feed.
"""
from __future__ import annotations

import httpx
from dataclasses import dataclass
from typing import Optional

NWS_BASE = "https://api.weather.gov"
HEADERS = {"User-Agent": "kalshi-weather-bot (contact: set-your-email-here)"}


@dataclass
class StationObservation:
    station_id: str
    precipitation_last_hour_mm: Optional[float]
    precipitation_last_3hr_mm: Optional[float]
    description: str
    timestamp: str


@dataclass
class PrecipForecast:
    period_name: str
    probability_of_precipitation_pct: Optional[int]
    short_forecast: str


def get_station_latest_observation(station_id: str) -> Optional[StationObservation]:
    """station_id is an NWS station code, e.g. 'KAUS' for Austin-Bergstrom."""
    url = f"{NWS_BASE}/stations/{station_id}/observations/latest"
    with httpx.Client(headers=HEADERS, timeout=10.0) as client:
        resp = client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json().get("properties", {})
        precip_1h = (data.get("precipitationLastHour") or {}).get("value")
        precip_3h = (data.get("precipitationLast3Hours") or {}).get("value")
        return StationObservation(
            station_id=station_id,
            precipitation_last_hour_mm=precip_1h,
            precipitation_last_3hr_mm=precip_3h,
            description=data.get("textDescription", ""),
            timestamp=data.get("timestamp", ""),
        )


def get_forecast_pop(lat: float, lon: float) -> list[PrecipForecast]:
    """
    Probability-of-precipitation forecast for a lat/lon. This is a genuine
    forecast (not an observation) — use it for markets that haven't resolved
    yet, and treat it as one input to your model, not the model itself.
    """
    with httpx.Client(headers=HEADERS, timeout=10.0) as client:
        points = client.get(f"{NWS_BASE}/points/{lat},{lon}")
        points.raise_for_status()
        forecast_url = points.json()["properties"]["forecast"]
        forecast = client.get(forecast_url)
        forecast.raise_for_status()
        periods = forecast.json()["properties"]["periods"]
        out = []
        for p in periods:
            pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
            out.append(PrecipForecast(
                period_name=p.get("name", ""),
                probability_of_precipitation_pct=pop,
                short_forecast=p.get("shortForecast", ""),
            ))
        return out


# Common settlement-station reference points for major Kalshi weather cities.
# Extend this as you add series. Coordinates are for the named station itself,
# not just "the city," since the station is what actually gets graded.
STATION_REFERENCE = {
    "KAUS": {"name": "Austin-Bergstrom Intl", "lat": 30.1975, "lon": -97.6664},
    "KLAS": {"name": "Las Vegas Harry Reid Intl", "lat": 36.0840, "lon": -115.1537},
    "KMDW": {"name": "Chicago Midway", "lat": 41.7868, "lon": -87.7522},
    "KHOU": {"name": "Houston Hobby", "lat": 29.6454, "lon": -95.2789},
    "KDEN": {"name": "Denver Intl", "lat": 39.8561, "lon": -104.6737},
}

PYEOF_WEATHER_DATA_PY

cat > rules_extractor.py << 'PYEOF_RULES_EXTRACTOR_PY'
"""
Kalshi market rules text is free-form and varies market to market. Before
trading a market you need to know, precisely: which station, which data
provider, what threshold counts as a "yes," and what happens on missing data.

Rather than hand-parsing every market's rules PDF, this uses the Anthropic
API to extract those fields into structured JSON, with a local cache so you
never re-pay for the same market twice. This is the one place an LLM
belongs in this system — parsing inconsistent legal text, not deciding
whether to trade.

ALWAYS spot-check a sample of extractions against the actual rules PDF
before trusting this at scale. A parsing mistake here (wrong station, wrong
threshold) silently breaks the whole strategy.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Optional

import anthropic

from config import SETTINGS

EXTRACTION_PROMPT = """You are extracting structured settlement rules from a \
prediction market's contract terms. Read the rules text and return ONLY a \
JSON object (no prose, no markdown fences) with these exact fields:

{
  "station_code": "<the NWS or airport station code, e.g. KAUS, or null if not stated>",
  "settlement_source": "<'NWS' | 'The Weather Company' | 'NOAA CDO' | 'other' | 'unclear'>",
  "measure": "<'precipitation_daily' | 'precipitation_monthly' | 'temperature_high' | 'temperature_low' | 'other'>",
  "threshold_description": "<plain-language threshold, e.g. 'strictly greater than 0 inches' or 'high temperature 85-89F'>",
  "trace_counts_as_zero": <true | false | null if not stated>,
  "fallback_rule": "<brief description of what happens if primary source has no data, or null>",
  "confidence": "<'high' | 'medium' | 'low' — your confidence this extraction is complete and correct>"
}

Rules text:
---
{rules_text}
---"""


@dataclass
class MarketRules:
    ticker: str
    station_code: Optional[str]
    settlement_source: str
    measure: str
    threshold_description: str
    trace_counts_as_zero: Optional[bool]
    fallback_rule: Optional[str]
    confidence: str


class RulesExtractor:
    def __init__(self, cache_path: str | None = None, api_key: str | None = None):
        self.cache_path = cache_path or SETTINGS.rules_cache_path
        self._client = anthropic.Anthropic(api_key=api_key or SETTINGS.anthropic_api_key)
        self._cache = self._load_cache()

    def _load_cache(self) -> dict:
        if os.path.exists(self.cache_path):
            with open(self.cache_path, "r") as f:
                return json.load(f)
        return {}

    def _save_cache(self) -> None:
        with open(self.cache_path, "w") as f:
            json.dump(self._cache, f, indent=2)

    def extract(self, ticker: str, rules_text: str, force: bool = False) -> MarketRules:
        if not force and ticker in self._cache:
            return MarketRules(**self._cache[ticker])

        if not rules_text.strip():
            result = MarketRules(
                ticker=ticker, station_code=None, settlement_source="unclear",
                measure="other", threshold_description="no rules text available",
                trace_counts_as_zero=None, fallback_rule=None, confidence="low",
            )
            self._cache[ticker] = asdict(result)
            self._save_cache()
            return result

        prompt = EXTRACTION_PROMPT.replace("{rules_text}", rules_text[:6000])
        response = self._client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(block.text for block in response.content if hasattr(block, "text"))
        try:
            parsed = json.loads(raw.strip().strip("`").removeprefix("json").strip())
        except (json.JSONDecodeError, ValueError):
            parsed = {
                "station_code": None, "settlement_source": "unclear", "measure": "other",
                "threshold_description": "extraction failed — parse manually",
                "trace_counts_as_zero": None, "fallback_rule": None, "confidence": "low",
            }

        result = MarketRules(ticker=ticker, **parsed)
        self._cache[ticker] = asdict(result)
        self._save_cache()
        return result

PYEOF_RULES_EXTRACTOR_PY

cat > risk_manager.py << 'PYEOF_RISK_MANAGER_PY'
"""
Everything that stands between "the model found an edge" and "money leaves
the account." This is the part that matters more than the model itself.

Rules encoded here:
  1. Never risk more than max_position_pct of bankroll on one contract.
  2. Hard daily loss limit — once tripped, the bot stops opening new
     positions until the next calendar day (existing positions are left
     to resolve, not panic-closed).
  3. Cap on total concurrent open positions, so a burst of correlated
     weather-system trades (same storm, five cities) can't blow past your
     intended risk in one cycle.
  4. Price sanity band — refuses to trade contracts priced too close to
     0 or 100 cents, where a single tick is a huge % move and slippage/fees
     eat any theoretical edge.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from config import SETTINGS


@dataclass
class RiskState:
    bankroll_cents: int
    day: date
    realized_pnl_today_cents: int = 0
    open_positions_count: int = 0

    def is_kill_switch_tripped(self) -> bool:
        loss_limit = -abs(int(self.bankroll_cents * SETTINGS.max_daily_loss_pct))
        return self.realized_pnl_today_cents <= loss_limit

    def roll_day_if_needed(self, today: date) -> None:
        if today != self.day:
            self.day = today
            self.realized_pnl_today_cents = 0


class RiskManager:
    def __init__(self, state: RiskState):
        self.state = state

    def max_contracts_for_trade(self, price_cents: int) -> int:
        """How many contracts we're willing to buy at this price, capped by
        max_position_pct of current bankroll."""
        if price_cents <= 0:
            return 0
        max_risk_cents = int(self.state.bankroll_cents * SETTINGS.max_position_pct)
        return max(0, max_risk_cents // price_cents)

    def approve_trade(self, price_cents: int, edge_cents: int) -> tuple[bool, str]:
        today = date.today()
        self.state.roll_day_if_needed(today)

        if self.state.is_kill_switch_tripped():
            return False, "daily loss kill switch tripped — no new trades today"

        if self.state.open_positions_count >= SETTINGS.max_open_positions:
            return False, f"at max open positions ({SETTINGS.max_open_positions})"

        if not (SETTINGS.min_contract_price_cents <= price_cents <= SETTINGS.max_contract_price_cents):
            return False, f"price {price_cents}c outside allowed band [{SETTINGS.min_contract_price_cents}, {SETTINGS.max_contract_price_cents}]"

        if edge_cents < SETTINGS.min_edge_cents:
            return False, f"edge {edge_cents}c below minimum {SETTINGS.min_edge_cents}c"

        contracts = self.max_contracts_for_trade(price_cents)
        if contracts < 1:
            return False, "position size rounds to 0 contracts under max_position_pct"

        return True, "approved"

    def record_fill(self, cost_cents: int) -> None:
        self.state.open_positions_count += 1

    def record_settlement(self, pnl_cents: int) -> None:
        self.state.bankroll_cents += pnl_cents
        self.state.realized_pnl_today_cents += pnl_cents
        self.state.open_positions_count = max(0, self.state.open_positions_count - 1)

PYEOF_RISK_MANAGER_PY

cat > storage.py << 'PYEOF_STORAGE_PY'
"""
Everything the bot does gets logged here. If you can't reconstruct exactly
why a trade happened six weeks later, you can't tell whether the strategy
is working or you're fooling yourself with a few lucky weeks.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager

from config import SETTINGS

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    market_price_cents INTEGER,
    model_probability REAL,
    edge_cents INTEGER,
    action TEXT NOT NULL,       -- 'traded' | 'skipped'
    reason TEXT,
    mode TEXT NOT NULL          -- 'paper' | 'live'
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    count INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    mode TEXT NOT NULL,
    order_id TEXT,
    status TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'won' | 'lost'
    settled_ts INTEGER,
    pnl_cents INTEGER,
    model_probability REAL,      -- the (pre-calibration-adjusted) probability behind this trade
    station_code TEXT,           -- settlement station, for calibration lookups
    measure TEXT                 -- 'precipitation_daily' etc, for calibration lookups
);

CREATE TABLE IF NOT EXISTS bankroll_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    bankroll_cents INTEGER NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS calibration_stats (
    station_code TEXT NOT NULL,
    measure TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    sum_predicted REAL NOT NULL DEFAULT 0,
    sum_actual REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (station_code, measure)
);
"""


def _migrate_add_columns(conn) -> None:
    """
    Best-effort migration for anyone who ran an earlier version of this bot
    before the calibration columns existed. Adding a column that already
    exists raises 'duplicate column' — that's fine, ignore it.
    """
    for stmt in (
        "ALTER TABLE trades ADD COLUMN model_probability REAL",
        "ALTER TABLE trades ADD COLUMN station_code TEXT",
        "ALTER TABLE trades ADD COLUMN measure TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass



@contextmanager
def get_conn():
    conn = sqlite3.connect(SETTINGS.db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_add_columns(conn)


def log_decision(ticker: str, side: str, market_price_cents: int, model_probability: float,
                  edge_cents: int, action: str, reason: str, mode: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO decisions (ts, ticker, side, market_price_cents, model_probability, "
            "edge_cents, action, reason, mode) VALUES (?,?,?,?,?,?,?,?,?)",
            (int(time.time()), ticker, side, market_price_cents, model_probability,
             edge_cents, action, reason, mode),
        )


def log_trade(ticker: str, side: str, count: int, price_cents: int, mode: str,
              order_id: str | None, model_probability: float | None = None,
              station_code: str | None = None, measure: str | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO trades (ts, ticker, side, count, price_cents, mode, order_id, "
            "model_probability, station_code, measure) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), ticker, side, count, price_cents, mode, order_id,
             model_probability, station_code, measure),
        )
        return cur.lastrowid


def get_open_trades(mode: str | None = None) -> list[dict]:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        if mode:
            rows = cur.execute("SELECT * FROM trades WHERE status='open' AND mode=?", (mode,)).fetchall()
        else:
            rows = cur.execute("SELECT * FROM trades WHERE status='open'").fetchall()
        return [dict(r) for r in rows]


def settle_trade(trade_id: int, won: bool, pnl_cents: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE trades SET status=?, settled_ts=?, pnl_cents=? WHERE id=?",
            ("won" if won else "lost", int(time.time()), pnl_cents, trade_id),
        )


def snapshot_bankroll(bankroll_cents: int, note: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bankroll_snapshots (ts, bankroll_cents, note) VALUES (?,?,?)",
            (int(time.time()), bankroll_cents, note),
        )


def load_last_bankroll(default_cents: int) -> int:
    """
    So a restart (crash, reboot, deploy) doesn't silently reset your
    bankroll back to STARTING_BANKROLL_CENTS and lose track of real P&L.
    Falls back to the configured starting value only if this is a fresh DB.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT bankroll_cents FROM bankroll_snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else default_cents


def record_calibration_outcome(station_code: str, measure: str,
                                predicted_probability: float, actual_outcome: bool) -> None:
    """
    Rolling record of (what we predicted) vs (what actually happened),
    bucketed by station+measure. This is the entire "learning" mechanism —
    no black box, just running sums you can inspect directly in the DB.
    """
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO calibration_stats (station_code, measure, n, sum_predicted, sum_actual)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(station_code, measure) DO UPDATE SET
                n = n + 1,
                sum_predicted = sum_predicted + excluded.sum_predicted,
                sum_actual = sum_actual + excluded.sum_actual
            """,
            (station_code, measure, predicted_probability, 1.0 if actual_outcome else 0.0),
        )


def get_calibration_stats(station_code: str, measure: str) -> tuple[int, float, float]:
    """Returns (n, avg_predicted, avg_actual) for this station+measure, or (0, 0, 0)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT n, sum_predicted, sum_actual FROM calibration_stats "
            "WHERE station_code=? AND measure=?",
            (station_code, measure),
        ).fetchone()
        if not row or row[0] == 0:
            return 0, 0.0, 0.0
        n, sum_pred, sum_actual = row
        return n, sum_pred / n, sum_actual / n

PYEOF_STORAGE_PY

cat > strategy.py << 'PYEOF_STRATEGY_PY'
"""
This is the actual "brain" — but deliberately a simple, inspectable one.
A black-box model you can't explain is the last thing you want with real
money, because when it starts losing you need to know whether it's normal
variance or the model broke.

Model, in plain terms, for a same-day precipitation market:
  1. Pull the settlement station's latest NWS observation (has it already
     rained measurably today, per the actual feed).
  2. Pull the NWS point forecast probability-of-precipitation for the
     remaining hours of the settlement window.
  3. Combine into a rough probability that the station will show
     measurable ( > 0", non-trace) precipitation by settlement.
  4. Compare to the market's implied probability (roughly, the YES price).
  5. Only flag a trade when the gap exceeds your configured minimum edge —
     and only on markets whose rules extraction has 'high' or 'medium'
     confidence, since a low-confidence rules read means you might not
     even know what you're betting on.

This is intentionally NOT a machine-learned model. Start here, log every
decision and outcome (storage.py does this), and only add complexity once
you have real settled trades to check the simple model against. A fancier
model built before you have that data is just a fancier way to be wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import calibration
from rules_extractor import MarketRules
from weather_data import StationObservation, PrecipForecast


@dataclass
class TradeSignal:
    ticker: str
    side: str  # 'yes' or 'no'
    model_probability: float  # model's estimate that the traded side resolves true
    model_probability_yes: float  # model's calibrated estimate of the YES event specifically —
                                   # always stored this way in trades table so calibration.py
                                   # (which tracks "yes" outcomes) reads a consistent quantity
                                   # regardless of which side got traded
    market_implied_probability: float
    edge_cents: int
    rationale: str


def estimate_precip_probability(
    observation: Optional[StationObservation],
    forecast: list[PrecipForecast],
    trace_counts_as_zero: Optional[bool],
) -> tuple[float, str]:
    """Returns (probability measurable precip occurs, human-readable rationale)."""
    notes = []

    # Already-observed precipitation this hour/3hr strongly predicts a "yes"
    # for a daily contract, since the day's total only needs to clear a
    # small threshold once.
    already_measurable = False
    if observation:
        precip_now = observation.precipitation_last_hour_mm or observation.precipitation_last_3hr_mm
        if precip_now and precip_now > 0.0:
            already_measurable = True
            notes.append(f"station already recorded {precip_now:.2f}mm this window")
        else:
            notes.append("no measurable precip at station yet this window")

    if already_measurable:
        return 0.97, "; ".join(notes)

    # Otherwise, fall back to the forecast POP for the remaining periods
    # today, taking the max across remaining daytime/night periods since
    # the contract only needs ONE measurable event, not persistent rain.
    relevant_pops = [
        f.probability_of_precipitation_pct for f in forecast[:2]
        if f.probability_of_precipitation_pct is not None
    ]
    if relevant_pops:
        max_pop = max(relevant_pops) / 100.0
        notes.append(f"forecast max POP over next periods: {max(relevant_pops)}%")
        # POP is not literally "probability of >0 inches at this exact station,"
        # it's probability of measurable precip somewhere in the forecast area —
        # treat it as a noisy proxy, not ground truth, hence no further inflation.
        return max_pop, "; ".join(notes)

    notes.append("no observation or forecast data available")
    return 0.5, "; ".join(notes)  # genuine uncertainty — will almost never clear min edge


def market_implied_probability(yes_price_cents: int) -> float:
    return yes_price_cents / 100.0


def evaluate_market(
    ticker: str,
    yes_price_cents: int,
    rules: MarketRules,
    observation: Optional[StationObservation],
    forecast: list[PrecipForecast],
) -> TradeSignal:
    raw_model_p, rationale = estimate_precip_probability(
        observation, forecast, rules.trace_counts_as_zero
    )

    # Apply the learned per-station calibration bias (see calibration.py).
    # Early on, before enough settled trades exist, this is a no-op.
    model_p, calibration_note = calibration.apply_calibration(
        raw_model_p, rules.station_code, rules.measure
    )
    rationale = f"{rationale}; calibration: {calibration_note}"

    market_p = market_implied_probability(yes_price_cents)

    # Decide which side has the edge. If model thinks YES is more likely
    # than the market does, the edge is on buying YES; if model thinks NO
    # is more likely, edge is on NO. Price for NO is (100 - yes_price).
    yes_edge = round((model_p - market_p) * 100)
    no_edge = round(((1 - model_p) - (1 - market_p)) * 100)  # == -yes_edge, kept explicit for clarity

    if yes_edge >= no_edge:
        side, edge_cents, price = "yes", yes_edge, yes_price_cents
    else:
        side, edge_cents, price = "no", no_edge, 100 - yes_price_cents

    confidence_note = f" [rules confidence: {rules.confidence}, source: {rules.settlement_source}]"
    return TradeSignal(
        ticker=ticker,
        side=side,
        model_probability=model_p if side == "yes" else 1 - model_p,
        model_probability_yes=model_p,
        market_implied_probability=market_p if side == "yes" else 1 - market_p,
        edge_cents=edge_cents,
        rationale=rationale + confidence_note,
    )

PYEOF_STRATEGY_PY

cat > calibration.py << 'PYEOF_CALIBRATION_PY'
"""
This is what "the bot learns over time" actually means here, concretely:

For each (station, measure) pair — e.g. (KAUS, precipitation_daily) — it
keeps a running average of what the model predicted vs what actually
happened. If Austin rain contracts the model called "70% likely" only
resolved yes 45% of the time historically, that's a real, measurable bias:
Austin's convective, hit-or-miss summer storms are exactly the kind of
pattern that inflates a naive POP-based estimate. The bias gets subtracted
from future Austin predictions automatically.

This is deliberately NOT a neural net, gradient descent, or anything that
can quietly develop behavior you can't inspect. It's an auditable running
average. You can query calibration_stats directly and see exactly why the
bot's Austin estimates drifted. That auditability matters more than
sophistication when real money is on the line — a fancier model you can't
explain is worse, not better, when it starts doing something surprising.

Requires a minimum sample size before applying any correction, because a
"bias" computed from 4 data points is noise, not a pattern — and a false
signal from small samples nudging real bets is worse than no correction.
"""
from __future__ import annotations

import storage

MIN_SAMPLES_FOR_CALIBRATION = 20
MAX_BIAS_ADJUSTMENT = 0.20  # never let calibration alone shift a prediction by more than this


def get_bias(station_code: str | None, measure: str | None) -> tuple[float, str]:
    """
    Returns (bias, explanation). Bias is added to the raw model probability:
    a negative bias means "this station's contracts have historically
    resolved yes less often than the model expected" (e.g. Austin).
    """
    if not station_code or not measure:
        return 0.0, "no station/measure on file — no calibration applied"

    n, avg_predicted, avg_actual = storage.get_calibration_stats(station_code, measure)

    if n < MIN_SAMPLES_FOR_CALIBRATION:
        return 0.0, f"only {n} settled samples for {station_code}/{measure} (need {MIN_SAMPLES_FOR_CALIBRATION}) — no calibration yet"

    raw_bias = avg_actual - avg_predicted
    clamped = max(-MAX_BIAS_ADJUSTMENT, min(MAX_BIAS_ADJUSTMENT, raw_bias))
    return clamped, (
        f"{station_code}/{measure}: n={n}, model avg {avg_predicted:.2f} vs actual {avg_actual:.2f} "
        f"-> bias {clamped:+.2f}"
    )


def apply_calibration(raw_probability: float, station_code: str | None, measure: str | None) -> tuple[float, str]:
    bias, note = get_bias(station_code, measure)
    adjusted = max(0.01, min(0.99, raw_probability + bias))
    return adjusted, note


def record_outcome(station_code: str | None, measure: str | None,
                    predicted_probability: float | None, actual_outcome: bool) -> None:
    """Call this once a market settles, feeding back the prediction that was
    actually used for that trade so future estimates for this station adjust."""
    if not station_code or not measure or predicted_probability is None:
        return
    storage.record_calibration_outcome(station_code, measure, predicted_probability, actual_outcome)

PYEOF_CALIBRATION_PY

cat > settlement.py << 'PYEOF_SETTLEMENT_PY'
"""
Runs each cycle before scanning for new trades. For every trade still marked
'open' in the DB, checks whether Kalshi has settled that market yet. If so:
  1. Computes real PnL and marks the trade won/lost.
  2. Updates the risk manager's bankroll (this is what makes bankroll
     tracking real instead of just "contracts bought so far").
  3. Feeds the outcome back into calibration.py — this is the actual
     learning step. Without this function running regularly, the bot never
     learns anything; the calibration table just stays empty.
"""
from __future__ import annotations

import logging

from kalshi_client import KalshiClient
from risk_manager import RiskManager
import calibration
import storage

log = logging.getLogger("kalshi_weather_bot.settlement")


def settle_resolved_trades(kalshi: KalshiClient, risk: RiskManager) -> int:
    open_trades = storage.get_open_trades()
    settled_count = 0

    # Avoid redundant API calls when multiple contracts on the same ticker are open.
    checked: dict[str, tuple[bool, str | None]] = {}

    for trade in open_trades:
        ticker = trade["ticker"]
        if ticker not in checked:
            try:
                checked[ticker] = kalshi.get_market_settlement(ticker)
            except Exception as e:
                log.warning(f"Could not check settlement for {ticker}: {e}")
                continue

        is_settled, result = checked[ticker]
        if not is_settled:
            continue

        won = (trade["side"] == result)
        count = trade["count"]
        price = trade["price_cents"]
        # Binary contract payout: winner gets 100c/contract, loser gets 0.
        # You already paid `price` cents/contract when you bought it.
        pnl_cents = (100 - price) * count if won else -price * count

        storage.settle_trade(trade["id"], won, pnl_cents)
        risk.record_settlement(pnl_cents)

        # Calibration tracks the YES-event probability consistently, regardless
        # of which side was actually traded — trades table stores
        # model_probability as the calibrated P(yes) at decision time (see bot.py).
        calibration.record_outcome(
            station_code=trade.get("station_code"),
            measure=trade.get("measure"),
            predicted_probability=trade.get("model_probability"),
            actual_outcome=(result == "yes"),
        )

        log.info(
            f"Settled {ticker}: {'WON' if won else 'LOST'}, pnl={pnl_cents}c, "
            f"new bankroll={risk.state.bankroll_cents}c"
        )
        settled_count += 1

    return settled_count

PYEOF_SETTLEMENT_PY

cat > bot.py << 'PYEOF_BOT_PY'
"""
Main entry point.

Default mode is PAPER TRADING: it does everything a live bot would do —
scan markets, extract rules, pull weather data, compute edges, size
positions — except it never calls kalshi_client.create_order(). Trades are
recorded to the local database as if they happened, at the real quoted
price, so you can evaluate the strategy against real market prices without
risking money.

To go live, ALL of the following must be true (defense in depth,
deliberately redundant):
    1. LIVE_TRADING=true in your .env
    2. You pass --live on the command line
    3. You type the confirmation phrase when prompted

Run:
    python bot.py                 # paper trading (default, safe)
    python bot.py --live          # live trading (requires .env flag + confirmation)
    python bot.py --once          # single scan cycle instead of continuous loop

Set RISK_MODE=conservative|balanced|aggressive in .env to control edge
threshold and position sizing (see config.py for exact presets).

Calibration: every settled trade feeds calibration.py, which tracks a
per-station bias between predicted and actual outcomes and applies it to
future estimates for that station. This is the "learning" in this bot —
an auditable running average, not a black-box model. See calibration.py
for how it works and settlement.py for where it gets fed.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date

from config import SETTINGS
from kalshi_client import KalshiClient
from rules_extractor import RulesExtractor
from risk_manager import RiskManager, RiskState
from strategy import evaluate_market
from weather_data import get_station_latest_observation, get_forecast_pop, STATION_REFERENCE
import settlement
import storage

from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=3),
    ],
)
log = logging.getLogger("kalshi_weather_bot")


def confirm_live_trading() -> bool:
    phrase = "I ACCEPT THE RISK"
    print("\n" + "=" * 60)
    print("LIVE TRADING REQUESTED — REAL MONEY WILL BE AT RISK")
    print(f"Bankroll on file: ${SETTINGS.starting_bankroll_cents / 100:.2f}")
    print(f"Max daily loss before kill switch: {SETTINGS.max_daily_loss_pct * 100:.1f}%")
    print(f"Max position size: {SETTINGS.max_position_pct * 100:.1f}% of bankroll")
    print("=" * 60)

    if not sys.stdin.isatty():
        # Running headless (systemd, cron, a detached process) — there's no
        # one to type a confirmation. Rather than crash on input() or,
        # worse, silently skip the confirmation, this requires a separate
        # explicit env var so headless live-trading is a deliberate choice
        # made once in your .env, not an accident of how the process starts.
        if SETTINGS.live_confirmed_headless:
            log.warning("No interactive terminal detected — proceeding on LIVE_TRADING_CONFIRMED=true.")
            return True
        log.error(
            "No interactive terminal detected, so the live-trading confirmation prompt "
            "can't be answered. Set LIVE_TRADING_CONFIRMED=true in .env if you deliberately "
            "want headless live trading (e.g. under systemd), or run 'python bot.py --live' "
            "manually in a terminal once first."
        )
        return False

    typed = input(f"Type '{phrase}' to proceed, or anything else to abort: ")
    return typed.strip() == phrase


def scan_and_trade(kalshi: KalshiClient, extractor: RulesExtractor,
                    risk: RiskManager, live: bool) -> None:
    mode = "live" if live else "paper"

    for series_ticker in SETTINGS.series_tickers:
        try:
            markets_resp = kalshi.get_markets(series_ticker=series_ticker, status="open")
        except Exception as e:
            log.error(f"Failed to fetch markets for {series_ticker}: {e}")
            continue

        for market in markets_resp.get("markets", []):
            ticker = market["ticker"]
            yes_price = market.get("yes_ask") or market.get("last_price")
            if not yes_price:
                continue

            # 1. Get and cache structured settlement rules for this market.
            rules_text = kalshi.get_market_rules_text(ticker)
            rules = extractor.extract(ticker, rules_text)

            if rules.confidence == "low":
                storage.log_decision(ticker, "n/a", yes_price, 0.5, 0, "skipped",
                                      "low-confidence rules extraction — needs manual review", mode)
                continue

            # 2. Pull weather data for the named station, if we know it.
            station = rules.station_code
            observation, forecast = None, []
            if station and station in STATION_REFERENCE:
                observation = get_station_latest_observation(station)
                ref = STATION_REFERENCE[station]
                forecast = get_forecast_pop(ref["lat"], ref["lon"])
            elif station:
                observation = get_station_latest_observation(station)
                # No lat/lon on file for forecast lookup — add it to STATION_REFERENCE
                # in weather_data.py to enable forecast-based signals for this station.

            # 3. Evaluate.
            signal = evaluate_market(ticker, yes_price, rules, observation, forecast)

            approved, reason = risk.approve_trade(
                price_cents=(yes_price if signal.side == "yes" else 100 - yes_price),
                edge_cents=signal.edge_cents,
            )

            if not approved:
                storage.log_decision(ticker, signal.side, yes_price, signal.model_probability,
                                      signal.edge_cents, "skipped", reason, mode)
                continue

            price = yes_price if signal.side == "yes" else 100 - yes_price
            contracts = risk.max_contracts_for_trade(price)

            storage.log_decision(ticker, signal.side, yes_price, signal.model_probability,
                                  signal.edge_cents, "traded", signal.rationale, mode)

            if live:
                try:
                    order = kalshi.create_order(
                        ticker=ticker, side=signal.side, action="buy",
                        count=contracts, price_cents=price,
                    )
                    order_id = order.get("order", {}).get("order_id")
                    log.info(f"LIVE order placed: {ticker} {signal.side} x{contracts} @ {price}c "
                             f"(edge {signal.edge_cents}c) — {order_id}")
                except Exception as e:
                    log.error(f"Order failed for {ticker}: {e}")
                    continue
            else:
                order_id = None
                log.info(f"PAPER trade: {ticker} {signal.side} x{contracts} @ {price}c "
                         f"(edge {signal.edge_cents}c) — {signal.rationale}")

            storage.log_trade(ticker, signal.side, contracts, price, mode, order_id,
                               model_probability=signal.model_probability_yes,
                               station_code=rules.station_code, measure=rules.measure)
            risk.record_fill(cost_cents=contracts * price)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Enable live order placement")
    parser.add_argument("--once", action="store_true", help="Run a single scan cycle and exit")
    args = parser.parse_args()

    live = args.live
    if live:
        if not SETTINGS.live_trading_enabled:
            log.error("LIVE_TRADING is not set to true in your .env — refusing to trade live.")
            sys.exit(1)
        if not confirm_live_trading():
            log.info("Live trading not confirmed. Exiting.")
            sys.exit(0)

    storage.init_db()
    extractor = RulesExtractor()
    # Resume from last known bankroll rather than resetting on every restart —
    # a crash or reboot shouldn't erase the bot's memory of real P&L.
    resumed_bankroll = storage.load_last_bankroll(SETTINGS.starting_bankroll_cents)
    risk = RiskManager(RiskState(bankroll_cents=resumed_bankroll, day=date.today()))

    kalshi = None
    if live:
        kalshi = KalshiClient()
    else:
        # Paper mode still needs real market prices — Kalshi's market data
        # endpoints are public/read-only, so this uses the same client but
        # create_order is simply never called in the paper branch above.
        try:
            kalshi = KalshiClient()
        except Exception as e:
            log.error(f"Even paper mode needs API credentials to read market data: {e}")
            sys.exit(1)

    log.info(f"Starting bot in {'LIVE' if live else 'PAPER'} mode, risk profile '{SETTINGS.risk_mode}'. "
             f"Bankroll: ${risk.state.bankroll_cents/100:.2f}, "
             f"scanning series: {SETTINGS.series_tickers}")

    consecutive_failures = 0
    while True:
        try:
            # Settle anything that's resolved since the last cycle FIRST —
            # this is what feeds the calibration loop and keeps bankroll
            # accurate before deciding on any new trades this cycle.
            settled = settlement.settle_resolved_trades(kalshi, risk)
            if settled:
                log.info(f"Settled {settled} resolved trade(s) this cycle.")

            scan_and_trade(kalshi, extractor, risk, live)
            storage.snapshot_bankroll(risk.state.bankroll_cents, note="cycle complete")
            consecutive_failures = 0
        except Exception as e:
            # One bad cycle (API hiccup, network blip, a market with malformed
            # data) should never take the whole bot down. Log it, back off,
            # and keep running — that's the difference between "unattended"
            # and "silently dead since Tuesday."
            consecutive_failures += 1
            log.error(f"Scan cycle failed ({consecutive_failures} in a row): {e}", exc_info=True)
            if consecutive_failures >= 5:
                log.error(
                    "5 consecutive cycle failures — something is likely structurally wrong "
                    "(bad credentials, API change, etc). Halting rather than looping forever. "
                    "Check the log above before restarting."
                )
                sys.exit(1)
            time.sleep(min(SETTINGS.poll_interval_seconds, 60) * consecutive_failures)
            continue

        if risk.state.is_kill_switch_tripped():
            log.warning("Daily loss kill switch tripped. Halting new trades until tomorrow.")

        if args.once:
            break
        time.sleep(SETTINGS.poll_interval_seconds)


if __name__ == "__main__":
    main()

PYEOF_BOT_PY

cat > requirements.txt << 'PYEOF_REQUIREMENTS_TXT'
httpx>=0.27
cryptography>=42.0
python-dotenv>=1.0
anthropic>=0.40

PYEOF_REQUIREMENTS_TXT


echo "Installing system packages (this may take a minute)..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip

echo "Setting up Python environment..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo ""
echo "=================================================="
echo "  Now I need your credentials. None of this is"
echo "  sent anywhere except into a file on THIS machine."
echo "=================================================="
echo ""

read -p "Kalshi API Key ID (from Kalshi Settings > API Keys): " KALSHI_KEY_ID

echo ""
echo "Now paste the FULL contents of your Kalshi private key file"
echo "(starts with -----BEGIN..., ends with -----END...-----)."
echo "Paste it, then press Enter, then press Ctrl-D:"
cat > kalshi_private_key.pem
chmod 600 kalshi_private_key.pem
echo "Private key saved."

echo ""
read -p "Anthropic API key (from console.anthropic.com): " ANTHROPIC_KEY

echo ""
read -p "Risk mode - conservative, balanced, or aggressive [balanced]: " RISK_MODE
RISK_MODE=${RISK_MODE:-balanced}

echo ""
read -p "Starting paper bankroll in dollars [500]: " BANKROLL_DOLLARS
BANKROLL_DOLLARS=${BANKROLL_DOLLARS:-500}
BANKROLL_CENTS=$((BANKROLL_DOLLARS * 100))

echo ""
read -p "Kalshi series tickers to scan, comma-separated [KXRAIN]: " SERIES
SERIES=${SERIES:-KXRAIN}

cat > .env << ENVEOF
KALSHI_BASE_URL=https://api.elections.kalshi.com/trade-api/v2
KALSHI_API_KEY_ID=${KALSHI_KEY_ID}
KALSHI_PRIVATE_KEY_PATH=${APP_DIR}/kalshi_private_key.pem
ANTHROPIC_API_KEY=${ANTHROPIC_KEY}
LIVE_TRADING=false
LIVE_TRADING_CONFIRMED=false
RISK_MODE=${RISK_MODE}
STARTING_BANKROLL_CENTS=${BANKROLL_CENTS}
MIN_CONTRACT_PRICE_CENTS=2
MAX_CONTRACT_PRICE_CENTS=90
MAX_OPEN_POSITIONS=15
POLL_INTERVAL_SECONDS=300
RULES_CACHE_PATH=${APP_DIR}/rules_cache.json
DB_PATH=${APP_DIR}/bot_state.db
SERIES_TICKERS=${SERIES}
ENVEOF

echo ".env written."

echo "Setting up background service..."
sudo tee /etc/systemd/system/kalshi-weather-bot.service > /dev/null << SERVICEEOF
[Unit]
Description=Kalshi Weather Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/bot.py
Restart=on-failure
RestartSec=30
StandardOutput=append:${APP_DIR}/service.log
StandardError=append:${APP_DIR}/service.log

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable kalshi-weather-bot
sudo systemctl start kalshi-weather-bot

sleep 2

echo ""
echo "=================================================="
echo "  Done. The bot is running in PAPER mode (no real"
echo "  money at risk) and will keep running in the"
echo "  background, including after you close this window"
echo "  or the server reboots."
echo "=================================================="
echo ""
echo "Check on it any time with:"
echo "  sudo systemctl status kalshi-weather-bot"
echo "  tail -f ${APP_DIR}/bot.log"
echo ""
echo "Stop it with:"
echo "  sudo systemctl stop kalshi-weather-bot"
echo ""
