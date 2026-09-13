"""
Runs several named strategies in parallel, ALL in paper simulation, all the
time — regardless of what the main bot's active RISK_MODE or LIVE_TRADING
setting is. Each strategy gets its own simulated bankroll and its own
running P&L, so over time the dashboard can show a real, apples-to-apples
comparison: which of these approaches would actually have made money.

This module never places a real order and never touches the real bankroll
in risk_manager — it's entirely observational. The active bot (bot.py's
main scan_and_trade + its one RiskManager) is the only thing that can ever
place a live order, and only when LIVE_TRADING=true. Promoting a shadow
strategy to "the real one" is a manual step: change RISK_MODE (or the
underlying strategy choice) in .env/the dashboard yourself, once you're
convinced by the data here — this module does not do that automatically.

Strategies defined below:
  calibrated_conservative / balanced / aggressive — the main model
    (strategy.py) at three different risk-threshold presets.
  arbitrage — buys both sides when yes_ask + no_ask < 100c (rare on a
    healthy Kalshi market; see strategies_lib.py for why).
  favorites_baseline — naive: buys YES whenever price >= 90c, no model.
    A deliberately dumb comparison point.
  longshot — the calibrated model, restricted to cheap (2-15c) contracts.
  swing — buy cheap, sell higher BEFORE resolution, on the price movement
    itself rather than the weather outcome. See the big caveat on this one
    right above its entry in STRATEGIES below — the thresholds are a
    starting guess, not something backed by real movement data yet.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from config import RISK_PRESETS, SETTINGS
from risk_manager import RiskManager, RiskState, RiskPreset
from strategy import TradeSignal
import strategies_lib as lib
import categories
import fees
import storage
import calibration
import depth_sizing as lib_depth
from kalshi_client import market_price_cents

# Position-size scaling for temp_calibrated_confidence_weighted, keyed by
# rules_extractor's MarketRules.confidence. "low" never actually reaches
# this (bot.py filters it out upstream, before any shadow strategy sees
# the market at all) — kept here anyway so the mapping is honest about
# what it WOULD do if that upstream filter ever changed.
CONFIDENCE_SIZE_MULTIPLIER = {"high": 1.0, "medium": 0.5, "low": 0.0}

# Automatic, mechanical performance dampening — deliberately NOT an LLM
# judgment call (unlike advisor.py's suggestions or retrospective.py's
# analysis, both of which require a human to read and act on them). This
# is a pure, deterministic circuit breaker: if a strategy's last
# COLD_STREAK_LOOKBACK_TRADES settled trades add up to worse than
# COLD_STREAK_ROI_THRESHOLD_PCT return-on-capital, its position sizing is
# scaled down by COLD_STREAK_DAMPENING_MULTIPLIER until enough of that
# cold streak ages out of the rolling window to recover. The asymmetry is
# what makes this safe to run with zero human review: it can only ever
# REDUCE size below the tier's normal cap, never increase it above
# baseline — there's no way for this mechanism, even if it's somehow
# wrong, to make a strategy MORE aggressive than its configured risk
# tier already allows. Requires COLD_STREAK_MIN_TRADES of real settled
# data before it activates at all, same "don't overreact to too little
# data" philosophy calibration.py and categories.py already use.
COLD_STREAK_MIN_TRADES = 10
COLD_STREAK_LOOKBACK_TRADES = 20
COLD_STREAK_ROI_THRESHOLD_PCT = -15.0
COLD_STREAK_DAMPENING_MULTIPLIER = 0.5


def performance_dampening_multiplier(perf: dict) -> float:
    """
    Pure function, deliberately separate from the storage query that
    produces `perf` (see storage.get_recent_strategy_performance) — keeps
    the actual DECISION logic testable without a database, and makes it
    easy to verify in isolation that this can only ever return a value
    <= 1.0 (see the module-level docstring above for why that asymmetry
    is what makes automating this safe).
    """
    if perf["trades"] < COLD_STREAK_MIN_TRADES:
        return 1.0
    if perf["roi_pct"] is not None and perf["roi_pct"] <= COLD_STREAK_ROI_THRESHOLD_PCT:
        return COLD_STREAK_DAMPENING_MULTIPLIER
    return 1.0


# Cross-strategy concentration dampening — see
# concentration_dampening_multiplier's docstring for the confirmed
# real-world motivation. Deliberately a SCALING response, not a hard cap:
# a hard "at most N strategies per event" rule would have to arbitrarily
# pick which strategies get the slot (whichever happens to evaluate
# first in a scan cycle), which isn't obviously better-informed than any
# other strategy's judgment. Scaling down every ADDITIONAL strategy's
# size instead lets every strategy still express its own signal, just
# with less weight as the same underlying outcome gets more crowded —
# same "reduce risk, never fully block, never coordinate decisions"
# philosophy as the other dampening mechanisms in this file.
CONCENTRATION_DAMPENING_MODERATE_THRESHOLD = 1   # 1-2 other strategies already exposed
CONCENTRATION_DAMPENING_SEVERE_THRESHOLD = 3     # 3+ other strategies already exposed
CONCENTRATION_DAMPENING_MODERATE_MULTIPLIER = 0.5
CONCENTRATION_DAMPENING_SEVERE_MULTIPLIER = 0.25


def concentration_dampening_multiplier(other_strategies_count: int) -> float:
    """
    Pure function, same testable-in-isolation reasoning as
    performance_dampening_multiplier above — provably can only ever
    return <= 1.0.

    CONFIRMED REAL-WORLD MOTIVATION: a loss-analysis review found 20+
    trades across nearly every strategy (calibrated variants, longshot,
    swing, tight_spread_calibrated, rain_always_trade) all independently
    buying the same losing side of the same underlying market
    (KXRAIN-26SEP11-SEA) simultaneously. Because nearly every directional
    strategy draws from the same underlying weather model with different
    risk tiers/filters layered on top, running 20+ of them in parallel
    doesn't add diversification when they all share the same blind spot —
    it just multiplies the damage from one bad judgment across the whole
    portfolio at once. This doesn't prevent that from happening again
    (each strategy still independently decides to trade), but it directly
    shrinks the aggregate exposure as more of them pile onto the same
    outcome, rather than letting every one of them trade at full,
    uncoordinated size.
    """
    if other_strategies_count >= CONCENTRATION_DAMPENING_SEVERE_THRESHOLD:
        return CONCENTRATION_DAMPENING_SEVERE_MULTIPLIER
    if other_strategies_count >= CONCENTRATION_DAMPENING_MODERATE_THRESHOLD:
        return CONCENTRATION_DAMPENING_MODERATE_MULTIPLIER
    return 1.0

# Bracket-arbitrage tuning — deliberately NOT in TUNABLE_PARAMS/advisor-editable,
# same reasoning as the calibrated model itself: this is math (capital
# efficiency + early-exit salvage), not a heuristic threshold to sweep.
#
# Legs priced at or above this (in cents, for whichever side the set is
# buying) are skipped at entry — the market is already saying that bracket
# has only a sliver of a chance of being the actual winner, so buying it
# ties up nearly its full price for a cent or two of edge. Skipping trades
# away the "mathematically guaranteed" property of the full hedge for a
# small, deliberate, named tail risk (that specific improbable bracket DOES
# win) in exchange for real capital efficiency elsewhere in the set.
BRACKET_NEAR_CERTAIN_SKIP_CENTS = 97

# Once a held bracket-arbitrage leg's current price has decayed to at or
# below this floor (and below what we paid for it), sell it now instead of
# carrying it all the way to a literal 0 at settlement — see
# check_bracket_arbitrage_offload(). Deliberately small: this is about
# salvaging a few otherwise-lost cents on a leg that's already basically
# decided, not an early-exit signal in its own right.
BRACKET_OFFLOAD_FLOOR_CENTS = 3

STRATEGIES = {
    "calibrated_conservative": {"kind": "calibrated", "risk": "conservative"},
    "calibrated_balanced":     {"kind": "calibrated", "risk": "balanced"},
    "calibrated_aggressive":   {"kind": "calibrated", "risk": "aggressive"},
    # Category-scoped calibrated variants — same model, same three risk
    # presets, but each one only ever evaluates markets in ONE category
    # (via cfg["category_filter"], checked once up front in
    # evaluate_and_log for every strategy kind, not just these) and keeps
    # its own separate paper bankroll. This is what makes "is aggressive
    # actually good on temperature specifically" an answerable question —
    # the plain calibrated_* strategies above blend rain+temperature+other
    # into one shared bankroll, which hides that.
    "temp_calibrated_conservative": {"kind": "calibrated", "risk": "conservative", "category_filter": "Temperature"},
    "temp_calibrated_balanced":     {"kind": "calibrated", "risk": "balanced",     "category_filter": "Temperature"},
    "temp_calibrated_aggressive":   {"kind": "calibrated", "risk": "aggressive",   "category_filter": "Temperature"},
    "rain_calibrated_conservative": {"kind": "calibrated", "risk": "conservative", "category_filter": "Rain"},
    "rain_calibrated_balanced":     {"kind": "calibrated", "risk": "balanced",     "category_filter": "Rain"},
    "rain_calibrated_aggressive":   {"kind": "calibrated", "risk": "aggressive",   "category_filter": "Rain"},
    # PIPELINE SMOKE TEST — not a real strategy, doesn't try to be
    # profitable, doesn't even use the model's edge estimate. The point is
    # only to prove evaluate -> approve -> size -> log actually executes
    # end to end on real rain markets every cycle, since that's been the
    # open question tonight. min_edge_cents_override=0 and
    # max_price_override=99 mean it accepts basically any priced market;
    # max_daily_loss_pct_override=1.0 means a losing streak (expected and
    # fine — it's paper money and isn't trying to win) never trips the
    # kill switch and stops it from doing its one job of just trading.
    "rain_always_trade": {"kind": "always_trade", "risk": "conservative", "category_filter": "Rain",
                            "min_edge_cents_override": 0, "max_price_override": 99,
                            "max_daily_loss_pct_override": 1.0},
    # Arbitrage's "edge" is a guaranteed profit in cents, not a probability
    # edge — a much lower bar clears it (even 1-2c guaranteed is worth
    # taking in theory; real fees would eat small amounts, which is exactly
    # the kind of thing this comparison is meant to surface).
    # Arbitrage's "price" is the COMBINED yes_ask+no_ask cost, which sits
    # near 100c by construction — the normal single-side price band (capped
    # at 90c) would reject every real arbitrage candidate, so it gets its
    # own band up to 99c.
    "arbitrage":               {"kind": "arbitrage",  "risk": "conservative", "min_edge_cents_override": 1,
                                 "max_price_override": 99},
    # Favorites baseline has NO edge concept at all by design — it trades
    # purely on price, specifically ABOVE the normal 90c ceiling (that's the
    # whole point of the strategy), so both the edge filter and the price
    # ceiling get overridden here.
    "favorites_baseline":      {"kind": "favorites",  "risk": "balanced", "threshold": 90,
                                 "min_edge_cents_override": 0, "max_price_override": 99},
    "longshot":                {"kind": "longshot",   "risk": "aggressive", "min_price": 2, "max_price": 15},
    # SWING — buys when the calibrated model still likes the price, then
    # sells as soon as the price rises to entry+exit_offset, regardless of
    # whether the market has resolved yet. If the exit target is never hit,
    # it falls through to normal settlement (held to resolution) like any
    # other strategy — see shadow.check_swing_exits().
    #
    # HONEST CAVEAT: entry_max=40 and exit_offset=20 (buy under 40c, sell
    # ~20c higher) are a starting guess, not a validated rule. price_history
    # is now logging every scanned market every cycle specifically so these
    # numbers can be replaced with real observed movement once there's
    # enough data (a few weeks minimum) — check storage.get_price_history()
    # per ticker before trusting this strategy's results over the others.
    "swing":                   {"kind": "swing", "risk": "balanced", "entry_max": 40, "exit_offset": 20},
    # CONFIDENCE-WEIGHTED — same calibrated model as above, but position
    # size is scaled down for medium-confidence rules extractions instead
    # of treated identically to high-confidence ones. Low confidence never
    # reaches here at all (bot.py already skips low-confidence markets
    # entirely before any strategy sees them) — see CONFIDENCE_SIZE_MULTIPLIER
    # below for the exact scaling.
    "temp_calibrated_confidence_weighted": {"kind": "calibrated_confidence_weighted", "risk": "balanced",
                                              "category_filter": "Temperature"},
    # FORECAST MOMENTUM — only fires when the calibrated model likes the
    # trade AND the NWS forecast for the relevant period moved by at least
    # forecast_shift_threshold_f since the previous scan cycle. The idea:
    # a normal calibrated edge can just be steady-state model-vs-market
    # disagreement; requiring a fresh forecast revision alongside it is a
    # filter for "the market probably hasn't caught up to new information
    # yet," not a separate probability model of its own.
    "temp_forecast_momentum": {"kind": "forecast_momentum", "risk": "balanced",
                                 "category_filter": "Temperature", "forecast_shift_threshold_f": 3.0},
    # SETTLEMENT WINDOW — trades ONLY in the last few hours before a
    # temperature_high market closes, on the strength of strategy.py's
    # near-close observation blending. measure_filter (not just
    # category_filter) matters here specifically: "Temperature" alone
    # would also let temperature_low markets through, and a daily low's
    # afternoon observation has no relationship to its overnight
    # settlement — see strategy.estimate_temperature_probability's
    # docstring for the full reasoning this strategy leans on.
    "temp_settlement_window": {"kind": "settlement_window", "risk": "balanced",
                                 "category_filter": "Temperature", "measure_filter": "temperature_high",
                                 "max_hours_until_close": 3.0},
    # RAIN SETTLEMENT WINDOW — same "settlement_window" kind as
    # temp_settlement_window, just measure_filter'd to precipitation_daily
    # instead of temperature_high. Needs zero new dispatch logic: bot.py
    # already passes hours_until_close unconditionally to
    # evaluate_and_log for every measure, and strategy.py's rain model
    # already applies its own near-close decay specifically for
    # precipitation_daily (see estimate_precip_probability's docstring).
    # Isolated into its own strategy for the same reason as its
    # temperature counterpart: lets retrospective and the confidence/edge
    # analytics check whether "near-close rain trades are more reliable"
    # holds up against real outcomes, separate from calibrated_balanced's
    # blended numbers across every hour of the day.
    "rain_settlement_window": {"kind": "settlement_window", "risk": "balanced",
                                "category_filter": "Rain", "measure_filter": "precipitation_daily",
                                "max_hours_until_close": 3.0},
    # DEPTH IMBALANCE — a genuinely different signal SOURCE, not another
    # spin on forecast-vs-market-price: heavy resting order-book depth on
    # one side relative to the other, independent of what the weather
    # model thinks. min_edge_cents_override=0 for the same reason
    # favorites_baseline needs it — this never claims a probability-based
    # edge, so the normal min-edge gate would reject every candidate
    # outright. max_price_override=99 for the same reason too: a real
    # imbalance can show up at any price, not just within the standard
    # 2-90c band other strategies assume.
    "depth_imbalance": {"kind": "depth_imbalance", "risk": "conservative",
                         "min_edge_cents_override": 0, "max_price_override": 99,
                         "min_total_depth": 20, "min_imbalance_ratio": 3.0},
    # CALIBRATION TRUSTED — trades only where the raw model has been
    # directly, empirically verified as well-aligned at that specific
    # station/measure (real settled data, small measured bias), rather
    # than trusting the model uniformly everywhere the way calibrated_*
    # does. Isolated into its own strategy so it's directly comparable to
    # calibrated_balanced: if this one's win rate is meaningfully better,
    # that's real evidence the trust-gating is worth something; if not,
    # that's a real finding too.
    "calibration_trusted": {"kind": "calibration_trusted", "risk": "balanced", "max_bias_for_trust": 0.05},
    # TIGHT SPREAD CALIBRATED — same calibrated model as calibrated_balanced,
    # gated on real market-quality (a tight bid-ask spread), not a
    # different probability model. Isolated so it's directly comparable
    # to calibrated_balanced: if avoiding wide-spread/thin-liquidity
    # markets actually improves outcomes, this shows it against real data.
    "tight_spread_calibrated": {"kind": "tight_spread_calibrated", "risk": "balanced", "max_spread_cents": 5},
    # CONFIRMED SIGNAL — trades only when the calibrated weather model
    # and pure order-book depth (two genuinely independent signal
    # sources) agree on direction. Can only ever reduce trade frequency
    # relative to calibrated_balanced, never increase it, since it adds
    # a requirement on top of the same edge/side calibrated_balanced
    # already needs.
    "confirmed_signal": {"kind": "confirmed_signal", "risk": "balanced",
                          "min_total_depth": 20, "min_imbalance_ratio": 3.0},
    # BRACKET ARBITRAGE — for a full set of mutually-exclusive brackets
    # (e.g. every temperature bucket for one city/day), buys the side (all
    # NO, or rarely all YES) whose combined price guarantees a profit no
    # matter which bracket actually wins — see evaluate_bracket_set() below
    # for the real math and evaluate_and_log_fees for why this needed its
    # own fee accounting instead of the generic per-order one.
    # min_edge_cents_override here means "minimum NET (already fee-adjusted)
    # cents of guaranteed profit per set" — kept deliberately above 0 to
    # leave room for execution slippage the fee model doesn't capture.
    # max_price_override is wide because "price" here is the summed cost
    # across the whole bracket set, not one contract (can exceed 99c easily).
    # bracket_arbitrage is DISABLED as of 2026-09-09 — 18 settled trades,
    # 0% win rate, -5656% ROI. This isn't "bad luck": a correctly-hedged
    # bracket set should structurally WIN most of its individual leg-bets
    # (buying NO across N mutually-exclusive brackets means exactly 1 of N
    # legs loses and the other N-1 win, every single time, regardless of
    # the actual weather) — a 0% win rate across 18 leg-settlements is
    # close to impossible unless something is inverted (wrong side bought,
    # a settlement mismatch, or backwards payout math). Real bug, not yet
    # root-caused. Kept here, commented out, rather than deleted, so
    # re-enabling later is a one-line change once the bug is found —
    # historical shadow_trades/decisions rows are left in the database
    # untouched for whoever eventually debugs this.
    # "bracket_arbitrage":       {"kind": "bracket_arbitrage", "risk": "conservative",
    #                              "min_edge_cents_override": 3, "max_price_override": 5000},
}

# Strategies excluded from dashboard/report summaries despite having
# historical data — see the bracket_arbitrage disable note above. Kept
# separate from simply removing the STRATEGIES entry so past trades stay
# queryable directly, just not surfaced on the leaderboard.
EXCLUDED_FROM_SUMMARY = {"bracket_arbitrage"}

# Only these strategy+param combinations are ever eligible for an
# advisor.py suggestion or a dashboard override — the calibrated model and
# arbitrage are never auto-tuned, on purpose (see advisor.py).
TUNABLE_PARAMS = {
    "swing": ["entry_max", "exit_offset"],
    "favorites_baseline": ["threshold"],
    "longshot": ["min_price", "max_price"],
}


def _load_active_strategies() -> dict:
    """STRATEGIES with any human-approved overrides (from the dashboard's
    Apply button) merged in. Computed once at import — overrides take
    effect on the next restart, same as every other setting in this app."""
    merged = {name: dict(cfg) for name, cfg in STRATEGIES.items()}
    try:
        overrides = storage.get_overrides()
        for name, params in overrides.items():
            if name in merged:
                allowed = TUNABLE_PARAMS.get(name, [])
                for param, value in params.items():
                    if param in allowed:
                        merged[name][param] = value
    except Exception:
        pass  # DB may not exist yet on a very first run — fall back to defaults
    return merged


ACTIVE_STRATEGIES = _load_active_strategies()

_engines: dict[str, RiskManager] | None = None


def _build_preset(cfg: dict) -> RiskPreset:
    p = RISK_PRESETS[cfg["risk"]]
    return RiskPreset(
        min_edge_cents=cfg.get("min_edge_cents_override", p["min_edge_cents"]),
        max_position_pct=p["max_position_pct"],
        max_daily_loss_pct=cfg.get("max_daily_loss_pct_override", p["max_daily_loss_pct"]),
        min_contract_price_cents=SETTINGS.min_contract_price_cents,
        max_contract_price_cents=cfg.get("max_price_override", SETTINGS.max_contract_price_cents),
        max_open_positions=SETTINGS.max_open_positions,
        max_contracts_per_trade=cfg.get("max_contracts_override", p["max_contracts_per_trade"]),
        max_slippage_cents=cfg.get("max_slippage_override", p["max_slippage_cents"]),
    )


def get_engines() -> dict[str, RiskManager]:
    global _engines
    if _engines is None:
        _engines = {}
        for name, cfg in ACTIVE_STRATEGIES.items():
            bankroll = storage.load_last_shadow_bankroll(name, SETTINGS.starting_bankroll_cents)
            # Seeds today's real realized P&L instead of assuming 0 — see
            # get_todays_realized_pnl_cents()'s docstring for why this
            # matters for the daily kill switch surviving a restart.
            realized_today = storage.get_todays_realized_pnl_cents(name)
            # Same reasoning, same bug class, for the open-position count —
            # see get_open_shadow_position_count()'s docstring for the
            # confirmed, severe consequences of NOT doing this.
            open_count = storage.get_open_shadow_position_count(name)
            state = RiskState(bankroll_cents=bankroll, day=date.today(),
                               realized_pnl_today_cents=realized_today,
                               open_positions_count=open_count)
            _engines[name] = RiskManager(state, _build_preset(cfg))
    return _engines


def evaluate_and_log(ticker: str, signal: Optional[TradeSignal], yes_ask: Optional[int],
                      no_ask: Optional[int], station_code: Optional[str], measure: Optional[str],
                      confidence: Optional[str] = None,
                      current_forecast_temp_f: Optional[float] = None,
                      previous_forecast_temp_f: Optional[float] = None,
                      yes_bids: Optional[list[tuple[int, int]]] = None,
                      no_bids: Optional[list[tuple[int, int]]] = None,
                      hours_until_close: Optional[float] = None,
                      yes_bid: Optional[int] = None,
                      event_ticker: Optional[str] = None) -> None:
    """Called once per scanned market per cycle. Every strategy independently
    decides whether IT would trade this market — never a real order.

    confidence/current_forecast_temp_f/previous_forecast_temp_f are optional
    and only used by temp_calibrated_confidence_weighted and
    temp_forecast_momentum respectively — every other strategy ignores them,
    so existing callers that don't pass them keep working unchanged.

    yes_bids/no_bids (from kalshi.get_orderbook_levels, fetched ONCE per
    ticker by the caller and shared across every strategy here — see
    bot.py) enable REAL depth-aware sizing via depth_sizing.py, same
    mechanism the main bot's actual trade path already uses: the quoted
    top-of-book price only holds for the first few contracts, so a paper
    trade sized off that price alone overstates how good a fill you'd
    really get, especially for anything beyond a token position. When
    omitted (a caller that hasn't fetched them, or a fetch that failed this
    cycle), every strategy falls back to the flat top-of-book assumption
    exactly as before — this is additive, never a hard requirement.

    hours_until_close: only used by the settlement_window kind, which
    gates on trading exclusively in the last few hours before a market
    closes (see strategy.py's near-close observation blending for why
    that window is worth treating differently).

    yes_bid: the resting bid, separate from yes_ask — only used by kinds
    that care about the bid-ask spread itself (tight_spread_only) or
    order-book imbalance (depth_imbalance), neither of which yes_ask/no_ask
    alone can express.

    event_ticker: Kalshi's own event grouping (every bucket/threshold
    market for one city+day shares one event_ticker). Used by favorites
    specifically to check "do I already hold a position on this same
    underlying event" — see storage.has_open_position_for_event's
    docstring for the confirmed bug this prevents: buying multiple
    mutually-conflicting bucket positions on the same underlying outcome
    isn't diversification, it's the same bet placed several times."""
    engines = get_engines()

    for name, cfg in ACTIVE_STRATEGIES.items():
        rm = engines[name]
        kind = cfg["kind"]

        # Category-scoped strategies (temp_calibrated_*, rain_calibrated_*,
        # temp_forecast_momentum, etc.) only ever evaluate markets in their
        # one category — checked once here, ahead of the per-kind branches
        # below, so it applies the same way no matter which kind a scoped
        # strategy ever uses.
        cat_filter = cfg.get("category_filter")
        if cat_filter and categories.category_for(measure) != cat_filter:
            continue

        # Same idea, one level narrower: a strategy can also be pinned to
        # a small set of specific stations (e.g. rain_always_trade -> every
        # currently-configured major metro) — cuts down every OTHER source
        # of variation (which day, discovery noise, unrecognized measures)
        # while diagnosing whether the pipeline fires at all, without
        # betting everything on one single city that might just be quiet
        # at any given moment (confirmed live: KHOU had zero quotes on
        # either side for a stretch — not a bug, just that one market
        # having nothing resting on its book right then). Accepts either a
        # single station string or a list of them.
        station_filter = cfg.get("station_filter")
        if station_filter:
            allowed_stations = station_filter if isinstance(station_filter, (list, tuple, set)) else {station_filter}
            if station_code not in allowed_stations:
                continue

        # One level narrower still: pins a strategy to one specific measure
        # within a category — needed for settlement_window, which only
        # makes sense for temperature_high (a daily low settles overnight,
        # so hours_until_close being small says nothing useful about it —
        # see strategy.estimate_temperature_probability's docstring for
        # the full reasoning). category_filter alone can't express this,
        # since "Temperature" covers both highs and lows.
        measure_filter = cfg.get("measure_filter")
        if measure_filter and measure != measure_filter:
            continue

        if kind in ("calibrated", "calibrated_confidence_weighted"):
            if not signal:
                continue
            price = lib.price_for_side(signal.side, yes_ask, no_ask)
            if price is None:
                continue
            candidate = lib.StrategyCandidate(signal.side, price, signal.edge_cents, signal.rationale)
            model_prob = signal.model_probability_yes

        elif kind == "calibration_trusted":
            # Different question from the calibrated_* strategies: those
            # apply the SAME learned correction everywhere and trade
            # regardless of whether it's backed by 20 samples or 2000, or
            # a bias near zero versus one already at its clamp ceiling.
            # This one only trades where the raw model has been directly,
            # empirically verified as well-aligned at THIS specific
            # station/measure — see calibration.is_trusted's docstring.
            if not signal:
                continue
            trusted, trust_note = calibration.is_trusted(
                station_code, measure, max_bias_for_trust=cfg.get("max_bias_for_trust", 0.05)
            )
            if not trusted:
                continue
            price = lib.price_for_side(signal.side, yes_ask, no_ask)
            if price is None:
                continue
            candidate = lib.StrategyCandidate(signal.side, price, signal.edge_cents,
                                               f"{signal.rationale}; trust check: {trust_note}")
            model_prob = signal.model_probability_yes

        elif kind == "tight_spread_calibrated":
            # Same calibrated model and edge as calibrated_balanced, but
            # gated on the bid-ask spread being tight — a real market-
            # quality filter, not a different probability model. A wide
            # spread on a thin book means the quoted price may not
            # reflect genuine price discovery yet; this only trades where
            # the market itself is actively agreeing on a price. Uses the
            # YES-side spread as a proxy for the whole market's liquidity
            # regardless of which side gets traded — Kalshi's yes/no books
            # are mechanically linked (buying NO is the same trade as
            # selling YES), so a tight YES spread reflects a genuinely
            # liquid market either way. A negative spread (crossed/bad
            # quote data) fails closed rather than being treated as
            # "very tight."
            if not signal or yes_bid is None or yes_ask is None:
                continue
            spread = yes_ask - yes_bid
            if spread < 0 or spread > cfg.get("max_spread_cents", 5):
                continue
            price = lib.price_for_side(signal.side, yes_ask, no_ask)
            if price is None:
                continue
            candidate = lib.StrategyCandidate(signal.side, price, signal.edge_cents,
                                               f"{signal.rationale}; spread={spread}c (tight market)")
            model_prob = signal.model_probability_yes

        elif kind == "confirmed_signal":
            # Two genuinely INDEPENDENT signals — the calibrated weather
            # model, and pure order-book depth (see
            # strategies_lib.book_imbalance_side) — required to point the
            # SAME direction before trading either alone. This is a
            # confirmation filter, not a third probability model: it can
            # only ever reduce this strategy's trade frequency relative to
            # calibrated_balanced, never increase it, since it adds a
            # requirement on top of the same edge/side calibrated_balanced
            # already needs. If requiring agreement between two
            # independent signal sources measurably improves win rate
            # over calibrated_balanced alone, that's real evidence
            # confirmation is worth something; if it doesn't, that's a
            # real finding too — either way this needs its own bucket to
            # find out, not mixed into calibrated_balanced's numbers.
            if not signal:
                continue
            book_side = lib.book_imbalance_side(
                yes_bids, no_bids,
                min_total_depth=cfg.get("min_total_depth", 20),
                min_imbalance_ratio=cfg.get("min_imbalance_ratio", 3.0),
            )
            if book_side is None or book_side != signal.side:
                continue
            price = lib.price_for_side(signal.side, yes_ask, no_ask)
            if price is None:
                continue
            candidate = lib.StrategyCandidate(signal.side, price, signal.edge_cents,
                                               f"{signal.rationale}; confirmed by order-book depth "
                                               f"(both point {signal.side})")
            model_prob = signal.model_probability_yes

        elif kind == "forecast_momentum":
            if not signal or current_forecast_temp_f is None or previous_forecast_temp_f is None:
                continue
            shift = abs(current_forecast_temp_f - previous_forecast_temp_f)
            if shift < cfg.get("forecast_shift_threshold_f", 3.0):
                continue  # nothing moved enough this cycle to be "momentum," not just noise
            price = lib.price_for_side(signal.side, yes_ask, no_ask)
            if price is None:
                continue
            candidate = lib.StrategyCandidate(signal.side, price, signal.edge_cents, signal.rationale)
            model_prob = signal.model_probability_yes

        elif kind == "settlement_window":
            # Trades ONLY in the last few hours before close, on the
            # strength of strategy.py's near-close observation blending
            # (temperature_high only — see its docstring for why a daily
            # low can't use this). Deliberately its OWN strategy rather
            # than folding into an existing calibrated_* one: every
            # calibrated_* strategy already benefits from the improved
            # signal automatically, but isolating "trades made specifically
            # because we were close to settlement" into one dedicated
            # bucket is what actually lets the confidence/edge analytics
            # and retrospective check whether this hypothesis holds up
            # against real outcomes, rather than mixing it in with trades
            # made many hours out under much higher uncertainty.
            if (not signal or hours_until_close is None
                    or hours_until_close > cfg.get("max_hours_until_close", 3.0)):
                continue
            price = lib.price_for_side(signal.side, yes_ask, no_ask)
            if price is None:
                continue
            candidate = lib.StrategyCandidate(signal.side, price, signal.edge_cents, signal.rationale)
            model_prob = signal.model_probability_yes

        elif kind == "depth_imbalance":
            # Deliberately independent of the weather model entirely —
            # see strategies_lib.depth_imbalance_candidate's docstring for
            # the full reasoning. A genuinely different signal SOURCE from
            # every other strategy here, not just a different threshold on
            # the same one — real diversification, not just more spins on
            # forecast-vs-market-price.
            candidate = lib.depth_imbalance_candidate(
                yes_ask, no_ask, yes_bids, no_bids,
                min_total_depth=cfg.get("min_total_depth", 20),
                min_imbalance_ratio=cfg.get("min_imbalance_ratio", 3.0),
            )
            model_prob = None

        elif kind == "arbitrage":
            candidate = lib.arbitrage_candidate(yes_ask, no_ask)
            if candidate:
                # Quick cheap gate first (kill switch, max open positions,
                # price band) on the naive top-of-book quote, same
                # two-stage pattern as every other kind — the REAL sizing
                # decision happens below via find_max_arbitrage_size, which
                # does its own, more accurate profitability check against
                # real depth, so this first check existing just avoids
                # doing a depth fetch/walk for something that was never
                # going to pass basic gating anyway.
                approved, reason = rm.approve_trade(candidate.price_cents, candidate.edge_cents)
                if approved:
                    if yes_bids is not None and no_bids is not None:
                        yes_ask_levels = lib_depth.implied_ask_levels(no_bids)
                        no_ask_levels = lib_depth.implied_ask_levels(yes_bids)
                        # Not a probabilistic bet — see max_contracts_for_trade's
                        # apply_contract_cap docstring in risk_manager.py for
                        # why arbitrage sizing skips the bet-specific
                        # contract/slippage ceilings and uses only the
                        # dollar-based bankroll cap plus real depth-walked
                        # profitability.
                        dollar_cap = rm.max_contracts_for_trade(candidate.price_cents, apply_contract_cap=False)
                        fill = lib_depth.find_max_arbitrage_size(
                            yes_ask_levels, no_ask_levels, fees.taker_fee_cents,
                            min_net_edge_cents=rm.preset.min_edge_cents,
                        )
                        if fill is None or fill.contracts_fillable < 1:
                            continue
                        pairs = min(fill.contracts_fillable, dollar_cap)
                        if pairs < 1:
                            continue
                        realized_price = round(fill.avg_price_cents)
                    else:
                        # No book data this cycle -- fall back to the
                        # original single-pair behavior exactly as before.
                        pairs, realized_price = 1, candidate.price_cents

                    storage.log_shadow_trade(name, ticker, "both", pairs, realized_price,
                                              model_probability=None, station_code=station_code, measure=measure,
                                              rationale=candidate.rationale)
                    rm.record_fill(cost_cents=pairs * realized_price)
            continue

        elif kind == "favorites":
            # CONFIRMED BUG this check fixes: this rule (buy anything
            # priced >=threshold) has no concept of market STRUCTURE — it
            # can't tell a directional "will it exceed 84?" market from a
            # narrow bucket "will it land in [84.5, 86.5)?" market that's
            # mutually exclusive with several sibling buckets for the SAME
            # underlying event. Confirmed live: it bought YES on four
            # different bucket markets for one temperature event
            # simultaneously, all at 98c, and lost all four (~4410c
            # combined) — not a calibration problem, a structural one.
            # Rather than try to parse ticker suffixes to detect which
            # markets are mutually exclusive (fragile, format-specific),
            # this takes the simpler, more robust position: never hold
            # more than one open favorites position on the same event at
            # all, regardless of whether the specific markets happen to be
            # mutually exclusive, overlapping, or independent — piling
            # into the same underlying outcome multiple ways isn't
            # diversification under any of those relationships.
            if yes_ask is None:
                continue
            if event_ticker and storage.has_open_position_for_event(name, event_ticker):
                continue
            candidate = lib.favorites_candidate(yes_ask, cfg.get("threshold", 90))
            model_prob = None

        elif kind == "longshot":
            if not signal:
                continue
            price = lib.price_for_side(signal.side, yes_ask, no_ask)
            if price is None:
                continue
            candidate = lib.longshot_candidate(signal, price, cfg.get("min_price", 2), cfg.get("max_price", 15))
            model_prob = signal.model_probability_yes

        elif kind == "swing":
            if not signal:
                continue
            price = lib.price_for_side(signal.side, yes_ask, no_ask)
            if price is None or price > cfg.get("entry_max", 40):
                continue
            # Still requires the calibrated model to see SOME edge — this
            # isn't pure price-momentum yet (that needs the movement data
            # to design responsibly), it's "cheap AND the model likes it,"
            # with the early-sell mechanism layered on top.
            candidate = lib.StrategyCandidate(signal.side, price, signal.edge_cents, signal.rationale)
            model_prob = signal.model_probability_yes

        elif kind == "always_trade":
            # No signal needed, no edge computed, no model probability —
            # deliberately. This isn't trying to be good; it's proving the
            # pipeline itself (evaluate -> approve -> size -> log) actually
            # runs end to end on real rain markets every cycle. Takes
            # whichever side has a REAL quoted ask right now (never
            # invents a price that isn't actually on the book) — many thin
            # rain brackets only have one side quoted at any given moment
            # (e.g. a resting bid with no matching ask), so requiring
            # specifically yes_ask meant this sat idle on markets that DO
            # have a genuine, tradeable price, just on the no side.
            if yes_ask is not None and 1 <= yes_ask <= 99:
                side, price = "yes", yes_ask
            elif no_ask is not None and 1 <= no_ask <= 99:
                side, price = "no", no_ask
            else:
                continue
            candidate = lib.StrategyCandidate(side, price, edge_cents=0,
                                               rationale="always-trade pipeline smoke test — ignores edge/model on "
                                                         "purpose, takes whichever side has a real quoted price")
            model_prob = None

        else:
            continue

        if not candidate:
            continue

        # always_trade/favorites/depth_imbalance don't claim any real edge
        # to check fees against in the first place (all three use
        # edge_cents=0 by design — no probability model backs a number to
        # test against fees). edge_already_net_of_fees is being reused
        # here to skip that check entirely, not because fees were actually
        # computed, but because "does 0 edge survive fees" is a
        # nonsensical question for a strategy that isn't making an edge
        # claim at all.
        #
        # CONFIRMED BUG this fixes: this exemption only ever covered
        # always_trade. favorites_baseline has the identical edge_cents=0
        # pattern and was NEVER exempted — meaning gross_expected_cents
        # (0 * contracts) minus any positive fee is always <= 0, so
        # approve_trade's fee-survival check silently rejected EVERY
        # favorites_baseline candidate it ever found, unconditionally,
        # regardless of min_edge_cents_override=0. Not "conditions rarely
        # arose" — structurally impossible to ever pass. Confirmed
        # directly while building depth_imbalance, which shares the same
        # edge_cents=0 pattern and would have had the identical bug.
        no_real_edge_claim = kind in ("always_trade", "favorites", "depth_imbalance")
        approved, reason = rm.approve_trade(candidate.price_cents, candidate.edge_cents,
                                             edge_already_net_of_fees=no_real_edge_claim)
        if not approved:
            continue

        contracts = rm.max_contracts_for_trade(candidate.price_cents)
        if contracts < 1:
            continue

        if kind == "calibrated_confidence_weighted":
            mult = CONFIDENCE_SIZE_MULTIPLIER.get(confidence, 1.0)
            contracts = int(contracts * mult)
            if contracts < 1:
                continue

        # Automatic performance dampening — see the module-level docstring
        # above for the full reasoning. Applied uniformly to every kind,
        # including arbitrage/bracket_arbitrage: if one of those is
        # somehow on a real losing streak, that's almost certainly a bug
        # (a mathematically-sound hedge shouldn't lose consistently), and
        # trading smaller while it gets investigated is a reasonable
        # default, not a wrong one.
        perf = storage.get_recent_strategy_performance(name, lookback=COLD_STREAK_LOOKBACK_TRADES)
        dampening = performance_dampening_multiplier(perf)
        if dampening < 1.0:
            contracts = max(1, int(contracts * dampening))
            candidate.rationale += (f"; sized down {int((1-dampening)*100)}% "
                                     f"(cold streak: {perf['trades']} recent trades, "
                                     f"{perf['roi_pct']:.1f}% ROI)")

        # Automatic calibration-aware dampening — same "reduce risk, never
        # increase it" philosophy as performance dampening above, just
        # triggered by calibration sample count instead of a losing
        # streak. Only applies to strategies that actually consume the
        # calibrated probability model (model_prob is None for
        # depth_imbalance/favorites/always_trade, which don't use
        # station-specific calibration at all). CONFIRMED REAL-WORLD
        # MOTIVATION: see calibration.calibration_dampening_multiplier's
        # docstring — 20+ trades across nearly every strategy all bought
        # the same losing side of the same underlying market, every one
        # with 7-13 calibration samples (below the 20-sample threshold),
        # because they all share the same underlying weather model and
        # all made the same mistake simultaneously. This doesn't fully
        # decorrelate strategies that share a model, but it directly
        # shrinks the damage when that shared model is wrong for a
        # specific, unproven station/measure.
        if model_prob is not None:
            cal_n, _, _ = storage.get_calibration_stats(station_code, measure)
            cal_dampening = calibration.calibration_dampening_multiplier(station_code, measure)
            if cal_dampening < 1.0:
                contracts = max(1, int(contracts * cal_dampening))
                candidate.rationale += (f"; sized down {int((1-cal_dampening)*100)}% "
                                         f"(low calibration: {cal_n} samples for "
                                         f"{station_code}/{measure})")

        # Cross-strategy concentration dampening — see
        # concentration_dampening_multiplier's docstring above for the
        # confirmed real-world motivation. Applied to every directional
        # kind EXCEPT arbitrage, which buys BOTH sides simultaneously and
        # is hedged by construction — multiple arbitrage positions on the
        # same event don't carry the same correlated directional risk a
        # crowd of one-sided directional bets does.
        if kind != "arbitrage" and event_ticker:
            other_count = storage.count_distinct_strategies_exposed_to_event(event_ticker, exclude_strategy=name)
            conc_dampening = concentration_dampening_multiplier(other_count)
            if conc_dampening < 1.0:
                contracts = max(1, int(contracts * conc_dampening))
                candidate.rationale += (f"; sized down {int((1-conc_dampening)*100)}% "
                                         f"({other_count} other strategies already exposed to this event)")

        # Real depth-aware sizing/pricing when the caller fetched the order
        # book this cycle (see the docstring above and depth_sizing.py) —
        # `contracts` above becomes the CEILING this can size up to, not
        # the final answer. Falls back to the flat top-of-book price/count
        # already computed above when book data isn't available.
        realized_price = candidate.price_cents
        if yes_bids is not None and no_bids is not None:
            opposite_bids = no_bids if candidate.side == "yes" else yes_bids
            ask_levels = lib_depth.implied_ask_levels(opposite_bids)

            if model_prob is not None:
                # CONFIRMED SEVERE BUG this fixes: model_prob is always
                # signal.model_probability_yes (the probability of YES),
                # but find_max_profitable_size computes
                # expected_value_cents = model_probability*100 - price,
                # which needs the probability that the SIDE BEING BOUGHT
                # wins — for a "no" trade that's 1-model_prob, not
                # model_prob itself. Passing model_probability_yes
                # unconditionally meant every "no" trade with real book
                # data available got evaluated against the WRONG
                # probability (e.g. a 90%-confident "no" bet, where
                # model_probability_yes=0.10, was evaluated as if it only
                # had a 10% chance of winning) — silently making almost
                # every genuinely profitable "no" trade look wildly
                # unprofitable and get rejected here, for as long as
                # depth-aware sizing has existed. "yes" trades were never
                # affected, since model_prob already IS the right number
                # for that side.
                side_win_probability = model_prob if candidate.side == "yes" else 1 - model_prob
                fill = lib_depth.find_max_profitable_size(
                    ask_levels, fees.taker_fee_cents, side_win_probability,
                    max_contracts_cap=contracts, min_net_edge_cents=rm.preset.min_edge_cents,
                    max_slippage_cents=rm.preset.max_slippage_cents,
                )
                if fill is None:
                    continue
            else:
                # favorites/always_trade don't carry a real probability
                # estimate to check profitability against (see their
                # branches above) — just cap size to what the book can
                # actually support AND to this tier's slippage tolerance,
                # no profit search.
                slippage_cap = lib_depth.max_contracts_within_slippage(ask_levels, rm.preset.max_slippage_cents)
                fill = lib_depth.estimate_fill(ask_levels, min(contracts, slippage_cap))
                if fill.contracts_fillable < 1:
                    continue
            contracts = fill.contracts_fillable
            realized_price = round(fill.avg_price_cents)

        exit_target = (realized_price + cfg["exit_offset"]) if kind == "swing" else None
        storage.log_shadow_trade(name, ticker, candidate.side, contracts, realized_price,
                                  model_probability=model_prob, station_code=station_code, measure=measure,
                                  exit_target_cents=exit_target, rationale=candidate.rationale,
                                  confidence=confidence, event_ticker=event_ticker)
        rm.record_fill(cost_cents=contracts * realized_price)


def evaluate_bracket_set(event_ticker: str, markets: list[dict],
                          leg_orderbooks: Optional[dict[str, tuple[list, list]]] = None) -> None:
    """
    Called once per event (a full group of mutually-exclusive brackets —
    e.g. every temperature bucket for one city/day), separately from
    evaluate_and_log which only ever sees one market at a time. Needs the
    WHOLE group at once because the arbitrage only exists across the full
    set, not in any single bracket.

    markets: list of market dicts (from kalshi_client.get_markets) sharing
    this event_ticker, each expected to have 'ticker', 'yes_ask', and
    either 'no_ask' or 'yes_bid'.

    leg_orderbooks (optional): {ticker: (yes_bids, no_bids)} from
    kalshi.get_orderbook_levels, one entry per member market — fetched by
    the caller (bot.py), since it needs a real API call per leg, unlike
    everything else in this function. When present for EVERY included leg,
    sizing walks each leg's own real depth simultaneously via
    depth_sizing.find_max_bracket_size, instead of assuming the flat
    top-of-book price holds at any size. Same reasoning as the 2-leg
    "arbitrage" kind's find_max_arbitrage_size for why there's no slippage
    limit here: this isn't a bet, so real profitability at real depth is
    the only thing that should stop it from sizing up. Falls back to the
    original flat top-of-book-price behavior when book data is missing for
    any included leg (a fetch failure, or none passed at all).

    Two refinements on top of the base guaranteed-hedge idea:
      - Legs priced at/above BRACKET_NEAR_CERTAIN_SKIP_CENTS (market already
        implying that bracket has only a sliver of a chance of being the
        actual winner) are skipped at entry — see the constant's docstring
        for the capital-efficiency-vs-tail-risk tradeoff being made. The
        min-edge gate below uses the WORST-CASE payout (the actual winner
        turns out to be one of the legs we DID buy — the common case, since
        we specifically excluded the near-certain losers) rather than
        best-case, so this stays conservative rather than overstating edge.
      - Each included leg is logged as an ordinary per-ticker shadow trade
        (real ticker, real side, real price) instead of one aggregate "set"
        row with a precomputed payout. This is what lets
        check_bracket_arbitrage_offload() sell an individual leg early as
        its price decays, and lets the ordinary settle() function handle
        settlement per-leg with no special-casing needed.
    """
    if "bracket_arbitrage" not in ACTIVE_STRATEGIES or len(markets) < 2:
        return

    cfg = ACTIVE_STRATEGIES["bracket_arbitrage"]
    rm = get_engines()["bracket_arbitrage"]

    legs = []  # (ticker, yes_ask, no_ask, measure, station_code)
    for m in markets:
        # market_price_cents() reads the real "<field>_dollars" keys
        # Kalshi's list endpoint actually returns (see its docstring in
        # kalshi_client.py) — a raw m.get("yes_ask") always silently
        # returned None here, since that key never existed on this
        # endpoint. This meant bracket_arbitrage's incomplete-data guard
        # below fired on literally every bracket set, every cycle.
        yes_ask = market_price_cents(m, "yes_ask")
        no_ask = market_price_cents(m, "no_ask")
        yes_bid = market_price_cents(m, "yes_bid")
        if no_ask is None and yes_bid is not None:
            no_ask = 100 - yes_bid
        if yes_ask is None or no_ask is None:
            return  # incomplete data for this event this cycle — skip rather than guess
        legs.append((m["ticker"], yes_ask, no_ask, m.get("measure"), m.get("station_code")))

    sum_yes = sum(l[1] for l in legs)

    # Direction 1 (the common one): buy NO on every bracket. Exactly one
    # loses (the winner), the rest pay out $1 each. Profitable whenever
    # sum_yes > 100 (Kalshi's normal overround/vig on a full bracket set).
    # Direction 2 (rare): buy YES on every bracket — profitable only if
    # sum_yes < 100, which would mean the set is underpriced overall.
    if sum_yes > 100:
        direction = "no"
    elif sum_yes < 100:
        direction = "yes"
    else:
        return  # exactly 100 — no edge either direction

    def price_of(leg):
        return leg[2] if direction == "no" else leg[1]  # no_ask or yes_ask

    skip_cents = cfg.get("near_certain_skip_cents", BRACKET_NEAR_CERTAIN_SKIP_CENTS)
    included = [l for l in legs if price_of(l) < skip_cents]
    skipped_count = len(legs) - len(included)
    if len(included) < 2:
        return  # need at least 2 real legs for the hedge to mean anything

    total_cost = sum(price_of(l) for l in included)
    # Worst case for the legs we actually hold: the real winner turns out
    # to be one of them (not one of the skipped near-certain-losers) — every
    # OTHER included leg pays 100c, that one pays 0.
    worst_case_payout = 100 * (len(included) - 1)
    gross_edge_per_set = worst_case_payout - total_cost
    if gross_edge_per_set <= 0:
        return

    # Quick, cheap gate on the naive top-of-book numbers first (kill
    # switch, max open positions, price band, and the tier's normal
    # per-unit min_edge_cents bar) — same two-stage pattern used
    # everywhere else tonight (main bot's real path, the 2-leg "arbitrage"
    # kind): no point fetching/walking real depth for something that
    # wouldn't even pass this basic check on the quoted price alone.
    # edge_already_net_of_fees=True: total_cost is a MULTI-LEG combined
    # price (can be in the hundreds of cents), not a single contract's
    # price — running it through the internal single-order fee estimate
    # would produce a meaningless number. The real, correctly-computed fee
    # check happens below (find_max_bracket_size's own search, or the
    # flat-fallback path's explicit per-leg fee sum).
    approved, reason = rm.approve_trade(total_cost, int(gross_edge_per_set), edge_already_net_of_fees=True)
    if not approved:
        return

    have_depth_data = (
        leg_orderbooks is not None and
        all(leg_orderbooks.get(l[0]) not in (None, (None, None)) for l in included) and
        all(leg_orderbooks[l[0]][0] is not None and leg_orderbooks[l[0]][1] is not None for l in included)
    )

    if have_depth_data:
        leg_ask_levels = []
        for ticker, yes_ask, no_ask, measure, station_code in included:
            yes_bids, no_bids = leg_orderbooks[ticker]
            # Buying NO on a leg is matched against resting YES bids
            # (inverted to implied NO asks); buying YES against resting NO
            # bids — same convention as depth_sizing.py's module docstring.
            opposite_bids = yes_bids if direction == "no" else no_bids
            leg_ask_levels.append(lib_depth.implied_ask_levels(opposite_bids))

        fill = lib_depth.find_max_bracket_size(leg_ask_levels, fees.taker_fee_cents,
                                                min_net_edge_cents=rm.preset.min_edge_cents)
        if fill is None:
            storage.log_decision(event_ticker, direction, total_cost, 1.0, 0, "skipped",
                                  f"no set size clears net-of-fee edge at real order book depth "
                                  f"({skipped_count} leg(s) skipped as near-certain)", "shadow")
            return

        sets = fill.contracts_fillable
        # Recompute each leg's OWN realized price at the chosen size for
        # individual per-leg logging — find_max_bracket_size only returns
        # the combined aggregate. Cheap: re-walking already-fetched levels
        # in memory, no extra API calls.
        for (ticker, yes_ask, no_ask, measure, station_code), levels in zip(included, leg_ask_levels):
            leg_fill = lib_depth.estimate_fill(levels, sets)
            realized_price = round(leg_fill.avg_price_cents)
            storage.log_shadow_trade("bracket_arbitrage", ticker, direction, sets, realized_price,
                                      station_code=station_code, measure=measure,
                                      rationale=f"bracket set of {len(included)} legs (of {len(included) + skipped_count} "
                                                f"total, {skipped_count} skipped as near-certain), depth-walked, "
                                                f"worst-case payout {worst_case_payout}c/set")
        rm.record_fill(cost_cents=fill.total_cost_cents)
        return

    # ---- Fallback: no book data available this cycle — original flat
    # top-of-book behavior, unchanged from before depth-awareness. ----
    sets = rm.max_contracts_for_trade(total_cost, apply_contract_cap=False)
    if sets < 1:
        return

    # Real fee: a SEPARATE order per bracket, each at its own price — must
    # sum the real per-order fee, not apply the formula to the summed price
    # (the formula is non-linear, so that would give a wrong number).
    total_fee_cents = sum(fees.taker_fee_cents(sets, price_of(l)) for l in included)
    net_edge_per_set = gross_edge_per_set - (total_fee_cents / sets)

    if net_edge_per_set < cfg.get("min_edge_cents_override", 3):
        storage.log_decision(event_ticker, direction, total_cost, 1.0, int(net_edge_per_set),
                              "skipped", f"worst-case edge {gross_edge_per_set}c doesn't clear "
                              f"fee-adjusted minimum after {total_fee_cents}c total fees "
                              f"({skipped_count} leg(s) skipped as near-certain)", "shadow")
        return

    for ticker, yes_ask, no_ask, measure, station_code in included:
        price = no_ask if direction == "no" else yes_ask
        storage.log_shadow_trade("bracket_arbitrage", ticker, direction, sets, price,
                                  station_code=station_code, measure=measure,
                                  rationale=f"bracket set of {len(included)} legs (of {len(included) + skipped_count} "
                                            f"total, {skipped_count} skipped as near-certain), flat top-of-book "
                                            f"pricing (no depth data this cycle), worst-case payout {worst_case_payout}c/set")
    rm.record_fill(cost_cents=total_cost * sets)


def settle_bracket_arbitrage(ticker_results: dict[str, tuple[bool, "str | None"]]) -> int:
    """
    LEGACY PATH ONLY — handles pre-refactor bracket_arbitrage rows that
    still carry a precomputed_payout_cents/sample_member_ticker (one
    aggregate row per set, from before legs were split individually). New
    rows from evaluate_bracket_set() above don't set those fields, so this
    function is a no-op for them by construction (the `if not sample_ticker`
    check below skips straight past) — they settle through the ordinary
    settle() function instead, same as every other strategy's trades,
    since they're now just normal per-ticker 'no'/'yes' rows.

    Kept rather than deleted so any bracket_arbitrage rows already sitting
    open in the DB from before this refactor still settle correctly instead
    of getting silently orphaned.
    """
    engines = get_engines()
    open_trades = storage.get_open_shadow_trades(strategy="bracket_arbitrage")
    settled_count = 0

    for trade in open_trades:
        sample_ticker = trade["sample_member_ticker"]
        if not sample_ticker or sample_ticker not in ticker_results:
            continue
        is_settled, _result = ticker_results[sample_ticker]
        if not is_settled:
            continue

        payout = trade["precomputed_payout_cents"] or 0
        cost = trade["price_cents"] * trade["count"]
        pnl_cents = payout - cost

        storage.settle_shadow_trade(trade["id"], pnl_cents > 0, pnl_cents)
        rm = engines.get("bracket_arbitrage")
        if rm:
            rm.record_settlement(pnl_cents)
        settled_count += 1

    return settled_count


def check_bracket_arbitrage_offload() -> int:
    """
    For every still-open, per-leg bracket_arbitrage position (legacy
    aggregate-set rows are skipped — they aren't individually sellable),
    checks the leg's current market price and sells it early once that
    price has decayed below both BRACKET_OFFLOAD_FLOOR_CENTS AND what we
    paid for it — i.e. once the market is signaling this specific bracket
    is very likely the actual loser — instead of holding every leg all the
    way to settlement, where a true loser pays exactly 0. This salvages
    whatever residual cents are still on the table rather than leaving
    them on the floor for the guaranteed-zero outcome.

    Deliberately conservative: never sells a leg that's still at or above
    its entry price (that's not a decaying loser, that's just normal price
    movement — nothing to salvage there) or above the floor (too early to
    call it basically decided). Runs every cycle, independent of
    settlement, same pattern as check_swing_exits().
    """
    engines = get_engines()
    open_legs = storage.get_open_shadow_trades(strategy="bracket_arbitrage")
    cfg = ACTIVE_STRATEGIES.get("bracket_arbitrage", {})
    floor = cfg.get("offload_floor_cents", BRACKET_OFFLOAD_FLOOR_CENTS)
    offloaded_count = 0

    for trade in open_legs:
        if trade.get("precomputed_payout_cents") is not None:
            continue  # legacy aggregate-set row — not individually sellable

        latest = storage.get_latest_price(trade["ticker"])
        if not latest:
            continue

        side = trade["side"]
        # Current value of what we hold: selling YES realizes yes_bid;
        # selling NO realizes (100 - yes_ask) — same convention as
        # check_swing_exits() uses for the same reason.
        current_value = latest["yes_bid"] if side == "yes" else (
            100 - latest["yes_ask"] if latest["yes_ask"] is not None else None
        )
        if current_value is None:
            continue
        if current_value >= floor or current_value >= trade["price_cents"]:
            continue  # not decaying, or not decayed enough yet — leave it

        pnl_cents = (current_value - trade["price_cents"]) * trade["count"]
        storage.close_shadow_trade_sold(trade["id"], pnl_cents)
        rm = engines.get("bracket_arbitrage")
        if rm:
            rm.record_settlement(pnl_cents)
        offloaded_count += 1

    return offloaded_count


def check_swing_exits() -> int:
    """Runs every cycle, independent of settlement. For every open 'swing'
    position, checks the latest logged price (see storage.log_price_snapshot,
    called each cycle in bot.py for every scanned market) against the exit
    target, and closes the position (sells) the moment it's reached —
    profit is (exit_price - entry_price), never mind how the market
    eventually resolves. Positions that never reach their target fall
    through to normal settlement in settle() below, same as any other
    strategy, once the market actually resolves."""
    engines = get_engines()
    open_swing = storage.get_open_shadow_trades(strategy="swing")
    closed_count = 0

    for trade in open_swing:
        if trade["exit_target_cents"] is None:
            continue
        latest = storage.get_latest_price(trade["ticker"])
        if not latest:
            continue

        side = trade["side"]
        # Selling YES realizes the yes_bid; selling NO realizes (100 - yes_ask).
        exit_price = latest["yes_bid"] if side == "yes" else (
            100 - latest["yes_ask"] if latest["yes_ask"] is not None else None
        )
        if exit_price is None or exit_price < trade["exit_target_cents"]:
            continue

        pnl_cents = (exit_price - trade["price_cents"]) * trade["count"]
        storage.close_shadow_trade_sold(trade["id"], pnl_cents)
        rm = engines.get("swing")
        if rm:
            rm.record_settlement(pnl_cents)
        closed_count += 1

    return closed_count


def settle(ticker_results: dict[str, tuple[bool, Optional[str]]]) -> int:
    """ticker_results: {ticker: (is_settled, 'yes'|'no'|None)} — reuses the
    same settlement lookups the real settlement.py already fetched this
    cycle, so this never makes its own extra API calls.

    Also feeds calibration.py from every settled trade that carries a real
    model_probability — not just the main bot's own (rare, single-strategy)
    real trades, the way settlement.py alone does. The calibrated shadow
    strategies (calibrated_*, temp_calibrated_*, rain_calibrated_*) already
    store the exact same model_probability_yes the main bot uses, at far
    higher volume (dozens of settled trades per strategy vs. a trickle from
    one active strategy) — record_outcome() itself already no-ops cleanly
    on trades without a real station_code/measure/model_probability
    (arbitrage's "both" side, always_trade, favorites), so this is safe to
    call unconditionally rather than needing to filter by kind here."""
    engines = get_engines()
    open_trades = storage.get_open_shadow_trades()
    settled_count = 0

    for trade in open_trades:
        ticker = trade["ticker"]
        if ticker not in ticker_results:
            continue
        is_settled, result = ticker_results[ticker]
        if not is_settled:
            continue

        count = trade["count"]
        price = trade["price_cents"]
        side = trade["side"]

        if side == "both":
            # Arbitrage: guaranteed payout of 100c regardless of result,
            # cost was `price` (the combined yes_ask+no_ask paid).
            pnl_cents = 100 - price
            won = True
        else:
            won = (side == result)
            pnl_cents = (100 - price) * count if won else -price * count

        storage.settle_shadow_trade(trade["id"], won, pnl_cents)
        rm = engines.get(trade["strategy"])
        if rm:
            rm.record_settlement(pnl_cents)

        # Calibration tracks the YES-event probability consistently,
        # regardless of which side was actually traded — same convention
        # settlement.py already uses for the main bot's real trades.
        calibration.record_outcome(
            station_code=trade.get("station_code"),
            measure=trade.get("measure"),
            predicted_probability=trade.get("model_probability"),
            actual_outcome=(result == "yes"),
        )

        settled_count += 1

    return settled_count


def snapshot_all() -> None:
    for name, rm in get_engines().items():
        storage.snapshot_shadow_bankroll(name, rm.state.bankroll_cents)
