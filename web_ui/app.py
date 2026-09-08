"""
A small password-protected web dashboard for the bot, so setup and
monitoring can happen through a normal webpage instead of nano/systemctl
in a terminal.

Runs as its own always-on service (kalshi-bot-ui), separate from the
trading bot itself, on port 8080. Visit http://YOUR_DROPLET_IP:8080

Security note, plainly: this is intentionally simple — one shared
password, no HTTPS out of the box. That's a reasonable tradeoff for a
personal single-user tool, but it does mean anyone who has both your
droplet's IP and the password can view/change config and start/stop
trading. Two things worth doing if that matters to you: (1) don't share
the URL or password, and (2) in DigitalOcean's "Networking" tab you can
restrict which IPs are allowed to reach port 8080 at all.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import secrets
from pathlib import Path

from flask import Flask, request, session, redirect, url_for, render_template_string

APP_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = APP_DIR / ".env"
KEY_PATH = APP_DIR / "kalshi_private_key.pem"
LOG_PATH = APP_DIR / "bot.log"
DB_PATH = APP_DIR / "bot_state.db"
SERVICE_NAME = "kalshi-weather-bot"

sys.path.insert(0, str(APP_DIR))
import storage  # noqa: E402
import shadow  # noqa: E402
import reporting  # noqa: E402
import categories  # noqa: E402

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)


# ---------- .env helpers ----------

def read_env() -> dict:
    d = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            d[k] = v
    return d


def write_env(d: dict) -> None:
    ENV_PATH.write_text("\n".join(f"{k}={v}" for k, v in d.items()) + "\n")


def restart_bot() -> str:
    result = subprocess.run(["systemctl", "restart", SERVICE_NAME], capture_output=True, text=True)
    return result.stderr or result.stdout or "restarted"


def service_status() -> str:
    result = subprocess.run(["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True)
    return result.stdout.strip()


def tail_log(n: int = 40) -> str:
    if not LOG_PATH.exists():
        return "(no log file yet — the bot hasn't started successfully)"
    lines = LOG_PATH.read_text(errors="replace").splitlines()
    return "\n".join(lines[-n:])


# ---------- auth ----------

@app.before_request
def require_login():
    if request.endpoint in ("login", "static"):
        return
    if not session.get("authenticated"):
        return redirect(url_for("login"))


LOGIN_PAGE = """
<!doctype html><html><head><title>Kalshi Bot Login</title>
<style>{{ css }}</style></head><body>
<div class="card" style="max-width:360px;margin:80px auto;">
<h2>Kalshi Weather Bot</h2>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
<form method="post">
<input type="password" name="password" placeholder="Dashboard password" autofocus>
<button type="submit">Log in</button>
</form>
</div></body></html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    env = read_env()
    expected = env.get("WEB_UI_PASSWORD", "")
    error = None
    if request.method == "POST":
        if expected and request.form.get("password") == expected:
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        error = "Wrong password."
    return render_template_string(LOGIN_PAGE, css=CSS, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- dashboard ----------

CSS = """
body { background:#0d1117; color:#e6edf3; font-family: -apple-system, sans-serif; }
.card { background:#161b22; border:1px solid #30363d; border-radius:10px; padding:24px; margin-bottom:20px; }
h1,h2,h3 { color:#e6edf3; }
input, select, textarea { width:100%; padding:8px; margin:6px 0 14px 0; background:#0d1117; color:#e6edf3;
  border:1px solid #30363d; border-radius:6px; box-sizing:border-box; font-family:inherit; }
textarea { font-family: monospace; height:140px; }
button { background:#238636; color:white; border:none; padding:10px 18px; border-radius:6px; cursor:pointer; font-size:14px; }
button.danger { background:#da3633; }
button.secondary { background:#30363d; }
.err { color:#f85149; }
.ok { color:#3fb950; }
.warn { color:#d29922; }
label { font-size:13px; color:#8b949e; }
pre { background:#0d1117; padding:12px; border-radius:6px; overflow-x:auto; font-size:12px; max-height:400px; overflow-y:auto; }
table { width:100%; border-collapse: collapse; }
td, th { padding:6px 10px; border-bottom:1px solid #30363d; text-align:left; font-size:13px; }
a { color:#58a6ff; }
.pill { padding:3px 10px; border-radius:12px; font-size:12px; }
.pill.active { background:#1a3a1a; color:#3fb950; }
.pill.inactive { background:#3a1a1a; color:#f85149; }
"""

DASHBOARD_PAGE = """
<!doctype html><html><head><title>Kalshi Bot Dashboard</title><style>{{ css }}</style></head><body style="max-width:900px;margin:0 auto;padding:20px;">

<h1>Kalshi Weather Bot <span style="float:right;font-size:14px;"><a href="/export">Export data</a> &nbsp;|&nbsp; <a href="/logout">Log out</a></span></h1>

<div class="card">
  <h2>Status:
    <span class="pill {{ 'active' if status=='active' else 'inactive' }}">{{ status }}</span>
    {% if live_mode %}<span class="pill inactive">LIVE — real money</span>{% else %}<span class="pill active">PAPER — no real money</span>{% endif %}
    {% if schema_verified %}<span class="pill active">Order schema verified</span>{% else %}<span class="pill inactive">Order schema NOT verified</span>{% endif %}
  </h2>
  <p>Bankroll (last known): ${{ "%.2f"|format(bankroll/100) }}</p>
  <form method="post" action="/control" style="display:inline;">
    <input type="hidden" name="action" value="restart">
    <button type="submit">Restart bot</button>
  </form>
  <form method="post" action="/control" style="display:inline;">
    <input type="hidden" name="action" value="stop">
    <button type="submit" class="secondary">Stop bot</button>
  </form>
  {% if message %}<p class="ok">{{ message }}</p>{% endif %}
</div>

<div class="card">
  <h2>Recent log</h2>
  <pre>{{ log_tail }}</pre>
</div>

<div class="card">
  <h2>Strategy comparison <span style="font-size:12px;color:#8b949e;">(all paper — none of these place real orders)</span></h2>
  <canvas id="strategyChart" height="90"></canvas>
  <table style="margin-top:16px;">
    <tr><th>#</th><th>Strategy</th><th>Bankroll</th><th>ROI</th><th>Settled</th><th>Win rate</th><th>Total P&L</th><th>Days tracked</th></tr>
    {% for s in shadow_summary %}
    <tr {{ 'style="background:#1a3a1a;"' if s.rank == 1 and s.enough_data else '' }}>
      <td>{{ s.rank }}</td>
      <td>{{ s.strategy }}</td>
      <td>${{ "%.2f"|format((s.bankroll_cents or 0)/100) }}</td>
      <td class="{{ 'ok' if (s.roi_pct or 0) >= 0 else 'err' }}">
        {{ "%+.1f%%"|format(s.roi_pct) if s.roi_pct is not none else "—" }}
      </td>
      <td>{{ s.settled }}</td>
      <td>{{ "%.0f%%"|format(s.win_rate*100) if s.win_rate is not none else "—" }}</td>
      <td class="{{ 'ok' if (s.total_pnl_cents or 0) >= 0 else 'err' }}">
        {{ "%.2f"|format((s.total_pnl_cents or 0)/100) }}
      </td>
      <td>{{ "%.0f"|format(s.days_tracked) if s.days_tracked is not none else "—" }}</td>
    </tr>
    {% if not s.enough_data %}
    <tr><td></td><td colspan="7" class="warn" style="font-size:12px;">
      ⚠ Only {{ s.settled }}/{{ min_sample_size }} settled trades — could easily be a streak, not skill yet.
    </td></tr>
    {% endif %}
    {% endfor %}
    {% if not shadow_summary %}<tr><td colspan="8">No shadow strategy data yet — give it a few scan cycles.</td></tr>{% endif %}
  </table>
</div>

<div class="card">
  <h2>By category <span style="font-size:12px;color:#8b949e;">(same paper trades, split by what kind of market they're on — some categories may just never be profitable, and that's a real finding, not a bug)</span></h2>
  {% for cat, data in category_summary.items() %}
  <div style="border:1px solid #30363d;border-radius:6px;padding:14px;margin-bottom:14px;">
    <h3 style="margin:0 0 6px 0;">
      {{ cat }}
      <span class="{{ 'ok' if (data.overall.total_pnl_cents or 0) >= 0 else 'err' }}" style="font-size:14px;margin-left:10px;">
        {{ "%+.1f%%"|format(data.overall.roi_pct) if data.overall.roi_pct is not none else "—" }} ROI
      </span>
      <span style="font-size:12px;color:#8b949e;margin-left:10px;">
        {{ data.overall.settled }} settled · {{ "%.0f%%"|format(data.overall.win_rate*100) if data.overall.win_rate is not none else "—" }} win rate
      </span>
    </h3>
    {% if not data.overall.enough_data %}
    <p class="warn" style="font-size:12px;margin:4px 0 10px 0;">
      ⚠ Only {{ data.overall.settled }}/{{ min_sample_size }} settled trades in this category overall — too early to call this category profitable or not.
    </p>
    {% endif %}
    <table>
      <tr><th>#</th><th>Strategy</th><th>Settled</th><th>Win rate</th><th>ROI</th></tr>
      {% for s in data.strategies %}
      <tr>
        <td>{{ s.rank }}</td>
        <td>{{ s.strategy }}{% if not s.enough_data %} <span class="warn" style="font-size:11px;">(low data)</span>{% endif %}</td>
        <td>{{ s.settled }}</td>
        <td>{{ "%.0f%%"|format(s.win_rate*100) if s.win_rate is not none else "—" }}</td>
        <td class="{{ 'ok' if (s.roi_pct or 0) >= 0 else 'err' }}">{{ "%+.1f%%"|format(s.roi_pct) if s.roi_pct is not none else "—" }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
  {% endfor %}
  {% if not category_summary %}<p style="color:#8b949e;">No settled trades yet — categories will appear once shadow trades resolve.</p>{% endif %}
</div>

<div class="card">
  <h2>Recent trades</h2>
  <table>
    <tr><th>Time</th><th>Ticker</th><th>Side</th><th>Count</th><th>Price</th><th>Status</th><th>PnL</th></tr>
    {% for t in trades %}
    <tr><td>{{ t.time }}</td><td>{{ t.ticker }}</td><td>{{ t.side }}</td><td>{{ t.count }}</td>
        <td>{{ t.price }}c</td><td>{{ t.status }}</td><td>{{ t.pnl }}</td></tr>
    {% endfor %}
    {% if not trades %}<tr><td colspan="7">No trades yet.</td></tr>{% endif %}
  </table>
</div>

<div class="card">
  <h2>Calibration (per-station learning)</h2>
  <table>
    <tr><th>Station</th><th>Measure</th><th>Settled trades</th><th>Model avg</th><th>Actual avg</th><th>Bias applied</th></tr>
    {% for c in calibration_rows %}
    <tr><td>{{ c.station }}</td><td>{{ c.measure }}</td><td>{{ c.n }}</td>
        <td>{{ c.model_avg }}</td><td>{{ c.actual_avg }}</td><td>{{ c.bias }}</td></tr>
    {% endfor %}
    {% if not calibration_rows %}<tr><td colspan="6">No calibration data yet — needs settled trades.</td></tr>{% endif %}
  </table>
</div>

<div class="card">
  <h2>Suggestions <span style="font-size:12px;color:#8b949e;">(Claude's weekly review of swing/favorites/longshot thresholds — nothing changes until you click Apply)</span></h2>
  {% for s in suggestions %}
  <div style="border:1px solid #30363d;border-radius:6px;padding:12px;margin-bottom:10px;">
    <strong>{{ s.strategy }}.{{ s.param }}</strong>: {{ s.current_value }} → {{ s.suggested_value }}
    <p style="color:#8b949e;margin:6px 0;">{{ s.rationale }}</p>
    <form method="post" action="/suggestion" style="display:inline;">
      <input type="hidden" name="id" value="{{ s.id }}">
      <input type="hidden" name="action" value="apply">
      <button type="submit">Apply</button>
    </form>
    <form method="post" action="/suggestion" style="display:inline;">
      <input type="hidden" name="id" value="{{ s.id }}">
      <input type="hidden" name="action" value="dismiss">
      <button type="submit" class="secondary">Dismiss</button>
    </form>
  </div>
  {% endfor %}
  {% if not suggestions %}<p style="color:#8b949e;">No pending suggestions right now — check back after the next weekly review, or once more trades have settled.</p>{% endif %}
</div>

<div class="card">
  <h2>Configuration</h2>
  <form method="post" action="/setup">
    <label>Kalshi API Key ID {% if has_kalshi_key %}(currently set — leave blank to keep it){% endif %}</label>
    <input type="text" name="kalshi_key_id" placeholder="{{ 'leave blank to keep current' if has_kalshi_key else 'paste your Kalshi Key ID' }}">

    <label>Kalshi private key {% if has_private_key %}(currently set — leave blank to keep it){% endif %}</label>
    <textarea name="private_key" placeholder="{{ 'leave blank to keep current' if has_private_key else '-----BEGIN ... paste full key contents ...-----END-----' }}"></textarea>

    <label>Anthropic API key {% if has_anthropic_key %}(currently set — leave blank to keep it){% endif %}</label>
    <input type="text" name="anthropic_key" placeholder="{{ 'leave blank to keep current' if has_anthropic_key else 'sk-ant-...' }}">

    <label>Risk mode</label>
    <select name="risk_mode">
      <option value="conservative" {{ 'selected' if risk_mode=='conservative' else '' }}>Conservative</option>
      <option value="balanced" {{ 'selected' if risk_mode=='balanced' else '' }}>Balanced</option>
      <option value="aggressive" {{ 'selected' if risk_mode=='aggressive' else '' }}>Aggressive</option>
    </select>

    <label>Starting paper bankroll ($)</label>
    <input type="number" name="bankroll" value="{{ bankroll_dollars }}">

    <label>Series tickers (comma-separated)</label>
    <input type="text" name="series" value="{{ series }}">

    <button type="submit">Save and restart bot</button>
  </form>
</div>

<div class="card">
  <h2>Order schema verification</h2>
  <p>Confirms create_order()/cancel_order() have been tested against a real order round-trip on Kalshi's demo API. Required before live trading can be enabled — see kalshi_client.py's create_order() docstring for why.</p>
  {% if schema_verified %}
    <p>Status: verified.</p>
    <form method="post" action="/verify_schema">
      <button type="submit" name="action" value="off" class="secondary">Mark unverified</button>
    </form>
  {% else %}
    <p>Status: not verified. Test create_order()/cancel_order() against the demo API (a full buy-then-cancel round trip) before flipping this on.</p>
    <form method="post" action="/verify_schema">
      <button type="submit" name="action" value="on">Mark schema verified</button>
    </form>
  {% endif %}
</div>

<div class="card">
  <h2 class="warn">Go live (real money)</h2>
  <p>Only do this after running in paper mode for a while and checking the trades table above.</p>
  {% if not schema_verified and not live_mode %}
    <p class="warn">Order schema must be verified (above) before live trading can be enabled.</p>
  {% endif %}
  <form method="post" action="/golive">
    {% if live_mode %}
      <button type="submit" name="action" value="off">Switch back to paper mode</button>
    {% else %}
      <label>Type exactly: I ACCEPT THE RISK</label>
      <input type="text" name="confirm">
      <button type="submit" name="action" value="on" class="danger" {{ 'disabled' if not schema_verified else '' }}>Enable live trading</button>
    {% endif %}
  </form>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<script>
  const chartData = {{ chart_data|tojson }};
  const ctx = document.getElementById('strategyChart');
  const colors = ['#58a6ff','#3fb950','#d29922','#f85149','#bc8cff','#39c5cf'];
  new Chart(ctx, {
    type: 'line',
    data: {
      datasets: Object.keys(chartData).map((name, i) => ({
        label: name,
        data: chartData[name].map(p => ({x: p[0]*1000, y: p[1]/100})),
        borderColor: colors[i % colors.length],
        backgroundColor: 'transparent',
        tension: 0.1,
        pointRadius: 2,
      }))
    },
    options: {
      responsive: true,
      scales: {
        x: { type: 'time', time: { unit: 'day' }, ticks: { color: '#8b949e' }, grid: { color: '#30363d' } },
        y: { ticks: { color: '#8b949e', callback: v => '$' + v }, grid: { color: '#30363d' } }
      },
      plugins: { legend: { labels: { color: '#e6edf3' } } }
    }
  });
</script>

</body></html>
"""


@app.route("/")
def dashboard():
    env = read_env()

    trades = []
    calibration_rows = []
    try:
        with storage.get_conn() as conn:
            conn.row_factory = lambda cur, row: {d[0]: row[i] for i, d in enumerate(cur.description)}
            cur = conn.cursor()
            for r in cur.execute("SELECT * FROM trades ORDER BY ts DESC LIMIT 20"):
                trades.append({
                    "time": r["ts"], "ticker": r["ticker"], "side": r["side"],
                    "count": r["count"], "price": r["price_cents"], "status": r["status"],
                    "pnl": r["pnl_cents"] if r["pnl_cents"] is not None else "—",
                })
            for r in cur.execute("SELECT * FROM calibration_stats"):
                n = r["n"]
                model_avg = r["sum_predicted"] / n if n else 0
                actual_avg = r["sum_actual"] / n if n else 0
                calibration_rows.append({
                    "station": r["station_code"], "measure": r["measure"], "n": n,
                    "model_avg": f"{model_avg:.2f}", "actual_avg": f"{actual_avg:.2f}",
                    "bias": f"{(actual_avg - model_avg):+.2f}" if n >= categories.MIN_SAMPLE_SIZE else "not enough data",
                })
    except Exception:
        pass  # DB may not exist yet on first run

    bankroll = storage.load_last_bankroll(int(env.get("STARTING_BANKROLL_CENTS", "50000")))

    shadow_summary = []
    category_summary = {}
    chart_data = {}
    try:
        shadow_summary = storage.get_shadow_summary()
        category_summary = storage.get_shadow_summary_by_category()
        for name in shadow.STRATEGIES.keys():
            history = storage.get_shadow_bankroll_history(name, limit=500)
            if history:
                chart_data[name] = [[ts, cents] for ts, cents in history]
    except Exception:
        pass  # shadow tables may not exist yet on a very first run

    return render_template_string(
        DASHBOARD_PAGE, css=CSS,
        status=service_status(),
        live_mode=env.get("LIVE_TRADING", "false").lower() == "true",
        bankroll=bankroll,
        log_tail=tail_log(),
        trades=trades,
        calibration_rows=calibration_rows,
        shadow_summary=shadow_summary,
        category_summary=category_summary,
        min_sample_size=categories.MIN_SAMPLE_SIZE,
        chart_data=chart_data,
        suggestions=storage.get_suggestions(status="pending"),
        has_kalshi_key=bool(env.get("KALSHI_API_KEY_ID")),
        has_private_key=KEY_PATH.exists() and KEY_PATH.stat().st_size > 100,
        has_anthropic_key=bool(env.get("ANTHROPIC_API_KEY")),
        risk_mode=env.get("RISK_MODE", "balanced"),
        bankroll_dollars=int(env.get("STARTING_BANKROLL_CENTS", "50000")) // 100,
        series=env.get("SERIES_TICKERS", "KXRAIN"),
        schema_verified=env.get("ORDER_SCHEMA_VERIFIED", "false").lower() == "true",
        message=request.args.get("message"),
    )


@app.route("/suggestion", methods=["POST"])
def suggestion_action():
    """The ONLY place in the whole app that ever calls storage.set_override().
    advisor.py can write suggestions; only a human clicking this button can
    turn one into an actual running change."""
    suggestion_id = int(request.form["id"])
    action = request.form.get("action")

    pending = storage.get_suggestions(status="pending")
    match = next((s for s in pending if s["id"] == suggestion_id), None)
    if not match:
        return redirect(url_for("dashboard", message="Suggestion not found (already handled?)."))

    if action == "apply":
        storage.set_override(match["strategy"], match["param"], match["suggested_value"])
        storage.update_suggestion_status(suggestion_id, "applied")
        restart_bot()
        msg = f"Applied: {match['strategy']}.{match['param']} = {match['suggested_value']}. Bot restarted."
    else:
        storage.update_suggestion_status(suggestion_id, "dismissed")
        msg = "Suggestion dismissed."

    return redirect(url_for("dashboard", message=msg))


@app.route("/setup", methods=["POST"])
def setup():
    env = read_env()

    if request.form.get("kalshi_key_id"):
        env["KALSHI_API_KEY_ID"] = request.form["kalshi_key_id"].strip()
    if request.form.get("anthropic_key"):
        env["ANTHROPIC_API_KEY"] = request.form["anthropic_key"].strip()
    if request.form.get("private_key"):
        KEY_PATH.write_text(request.form["private_key"].strip() + "\n")
        os.chmod(KEY_PATH, 0o600)

    env["RISK_MODE"] = request.form.get("risk_mode", "balanced")
    env["STARTING_BANKROLL_CENTS"] = str(int(float(request.form.get("bankroll", 500)) * 100))
    env["SERIES_TICKERS"] = request.form.get("series", "KXRAIN").strip()

    env.setdefault("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
    env.setdefault("KALSHI_PRIVATE_KEY_PATH", str(KEY_PATH))
    env.setdefault("LIVE_TRADING", "false")
    env.setdefault("LIVE_TRADING_CONFIRMED", "false")
    env.setdefault("MIN_CONTRACT_PRICE_CENTS", "2")
    env.setdefault("MAX_CONTRACT_PRICE_CENTS", "90")
    env.setdefault("MAX_OPEN_POSITIONS", "15")
    env.setdefault("POLL_INTERVAL_SECONDS", "300")
    env.setdefault("RULES_CACHE_PATH", str(APP_DIR / "rules_cache.json"))
    env.setdefault("DB_PATH", str(DB_PATH))

    write_env(env)
    restart_bot()
    return redirect(url_for("dashboard", message="Saved. Bot restarted with new settings."))


@app.route("/control", methods=["POST"])
def control():
    action = request.form.get("action")
    if action == "restart":
        restart_bot()
        msg = "Bot restarted."
    elif action == "stop":
        subprocess.run(["systemctl", "stop", SERVICE_NAME])
        msg = "Bot stopped."
    else:
        msg = "Unknown action."
    return redirect(url_for("dashboard", message=msg))


@app.route("/verify_schema", methods=["POST"])
def verify_schema():
    env = read_env()
    action = request.form.get("action")
    if action == "on":
        env["ORDER_SCHEMA_VERIFIED"] = "true"
        msg = "Order schema marked verified. Live trading can now be enabled."
    else:
        env["ORDER_SCHEMA_VERIFIED"] = "false"
        msg = "Order schema marked unverified."
        # Verification is a prerequisite for live trading — pulling it back
        # also pulls live trading back to paper mode, so the two flags can
        # never end up out of sync (verified=false, live=true).
        env["LIVE_TRADING"] = "false"
        env["LIVE_TRADING_CONFIRMED"] = "false"
    write_env(env)
    restart_bot()
    return redirect(url_for("dashboard", message=msg))


@app.route("/golive", methods=["POST"])
def golive():
    env = read_env()
    action = request.form.get("action")
    if action == "on":
        if env.get("ORDER_SCHEMA_VERIFIED", "false").lower() != "true":
            return redirect(url_for("dashboard", message="Order schema isn't marked verified yet — do that first."))
        if request.form.get("confirm", "").strip() != "I ACCEPT THE RISK":
            return redirect(url_for("dashboard", message="Confirmation phrase didn't match — nothing changed."))
        env["LIVE_TRADING"] = "true"
        env["LIVE_TRADING_CONFIRMED"] = "true"
        msg = "LIVE TRADING ENABLED. Real money is now at risk."
    else:
        env["LIVE_TRADING"] = "false"
        env["LIVE_TRADING_CONFIRMED"] = "false"
        msg = "Back to paper mode."
    write_env(env)
    restart_bot()
    return redirect(url_for("dashboard", message=msg))


EXPORT_PAGE = """
<!doctype html><html><head><title>Export data</title><style>{{ css }}</style></head><body style="max-width:900px;margin:0 auto;padding:20px;">
<h1>Export <span style="float:right;font-size:14px;"><a href="/">Back to dashboard</a></span></h1>
<p style="color:#8b949e;">Copy everything in the box below and paste it into your chat with Claude when you want to talk through what's actually happening.</p>
<textarea readonly style="height:600px;font-family:monospace;font-size:12px;" onclick="this.select()">{{ export_text }}</textarea>
</body></html>
"""


@app.route("/export")
def export_data():
    env = read_env()
    export_text = reporting.build_export_text(env)
    return render_template_string(EXPORT_PAGE, css=CSS, export_text=export_text)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
