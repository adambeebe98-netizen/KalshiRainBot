# Evaluation harness — design

Status: **proposed, not implemented.** This document is the plan.

## 0. The one property that matters

Everything downstream is only as trustworthy as this harness. Its job is
not to find edge — it is to **make false edge hard to manufacture**. Every
design choice below trades convenience for that.

A harness fails in exactly two ways, and they are not symmetric:

- **Type I (fatal):** reports edge that isn't there. You trade money on
  noise. Every mechanism here — point-in-time enforcement, purging,
  net-of-fees, trial counting, baselines — exists to prevent this.
- **Type II (survivable):** misses real edge. You leave money on the
  table and iterate.

When a choice is ambiguous, this design takes the Type II risk.

The harness is also subject to one hard boundary: **it never reads the
VAULT split.** Not by convention — `evaluation/` never imports a path that
can reach `allow_vault=True`, and a test asserts it.

---

## 1. What the data actually supports

From the Task 2 audit. These are the binding constraints, not opinions:

| Constraint | Consequence for the harness |
|---|---|
| Archive is **hourly candlesticks**, closes only, 2.23M points / 59,106 markets | Decision times quantise to the hour. Sub-hour latency is unrepresentable. |
| **No historical order book depth at all** | "Walk real depth" is impossible pre-2026-09-18. Partial fills must come from a volume-participation model. |
| 38.6% of archive hours have **zero volume** | No-fill is the common case, automatically. |
| Archive has **no received_at** — one backfill batch on 2026-09-17/18 | Availability must be *declared* from event time, never measured. |
| Archive weather has **no forecast issue time, unknown lead time**, and comes from Open-Meteo while live uses NWS | Weather features on the archive cannot be point-in-time verified **at all**. |
| Observation lag measured at **median 21 / p90 25 / max 66 min** | Any declared observation availability must be ≥ p90, parameterised. |
| Market lifetime median **39h** (p90 41h) | Label periods overlap test windows constantly → purge is not optional. |
| Archive base rate **17.1% YES** (10,130 / 59,133); 40 rows resolve `scalar` | Baselines must handle a heavily skewed prior; `scalar` needs an explicit exclusion. |
| Live tick capture: top-of-book **with size**, ms timestamps, trade prints with aggressor side — but both services are **currently stopped** | A far better execution model is possible for the live era only, and only if collection resumes. |

---

## 2. Module map

```
evaluation/
  DESIGN.md        this file
  pit.py           point-in-time data access + availability policies
  folds.py         purged, embargoed walk-forward split generation
  execution.py     execution models and their declared assumptions
  objective.py     net-of-fee P&L accounting (wraps fees.py)
  stats.py         normal CDF/inverse, Sharpe, deflated Sharpe, bootstrap
  registry.py      cumulative trial counter, run records, config hashing
  baselines.py     the three mandatory baselines
  harness.py       orchestration, verdict, result records
tests/
  test_eval_pit.py  test_eval_folds.py  test_eval_execution.py
  test_eval_stats.py  test_eval_registry.py  test_eval_baselines.py
  test_eval_acceptance.py     <- the test that justifies the project
```

