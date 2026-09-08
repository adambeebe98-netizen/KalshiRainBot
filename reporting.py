"""
Builds the plain-text performance summary used in two places:
  - the dashboard's /export page (for you to copy into a chat)
  - advisor.py's weekly Claude review (fed as the data to analyze)
One function, two consumers, so they can never drift out of sync with
each other — if you can see it on /export, that's exactly what advisor.py
based its suggestions on.
"""
from __future__ import annotations

import datetime
import sqlite3
import subprocess
from pathlib import Path

import storage

APP_DIR = Path(__file__).resolve().parent
LOG_PATH = APP_DIR / "bot.log"


def service_status(name: str) -> str:
    try:
        result = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True)
        return result.stdout.strip()
    except Exception:
        return "unknown"


def tail_log_lines(n: int = 500) -> list[str]:
    if not LOG_PATH.exists():
        return []
    return LOG_PATH.read_text(errors="replace").splitlines()[-n:]


def build_export_text(env: dict | None = None) -> str:
    env = env or {}
    lines = []
    lines.append(f"=== EXPORT generated at {datetime.datetime.utcnow().isoformat()}Z ===")
    lines.append(f"Bot status: {service_status('kalshi-weather-bot')} | "
                 f"Live trading: {env.get('LIVE_TRADING', 'false')} | "
                 f"Risk mode: {env.get('RISK_MODE', 'balanced')}")
    try:
        bankroll = storage.load_last_bankroll(int(env.get("STARTING_BANKROLL_CENTS", "50000")))
        lines.append(f"Real bankroll (active bot): ${bankroll / 100:.2f}")
    except Exception as e:
        lines.append(f"(active bankroll unavailable: {e})")
    lines.append("")

    lines.append("--- STRATEGY COMPARISON (all paper, ranked by total P&L) ---")
    try:
        for s in storage.get_shadow_summary():
            wr = f"{s['win_rate']*100:.0f}%" if s["win_rate"] is not None else "n/a"
            roi = f"{s['roi_pct']:+.1f}%" if s["roi_pct"] is not None else "n/a"
            days = f"{s['days_tracked']:.0f}d" if s["days_tracked"] is not None else "n/a"
            low_data = "  [LOW DATA]" if not s["enough_data"] else ""
            lines.append(f"#{s['rank']:<2} {s['strategy']:<24} roi={roi:<8} bankroll=${(s['bankroll_cents'] or 0)/100:>8.2f}  "
                         f"settled={s['settled']:<4} win_rate={wr:<6} total_pnl=${(s['total_pnl_cents'] or 0)/100:.2f}  tracked={days}{low_data}")
    except Exception as e:
        lines.append(f"(shadow summary unavailable: {e})")
    lines.append("")

    lines.append("--- BY CATEGORY (Rain vs Temperature vs Other — same paper trades, split by market type) ---")
    try:
        for cat, data in storage.get_shadow_summary_by_category().items():
            o = data["overall"]
            wr = f"{o['win_rate']*100:.0f}%" if o["win_rate"] is not None else "n/a"
            roi = f"{o['roi_pct']:+.1f}%" if o["roi_pct"] is not None else "n/a"
            low_data = "  [LOW DATA]" if not o["enough_data"] else ""
            lines.append(f"{cat}: roi={roi} settled={o['settled']} win_rate={wr} "
                         f"total_pnl=${(o['total_pnl_cents'] or 0)/100:.2f}{low_data}")
            for s in data["strategies"]:
                swr = f"{s['win_rate']*100:.0f}%" if s["win_rate"] is not None else "n/a"
                sroi = f"{s['roi_pct']:+.1f}%" if s["roi_pct"] is not None else "n/a"
                slow = "  [LOW DATA]" if not s["enough_data"] else ""
                lines.append(f"    #{s['rank']:<2} {s['strategy']:<24} roi={sroi:<8} settled={s['settled']:<4} win_rate={swr}{slow}")
    except Exception as e:
        lines.append(f"(category summary unavailable: {e})")
    lines.append("")

    lines.append("--- CALIBRATION (per station/measure) ---")
    try:
        with storage.get_conn() as conn:
            conn.row_factory = sqlite3.Row
            for r in conn.execute("SELECT * FROM calibration_stats"):
                n = r["n"]
                model_avg = r["sum_predicted"] / n if n else 0
                actual_avg = r["sum_actual"] / n if n else 0
                lines.append(f"{r['station_code']:<8} {r['measure']:<22} n={n:<5} "
                             f"model_avg={model_avg:.2f} actual_avg={actual_avg:.2f} bias={(actual_avg-model_avg):+.2f}")
    except Exception as e:
        lines.append(f"(calibration unavailable: {e})")
    lines.append("")

    lines.append("--- LAST 30 REAL TRADES (the active bot) ---")
    try:
        with storage.get_conn() as conn:
            conn.row_factory = sqlite3.Row
            for r in conn.execute("SELECT * FROM trades ORDER BY ts DESC LIMIT 30"):
                lines.append(f"{r['ticker']:<20} {r['side']:<4} x{r['count']:<4} @ {r['price_cents']}c  "
                             f"status={r['status']:<6} pnl={r['pnl_cents']}")
    except Exception as e:
        lines.append(f"(trades unavailable: {e})")
    lines.append("")

    lines.append("--- LAST 60 SHADOW TRADES (all strategies) ---")
    try:
        with storage.get_conn() as conn:
            conn.row_factory = sqlite3.Row
            for r in conn.execute("SELECT * FROM shadow_trades ORDER BY ts DESC LIMIT 60"):
                lines.append(f"{r['strategy']:<22} {r['ticker']:<20} {r['side']:<4} x{r['count']:<4} @ "
                             f"{r['price_cents']}c status={r['status']:<6} pnl={r['pnl_cents']}")
    except Exception as e:
        lines.append(f"(shadow trades unavailable: {e})")
    lines.append("")

    lines.append("--- SERIES/MARKETS SEEN RECENTLY (from bot.log) ---")
    try:
        log_lines = [l for l in tail_log_lines(500) if "Series " in l and "open market" in l]
        lines.extend(log_lines[-20:])
    except Exception as e:
        lines.append(f"(log parse unavailable: {e})")

    return "\n".join(lines)
