"""
Execution models: what a decision actually costs to act on.

The dangerous thing a backtest does is assume it got filled. Assume a fill
at the close price of the bar you decided in, at any size you like, and
every strategy looks profitable -- you have modelled a market that grants
wishes.

So every model here must declare what it does and does not model, in a
frozen `ExecutionAssumptions` record that is copied into every result.
There is no default: a result cannot be built without one. If the
assumptions are wrong the numbers are wrong, and that should be legible
from the output rather than buried in a module nobody rereads.

The archive constrains this hard. It is hourly candles with no order book
depth anywhere before 2026-09-18, and 38.6% of its hours have zero volume.
`HourlyCandleExecution` therefore quantises latency to the hour, fills at
top of book with no walk, caps size by a participation rate rather than by
real depth, and refuses to fill at all in an hour where nothing traded.
That last one matters: no-fill is the common case, not an edge case.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class NoExecutionDataError(RuntimeError):
    """The chosen model has no supporting data for the period asked of it.

    Raised rather than silently falling back to a more optimistic model --
    a fallback would substitute a better fill than the data can justify
    exactly where the data is thinnest.
    """


@dataclass(frozen=True)
class Decision:
    """What a candidate returns. `contracts = 0` means abstain, which is a
    real and usually correct answer -- most markets on most hours should
    not be traded."""
    ticker: str
    side: str            # 'yes' | 'no'
    contracts: int
    decided_at: int      # epoch seconds

    def __post_init__(self):
        if self.side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {self.side!r}")
        if self.contracts < 0:
            raise ValueError("contracts cannot be negative")

    @property
    def is_abstain(self) -> bool:
        return self.contracts == 0


@dataclass(frozen=True)
class Fill:
    ticker: str
    side: str
    contracts: int              # actual -- frequently less than requested
    price_cents: int
    filled_at: int
    requested_contracts: int

    @property
    def was_partial(self) -> bool:
        return self.contracts < self.requested_contracts

    @property
    def cost_cents(self) -> int:
        return self.contracts * self.price_cents


@dataclass(frozen=True)
class Position:
    """An open position, carried between decisions."""
    ticker: str
    side: str
    contracts: int
    entry_price_cents: int
    entered_at: int

    def unrealised_cents(self, mark_cents: int) -> int:
        return (mark_cents - self.entry_price_cents) * self.contracts


@dataclass(frozen=True)
class ExecutionAssumptions:
    """Copied verbatim into every result. Reading this should tell you how
    much to believe the number it accompanies."""
    model: str
    latency_seconds: int
    latency_resolution_seconds: int
    participation_rate: float
    uses_depth: bool
    models_queue_position: bool
    models_market_impact: bool
    models_adverse_selection: bool
    notes: tuple[str, ...] = field(default=())

    def describe(self) -> str:
        lines = [f"execution model: {self.model}",
                 f"  latency: {self.latency_seconds}s "
                 f"(resolution {self.latency_resolution_seconds}s)",
                 f"  participation cap: {self.participation_rate:.0%} of volume"]
        for label, value in (("order book depth", self.uses_depth),
                             ("queue position", self.models_queue_position),
                             ("market impact", self.models_market_impact),
                             ("adverse selection", self.models_adverse_selection)):
            lines.append(f"  {label}: {'modelled' if value else 'NOT modelled'}")
        lines.extend(f"  note: {n}" for n in self.notes)
        return "\n".join(lines)


class ExecutionModel:
    """Interface. `execute` returns a Fill or None; None means no fill,
    which is an ordinary outcome and not an error."""

    def assumptions(self) -> ExecutionAssumptions:
        raise NotImplementedError

    def execute(self, decision: Decision, view) -> Fill | None:
        raise NotImplementedError


class HourlyCandleExecution(ExecutionModel):
    """Execution against the hourly candle archive.

    Fill price is the NEXT candle's ask (for a yes buy) or the implied no
    price from its bid (for a no buy) -- never the close. The close may be
    a trade print from a moment we could not have traded at, and the
    backfill's own price/ask/bid fallback makes it ambiguous which of the
    three a given close actually is.
    """

    # How far past the decision to look for a fillable candle. Beyond this
    # the order would have sat unfilled for days, which is not a fill in
    # any meaningful sense -- better to report no fill than to pretend a
    # stale order executed.
    MAX_LOOKAHEAD_S = 7 * 86400

    def __init__(self, latency_seconds: int = 0,
                 participation_rate: float = 0.10):
        if not 0.0 < participation_rate <= 1.0:
            raise ValueError("participation_rate must be in (0, 1]")
        if latency_seconds < 0:
            raise ValueError("latency_seconds cannot be negative")
        self.latency_seconds = latency_seconds
        self.participation_rate = participation_rate

    def assumptions(self) -> ExecutionAssumptions:
        return ExecutionAssumptions(
            model="HourlyCandleExecution",
            latency_seconds=self.latency_seconds,
            latency_resolution_seconds=3600,
            participation_rate=self.participation_rate,
            uses_depth=False,
            models_queue_position=False,
            models_market_impact=False,
            models_adverse_selection=False,
            notes=(
                "no order book depth exists in the archive; the "
                "participation cap is a stand-in for depth, not a "
                "measurement of it",
                "fill price is top of book with no walk, so large orders "
                "are priced as if the whole size traded at the touch",
                "latency below one hour is unrepresentable here; a smaller "
                "value does not mean what it says",
                "a zero-volume hour produces no fill, which is 38.6% of "
                "archive hours",
            ),
        )

    def execute(self, decision: Decision, view) -> Fill | None:
        if decision.is_abstain:
            return None

        earliest = decision.decided_at + self.latency_seconds

        # The candidate's view cannot see the candle we fill in -- by
        # construction, since that candle is in its future. The execution
        # model is harness infrastructure and is *supposed* to see what
        # happened after the decision; that is what "how did the order
        # fill" means. So it builds its own view at a later moment rather
        # than reaching around the candidate's filter, which keeps the
        # availability policy (including any candle publish lag) applied.
        from evaluation.pit import PointInTimeView
        forward = PointInTimeView(
            earliest + self.MAX_LOOKAHEAD_S,
            sources=["historical_price_points"], db_path=view.db_path,
            price_cache=getattr(view, "price_cache", None))
        future = [c for c in forward.price_points(decision.ticker)
                  if c["ts"] > earliest]
        if not future:
            return None
        candle = future[0]

        volume = candle.get("volume") or 0
        if volume <= 0:
            return None

        price = self._fill_price(decision.side, candle)
        if price is None:
            return None

        capacity = int(self.participation_rate * volume)
        contracts = min(decision.contracts, capacity)
        if contracts <= 0:
            return None

        return Fill(ticker=decision.ticker, side=decision.side,
                    contracts=contracts, price_cents=price,
                    filled_at=candle["ts"],
                    requested_contracts=decision.contracts)

    def execute_exit(self, position: Position, view,
                     as_of: int) -> Fill | None:
        """Close a position, paying the spread in the other direction.

        This is the half a hold-to-settlement backtest never has to model,
        and the half where an optimistic assumption does the most damage:
        getting OUT is where a strategy that looked profitable on paper
        discovers what the book actually costs. Selling YES hits the bid,
        not the ask; selling NO means buying YES back at the ask, so it
        realises 100 minus that.
        """
        earliest = as_of + self.latency_seconds
        from evaluation.pit import PointInTimeView
        forward = PointInTimeView(
            earliest + self.MAX_LOOKAHEAD_S,
            sources=["historical_price_points"], db_path=view.db_path,
            price_cache=getattr(view, "price_cache", None))
        future = [c for c in forward.price_points(position.ticker)
                  if c["ts"] > earliest]
        if not future:
            return None
        candle = future[0]
        volume = candle.get("volume") or 0
        if volume <= 0:
            return None

        if position.side == "yes":
            bid = candle.get("yes_bid_cents")
            price = int(bid) if bid is not None and 0 <= bid <= 100 else None
        else:
            ask = candle.get("yes_ask_cents")
            price = (100 - int(ask)) if ask is not None and 0 <= ask <= 100 else None
        if price is None:
            return None

        capacity = int(self.participation_rate * volume)
        contracts = min(position.contracts, capacity)
        if contracts <= 0:
            return None
        return Fill(ticker=position.ticker, side=position.side,
                    contracts=contracts, price_cents=price,
                    filled_at=candle["ts"],
                    requested_contracts=position.contracts)

    @staticmethod
    def _fill_price(side: str, candle: dict) -> int | None:
        """Buying YES pays the ask. Buying NO is economically selling YES
        at the bid, so it costs 100 - bid. Returns None when the relevant
        side of the book is absent -- we cannot claim a price we have no
        evidence was available."""
        if side == "yes":
            ask = candle.get("yes_ask_cents")
            return int(ask) if ask is not None and 0 < ask <= 100 else None
        bid = candle.get("yes_bid_cents")
        if bid is None or not 0 <= bid < 100:
            return None
        return 100 - int(bid)


class ExecutionCoverage:
    """Guards against evaluating a model over a period it has no data for.

    Without this, a candidate run over the archive/live gap (2026-07-19 to
    2026-09-10) would simply never fill and be reported as flat, which
    reads as "no edge" rather than as "no data".
    """

    def __init__(self, model: ExecutionModel, source: str):
        self.model = model
        self.source = source

    def require(self, start: int, end: int, db_path: str | None = None) -> int:
        import sqlite3
        from config import SETTINGS
        conn = sqlite3.connect(db_path or SETTINGS.db_path)
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            (n,) = conn.execute(
                f"SELECT COUNT(*) FROM {self.source} WHERE ts >= ? AND ts < ?",
                (start, end)).fetchone()
        finally:
            conn.close()
        if n == 0:
            raise NoExecutionDataError(
                f"{self.model.assumptions().model} has no rows in {self.source} "
                f"for [{start}, {end}). Refusing rather than reporting a flat "
                f"result, which would read as 'no edge' when it means 'no data'.")
        return n