Pure Python: numpy/scipy/pandas are **not installed** on the droplet and
this design does not add them. All statistics are implemented directly
(`math.erf` for Φ, Acklam's rational approximation for Φ⁻¹) with tests
against published reference values.

---

## 3. Point-in-time correctness (goal 1)

### 3.1 The problem with the requirement as stated

> "the data access layer takes a decision timestamp and filters on
> `received_at <= that timestamp`. If a table can't support that, the
> harness must refuse to use it"

Taken literally, this disqualifies **every table the archive lives in**.
`historical_price_points` has only `end_period_ts` (event time);
`historical_markets` has `backfilled_ts`, which is when we downloaded it
in September 2026, not when the market existed. A harness that refuses
everything without a real `received_at` can evaluate nothing at all,
because the only tables with a true receive time are `realtime_ticks` and
`realtime_weather_obs`, which together hold 23 minutes of data.

### 3.2 The design: availability is a typed, declared property

Every source is wrapped in a `Source` carrying an explicit
`AvailabilityPolicy`, and the DAL filters on `available_at(row) <= as_of`
for all of them. Enforcement is structural and identical in all three
cases; what differs is where the number comes from and how loudly the
result says so.

```python
class Availability(Enum):
    MEASURED = "measured"   # row carries a real received_ts
    DECLARED = "declared"   # available_at = event_ts + stated_lag
    UNKNOWN  = "unknown"    # we cannot establish availability at all
```

| Source | Policy | `available_at` |
|---|---|---|
| `realtime_ticks` | MEASURED | `received_ts` |
| `realtime_weather_obs` | MEASURED | `received_ts` |
| `historical_price_points` | DECLARED | `ts` (candle **end**) + `candle_publish_lag` (default 0) |
| `historical_markets` (metadata: threshold, station, close_time) | DECLARED | `open_time` — the market's terms are public from the moment it opens |
| `historical_markets.result` / `expiration_value` | DECLARED | `close_time` — **the label**, never a feature |
| `historical_weather_points` | **UNKNOWN** | — refused by default |
| `market_snapshots`, `price_history`, `forecast_history`, `decisions` | DECLARED | `ts` (our write time *is* an upper bound on receipt — conservative and correct) |

`UNKNOWN` sources raise `UnavailableSourceError` unless the caller passes
`allow_unverified=True` **with a reason**, which is logged to
`eval_unverified_access` — deliberately the same shape as the vault gate,
because "I used data whose availability I can't prove" deserves the same
audit trail as "I looked at the holdout."

### 3.3 The consequence worth stating plainly

Under this design, **v1 cannot honestly evaluate weather-feature
candidates on the archive.** The archive's forecast series has no issue
time, silently drops revisions (`INSERT OR IGNORE` on `(station, ts)`),
and comes from a different provider than production. The most likely lead
time is near-zero — i.e. it is close to a *nowcast*, which would leak the
answer into a temperature market.

That is not a limitation the harness should paper over. It means:

- v1 evaluates **price- and structure-based** candidates on the archive
  (spread, momentum, favorite/longshot structure, bracket coherence,
  time-to-close effects). These are point-in-time clean.
- Weather-feature candidates are evaluable **only** on live-captured data
  going forward, where `market_snapshots.ts` bounds receipt and
  `realtime_weather_obs` has a measured lag — which is another reason the
  stopped collectors matter.

### 3.4 API sketch

```python
view = PointInTimeView(as_of=decision_ts, sources=[PRICES, MARKET_META])
candles = view.price_points(ticker)        # only rows available_at <= as_of
meta    = view.market(ticker)              # terms only; result attribute absent
view.label(ticker)                         # AttributeError — labels aren't on the view
```

The label is not reachable from the view at all. A candidate function
receives a `PointInTimeView` and returns a `Decision`; it is never handed
the outcome. The oracle strategy in the acceptance test gets its peek
through a separate, explicitly-named `OracleView` that exists only in
test code, so "could a candidate accidentally see the label" has a
one-word answer: no, there is no accessor.

---

## 4. Purged, embargoed walk-forward (goal 2)

Chronological folds only. For a market, the **label period** is
`[open_time, close_time]` — the outcome is determined across that whole
span, not at a point.

```
fold k:   [train .................][purge][   test   ][embargo][ next train ...
```

- **PURGE:** drop any training market whose label period overlaps the test
  window: `open_time < test_end AND close_time > test_start`. With a
  median 39h lifetime, this drops roughly the last two days of every
  training window — small, but exactly the rows that leak.
- **EMBARGO:** after the test window, skip `embargo` of wall-clock time
  before training resumes in later folds. Guards against serial
  correlation between adjacent periods (same weather system, same
  bracket set) rather than against direct label overlap.

Both are explicit parameters (`purge: timedelta`, `embargo: timedelta`),
defaulted to `purge=48h` (> p90 market lifetime) and `embargo=24h`, and
both are recorded in the run record.

Folds are generated **only** over TRAIN and DEV via `splits.load`. A test
asserts `folds.generate()` never emits a window intersecting
`[VAULT_START, VAULT_END)`.

**Required test (explicitly asked for):** construct a market that opens
before a test window and settles inside it, confirm it appears in the
unpurged training set and is absent from the purged one.

---

## 5. Net-of-fees objective (goal 3)

All accounting flows through `fees.py`, including the $1.75 per-order cap.
`Result` objects expose `net_pnl_cents`, `net_sharpe`, `net_roi`. Gross
figures are available **only** as `_diagnostic_gross_pnl_cents`, are
absent from every `summary()` / `__str__` / report table, and a test
asserts no public reporting surface emits a gross figure as a headline.

**One correction to the stated requirement.** "Charge fees on both entry
and exit" is wrong for positions held to settlement: Kalshi charges the
taker fee on execution, and nothing at settlement. Charging twice would
overstate costs by one full fee on the majority of trades and would bias
the harness toward rejecting real edge (a Type II error, but a large and
unnecessary one).

Correct model, matching the live code:

- entry order → always `fees.taker_fee_cents`
- closed early by an order → **plus** an exit fee
- held to settlement → no second fee

A `conservative_fees=True` flag will charge the exit fee unconditionally
for anyone who wants the pessimistic bound, off by default.

---

## 6. Execution (goal 4)

An `ExecutionModel` maps `(Decision, PointInTimeView)` → `Fill | None`,
and — mandatorily — exposes `assumptions() -> ExecutionAssumptions`, a
frozen record that is copied into every result. A result cannot be
constructed without one; there is no default.

### 6.1 `HourlyCandleExecution` (the archive, 2024-09 → 2026-07)

- Decision at `T` → order at `T + latency`, which quantises to the **next
  candle boundary**. Declared: `latency_resolution_seconds = 3600`.
- Fill price: the next candle's `yes_ask_cents` for a buy, `yes_bid_cents`
  for a sell. Never that candle's close price (which may be a trade print
  from a moment we could not have traded at, and which the backfill's
  silent `price.close → ask.close → bid.close` fallback makes ambiguous
  anyway).
