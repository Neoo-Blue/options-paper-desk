"""python report.py -> 3-line daily summary. Pulls the latest state from the GitHub repo first
(the engine runs on GitHub Actions); pass --no-pull to use the local copy as-is."""
import os
import subprocess
import sys
from datetime import datetime
import stats
from market_calendar import ET

if "--no-pull" not in sys.argv:
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run([os.path.join(here, "sync_state.sh")], capture_output=True, text=True)
    if r.returncode:
        print(f"(warning: could not pull latest state from GitHub; showing local copy: {r.stderr.strip()[:150]})")
s = stats.load()
if not s:
    print("Balance: no state yet\nToday's P&L: n/a\nChanges: engine has not run")
    raise SystemExit
c = stats.compute(s)
today = datetime.now(ET).date().isoformat()
sod = s.get("daily", {}).get(today, {}).get("sod_equity")
tt = [t for t in s["trades"] if t["time"][:10] == today]
opened = [f"{t['symbol']} @{t['price']}" for t in tt if t["action"] == "BUY"]
closed = [f"{t['symbol']} {t['action'].lower()} ${t['pnl']:+.2f}" for t in tt if t["action"] in ("SELL", "EXPIRE")]
flags = (" | FULL STOP" if s["full_stop"] else "") + (" | DAILY CAP HIT" if (s.get("day") or {}).get("date") == today and s["day"].get("daily_cap_hit") else "")
print(f"Balance: equity ${c['equity']:.2f} (cash ${c['cash']:.2f}, {len(s['positions'])} open) | total P&L ${c['pnl']:+.2f} since ${s['start_cash']:.2f} start{flags}")
print(f"Today's P&L ({today} ET): " + (f"${c['equity']-sod:+.2f} vs start-of-day ${sod:.2f}" if sod is not None else "$0.00 (no trading session today yet)"))
print(f"Changes: opened {len(opened)} [{', '.join(opened) or '-'}]; closed {len(closed)} [{', '.join(closed) or '-'}] (quotes ~15 min delayed, Yahoo)")
gaps = stats.today_events(s, "missed_checks", today)
stales = stats.today_events(s, "stale_periods", today)
live = stats.stale_positions(s)
fmt = lambda iso: datetime.fromisoformat(iso).astimezone(ET).strftime("%H:%M")  # noqa: E731
parts = [f"missed checks {len(gaps)}" + (f" [{', '.join(f'{fmt(g['start'])}-{fmt(g['end'])} ET ({g['market_seconds']/60:.0f}m)' for g in gaps)}]" if gaps else "")]
parts.append(f"stale-quote periods {len(stales)}" + (f" [{', '.join(f'{x['symbol']} {fmt(x['start'])}-{fmt(x['end'])} ET ({x['seconds']/60:.0f}m)' for x in stales)}]" if stales else ""))
if live:
    parts.append("STALE NOW: " + ", ".join(f"{p['symbol']} last good quote {a/60:.0f}m ago" for p, a in live))
print("Data health: " + "; ".join(parts))
