"""
One-time backfill: charge real Kalshi fees against every already-settled
trade, then rebuild the bankroll snapshots the leaderboard reads from.

WHY THIS EXISTS
---------------
Settlement used to book pure gross P&L — (100 - price) * count on a win,
-price * count on a loss — and never subtracted the taker fee that was
really paid to open the position. Every strategy's recorded history is
therefore better than the same trades would have been in a real account,
and not uniformly so: fees scale with the NUMBER of orders, so the
high-frequency strategies are flattered most and the leaderboard ranking
between them is biased, not just shifted. settlement.py and shadow.py now
deduct fees at settlement; this script applies the same correction to the
rows that settled before that change.

WHAT IT CHANGES
---------------
  * shadow_trades / trades, settled rows only ('won', 'lost', 'sold'):
      pnl_cents -= fee, fee_cents_paid filled in where it was NULL,
      fees_applied_to_pnl = 1.
  * shadow_bankroll_snapshots / bankroll_snapshots: one new corrected
    snapshot per strategy (and for the main bot), computed as
    starting_bankroll + SUM(net pnl) — the same identity the live
    RiskManager maintains incrementally via record_settlement().

Open positions are left completely alone: their fee is charged when they
settle, by the new settlement code, on the way through.

IDEMPOTENT: rows already marked fees_applied_to_pnl=1 are skipped, so
running this twice can't double-charge anything.

STOP THE BOT FIRST. The running process holds each strategy's bankroll in
memory and re-snapshots it every cycle — it would overwrite the corrected
snapshots within minutes, and it re-seeds from the DB only on startup:

    systemctl stop kalshi-weather-bot
    venv/bin/python rederive_fees.py --apply
    systemctl start kalshi-weather-bot

Run without --apply first for a full dry-run report.
"""
from __future__ import annotations

import argparse
import sqlite3
import time

from config import SETTINGS
import fees
import storage

SETTLED_STATUSES = ("won", "lost", "sold")


def _rows_needing_fees(conn, table: str) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in SETTLED_STATUSES)
    return conn.execute(
        f"SELECT * FROM {table} WHERE status IN ({placeholders}) "
        "AND COALESCE(fees_applied_to_pnl, 0) = 0 AND pnl_cents IS NOT NULL",
        SETTLED_STATUSES,
    ).fetchall()


def _fee_for(row: sqlite3.Row) -> int:
    """
    The fee to charge this row, matching what the live settlement paths
    now charge for the same trade.

    A 'sold' row was closed by a second, real taker order (swing exit,
    bracket offload), so it pays twice. The exit price isn't stored
    directly, but it's exactly recoverable from the P&L that WAS stored:
    gross pnl = (exit_price - entry_price) * count, so
    exit_price = entry_price + pnl / count.

    Legacy aggregate bracket_arbitrage rows (precomputed_payout_cents set)
    carry a COMBINED multi-leg price in price_cents, not a real contract
    price — the per-order fee formula is meaningless applied to it, so
    those are charged only whatever real fee was recorded, the same
    decision settle_bracket_arbitrage() makes.
    """
    keys = row.keys()
    if "precomputed_payout_cents" in keys and row["precomputed_payout_cents"] is not None:
        return int(row["fee_cents_paid"] or 0)

    entry_fee = fees.entry_fee_for_row(row)
    if row["status"] != "sold":
        return entry_fee

    count = row["count"] or 1
    exit_price = int(round(row["price_cents"] + (row["pnl_cents"] / count)))
    exit_price = max(0, min(100, exit_price))
    return entry_fee + fees.exit_fee_cents(count, exit_price)