- **Partial fills are the default**: filled size is
  `min(requested, participation_rate * candle_volume)`, default
  `participation_rate = 0.10`. Zero-volume hour → **no fill** (38.6% of
  hours). No requested size is ever fully filled without volume to
  support it.
- Declared assumptions, verbatim in output: no depth data exists;
  fill price is top-of-book with no walk; no queue position; no market
  impact; no adverse selection; participation cap is a stand-in for depth,
  not a measurement of it.

### 6.2 `TopOfBookTickExecution` (live era, 2026-09-18 →)

Uses `realtime_ticks` with `yes_bid_size_fp` / `yes_ask_size_fp` from
`raw_json` and millisecond `ts_ms`. Real latency in milliseconds, fill
capped at displayed top-of-book size, trade prints with `taker_side`
usable for a queue-position estimate. Strictly better, and strictly
limited to the period where the collectors were running. Specified now,
built after v1 — it has no data to run on until the services restart.

### 6.3 Refusal

If a candidate is evaluated over a period where its chosen execution
model has no supporting data, the harness raises rather than falling back
to a more optimistic model.

---

## 7. Trial-count awareness (goal 5)

### 7.1 The counter

`eval_trials` — append-only, one row per candidate evaluation ever:
`(id, ts, run_id, candidate_name, config_hash, code_commit, net_sharpe)`.
The cumulative count `N` is `SELECT COUNT(*)`. It only ever grows.
Deleting from it invalidates every subsequent claim, and that is stated in
the table comment.

Every result carries `trials_to_date = N`. There is no code path that
computes a headline metric without it.

### 7.2 Deflated Sharpe ratio

Bailey & López de Prado. Expected maximum Sharpe under the null of no
skill across `N` trials:

```
SR₀ = σ(SR_trials) · [ (1-γ)·Φ⁻¹(1 - 1/N) + γ·Φ⁻¹(1 - 1/(N·e)) ]
```

(γ = Euler–Mascheroni ≈ 0.5772.) Then

