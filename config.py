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
RISK_PRESETS = {
    "conservative": {"min_edge_cents": 10, "max_position_pct": 0.015, "max_daily_loss_pct": 0.04, "max_contracts_per_trade": 15},
    "balanced":     {"min_edge_cents": 6,  "max_position_pct": 0.03,  "max_daily_loss_pct": 0.06, "max_contracts_per_trade": 25},
    "aggressive":   {"min_edge_cents": 3,  "max_position_pct": 0.05,  "max_daily_loss_pct": 0.10, "max_contracts_per_trade": 40},
}
_RISK_MODE = os.getenv("RISK_MODE", "balanced").strip().lower()
_PRESET = RISK_PRESETS.get(_RISK_MODE, RISK_PRESETS["balanced"])


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
    # A third, independent gate specifically for whether create_order()'s
    # request schema has been confirmed against the CURRENT live Kalshi API.
    # Defaults false on purpose — see kalshi_client.py's create_order docstring.
    order_schema_verified: bool = _bool("ORDER_SCHEMA_VERIFIED", False)

    # --- Risk mode ---
    # 'conservative' | 'balanced' | 'aggressive' — sets the three risk knobs
    # below to a sane preset. Falls back to 'balanced' on an unrecognized value.
    risk_mode: str = _RISK_MODE if _RISK_MODE in RISK_PRESETS else "balanced"

    # --- Bankroll / risk controls ---
    starting_bankroll_cents: int = _int("STARTING_BANKROLL_CENTS", 50_000)  # $500 default
    max_position_pct: float = _float("MAX_POSITION_PCT", _PRESET["max_position_pct"])
    max_daily_loss_pct: float = _float("MAX_DAILY_LOSS_PCT", _PRESET["max_daily_loss_pct"])
    min_edge_cents: int = _int("MIN_EDGE_CENTS", _PRESET["min_edge_cents"])
    # A fixed, price- and bankroll-independent ceiling, separate from
    # max_position_pct — see RiskPreset.max_contracts_per_trade's docstring
    # in risk_manager.py for why both are needed together.
    max_contracts_per_trade: int = _int("MAX_CONTRACTS_PER_TRADE", _PRESET["max_contracts_per_trade"])
    min_contract_price_cents: int = _int("MIN_CONTRACT_PRICE_CENTS", 2)
    max_contract_price_cents: int = _int("MAX_CONTRACT_PRICE_CENTS", 90)
    max_open_positions: int = _int("MAX_OPEN_POSITIONS", 15)

    # --- Loop timing ---
    poll_interval_seconds: int = _int("POLL_INTERVAL_SECONDS", 300)  # 5 min between scan cycles
    rules_cache_path: str = os.getenv("RULES_CACHE_PATH", "rules_cache.json")
    db_path: str = os.getenv("DB_PATH", "bot_state.db")

    # --- Market scope ---
    # Kalshi weather series tickers to scan. When AUTO_DISCOVER_SERIES is true
    # (the default), this list is ignored in favor of live discovery — see
    # kalshi_client.discover_series_tickers() and bot.py — so newly listed
    # or delisted cities are picked up automatically without editing config.
    # This becomes the fallback list only if discovery itself fails.
    series_tickers: tuple = tuple(
        t.strip() for t in os.getenv(
            "SERIES_TICKERS",
            "KXRAIN,KXHIGHTEMP"
        ).split(",") if t.strip()
    )
    auto_discover_series: bool = _bool("AUTO_DISCOVER_SERIES", True)
    discovery_keywords: tuple = tuple(
        k.strip() for k in os.getenv("DISCOVERY_KEYWORDS", "rain,KXHIGH,KXLOW").split(",") if k.strip()
    )
    discovery_refresh_seconds: int = _int("DISCOVERY_REFRESH_SECONDS", 3600)  # re-check hourly
    advisor_interval_seconds: int = _int("ADVISOR_INTERVAL_SECONDS", 7 * 24 * 3600)  # weekly by default


SETTINGS = Settings()