def rederive(apply: bool) -> None:
    with storage.get_conn() as conn:
        conn.row_factory = sqlite3.Row

        totals: dict[str, dict] = {}
        for table, key in (("shadow_trades", "strategy"), ("trades", None)):
            for row in _rows_needing_fees(conn, table):
                who = row[key] if key else "__main_bot__"
                fee = _fee_for(row)
                net = row["pnl_cents"] - fee
                bucket = totals.setdefault(who, {"table": table, "rows": 0, "fees": 0,
                                                  "gross": 0, "net": 0})
                bucket["rows"] += 1
                bucket["fees"] += fee
                bucket["gross"] += row["pnl_cents"]
                bucket["net"] += net
                if apply:
                    # Stores the TOTAL fee charged against this row, which
                    # for a 'sold' row is both orders' fees, not just the
                    # entry fee already sitting in the column — same meaning
                    # the live settlement paths now write.
                    conn.execute(
                        f"UPDATE {table} SET pnl_cents=?, fee_cents_paid=?, "
                        "fees_applied_to_pnl=1 WHERE id=?",
                        (net, fee, row["id"]),
                    )

        print(f"{'APPLIED' if apply else 'DRY RUN'} — fee correction on settled trades\n")
        print(f"{'strategy':<40} {'rows':>6} {'fees':>10} {'gross P&L':>12} {'net P&L':>12}")
        print("-" * 84)
        for who, b in sorted(totals.items(), key=lambda kv: kv[1]["net"], reverse=True):
            print(f"{who:<40} {b['rows']:>6} {b['fees']:>10} {b['gross']:>12} {b['net']:>12}")
        print("-" * 84)
        print(f"{'TOTAL':<40} {sum(b['rows'] for b in totals.values()):>6} "
              f"{sum(b['fees'] for b in totals.values()):>10} "
              f"{sum(b['gross'] for b in totals.values()):>12} "
              f"{sum(b['net'] for b in totals.values()):>12}")

        # --- Rebuild the bankrolls the leaderboard and every restart read ---
        # Derived from the settled rows themselves rather than adjusted
        # incrementally, so this lands on the right number whether or not
        # some rows were already net of fees. On a DRY RUN the rows in the
        # DB are still gross, so the pending fees computed above are
        # subtracted here explicitly — otherwise the preview would report
        # "no change" and hide the very thing being previewed.
        pending_fees = {who: b["fees"] for who, b in totals.items()}
        start = SETTINGS.starting_bankroll_cents
        now = int(time.time())
        print("\ncorrected bankrolls (starting bankroll + net realized P&L):")
        strategies = [r[0] for r in conn.execute(
            "SELECT DISTINCT strategy FROM shadow_bankroll_snapshots")]
        for s in sorted(strategies):
            placeholders = ",".join("?" for _ in SETTLED_STATUSES)
            realized = conn.execute(
                f"SELECT COALESCE(SUM(pnl_cents), 0) FROM shadow_trades "
                f"WHERE strategy=? AND status IN ({placeholders})",
                (s, *SETTLED_STATUSES),
            ).fetchone()[0]
            old = conn.execute(
                "SELECT bankroll_cents FROM shadow_bankroll_snapshots WHERE strategy=? "
                "ORDER BY ts DESC LIMIT 1", (s,)).fetchone()
            corrected = start + realized - (0 if apply else pending_fees.get(s, 0))
            print(f"  {s:<40} {(old[0] if old else 0):>10} -> {corrected:>10}")
            if apply:
                conn.execute(
                    "INSERT INTO shadow_bankroll_snapshots (ts, strategy, bankroll_cents) "
                    "VALUES (?,?,?)", (now, s, corrected))

        placeholders = ",".join("?" for _ in SETTLED_STATUSES)
        main_realized = conn.execute(
            f"SELECT COALESCE(SUM(pnl_cents), 0) FROM trades WHERE status IN ({placeholders})",
            SETTLED_STATUSES,
        ).fetchone()[0]
        main_old = conn.execute(
            "SELECT bankroll_cents FROM bankroll_snapshots ORDER BY ts DESC LIMIT 1").fetchone()
        main_corrected = start + main_realized - (0 if apply else pending_fees.get("__main_bot__", 0))
        print(f"  {'__main_bot__':<40} {(main_old[0] if main_old else 0):>10} -> {main_corrected:>10}")
        if apply:
            conn.execute(
                "INSERT INTO bankroll_snapshots (ts, bankroll_cents, note) VALUES (?,?,?)",
                (now, main_corrected, "corrected: fees deducted from historical P&L"))

        if not apply:
            print("\nNothing written. Re-run with --apply (with the bot stopped) to commit.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="actually write the corrections (default is a dry run)")
    args = parser.parse_args()
    storage.init_db()  # ensures fees_applied_to_pnl exists before we query it
    rederive(apply=args.apply)