```
DSR = Φ[ (SR - SR₀)·√(T-1) / √(1 - γ₃·SR + ((γ₄-1)/4)·SR²) ]
```

with `γ₃`, `γ₄` the skew and kurtosis of the return series — which matter
a great deal here, because binary-payout returns are violently non-normal.

This directly encodes the user's point: `SR₀ ≈ √(2 ln N) · σ(SR_trials)`,
so at N = 10⁶ the best of a million noise strategies scores ~5.3σ by luck.
The harness makes that unforgettable by printing, on every report:
`trials to date: N — a no-skill best-of-N scores SR₀ = x.xx by luck`.

### 7.3 A second, more robust gate

Sharpe assumes something close to iid, roughly-symmetric returns. Binary
markets produce bimodal, lumpy, serially-clustered returns (a whole
bracket set resolves at once), so DSR alone is fragile here. The harness
will additionally run a **stationary block bootstrap** over the
per-trade net P&L series (block length ≈ one market-day, to preserve the
clustering) and report the fraction of resamples with mean ≤ 0. Both
gates must pass. This is cheap, assumption-light insurance against the
DSR formula being the wrong tool for this payoff shape.

---

## 8. Baselines (goal 6)

Every candidate is scored against all three, on the identical folds, with
identical fees and execution:

1. **Market price** — the market-implied probability is the forecast.
   Operationally: a candidate must beat "pay the ask, net of fees" — i.e.
   it must have edge over the price it would trade at.
2. **Constant base rate, fit on TRAIN only.** `P(yes)` estimated from
   training-window settled markets and applied as a fixed probability in
   the test window. The test window's own base rate is never computed
   inside a fold; a test asserts the baseline's fitted value is identical
   across all test windows of a fold.
3. **Best existing heuristic.** The leading live strategy, re-run on the
   same folds (its live P&L is not comparable — different period,
   different data).

**Verdict:** a candidate is `PASS` only if, net of fees, out of sample, it
beats all three **and** DSR > 0.95 **and** the bootstrap gate passes.
Anything else is `FAIL`, reported with the reason, regardless of absolute
numbers. There is no "promising" state — that is how candidates survive
to be re-tested until they pass by luck.

---

## 9. Reproducibility (goal 7)

`eval_runs` records, per run: `run_id`, `ts`, `config_hash` (SHA-256 of
canonical-JSON config), `seed`, `code_commit` (`bot.get_git_commit()`),
`splits_used`, `fold_params`, `execution_assumptions`, and **data snapshot
boundaries** — `MAX(id)` per source table at run start.

Re-running a config re-applies those boundaries, so a run is reproducible
even though the live tables keep growing. If the pinned boundary no longer
exists (rows deleted), the harness raises rather than silently running on
different data. Determinism otherwise comes from: seeded RNG threaded
explicitly (never module-level `random`), `ORDER BY` on every query, and
no iteration over sets or dict-key order for anything that reaches output.

A test runs the same config twice and asserts byte-identical result
records.

---

## 10. Schema additions

```sql
eval_runs(run_id TEXT PK, ts, config_hash, seed, code_commit,
          splits_used, purge_seconds, embargo_seconds,
          execution_model, execution_assumptions_json,
          data_boundaries_json)
eval_trials(id PK, ts, run_id, candidate_name, config_hash,
            code_commit, net_sharpe, verdict)          -- append-only, never deleted
eval_unverified_access(id PK, ts, caller, reason, source, rows_returned)
```

No changes to any existing table.

---

## 11. Test plan

Unit tests per module, plus the two that justify the harness:

### 11.1 Acceptance: 1000 random strategies must show NO EDGE

~1000 candidates making seeded-random decisions with no access to signal,
run through the *full* harness, asserting every one is `FAIL` — including
the luckiest.

Two deliberate variants, because the obvious version of this test is
vacuous:

- **`fees=on` (realistic):** random strategies lose by construction, since
  they pay the spread and the fee every time. This proves the pipeline
  runs end to end, but it would pass even if the entire trial-count
  machinery were deleted.
