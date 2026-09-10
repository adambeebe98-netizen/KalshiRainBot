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
import time
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


def tail_log(n: int = 200) -> str:
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
<!doctype html><html><head><title>Kalshi Weather Bot</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{{ css }}</style></head><body>
<div class="panel" style="max-width:340px;margin:100px auto;text-align:center;">
<h1 style="margin-bottom:4px;"><span class="brand-mark">◐</span>Kalshi Weather Bot</h1>
<p class="subtext">Sign in to view the dashboard.</p>
{% if error %}<p class="err" style="font-size:13px;">{{ error }}</p>{% endif %}
<form method="post">
<input type="password" name="password" placeholder="Dashboard password" autofocus>
<button type="submit" style="width:100%;">Log in</button>
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
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

:root {
  --bg: #0A0E16;
  --surface: #121826;
  --surface-2: #1A2233;
  --border: #232D40;
  --text: #E9EEF5;
  --text-dim: #7C8BA3;
  --accent: #38BDCB;
  --accent-dim: #22626B;
  --profit: #4FD98C;
  --loss: #F0654F;
  --warning: #E8AA4C;
  --radius: 8px;
}

* { box-sizing: border-box; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: "IBM Plex Sans", -apple-system, sans-serif;
  margin: 0;
  padding: 0;
  line-height: 1.5;
}

.num, td.num, .num-value, table.data-table td:not(:first-child) {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-variant-numeric: tabular-nums;
}

.shell { max-width: 1180px; margin: 0 auto; padding: 0 20px 60px; }

/* ---------- top bar ---------- */
.topbar {
  display: flex; align-items: center; gap: 16px;
  padding: 18px 0; margin-bottom: 24px;
  border-bottom: 1px solid var(--border);
}
.brand { font-size: 17px; font-weight: 600; letter-spacing: -0.01em; }
.brand-mark { color: var(--accent); margin-right: 6px; }
.status-pills { display: flex; gap: 8px; flex: 1; }
.topbar-links { display: flex; gap: 16px; font-size: 13px; }
.topbar-links a { color: var(--text-dim); text-decoration: none; }
.topbar-links a:hover { color: var(--accent); }

.pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 500;
  border: 1px solid transparent;
}
.pill .dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
.pill.pill-ok { background: rgba(79,217,140,0.1); color: var(--profit); border-color: rgba(79,217,140,0.25); }
.pill.pill-ok .dot { background: var(--profit); }
.pill.pill-warn { background: rgba(232,170,76,0.1); color: var(--warning); border-color: rgba(232,170,76,0.25); }
.pill.pill-warn .dot { background: var(--warning); }
.pill.pill-err { background: rgba(240,101,79,0.1); color: var(--loss); border-color: rgba(240,101,79,0.25); }
.pill.pill-err .dot { background: var(--loss); }
.pill.pill-neutral { background: rgba(124,139,163,0.12); color: var(--text-dim); border-color: var(--border); }

.banner {
  background: var(--surface-2); border: 1px solid var(--accent-dim); border-radius: var(--radius);
  padding: 12px 16px; margin-bottom: 20px; font-size: 14px; color: var(--text);
}

/* ---------- headings ---------- */
h1 { font-size: 20px; font-weight: 600; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 16px; font-weight: 600; margin: 0 0 4px; letter-spacing: -0.01em; }
h3 { font-size: 14px; font-weight: 600; margin: 0 0 6px; }
.subtext { font-size: 12.5px; color: var(--text-dim); margin: 0 0 16px; }
.empty-state { color: var(--text-dim); font-size: 14px; padding: 24px 0; text-align: center; }

/* ---------- panels ---------- */
.panel {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 20px 22px; margin-bottom: 18px;
}

/* ---------- hero: open positions ---------- */
.hero {
  background: var(--surface); border: 1px solid var(--border); border-top: 2px solid var(--accent);
  border-radius: var(--radius); padding: 20px 22px; margin-bottom: 18px;
}
.hero-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 4px; }
.count-badge {
  background: var(--surface-2); color: var(--text-dim); font-size: 12px;
  padding: 2px 9px; border-radius: 12px; font-family: "IBM Plex Mono", monospace;
}
.position-groups { margin-top: 14px; display: flex; flex-direction: column; gap: 6px; }

