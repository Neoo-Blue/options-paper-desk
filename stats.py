"""Shared read-only stats for dashboard and report."""
import json
import os
from datetime import datetime
import config
import fees
from market_calendar import ET


def load():
    if not os.path.exists(config.STATE_FILE):
        return None
    with open(config.STATE_FILE) as f:
        return json.load(f)


def liq(pos, on):
    b = pos.get("mark_bid")
    if not b or b <= 0:
        return 0.0
    return round(b * 100 * pos["qty"] - fees.sell_fees(pos["qty"], b, on)["total"], 2)


def compute(s):
    today = datetime.now(ET).date()
    eq = round(s["cash"] + sum(liq(p, today) for p in s["positions"]), 2)
    closes = [t for t in s["trades"] if t["action"] in ("SELL", "EXPIRE")]
    wins = sum(1 for t in closes if t["pnl"] > 0)
    peak, mdd, mdd_pct = None, 0.0, 0.0
    for h in s["equity_history"] + [{"equity": eq}]:
        v = h["equity"]
        peak = v if peak is None else max(peak, v)
        if peak - v > mdd:
            mdd, mdd_pct = peak - v, (peak - v) / peak if peak else 0
    total_fees = round(sum(t.get("fees", 0) for t in s["trades"]), 2)
    return {
        "equity": eq, "cash": s["cash"], "pnl": round(eq - s["start_cash"], 2),
        "pnl_pct": (eq / s["start_cash"] - 1) * 100, "closed": len(closes), "wins": wins,
        "win_rate": (wins / len(closes) * 100) if closes else None,
        "realized": round(sum(t["pnl"] for t in closes), 2), "fees": total_fees,
        "max_dd": round(mdd, 2), "max_dd_pct": mdd_pct * 100,
    }


def stale_positions(s, now=None):
    """Open positions whose last good quote is older than STALE_QUOTE_SECONDS during market hours."""
    from market_calendar import is_market_open
    now = now or datetime.now(ET)
    if not is_market_open(now):
        return []
    out = []
    for p in s["positions"]:
        mt = p.get("mark_time")
        age = (now - datetime.fromisoformat(mt)).total_seconds() if mt else None
        if age is not None and age > config.STALE_QUOTE_SECONDS:
            out.append((p, age))
    return out


def today_events(s, key, day_iso):
    return [g for g in s.get(key, []) if g["start"][:10] == day_iso or g["end"][:10] == day_iso]
