
"""
Turn joined markets into training rows, without look-ahead.

Everything so far produces two aligned piles: Kalshi price candles, and
an ESPN probability curve for the same game. This makes rows of them --
one per candle, each carrying what was KNOWN AT THAT MOMENT and a label
that was not.

LOOK-AHEAD IS THE ONLY THING THAT MATTERS HERE. A row whose "independent
probability" came from later in the game trains a model that appears to
beat the market and cannot, because at trade time that number did not
exist. It is invisible in every metric except live P&L, which is the
expensive place to discover it. So:

  * the probability attached to a candle ending at t is the LAST ESPN
    point whose wallclock is <= t. Not the nearest, which can be in the
    future. Not interpolated between neighbours, which peeks at the one
    after.
  * a candle with no qualifying point gets None, not the first available
    value. Pregame candles genuinely have no in-game estimate and
    pretending otherwise invents information.
  * the label is the settled result, used only as a label.

WHY THE CURVE NEEDS A PLAY JOIN. ESPN's probability points carry a
playId and no timestamp. Plays carry the wallclock. So the curve only
has a clock via the plays, and the two sources shape that differently:

  MLB   site API: plays[] with `wallclock`, winprobability[] with
        `playId`. 76/76 resolve.
  NFL   site API returns NO plays, so this uses the core API's
        plays (`wallclock`) and probabilities (`play_id`). 198/198.

Soccer has neither plays nor a curve -- only boxscore and pregame odds
-- so those markets produce price-only rows and are labelled as such
rather than silently carrying empty columns.

Built to run on the machine with the disk and the GPU, not the droplet:
4,778 MLB games times ~550 plays is not work for a 1 vCPU box with
180 MB free.
"""
from __future__ import annotations

import bisect
import datetime as dt
from typing import Iterable

import market_join as mj


def to_epoch(value) -> int | None:
    """Accept the several time shapes these feeds use."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(dt.datetime.fromisoformat(
            text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


class ProbabilityCurve:
    """An ESPN win-probability curve placed on a real clock.

    Holds (timestamp, probabilities) sorted by time, and answers "what
    was the estimate as of t" by binary search -- strictly backwards.
    """

    def __init__(self, points: list[tuple[int, dict]]):
        points = sorted(points, key=lambda p: p[0])
        self._times = [t for t, _ in points]
        self._values = [v for _, v in points]

    def __len__(self) -> int:
        return len(self._times)

    @property
    def span(self) -> tuple[int, int] | None:
        if not self._times:
            return None
        return self._times[0], self._times[-1]

    def as_of(self, when: int) -> dict | None:
        """The last point at or before `when`.

        bisect_right then step back one: a point exactly at `when` is
        allowed (it was known then), a point after it never is.
        """
        if not self._times:
            return None
        idx = bisect.bisect_right(self._times, when) - 1
        if idx < 0:
            return None
        return self._values[idx]


def _play_clock(plays: Iterable[dict]) -> dict[str, int]:
    """play id -> wallclock epoch, for whichever shape the plays are in.

    The site API (MLB) and core API (NFL) name these fields the same
    way for id and wallclock, so one mapping covers both.
    """
    out: dict[str, int] = {}
    for play in plays or ():
        pid = play.get("id")
        stamp = to_epoch(play.get("wallclock"))
        if pid is not None and stamp is not None:
            out[str(pid)] = stamp
    return out


def build_curve(plays: list[dict], points: list[dict],
                fixture: dict) -> ProbabilityCurve:
    """Attach each probability point to its play's wallclock.

    Points whose play has no wallclock are DROPPED rather than guessed
    at. A curve with holes is honest; a curve with invented times is
    not, and the holes are what `as_of` is for.
    """
    clock = _play_clock(plays)
    placed: list[tuple[int, dict]] = []
    for point in points or ():
        pid = point.get("playId") or point.get("play_id")
        if pid is None:
            continue
        stamp = clock.get(str(pid))
        if stamp is None:
            continue
        home = point.get("homeWinPercentage")
        if home is None:
            home = point.get("home_win_pct")
        if home is None:
            continue
        value = {
            "p_yes": mj.probability_for_yes(home, fixture),
            "p_home": float(home),
        }
        # Spread and total estimates ride along where the core API
        # provides them; they belong to different Kalshi series but
        # come from the same request.
        for src, dst in (("spread_cover_home", "spread_cover_home"),
                         ("spreadCoverProbHome", "spread_cover_home"),
                         ("total_over", "total_over"),
                         ("totalOverProb", "total_over")):
            if point.get(src) is not None:
                value[dst] = float(point[src])
        placed.append((stamp, value))
    return ProbabilityCurve(placed)


def candle_rows(market: dict, curve: ProbabilityCurve,
                fixture: dict, series: str,
                include_pregame: bool = True) -> list[dict]:
    """One row per candle, carrying only what was known at its close.

    Hourly and minute candles are both emitted, tagged by resolution.
    They overlap in the final hours by design -- the minute series is
    the only one that reaches settlement, and a consumer that wants a
    single series can filter on `resolution`.
    """
    result = market.get("result")
    label = 1 if result == "yes" else 0 if result == "no" else None
    close_ts = to_epoch(market.get("close_time"))
    rows: list[dict] = []

    for resolution, key in (("hourly", "candles_hourly"),
                            ("minute", "candles_minute")):
        for candle in market.get(key) or ():
            # end_period_ts is the moment this bar CLOSED, so it is the
            # latest instant its contents were known.
            t = to_epoch(candle.get("end_period_ts"))
            if t is None:
                continue
            estimate = curve.as_of(t)
            if estimate is None and not include_pregame:
                continue

            price = mj.candle_price(candle)
            bid = mj.candle_price(candle, "yes_bid")
            ask = mj.candle_price(candle, "yes_ask")
            try:
                volume = float(candle.get("volume") or 0)
            except (TypeError, ValueError):
                volume = 0.0
            try:
                oi = float(candle.get("open_interest") or 0)
            except (TypeError, ValueError):
                oi = 0.0

            rows.append({
                "ticker": market.get("ticker"),
                "series": series,
                "event_id": fixture.get("event_id"),
                "resolution": resolution,
                "ts": t,
                "seconds_to_close": (close_ts - t) if close_ts else None,
                "price": price,
                "bid": bid,
                "ask": ask,
                "spread": (ask - bid) if (ask is not None
                                          and bid is not None) else None,
                "volume": volume,
                "open_interest": oi,
                # None where the game had not started. Filling this with
                # the opening estimate would hand every pregame row a
                # number nobody had.
                "p_independent": (estimate or {}).get("p_yes"),
                "spread_cover_home": (estimate or {}).get("spread_cover_home"),
                "total_over": (estimate or {}).get("total_over"),
                "in_game": estimate is not None,
                "yes_is_home": fixture.get("yes_is_home"),
                "label": label,
            })
    rows.sort(key=lambda r: (r["resolution"], r["ts"]))
    return rows


def edge(row: dict) -> float | None:
    """Independent estimate minus market price.

    Positive means the independent source thinks YES is likelier than
    the price implies. This is a DIAGNOSTIC, not a signal: it ignores
    fees, the spread, and whether anything was on the book -- all three
    of which killed candidates before.
    """
    if row.get("p_independent") is None or row.get("price") is None:
        return None
    return row["p_independent"] - row["price"]