- **`fees=off, zero-spread synthetic exchange` (the real test):** random
  strategies now have a true mean of exactly zero, so the best of 1000
  will show a large positive Sharpe **by luck alone** — around
  √(2 ln 1000) ≈ 3.7σ. This is the variant that actually tests goal 5. The
  assertion is that the harness still returns `FAIL` for it, and that the
  reported `SR₀` is in the expected range.

Runs on a **synthetic fixture** (a few hundred generated markets with
known-random outcomes), not the real archive — the test must be fast,
deterministic, and independent of the database's contents. A separate
opt-in script runs the same experiment against real TRAIN data. Both use a
temp DB so the real `eval_trials` counter is never polluted by tests.

### 11.2 Acceptance: an oracle must be detected

A candidate that peeks at the settlement result (via the test-only
`OracleView`) must come back `PASS` with a very high DSR and must beat all
three baselines. Proves the harness can see edge when edge is real — i.e.
that the gates above are strict, not merely broken.

### 11.3 Other required tests

- overlapping-label rows are dropped by purge (explicitly requested)
- no fold window ever intersects the vault range
- a source with `UNKNOWN` availability refuses without a logged reason
- a feature timestamped after `as_of` is invisible to the view
- gross P&L appears in no public reporting surface
- identical config → identical results
- fee cap: 200 contracts @ 50c costs 175c, not 350c

---

## 12. Things in the brief I think are wrong or impractical

Flagged rather than silently worked around:

1. **`received_at` on every table (goal 1).** Impossible as stated — the
   archive has no receive time and never will. Proposal: typed
   availability policies (§3.2), enforced identically, with `UNKNOWN`
   refused by default and every declared lag stamped into the output.

2. **Weather features can't be point-in-time validated on the archive
   (§3.3).** This is the most consequential finding: a weather bot's
   harness cannot honestly evaluate weather features on its own two-year
   archive. v1 is restricted to price/structure candidates there. Worth
   deciding explicitly rather than discovering later.

3. **"Fees on both entry and exit" (goal 3)** overstates cost for
   held-to-settlement positions, which are most of them. Recommend
   entry-always / exit-only-if-sold, with a conservative flag.

4. **"Walking real depth" (goal 4)** cannot be done on the archive — there
   is no depth, anywhere, before 2026-09-18. Replaced by an explicit
   volume-participation cap, declared as a stand-in.

5. **Latency below one hour is unrepresentable** on archive data. The
   parameter is kept (it is real for the tick era) but the archive model
   declares `latency_resolution = 3600s`, so asking for 2-second latency
   there does not silently mean what it says.

6. **Sharpe/DSR is a poor fit for binary payoffs.** Keeping it as asked,
   adding a block bootstrap as a second mandatory gate (§7.3).

7. **A single global base rate is a weak baseline** — 17.1% YES across a
   pool containing both 98c favourites and 2c longshots is nearly
   uninformative. Implementing it as specified, plus a price-bucketed base
   rate as a diagnostic. Also: 40 archive markets resolve `scalar` rather
   than yes/no and will be excluded explicitly, not silently.

8. **The existing heuristics are not pure functions.** `shadow.py`'s
   strategies write to the database as they evaluate. Baseline (c)
   requires a pure adapter; I propose reimplementing the leading strategy
   in `baselines.py` and adding a test asserting the adapter reproduces
   `shadow.py`'s decision on fixture inputs, so "the baseline drifted from
   the real strategy" is a test failure rather than a silent
   misattribution.

9. **Gap in coverage.** The archive ends 2026-07-19 and live capture
   starts 2026-09-10. Nothing to do in the harness, but fold generation
   must not span that hole as if it were continuous.

---

## 13. Build order

1. `stats.py` + tests (pure functions, verifiable against published values)
2. `pit.py` + tests (the core safety property)
3. `folds.py` + tests (incl. the purge test)
4. `objective.py`, `execution.py` + tests
5. `registry.py` + tests
6. `baselines.py` + tests
7. `harness.py`
8. **acceptance tests** — 1000 random + oracle
9. A worked example run on TRAIN, reported end to end

Out of scope for this piece: the tick-era execution model (no data until
collection resumes), any actual strategy search, and anything that reads
the vault.
