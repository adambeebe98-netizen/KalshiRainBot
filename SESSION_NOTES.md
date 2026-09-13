# Session Notes — September 2026 Build Session

A reference for what changed in this session, why, and what to actually
check once real data accumulates. Written because the volume of change
here is large enough that "what did we even do" is a real question worth
having a clean answer to later — for Adam, or for a future Claude session
picking this back up.

Test suite grew from 224 to 392 tests over the course of this session.
Every fix and every new strategy below has direct test coverage; where a
fix corrects a *confirmed* bug (not a hypothetical one), the tests
reproduce the exact real-world scenario that surfaced it.

---

## 1. New trading strategies (6)

Each is isolated in its own bucket specifically so its performance is
directly comparable to whatever baseline it's meant to test or beat —
none of this is meant to be evaluated as "the fleet," but as six separate
hypotheses running in parallel.

- **`temp_settlement_window`** — trades only in the final hours before a
  `temperature_high` market closes, on the strength of a core model
  improvement (see §3 below: observed temperature now actually gets used
  near settlement, not just shown in rationale text). Correctly excluded
  from `temperature_low`, which settles overnight.
- **`rain_settlement_window`** — the rain analog, using the parallel
  near-close decay logic for precipitation (§3). Needed zero new dispatch
  code — same `settlement_window` kind, different `measure_filter`.
- **`depth_imbalance`** — trades purely on order-book depth imbalance,
  deliberately independent of the weather model. Real diversification,
  not another spin on forecast-vs-market-price.
- **`calibration_trusted`** — trades only where the model has been
  directly, empirically verified as well-aligned at that specific
  station/measure (20+ real samples, small measured bias) — versus
  `calibrated_*`, which applies the same correction everywhere regardless
  of how much evidence backs it.
- **`tight_spread_calibrated`** — same calibrated model as
  `calibrated_balanced`, gated on the bid-ask spread being tight (real
  market liquidity, not a different probability estimate).
- **`confirmed_signal`** — trades only when the calibrated model and
  order-book depth (two independent signal sources) agree on direction.

## 2. Confirmed bugs found and fixed

All of these were found through rigorous testing or direct review of real
trade data — not hypothetical concerns. Each has a direct regression
test reproducing the original failure.

1. **`favorites_baseline` could never trade, ever.** The fee-survival
   exemption only covered `always_trade`, but `favorites` shares the
   identical `edge_cents=0` pattern — `0 × contracts − any fee` is always
   negative, so every candidate was silently rejected. Not "conditions
   rarely arose" — structurally impossible to pass, for the strategy's
   entire history.
2. **Every "no"-side price was fabricated.** `100 - yes_ask` was used
   instead of the already-correct `no_ask` parameter, silently losing
   valid trades whenever `yes_ask` happened to be missing (a common thin-
   market case).
3. **The severe one: NO-side trades were priced against the wrong
   probability.** `find_max_profitable_size` needs "probability the
   traded side wins," but `model_probability_yes` was passed
   unconditionally regardless of side. A 90%-confident NO bet
   (`model_probability_yes=0.10`) was evaluated as a 10%-confident bet
   whenever real order-book depth was available, silently rejecting it as
   unprofitable. Affected nearly every calibrated strategy's NO-side
   trades, for as long as depth-aware sizing existed.
4. **`favorites_baseline` bought multiple mutually-conflicting positions
   on one underlying event.** Confirmed live: four different bucket
   markets for the same temperature event, all at 98¢, all lost
   (~4,410¢ combined) — its rule had no concept of "already exposed to
   this event." Fixed: at most one open favorites position per event,
   regardless of whether the specific markets are technically mutually
   exclusive.
5. **Advisor suggestions silently accumulated duplicates.** Nothing
   checked whether a pending suggestion for a given (strategy, param)
   already existed before writing a new one — confirmed live, e.g.
   `swing.exit_offset` had two pending suggestions proposing different
   values (12 and 15) from different runs. Fixed: a fresh suggestion now
   supersedes the stale one for the same param.
6. **`trace_counts_as_zero` is extracted from rules text but never used**
   — flagged, not fixed. NWS's structured API doesn't cleanly distinguish
   "trace" precipitation from "exactly zero," so a verified fix isn't
   possible without live-data confirmation of NWS's actual encoding.
   Documented as a known limitation rather than guessed at.

## 2b. Audit findings (no behavior change — documentation and permanent tests only)

A static-analysis pass across the core modules, looking for the same
"accepted but silently unused parameter" pattern that caused the
`observed_temp_f`/`trace_counts_as_zero` bugs above, surfaced one more
worth understanding precisely:

- **`RiskManager.record_fill`'s `cost_cents` parameter is genuinely
  unused for bankroll math — verified directly, not a bug.** Bankroll
  only changes at settlement via the NET pnl_cents figure; deducting
  cost at entry and adding gross payout at exit would be mathematically
  equivalent, so doing neither at entry and adding the net figure at
  exit nets out correctly (confirmed with a real open→settle trace).
  **The real, worth-knowing consequence**: `max_position_pct` bounds risk
  on any ONE trade against a static bankroll figure, not cumulative
  exposure across many simultaneously open positions. The actual bound
  on total exposure comes from `max_open_positions × max_contracts_per_trade`
  instead — with this preset's defaults, that theoretical worst case
  (every slot filled simultaneously at the max price) works out to
  roughly 68% of a $500 bankroll, not obviously implied by
  `max_position_pct` alone. Documented thoroughly in `record_fill`'s
  docstring and covered by permanent regression tests
  (`TestBankrollAccountingModel`) so this can't silently break later
  without a test catching it. No behavior was changed — this was a
  verify-and-document pass, not a fix, since the existing caps already
  provide a survivable (if not razor-tight) backstop and changing core
  bankroll/sizing math deserves its own dedicated, carefully-considered
  session rather than being folded into this one.