/* Per-strategy group — a details.sub, but with its own visible container
   so it reads as a distinct block even collapsed, unlike the plain-text
   .sub used inside Diagnostics. */
details.position-group {
  background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius);
}
details.position-group > summary {
  padding: 10px 14px; font-size: 13.5px; display: flex; align-items: center; gap: 8px;
}
details.position-group > summary::before { font-size: 10px; }
details.position-group .sub-body { padding: 2px 14px 10px 30px; }

/* Per-position row inside a group — collapsed to one compact line of
   base data (ticker, side, entry->now, size, unrealized) by default;
   opens to reveal the goal and full reasoning. */
details.position-row { border-top: 1px solid var(--border); }
details.position-row:first-child { border-top: none; }
details.position-row > summary {
  cursor: pointer; list-style: none; padding: 8px 0; display: flex; align-items: center;
  gap: 12px; flex-wrap: wrap; font-size: 12.5px;
}
details.position-row > summary::-webkit-details-marker { display: none; }
details.position-row > summary::before { content: "▸ "; color: var(--accent); font-size: 9px; }
details.position-row[open] > summary::before { content: "▾ "; }
.ticker { font-family: "IBM Plex Mono", monospace; font-size: 13px; font-weight: 500; }
.tag {
  font-size: 11px; padding: 2px 8px; border-radius: 10px;
  background: rgba(56,189,203,0.1); color: var(--accent); border: 1px solid rgba(56,189,203,0.25);
}
.tag.side-yes { background: rgba(79,217,140,0.1); color: var(--profit); border-color: rgba(79,217,140,0.25); }
.tag.side-no { background: rgba(240,101,79,0.1); color: var(--loss); border-color: rgba(240,101,79,0.25); }
.tag.side-both { background: rgba(232,170,76,0.1); color: var(--warning); border-color: rgba(232,170,76,0.25); }
.tag.dampened { background: rgba(232,170,76,0.1); color: var(--warning); border-color: rgba(232,170,76,0.25); }
.position-detail { padding: 0 0 12px 16px; }
.num-inline { font-size: 12.5px; margin: 0 0 6px; }
.num-inline .num-label { color: var(--text-dim); font-weight: 500; }
.position-rationale { font-size: 12.5px; color: var(--text-dim); margin: 6px 0 0; font-style: italic; }

/* ---------- tables ---------- */
table.data-table { width: 100%; border-collapse: collapse; }
table.data-table th {
  text-align: left; font-size: 11px; font-weight: 500; color: var(--text-dim);
  padding: 6px 10px; border-bottom: 1px solid var(--border);
}
table.data-table td { padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 13px; }
table.data-table tr:last-child td { border-bottom: none; }
table.data-table tr.rank-1 { background: rgba(56,189,203,0.06); }

/* ---------- misc components ---------- */
.ok { color: var(--profit); }
.err { color: var(--loss); }
.warn-text { color: var(--warning); }
.suggestion-card, .category-card {
  background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 14px 16px; margin-bottom: 12px;
}
.retro-text { white-space: pre-wrap; line-height: 1.6; font-size: 13.5px; }
.retro-meta { font-size: 12px; color: var(--text-dim); margin-bottom: 10px; }

input, select, textarea {
  width: 100%; padding: 9px 10px; margin: 6px 0 14px; background: var(--bg); color: var(--text);
  border: 1px solid var(--border); border-radius: 6px; box-sizing: border-box;
  font-family: inherit; font-size: 13.5px;
}
textarea { font-family: "IBM Plex Mono", monospace; height: 130px; }
label { font-size: 12.5px; color: var(--text-dim); display: block; }

