"""Local read-only dashboard. Binds 0.0.0.0:8050 (override with OPD_PORT)."""
import html
import json
import os
from collections import deque
from datetime import datetime
from flask import Flask, Response, jsonify

import config
import stats
from market_calendar import ET, is_market_open

app = Flask(__name__)
E = html.escape


def fmt_t(iso):
    if not iso:
        return "-"
    d = datetime.fromisoformat(iso).astimezone(ET)
    return d.strftime("%Y-%m-%d %H:%M:%S ET")


def stale_badge(p, stale_ids):
    return f" <b class=bad>STALE {stale_ids[p['id']] / 60:.1f} min</b>" if p["id"] in stale_ids else ""


def pnl_cell(t):
    return f"${t['pnl']:+.2f}" if "pnl" in t else ""


@app.route("/api/state")
def api_state():
    s = stats.load()
    return jsonify({"state": s, "stats": stats.compute(s) if s else None})


@app.route("/")
def index():
    s = stats.load()
    now = datetime.now(ET)
    if not s:
        return Response("<h1>No state yet - engine has not run.</h1>", mimetype="text/html")
    c = stats.compute(s)
    st = s.get("status", {})
    day = s.get("day") or {}
    today = now.date().isoformat()
    cap_hit = day.get("date") == today and day.get("daily_cap_hit")
    risk = []
    risk.append(("FULL STOP (no new trades until reset)", "bad") if s["full_stop"] else ("Full stop: not triggered", "ok"))
    risk.append((f"Daily loss cap HIT: {day.get('daily_cap_reason','')}", "bad") if cap_hit else ("Daily loss cap: not hit", "ok"))
    if day.get("date") == today:
        risk.append((f"Start-of-day equity ${day['sod_equity']:.2f}; daily floor ${day['sod_equity']*(1-config.DAILY_LOSS_CAP_FRAC):.2f}; entries today {day['entries']}/{config.MAX_ENTRIES_PER_DAY}", ""))
    risk.append((f"Max per position: 40% of equity = ${config.MAX_POSITION_FRAC*c['equity']:.2f}; full-stop level ${config.FULL_STOP_EQUITY:.2f}", ""))
    stale_ids = {p["id"]: age for p, age in stats.stale_positions(s, now)}
    if stale_ids:
        risk.insert(0, (f"STALE QUOTE: open position's last good quote is {max(stale_ids.values())/60:.1f} min old "
                        f"(> {config.STALE_QUOTE_SECONDS//60} min). Exits/risk checks are working off an old mark.", "bad"))
    gaps = stats.today_events(s, "missed_checks", today)
    stales = stats.today_events(s, "stale_periods", today)
    gap_rows = "".join(f"<tr><td>{fmt_t(g['start'])}</td><td>{fmt_t(g['end'])}</td><td>{g['seconds']/60:.1f} min</td>"
                       f"<td>{g['market_seconds']/60:.1f} min</td><td>{E(', '.join(g.get('open_positions') or []) or 'none')}</td></tr>"
                       for g in reversed(gaps)) or "<tr><td colspan=5 class=ok>No missed checks today</td></tr>"
    stale_rows = "".join(f"<tr><td>{E(x['symbol'])}</td><td>{fmt_t(x['start'])}</td><td>{fmt_t(x['end'])}</td>"
                         f"<td>{x['seconds']/60:.1f} min</td><td>{x.get('failures', 0)}</td><td>{E(x.get('ended_by', ''))}</td></tr>"
                         for x in reversed(stales)) or "<tr><td colspan=6 class=ok>No stale-quote periods today</td></tr>"
    pos_rows = "".join(
        f"<tr><td>{E(p['symbol'])}</td><td>{p['kind']}</td><td>{p['strike']}</td><td>{p['expiry']}</td>"
        f"<td>{p['entry_price']:.2f}</td><td>${p['entry_cost']:.2f}</td>"
        f"<td>{(p.get('mark_bid') or 0):.2f}{'' if p.get('quote_ok') else ' <b class=bad>(last fetch failed)</b>'}"
        f"{stale_badge(p, stale_ids)}</td>"
        f"<td>${stats.liq(p, now.date()):.2f}</td><td>${stats.liq(p, now.date())-p['entry_cost']:+.2f}</td><td>{fmt_t(p.get('mark_time'))}</td></tr>"
        for p in s["positions"]) or "<tr><td colspan=10>No open positions</td></tr>"
    tr_rows = "".join(
        f"<tr><td>{fmt_t(t['time'])}</td><td>{t['action']}</td><td>{E(t['symbol'])}</td><td>{t['expiry']}</td>"
        f"<td>{t['price']}</td><td>${t['fees']:.2f}</td><td>${t['cash_flow']:+.2f}</td>"
        f"<td>{pnl_cell(t)}</td><td>{E(t.get('reason',''))}</td></tr>"
        for t in reversed(s["trades"][-200:])) or "<tr><td colspan=9>No trades yet</td></tr>"
    sigs = "".join(f"<li>{E(str(x))}</li>" for x in st.get("signals", [])) or "<li>none</li>"
    logtail = ""
    for lf in (config.LOG_TAIL_FILE, config.LOG_FILE):   # committed tail from the GitHub runner first
        try:
            with open(lf) as f:
                logtail = "".join(deque(f, 40))
            break
        except OSError:
            continue
    source = f"engine on {E(os.environ.get('OPD_RUNNER_NAME', 'GitHub Actions'))}" if os.environ.get("GITHUB_ACTIONS") else "local engine"
    try:
        with open(config.SYNC_STATUS_FILE) as f:
            sy = json.load(f)
        source = (f"GitHub repo {E(sy.get('repo', ''))} @ {E(sy.get('commit', '?'))} "
                  f"(last pull {fmt_t(sy.get('last_ok'))}{'' if sy.get('ok') else ', LAST PULL FAILED: ' + E(sy.get('error', ''))})")
    except (OSError, ValueError):
        pass
    wr = f"{c['win_rate']:.0f}% ({c['wins']}/{c['closed']})" if c["win_rate"] is not None else "n/a (no closed trades)"
    market = "OPEN" if is_market_open(now) else "CLOSED"
    page = f"""<!doctype html><html><head><meta charset=utf-8><meta http-equiv=refresh content=30>
<title>Options Paper Desk</title><style>
body{{font-family:system-ui,sans-serif;margin:20px;background:#0f1115;color:#e6e6e6}} table{{border-collapse:collapse;width:100%;margin-bottom:18px}}
td,th{{border:1px solid #333;padding:4px 8px;font-size:13px;text-align:left}} th{{background:#1b1f27}}
.cards{{display:flex;gap:12px;flex-wrap:wrap}} .card{{background:#1b1f27;padding:12px 16px;border-radius:8px;min-width:150px}}
.big{{font-size:22px;font-weight:600}} .ok{{color:#5fd38d}} .bad{{color:#ff6b6b}} .warn{{background:#5a4500;color:#ffd866;padding:8px 12px;border-radius:6px;font-weight:600}}
pre{{background:#000;padding:8px;font-size:11px;overflow:auto;max-height:300px}}</style></head><body>
<h2>Options Paper Desk <small>(PAPER TRADING SIMULATION - no real money, no broker)</small></h2>
<div class=warn>Quotes delayed ~15 min (Yahoo Finance). Fills: buy at ask, sell at bid, Robinhood pass-through fees applied.</div>
<p>Now {now:%Y-%m-%d %H:%M:%S ET} | Market {market} | Last engine tick {fmt_t(st.get('last_tick'))} | Last data update {fmt_t(st.get('last_data_update'))} | Page auto-refreshes every 30s<br>State source: {source}</p>
<div class=cards>
<div class=card>Equity (liquidation value)<div class=big>${c['equity']:.2f}</div></div>
<div class=card>Cash<div class=big>${c['cash']:.2f}</div></div>
<div class=card>Total P&L (after spreads &amp; fees)<div class="big {'ok' if c['pnl']>=0 else 'bad'}">${c['pnl']:+.2f} ({c['pnl_pct']:+.1f}%)</div></div>
<div class=card>Realized P&L<div class=big>${c['realized']:+.2f}</div></div>
<div class=card>Fees paid<div class=big>${c['fees']:.2f}</div></div>
<div class=card>Win rate<div class=big>{wr}</div></div>
<div class=card>Max drawdown<div class=big>${c['max_dd']:.2f} ({c['max_dd_pct']:.1f}%)</div></div>
</div>
<h3>Risk status</h3><ul>{''.join(f'<li class="{k}">{E(v)}</li>' for v, k in risk)}</ul>
<h3>Open positions</h3><table><tr><th>Contract</th><th>Type</th><th>Strike</th><th>Expiry</th><th>Entry (ask)</th><th>Entry cost incl. fees</th><th>Current bid</th><th>Net liq value</th><th>Unrealized</th><th>Quote time</th></tr>{pos_rows}</table>
<h3>Missed checks today (engine loop gaps &gt; {config.GAP_FACTOR}x the {config.POLL_SECONDS_MARKET}s poll during market hours)</h3>
<table><tr><th>Last check before gap</th><th>Next check</th><th>Gap</th><th>Market time missed</th><th>Open positions during gap</th></tr>{gap_rows}</table>
<h3>Stale-quote periods today (open position without a good quote &gt; {config.STALE_QUOTE_SECONDS//60} min)</h3>
<table><tr><th>Contract</th><th>Last good quote</th><th>Recovered / closed</th><th>Duration</th><th>Failed fetches</th><th>Ended by</th></tr>{stale_rows}</table>
<h3>Trade log</h3><table><tr><th>Time</th><th>Action</th><th>Contract</th><th>Expiry</th><th>Price</th><th>Fees</th><th>Cash flow</th><th>P&L</th><th>Reason</th></tr>{tr_rows}</table>
<h3>Latest signals</h3><ul>{sigs}</ul>
<h3>Engine log (tail)</h3><pre>{E(logtail)}</pre>
<p>Fee source: {' , '.join(E(u) for u in config.FEE_SOURCE_URLS)}</p>
</body></html>"""
    return Response(page, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("OPD_PORT", "8050")), debug=False, use_reloader=False)