## 3. Core model improvements (affect every existing strategy, not just new ones)

- **Temperature**: `observed_temp_f` used to be accepted by
  `estimate_temperature_probability` but completely ignored in the actual
  math — only shown in rationale text. Near settlement, the model now
  blends the live observation in and narrows its assumed error, since a
  fresh reading a few hours out beats an hours-old forecast. Strictly
  scoped to `temperature_high` — a daily low settles overnight, so an
  afternoon reading says nothing about it.
- **Precipitation**: same idea, applied to POP. A "40% chance of rain
  today" forecast already reflects the whole day; by evening with nothing
  observed yet, the genuinely remaining chance is lower. Scoped away from
  `precipitation_monthly`, where "hours until close" means something
  completely different.

## 4. New safety mechanisms (all mechanical, all can only ever reduce size)

Three independent dampening layers now stack on top of each other,
applied in this order:

1. **Performance dampening** (built earlier this session) — a real cold
   streak (20 recent trades, ROI below threshold) halves size.
2. **Calibration dampening** — trading a station/measure with little or
   no settlement history sizes down automatically (0.25x at zero samples,
   0.5x below the 20-sample threshold). Confirmed motivation: a
   loss-analysis review found 20+ trades across nearly every strategy
   simultaneously buying the same losing side of one underlying market,
   every one with 7-13 calibration samples.
3. **Concentration dampening** — as more *different* strategies pile onto
   the same underlying event, each additional one sizes down (0.5x with
   1-2 others already exposed, 0.25x with 3+). Deliberately a scaling
   response, not a hard cap — a hard cap would have to arbitrarily pick
   which strategies get the slot. Exempts `arbitrage`, which is hedged
   by construction.

**All three are now visible in the trade's own rationale text**, not
just silently applied — e.g. `"sized down 75% (low calibration: 0
samples for KSEA/precipitation_daily)"`. This was previously invisible;
the loss-analysis review that found bug #4 above depended entirely on
reading rationale text, so making dampening auditable the same way felt
important rather than optional.

## 5. Dashboard changes

- Merged "Open Positions" and "Strategy Performance" into one section —
  each strategy is a single expandable row (performance stats + its own
  positions), instead of two separate lists repeating the same names.
- Bot heartbeat ("last scan Xm ago"), flagged red if stale.
- Consolidated safety banner: kill-switched / cooling-off strategy counts
  surfaced at the top, not buried per-row.
- Trend arrows and big-move flags on positions.
- Auto-refresh that defers rather than interrupting when something is
  genuinely open and visible.
- Removed the "0/20 settled trades, could be a streak" warnings per
  explicit request.
- SSH elimination: dashboard password change, "pull latest code &
  restart," manual advisor/retrospective triggers, and a "wipe all paper
  data" action, all from the dashboard itself.

## 6. What to actually check once data accumulates

- Does `calibration_trusted`'s win rate meaningfully beat
  `calibrated_balanced`'s? That's the direct test of whether requiring
  proven trust is worth the reduced trade frequency.
- Does `tight_spread_calibrated` show fewer bad fills than
  `calibrated_balanced`?
- Does `confirmed_signal`'s lower trade count come with a meaningfully
  higher win rate, or does requiring agreement just filter out equally-
  good trades along with bad ones?
- Does `depth_imbalance` hold up beyond its first handful of trades, or
  was the early win noise?
- Do NO-side trades across the calibrated strategies show a visible
  uptick in frequency/volume now that bug #3 is fixed? A sudden shift in
  YES/NO trade balance post-fix would be the clearest confirming signal.
- Does the SEA-style correlated-loss pattern recur at meaningfully
  smaller scale, given concentration + calibration dampening should both
  apply to it now?

No action needed on any of the above yet — just what's worth looking at
once there's enough settled data to say something real.

## 7. Data extraction for calibration/interaction analysis

An explicit push, at Adam's direction, toward richer structured data —
several fields were already computed at decision time but discarded
before now, or only ever surfaced as free text in rationale. Six new
columns on `shadow_trades`, all populated automatically with zero change
to trading behavior:

- **`market_implied_probability`** — the market's own probability
  estimate at decision time.
- **`raw_model_probability`** — the model's estimate BEFORE calibration's
  bias correction, stored alongside the calibrated value
  (`model_probability`) so calibration's real, measured effect is
  directly queryable (`raw_model_probability` vs `model_probability` vs
  actual outcome) instead of reconstructed from rationale text.
- **`hours_until_close_at_decision`** — how long until settlement when
  the trade was actually made.
- **`performance_dampening_multiplier`, `calibration_dampening_multiplier`,
  `concentration_dampening_multiplier`** — the actual numeric value each
  dampening layer applied (1.0 = no dampening, correctly distinct from
  NULL/not-applicable for non-model strategies), not just the rationale
  text notes.

**Side finding, flagged not fixed**: `arbitrage`'s own code path
completely bypasses the shared dampening block, despite an existing
comment claiming dampening applies "uniformly...including arbitrage."
Worth investigating in a future session — left alone here since it's a
behavior question, not a data-extraction one.

With this in place, questions like "does the raw-to-calibrated gap
predict which trades lose" or "does ROI differ when concentration
dampening kicked in" become plain SQL against real columns, not
string-parsing against rationale text.