button {
  background: var(--accent); color: #05161A; border: none; padding: 9px 16px;
  border-radius: 6px; cursor: pointer; font-size: 13.5px; font-weight: 500; font-family: inherit;
}
button:hover { filter: brightness(1.1); }
button.secondary { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); }
button.danger { background: var(--loss); color: #1A0805; }
button:disabled { opacity: 0.4; cursor: not-allowed; }

pre {
  background: var(--bg); padding: 12px; border-radius: 6px; overflow-x: auto;
  font-size: 12px; max-height: 400px; overflow-y: auto; font-family: "IBM Plex Mono", monospace;
  border: 1px solid var(--border);
}
a { color: var(--accent); }

/* ---------- collapsible diagnostics/settings ---------- */
details.mega { margin-bottom: 18px; }
details.mega > summary {
  cursor: pointer; list-style: none; user-select: none;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 14px 18px; font-size: 14px; font-weight: 600; color: var(--text-dim);
  display: flex; align-items: center; gap: 8px;
}
details.mega > summary::-webkit-details-marker { display: none; }
details.mega > summary::before { content: "▸"; color: var(--accent); font-size: 11px; transition: transform 0.15s; }
details.mega[open] > summary::before { transform: rotate(90deg); }
details.mega[open] > summary { border-radius: var(--radius) var(--radius) 0 0; border-bottom: none; }
.mega-body {
  border: 1px solid var(--border); border-top: none; border-radius: 0 0 var(--radius) var(--radius);
  padding: 4px 18px 18px;
}
details.sub { margin: 14px 0; }
details.sub > summary {
  cursor: pointer; list-style: none; font-size: 13.5px; font-weight: 500; padding: 8px 0; color: var(--text);
}
details.sub > summary::-webkit-details-marker { display: none; }
details.sub > summary::before { content: "▸ "; color: var(--accent); font-size: 10px; }
details.sub[open] > summary::before { content: "▾ "; }
.sub-body { padding: 4px 0 12px 16px; }
"""

DASHBOARD_PAGE = """
<!doctype html><html><head><title>Kalshi Weather Bot</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{{ css }}</style></head><body>
<div class="shell">

<header class="topbar">
  <div class="brand"><span class="brand-mark">◐</span>Kalshi Weather Bot</div>
  <div class="status-pills">
    <span class="pill {{ 'pill-ok' if status=='active' else 'pill-err' }}"><span class="dot"></span>{{ status }}</span>
    {% if live_mode %}<span class="pill pill-err"><span class="dot"></span>live — real money</span>
    {% else %}<span class="pill pill-neutral"><span class="dot"></span>paper mode</span>{% endif %}
  </div>
  <nav class="topbar-links">
    <a href="/export">Export</a>
    <a href="/logout">Log out</a>
  </nav>
</header>

{% if message %}<div class="banner">{{ message }}</div>{% endif %}

<section class="hero">
  <div class="hero-head">
    <h1>Open positions</h1>
    <span class="count-badge">{{ open_positions_total }} open</span>
  </div>
  <p class="subtext">What the bot is holding right now, grouped by strategy. Click a strategy to see its positions; click a position for the reasoning behind it.{% if open_positions_total > open_positions|length %} Showing the most recent {{ open_positions|length }} of {{ open_positions_total }}.{% endif %}</p>
  <div class="position-groups">
    {% for strat, positions in positions_by_strategy.items() %}
    <details class="sub position-group">
      <summary>{{ strat }} <span class="count-badge">{{ positions|length }}</span></summary>
      <div class="sub-body">
        {% for p in positions %}
        <details class="position-row">
          <summary>
            <span class="ticker">{{ p.ticker }}</span>
            <span class="tag side-{{ p.side }}">{{ p.side }}</span>
            <span class="num">{{ p.price_cents }}c → {{ p.current_price_cents }}c</span>
            <span class="num">×{{ p.count }}</span>
            <span class="num {{ 'ok' if (p.unrealized_pnl_cents or 0) >= 0 else 'err' }}">{{ "%+.2f"|format((p.unrealized_pnl_cents or 0)/100) }}</span>
          </summary>
          <div class="position-detail">
            {% if p.exit_target_cents %}<p class="num-inline"><span class="num-label">Goal:</span> sell at {{ p.exit_target_cents }}c</p>{% endif %}
            {% if p.confidence %}<p class="num-inline"><span class="num-label">Confidence:</span> {{ p.confidence }}</p>{% endif %}
            {% if p.rationale %}<p class="position-rationale">{{ p.rationale }}</p>{% endif %}
          </div>
        </details>
        {% endfor %}
      </div>
    </details>
    {% endfor %}
    {% if not positions_by_strategy %}<p class="empty-state">No open positions right now.</p>{% endif %}
  </div>
</section>

<section class="panel">
  <h2>Strategy performance</h2>
  <p class="subtext">All paper — none of these place real orders.</p>
  <canvas id="strategyChart" height="90"></canvas>
  <table class="data-table" style="margin-top:16px;">
    <tr><th>#</th><th>Strategy</th><th>Bankroll</th><th>ROI</th><th>Active</th><th>Deployed</th><th>Current value</th><th>Unrealized</th><th>Settled</th><th>Win rate</th><th>Total P&L</th><th>Days</th></tr>
    {% for s in shadow_summary %}
    <tr class="{{ 'rank-1' if s.rank == 1 and s.enough_data else '' }}">
      <td>{{ s.rank }}</td>
      <td>{{ s.strategy }}{% if s.dampened %} <span class="tag dampened">cooling off</span>{% endif %}</td>
      <td>${{ "%.2f"|format((s.bankroll_cents or 0)/100) }}</td>
      <td class="{{ 'ok' if (s.roi_pct or 0) >= 0 else 'err' }}">{{ "%+.1f%%"|format(s.roi_pct) if s.roi_pct is not none else "—" }}</td>
      <td>{{ s.open }}</td>
      <td>${{ "%.2f"|format((s.open_capital_cents or 0)/100) }}</td>
      <td>${{ "%.2f"|format((s.current_value_cents or 0)/100) }}</td>
      <td class="{{ 'ok' if (s.unrealized_pnl_cents or 0) >= 0 else 'err' }}">{{ "%+.2f"|format((s.unrealized_pnl_cents or 0)/100) }}</td>
      <td>{{ s.settled }}</td>
      <td>{{ "%.0f%%"|format(s.win_rate*100) if s.win_rate is not none else "—" }}</td>
      <td class="{{ 'ok' if (s.total_pnl_cents or 0) >= 0 else 'err' }}">{{ "%.2f"|format((s.total_pnl_cents or 0)/100) }}</td>
      <td>{{ "%.0f"|format(s.days_tracked) if s.days_tracked is not none else "—" }}</td>
    </tr>
    {% if not s.enough_data %}
    <tr><td></td><td colspan="11" class="warn-text" style="font-size:12px;">Only {{ s.settled }}/{{ min_sample_size }} settled trades — could easily be a streak, not skill yet.</td></tr>
    {% endif %}
    {% endfor %}
    {% if not shadow_summary %}<tr><td colspan="12">No shadow strategy data yet — give it a few scan cycles.</td></tr>{% endif %}
  </table>
</section>

<section class="panel">
  <h2>Loss analysis</h2>
  <p class="subtext">Claude's periodic qualitative review of settled trades — read-only, nothing here changes any behavior automatically.</p>
  {% if latest_retrospective %}
  <p class="retro-meta">Reviewed {{ latest_retrospective.trades_analyzed }} trades ({{ latest_retrospective.wins_analyzed }} won, {{ latest_retrospective.losses_analyzed }} lost) — {{ latest_retrospective.ago }} ago.</p>
  <div class="retro-text">{{ latest_retrospective.analysis_text }}</div>
  {% else %}
  <p class="empty-state">No retrospective yet — needs at least 20 settled trades in the review window.</p>
  {% endif %}
</section>

<section class="panel">
  <h2>Suggestions</h2>
  <p class="subtext">Claude's weekly review of swing/favorites/longshot thresholds — nothing changes until you click Apply.</p>
  {% for s in suggestions %}
  <div class="suggestion-card">
    <strong>{{ s.strategy }}.{{ s.param }}</strong>: {{ s.current_value }} → {{ s.suggested_value }}
    <p class="subtext" style="margin:6px 0;">{{ s.rationale }}</p>
    <form method="post" action="/suggestion" style="display:inline;">
      <input type="hidden" name="id" value="{{ s.id }}"><input type="hidden" name="action" value="apply">
      <button type="submit">Apply</button>
    </form>
    <form method="post" action="/suggestion" style="display:inline;">
      <input type="hidden" name="id" value="{{ s.id }}"><input type="hidden" name="action" value="dismiss">
      <button type="submit" class="secondary">Dismiss</button>
    </form>
  </div>
  {% endfor %}
  {% if not suggestions %}<p class="empty-state">No pending suggestions right now.</p>{% endif %}
</section>

<details class="mega">
  <summary>Diagnostics &amp; bot internals</summary>
  <div class="mega-body">

    <details class="sub" open>
      <summary>By category</summary>
      <div class="sub-body">
        <canvas id="categoryChart" height="80"></canvas>
        {% for cat, data in category_summary.items() %}
        <div class="category-card">
          <h3>{{ cat }}
            <span class="{{ 'ok' if (data.overall.total_pnl_cents or 0) >= 0 else 'err' }}" style="font-size:13px;margin-left:10px;">{{ "%+.1f%%"|format(data.overall.roi_pct) if data.overall.roi_pct is not none else "—" }} ROI</span>
            <span class="subtext" style="margin-left:8px;display:inline;">{{ data.overall.settled }} settled · {{ "%.0f%%"|format(data.overall.win_rate*100) if data.overall.win_rate is not none else "—" }} win rate</span>
          </h3>
          {% if not data.overall.enough_data %}<p class="warn-text" style="font-size:12px;">Only {{ data.overall.settled }}/{{ min_sample_size }} settled trades in this category — too early to call.</p>{% endif %}
          <table class="data-table">
            <tr><th>#</th><th>Strategy</th><th>Settled</th><th>Win rate</th><th>ROI</th></tr>
            {% for s in data.strategies %}
            <tr><td>{{ s.rank }}</td><td>{{ s.strategy }}{% if not s.enough_data %} <span class="warn-text" style="font-size:11px;">(low data)</span>{% endif %}</td>
                <td>{{ s.settled }}</td><td>{{ "%.0f%%"|format(s.win_rate*100) if s.win_rate is not none else "—" }}</td>
                <td class="{{ 'ok' if (s.roi_pct or 0) >= 0 else 'err' }}">{{ "%+.1f%%"|format(s.roi_pct) if s.roi_pct is not none else "—" }}</td></tr>
            {% endfor %}
          </table>
        </div>
        {% endfor %}
        {% if not category_summary %}<p class="empty-state">No settled trades yet.</p>{% endif %}
      </div>
    </details>

    <details class="sub">
      <summary>Does the model's own confidence predict outcomes?</summary>
      <div class="sub-body">
        <p class="subtext">By estimated edge at decision time:</p>
        <table class="data-table">
          <tr><th>Edge size</th><th>Settled</th><th>Win rate</th><th>Total P&L</th></tr>
          {% for b in edge_buckets %}
          <tr><td>{{ b.edge_bucket }}</td><td>{{ b.trades }}</td><td>{{ "%.0f%%"|format(b.win_rate*100) if b.win_rate is not none else "—" }}</td>
              <td class="{{ 'ok' if (b.total_pnl_cents or 0) >= 0 else 'err' }}">{{ "%.2f"|format((b.total_pnl_cents or 0)/100) }}</td></tr>
          {% endfor %}
          {% if not edge_buckets or edge_buckets|sum(attribute='trades') == 0 %}<tr><td colspan="4">No settled trades with a real model probability yet.</td></tr>{% endif %}
        </table>
        <p class="subtext" style="margin-top:12px;">By rules-extraction confidence:</p>
        <table class="data-table">
          <tr><th>Confidence</th><th>Settled</th><th>Win rate</th><th>Total P&L</th></tr>
          {% for c in confidence_breakdown %}
          <tr><td>{{ c.confidence }}</td><td>{{ c.trades }}</td><td>{{ "%.0f%%"|format(c.win_rate*100) if c.win_rate is not none else "—" }}</td>
              <td class="{{ 'ok' if (c.total_pnl_cents or 0) >= 0 else 'err' }}">{{ "%.2f"|format((c.total_pnl_cents or 0)/100) }}</td></tr>
          {% endfor %}
          {% if not confidence_breakdown %}<tr><td colspan="4">No settled trades yet.</td></tr>{% endif %}
        </table>
      </div>
    </details>

    <details class="sub">
      <summary>Calibration (per-station learning)</summary>
      <div class="sub-body">
        <table class="data-table">
          <tr><th>Station</th><th>Measure</th><th>Settled</th><th>Model avg</th><th>Actual avg</th><th>Bias applied</th></tr>
          {% for c in calibration_rows %}
          <tr><td>{{ c.station }}</td><td>{{ c.measure }}</td><td>{{ c.n }}</td><td>{{ c.model_avg }}</td><td>{{ c.actual_avg }}</td><td>{{ c.bias }}</td></tr>
          {% endfor %}
          {% if not calibration_rows %}<tr><td colspan="6">No calibration data yet.</td></tr>{% endif %}
        </table>
      </div>
    </details>

    <details class="sub">
      <summary>Why isn't it trading?</summary>
      <div class="sub-body">
        <p class="subtext">Last 24h, main bot's decision gate — every scanned market lands in exactly one bucket.</p>
        <p>{{ decision_summary.counts.total }} markets evaluated — {{ decision_summary.counts.traded }} traded, {{ decision_summary.counts.skipped }} skipped.</p>
        <table class="data-table">
          <tr><th>Skip reason</th><th>Count</th></tr>
          {% for reason, count in decision_summary.skip_reasons.items() %}<tr><td>{{ reason }}</td><td>{{ count }}</td></tr>{% endfor %}
          {% if not decision_summary.skip_reasons %}<tr><td colspan="2">No skips logged in the last 24h.</td></tr>{% endif %}
        </table>
      </div>
    </details>

    <details class="sub">
      <summary>Open markets scanner</summary>
      <div class="sub-body">
        <table class="data-table">
          <tr><th>Ticker</th><th>Yes ask</th><th>Yes bid</th><th>No ask (implied)</th><th>Last seen</th></tr>
          {% for m in open_markets %}
          <tr><td>{{ m.ticker }}</td><td>{{ (m.yes_ask ~ 'c') if m.yes_ask is not none else '— (no live ask)' }}</td>
              <td>{{ (m.yes_bid ~ 'c') if m.yes_bid is not none else '—' }}</td>
              <td>{{ ((100 - m.yes_bid) ~ 'c') if m.yes_bid is not none else '—' }}</td><td>{{ m.last_seen }}</td></tr>
          {% endfor %}
          {% if not open_markets %}<tr><td colspan="5">No markets scanned yet this cycle.</td></tr>{% endif %}
        </table>
      </div>
    </details>

    <details class="sub">
      <summary>Recent trades (raw)</summary>
      <div class="sub-body">
        <table class="data-table">
          <tr><th>Time</th><th>Ticker</th><th>Side</th><th>Count</th><th>Price</th><th>Status</th><th>PnL</th></tr>
          {% for t in trades %}
          <tr><td>{{ t.time }}</td><td>{{ t.ticker }}</td><td>{{ t.side }}</td><td>{{ t.count }}</td><td>{{ t.price }}c</td><td>{{ t.status }}</td><td>{{ t.pnl }}</td></tr>
          {% endfor %}
          {% if not trades %}<tr><td colspan="7">No trades yet.</td></tr>{% endif %}
        </table>
      </div>
    </details>

    <details class="sub">
      <summary>Bankroll charts</summary>
      <div class="sub-body">
        <canvas id="strategyChart2" height="90"></canvas>
      </div>
    </details>

    <details class="sub">
      <summary>Bot log</summary>
      <div class="sub-body"><pre>{{ log_tail }}</pre></div>
    </details>

  </div>
</details>

<details class="mega">
  <summary>Settings &amp; controls</summary>
  <div class="mega-body">

    <details class="sub" open>
      <summary>Configuration</summary>
      <div class="sub-body">
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
    </details>

    <details class="sub">
      <summary>Order schema verification</summary>
      <div class="sub-body">
        <p class="subtext">Confirms create_order()/cancel_order() have been tested against a real order round-trip on Kalshi's demo API. Required before live trading can be enabled.</p>
        {% if schema_verified %}
          <p>Status: verified.</p>
          <form method="post" action="/verify_schema"><button type="submit" name="action" value="off" class="secondary">Mark unverified</button></form>
        {% else %}
          <p>Status: not verified.</p>
          <form method="post" action="/verify_schema"><button type="submit" name="action" value="on">Mark schema verified</button></form>
        {% endif %}
      </div>
    </details>

    <details class="sub">
      <summary class="warn-text">Go live (real money)</summary>
      <div class="sub-body">
        <p class="subtext">Only do this after running in paper mode for a while and checking the trades above.</p>
        {% if not schema_verified and not live_mode %}<p class="warn-text">Order schema must be verified first.</p>{% endif %}
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
    </details>

    <details class="sub">
      <summary>Bot controls</summary>
      <div class="sub-body">
        <p>Bankroll (last known): ${{ "%.2f"|format(bankroll/100) }}</p>
        <form method="post" action="/control" style="display:inline;"><input type="hidden" name="action" value="restart"><button type="submit">Restart bot</button></form>
        <form method="post" action="/control" style="display:inline;"><input type="hidden" name="action" value="stop"><button type="submit" class="secondary">Stop bot</button></form>
      </div>
    </details>

  </div>
</details>

</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<script>
  const chartData = {{ chart_data|tojson }};
  const colors = ['#38BDCB','#4FD98C','#E8AA4C','#F0654F','#9B8CFF','#5FD0E8'];
  const chartOpts = {
    responsive: true,
    scales: {
      x: { type: 'time', time: { unit: 'day' }, ticks: { color: '#7C8BA3' }, grid: { color: '#232D40' } },
      y: { ticks: { color: '#7C8BA3', callback: v => '$' + v }, grid: { color: '#232D40' } }
    },
    plugins: { legend: { labels: { color: '#E9EEF5' } } }
  };
  function buildStrategyChart(ctx) {
    if (!ctx) return;
    new Chart(ctx, {
      type: 'line',
      data: { datasets: Object.keys(chartData).map((name, i) => ({
        label: name, data: chartData[name].map(p => ({x: p[0]*1000, y: p[1]/100})),
        borderColor: colors[i % colors.length], backgroundColor: 'transparent', tension: 0.1, pointRadius: 2,
      })) },
      options: chartOpts,
    });
  }
  buildStrategyChart(document.getElementById('strategyChart'));
  document.querySelector('details.mega:nth-of-type(1)').addEventListener('toggle', function() {
    if (this.open) buildStrategyChart(document.getElementById('strategyChart2'));
  }, { once: true });

  const categoryPnlData = {{ category_pnl_history|tojson }};
  const categoryColors = { Rain: '#38BDCB', Temperature: '#E8AA4C', Other: '#9B8CFF' };
  function buildCategoryChart() {
    const el = document.getElementById('categoryChart');
    if (!el || el.dataset.built) return;
    el.dataset.built = '1';
    new Chart(el, {
      type: 'line',
      data: { datasets: Object.keys(categoryPnlData).map((cat, i) => ({
        label: cat + ' (cumulative P&L)', data: categoryPnlData[cat].map(p => ({x: p[0]*1000, y: p[1]/100})),
        borderColor: categoryColors[cat] || colors[i % colors.length], backgroundColor: 'transparent', tension: 0.1, pointRadius: 2,
      })) },
      options: chartOpts,
    });
  }
  const categoryDetails = document.getElementById('categoryChart') ? document.getElementById('categoryChart').closest('details') : null;
  if (categoryDetails) {
    if (categoryDetails.open) buildCategoryChart();
    categoryDetails.addEventListener('toggle', function() { if (this.open) buildCategoryChart(); });
  }
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
    category_pnl_history = {}
    open_markets = []
    decision_summary = {"counts": {"traded": 0, "skipped": 0, "total": 0}, "skip_reasons": {}}
    latest_retrospective = None
    edge_buckets = []
    confidence_breakdown = []
    open_positions = []
    open_positions_total = 0
    positions_by_strategy = {}
    try:
        shadow_summary = storage.get_shadow_summary()
        for s in shadow_summary:
            # Surfaces the automatic performance-dampening mechanism (see
            # shadow.performance_dampening_multiplier) on the dashboard —
            # it runs silently otherwise, and a strategy suddenly sizing
            # smaller with no visible explanation would just look like a
            # bug.
            perf = storage.get_recent_strategy_performance(s["strategy"])
            s["dampened"] = shadow.performance_dampening_multiplier(perf) < 1.0
        category_summary = storage.get_shadow_summary_by_category()
        for name in shadow.STRATEGIES.keys():
            history = storage.get_shadow_bankroll_history(name, limit=500)
            if history:
                chart_data[name] = [[ts, cents] for ts, cents in history]
        category_pnl_history = storage.get_category_pnl_over_time()
        decision_summary = storage.get_decision_summary(hours=24)
        now = int(time.time())
        for m in storage.get_current_open_markets():
            age_s = now - m["ts"]
            last_seen = f"{age_s}s ago" if age_s < 120 else f"{age_s // 60}m ago"
            open_markets.append({**m, "last_seen": last_seen})
        recent_retros = storage.get_recent_retrospectives(limit=1)
        if recent_retros:
            r = recent_retros[0]
            age_s = now - r["ts"]
            ago = f"{age_s // 3600}h" if age_s >= 3600 else f"{age_s // 60}m"
            latest_retrospective = {**r, "ago": ago}
        edge_buckets = storage.get_win_rate_by_edge_bucket()
        confidence_breakdown = storage.get_win_rate_by_confidence()
        open_positions = storage.get_open_positions_detail()
        open_positions_total = storage.get_open_shadow_position_total_count()
        # Grouped by strategy for the dashboard's collapsible view — a flat
        # list of every open position becomes an unmanageable scroll past a
        # handful of trades, so this chunks it into one dropdown per
        # strategy (busiest first), with each individual position itself
        # collapsed to a one-line summary until clicked open for the
        # reasoning behind it.
        positions_by_strategy: dict[str, list[dict]] = {}
        for p in open_positions:
            positions_by_strategy.setdefault(p["strategy"], []).append(p)
        positions_by_strategy = dict(
            sorted(positions_by_strategy.items(), key=lambda kv: len(kv[1]), reverse=True)
        )
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
        category_pnl_history=category_pnl_history,
        open_markets=open_markets,
        decision_summary=decision_summary,
        suggestions=storage.get_suggestions(status="pending"),
        latest_retrospective=latest_retrospective,
        edge_buckets=edge_buckets,
        confidence_breakdown=confidence_breakdown,
        open_positions=open_positions,
        open_positions_total=open_positions_total,
        positions_by_strategy=positions_by_strategy,
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
<!doctype html><html><head><title>Export data</title><style>{{ css }}</style></head><body>
<div class="shell" style="max-width:900px;">
<header class="topbar">
  <div class="brand">Export</div>
  <div style="flex:1;"></div>
  <nav class="topbar-links"><a href="/">Back to dashboard</a></nav>
</header>
<p class="subtext">Copy everything in the box below and paste it into your chat with Claude when you want to talk through what's actually happening.</p>
<textarea readonly style="height:600px;" onclick="this.select()">{{ export_text }}</textarea>
</div>
</body></html>
"""


@app.route("/export")
def export_data():
    env = read_env()
    export_text = reporting.build_export_text(env)
    return render_template_string(EXPORT_PAGE, css=CSS, export_text=export_text)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
