"""Dry run against REAL Yahoo chains. Uses a separate test state file (data/dryrun_state.json, persist=False);
never touches data/state.json. Prints what the parser sees and what the selection/risk logic decides."""
import logging
import sys
from datetime import datetime, date

import config
from data import YahooProvider
from engine import Engine, setup_logging
from market_calendar import ET
import os

setup_logging(os.path.join(config.BASE_DIR, "logs", "dryrun.log"))
logging.getLogger("opd").handlers = logging.getLogger("opd").handlers[:1]  # file only; we print our own summary
p = YahooProvider()
eng = Engine(p, state_path=os.path.join(config.BASE_DIR, "data", "dryrun_state.json"), persist=False)
wall = datetime.now(ET)
# Use the most recent real session (latest SPY 1-min bar) as 'now' so the signal sees that session's bars.
_b = p.intraday_bars("SPY")
now = _b.index[-1].to_pydatetime() if _b is not None and len(_b) else wall
# pretend it is 11:00 ET today for the trading-day bookkeeping only (signals use today's real 1-min bars)
sim = now.replace(hour=11, minute=0, second=0, microsecond=0)
eng.roll_day(sim)
print(f"Dry run at {wall:%Y-%m-%d %H:%M %Z}, evaluating session of {now:%Y-%m-%d %H:%M %Z}. {config.DATA_SOURCE_LABEL}. Test state only, nothing recorded.")
tot = {"rows": 0, "bid>0": 0, "ask>0": 0, "vol>0": 0, "oi>0": 0}
for t in config.UNIVERSE:
    try:
        sg = eng.signal(t, now)
        exps = [e for e in p.expiries(t) if 0 <= (date.fromisoformat(e) - now.date()).days <= config.MAX_DTE]
        rows = [r for e in exps for r in p.chain(t, e)]
    except Exception as e:
        print(f"{t}: ERROR {e}")
        continue
    c = {"rows": len(rows), "bid>0": sum(1 for r in rows if (r['bid'] or 0) > 0),
         "ask>0": sum(1 for r in rows if (r['ask'] or 0) > 0), "vol>0": sum(1 for r in rows if (r['volume'] or 0) > 0),
         "oi>0": sum(1 for r in rows if (r['oi'] or 0) > 0)}
    for k in tot:
        tot[k] += c[k]
    side = (sg or {}).get("side") or ("call" if (sg and sg["move_pct"] >= 0) else "put")
    print(f"\n{t}: expiries<=7DTE {exps}  contracts {c}")
    if sg:
        print(f"  today's real 1m bars: open {sg['open']} last {sg['last']} ({sg['move_pct']:+.2f}%, threshold "
              f"{sg['threshold_pct']:.2f}%) VWAP {sg['vwap']} EMA20 {sg['ema']} -> signal {sg['side']} (last bar {sg['bar_time']})")
    spot = sg["last"] if sg else None
    if spot:
        cands, rej = eng.candidate_contracts(t, side, spot, now.date())
        print(f"  selection ({side}, spot {spot}): eligible {len(cands)}, rejected {len(rej)}")
        for s, why in rej[:4]:
            print(f"    skip {s}: {why}")
        for r in cands[:3]:
            pos, why = eng.open_position(r, sim, "dryrun")
            print(f"    candidate {r['symbol']} bid {r['bid']} ask {r['ask']} -> {why}")
            if pos:
                eng.s["positions"].clear(); eng.s["cash"] = config.STARTING_CASH
        # info only: OTM near-money contracts and their LAST trade price, to show what a ~$20 cap can afford
        near = sorted([r for e in exps[:1] for r in p.chain(t, e) if r['kind'] == side and r['strike'] and
                       ((r['strike'] > spot) if side == 'call' else (r['strike'] < spot))],
                      key=lambda r: abs(r['strike'] - spot))[:3]
        for r in near:
            print(f"    info: {r['symbol']} strike {r['strike']} bid {r['bid']} ask {r['ask']} last {r['last']} "
                  f"vol {r['volume']} OI {r['oi']}")
# Fill/fee/risk path on whatever REAL two-sided quotes Yahoo returned after hours (test engine, not persisted)
print("\nReal rows with bid>0 and ask>0 right now (after-hours stale quotes), run through filters + fill/fee/cap logic:")
for t in config.UNIVERSE:
    for e in p.expiries(t):
        if (date.fromisoformat(e) - now.date()).days > config.MAX_DTE:
            continue
        for r in p.chain(t, e):
            if (r["bid"] or 0) > 0 and (r["ask"] or 0) > 0:
                why = eng.liquidity_reject(r)
                eng.s["positions"].clear(); eng.s["cash"] = config.STARTING_CASH; eng.s["day"]["entries"] = 0
                pos, res = (None, "not attempted (filter)") if why else eng.open_position(r, sim, "dryrun")
                cost = f" cost ${pos['entry_cost']:.2f} (ask {r['ask']}*100 + fees ${pos['entry_fees']['total']:.2f})" if pos else ""
                import fees as _f
                c = round(r["ask"] * 100 + _f.buy_fees(1)["total"], 2)
                capok, capwhy = eng.can_open(c, sim)
                print(f"  {r['symbol']} bid {r['bid']} ask {r['ask']} vol {r['volume']} OI {r['oi']} | filter: {why or 'pass'} "
                      f"| fill: {res}{cost} | risk check on ${c:.2f}: {capwhy}")
eng.s["positions"].clear(); eng.s["cash"] = config.STARTING_CASH
print(f"\nTOTAL parsed contracts (<=7DTE): {tot}")
