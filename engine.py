"""Options paper-trading engine (PURE SIMULATION - no broker, no real orders, no real money).

Usage:
  python engine.py            # run forever (poll during NYSE hours, idle otherwise)
  python engine.py --once     # one tick then exit
  python engine.py --reset    # reset state to $50 (backs up old state). Only way to clear a full stop.
"""
import argparse
import copy
import fcntl
import json
import logging
import os
import shutil
import sys
import time as _time
import uuid
from datetime import datetime, date, timedelta

import config
import fees
import market_calendar as mc
from market_calendar import ET

log = logging.getLogger("opd")
PROC_ID = uuid.uuid4().hex[:12]   # identifies this process run (pids repeat across machines)
def fmt_et(iso):
    return datetime.fromisoformat(iso).astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET") if iso else "-"


VALID = lambda x: x is not None and x > 0  # noqa: E731


def setup_logging(path=config.LOG_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    h = logging.FileHandler(path)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S %Z"))
    log.handlers = [h, logging.StreamHandler(sys.stdout)]
    log.setLevel(logging.INFO)


# ------------------------------------------------------------------ state

def new_state():
    return {
        "version": 1, "created": datetime.now(ET).isoformat(),
        "start_cash": config.STARTING_CASH, "cash": config.STARTING_CASH,
        "positions": [], "trades": [], "equity_history": [], "daily": {},
        "day": None, "full_stop": False, "full_stop_reason": None,
        "last_exit": {}, "next_id": 1, "status": {}, "data_label": config.DATA_SOURCE_LABEL,
    }


def load_state(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return new_state()


def save_state(state, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ------------------------------------------------------------------ engine

class Engine:
    def __init__(self, provider, state_path=config.STATE_FILE, persist=True):
        self.p = provider
        self.path = state_path
        self.persist = persist
        self.s = load_state(state_path)

    def save(self):
        if self.persist:
            save_state(self.s, self.path)

    # ---------- valuation
    def position_liq_value(self, pos, on: date):
        """Net liquidation value at the last real bid (minus sell fees). None if never had a bid."""
        b = pos.get("mark_bid")
        if not VALID(b):
            return 0.0  # a zero bid is worth 0 if sold now; we never invent a higher mark
        gross = b * config.CONTRACT_MULTIPLIER * pos["qty"]
        return round(gross - fees.sell_fees(pos["qty"], b, on)["total"], 2)

    def equity(self, on: date = None):
        on = on or datetime.now(ET).date()
        return round(self.s["cash"] + sum(self.position_liq_value(p, on) for p in self.s["positions"]), 2)

    # ---------- day roll + risk
    def roll_day(self, now):
        d = now.date().isoformat()
        if (self.s.get("day") or {}).get("date") != d and mc.is_trading_day(now.date()) and now.time() >= config.MARKET_OPEN:
            eq = self.equity(now.date())
            self.s["day"] = {"date": d, "sod_equity": eq, "daily_cap_hit": False, "entries": 0}
            self.s["daily"].setdefault(d, {})["sod_equity"] = eq
            log.info("New trading day %s, start-of-day equity $%.2f", d, eq)

    def update_risk(self, now):
        eq = self.equity(now.date())
        if not self.s["full_stop"] and eq <= config.FULL_STOP_EQUITY:
            self.s["full_stop"] = True
            self.s["full_stop_reason"] = f"equity ${eq:.2f} <= ${config.FULL_STOP_EQUITY:.2f} (50% of start) at {now.isoformat()}"
            log.warning("FULL STOP triggered: %s", self.s["full_stop_reason"])
        day = self.s.get("day")
        if day and day["date"] == now.date().isoformat() and not day["daily_cap_hit"]:
            floor = day["sod_equity"] * (1 - config.DAILY_LOSS_CAP_FRAC)
            if eq <= floor:
                day["daily_cap_hit"] = True
                day["daily_cap_reason"] = f"equity ${eq:.2f} <= ${floor:.2f} (SOD ${day['sod_equity']:.2f} -15%) at {now.isoformat()}"
                log.warning("DAILY LOSS CAP hit: %s", day["daily_cap_reason"])
        return eq

    def can_open(self, total_cost, now):
        if self.s["full_stop"]:
            return False, "full stop active"
        day = self.s.get("day")
        if not day or day["date"] != now.date().isoformat():
            return False, "no trading day record"
        if day["daily_cap_hit"]:
            return False, "daily loss cap hit"
        if day["entries"] >= config.MAX_ENTRIES_PER_DAY:
            return False, "max entries per day reached"
        if len(self.s["positions"]) >= config.MAX_OPEN_POSITIONS:
            return False, "max open positions"
        if total_cost > self.s["cash"] + 1e-9:
            return False, f"cost ${total_cost:.2f} > cash ${self.s['cash']:.2f}"
        cap = config.MAX_POSITION_FRAC * self.equity(now.date())
        if total_cost > cap + 1e-9:
            return False, f"cost ${total_cost:.2f} > 40% cap ${cap:.2f}"
        return True, "ok"

    # ---------- fills
    def _trade_id(self):
        i = self.s["next_id"]
        self.s["next_id"] += 1
        return i

    def open_position(self, q, now, reason):
        """Buy 1 contract at the ASK. q is a real quote row."""
        if not (VALID(q.get("ask")) and VALID(q.get("bid"))):
            return None, "no valid bid/ask"
        qty = config.CONTRACTS_PER_TRADE
        f = fees.buy_fees(qty)
        premium = round(q["ask"] * config.CONTRACT_MULTIPLIER * qty, 2)
        cost = round(premium + f["total"], 2)
        ok, why = self.can_open(cost, now)
        if not ok:
            return None, why
        tid = self._trade_id()
        pos = {"id": tid, "symbol": q["symbol"], "underlying": q["underlying"], "kind": q["kind"],
               "strike": q["strike"], "expiry": q["expiry"], "qty": qty, "entry_price": q["ask"],
               "entry_fees": f, "entry_cost": cost, "entry_time": now.isoformat(), "entry_reason": reason,
               "mark_bid": q["bid"], "mark_ask": q["ask"], "mark_time": now.isoformat(), "quote_ok": True}
        self.s["cash"] = round(self.s["cash"] - cost, 2)
        self.s["positions"].append(pos)
        self.s["trades"].append({"id": tid, "time": now.isoformat(), "action": "BUY", "symbol": q["symbol"],
                                 "underlying": q["underlying"], "kind": q["kind"], "strike": q["strike"],
                                 "expiry": q["expiry"], "qty": qty, "price": q["ask"], "fees": f["total"],
                                 "fee_detail": f, "cash_flow": -cost, "reason": reason,
                                 "quote": {"bid": q["bid"], "ask": q["ask"], "volume": q.get("volume"), "oi": q.get("oi")}})
        if self.s.get("day"):
            self.s["day"]["entries"] += 1
        log.info("PAPER BUY %s @ ask %.2f (bid %.2f) fees $%.2f total cost $%.2f | %s | cash now $%.2f [%s]",
                 q["symbol"], q["ask"], q["bid"], f["total"], cost, reason, self.s["cash"], config.DATA_SOURCE_LABEL)
        self.save()
        return pos, "filled"

    def close_position(self, pos, bid, now, reason):
        """Sell at the BID. Refuses without a real positive bid."""
        if not VALID(bid):
            log.warning("no quote: cannot close %s (%s) - bid missing/zero, holding", pos["symbol"], reason)
            return None
        f = fees.sell_fees(pos["qty"], bid, now.date())
        proceeds = round(bid * config.CONTRACT_MULTIPLIER * pos["qty"] - f["total"], 2)
        pnl = round(proceeds - pos["entry_cost"], 2)
        return self._finish(pos, now, "SELL", bid, f, proceeds, pnl, reason)

    def settle_expired(self, pos, underlying_close, now):
        k = pos["strike"]
        intrinsic = max(0.0, underlying_close - k) if pos["kind"] == "call" else max(0.0, k - underlying_close)
        intrinsic = round(intrinsic, 4)
        proceeds = round(intrinsic * config.CONTRACT_MULTIPLIER * pos["qty"] - config.FEES["expiry_settlement_fee"], 2)
        pnl = round(proceeds - pos["entry_cost"], 2)
        f = {"total": config.FEES["expiry_settlement_fee"]}
        return self._finish(pos, now, "EXPIRE", intrinsic, f, proceeds, pnl,
                            f"expired; underlying close {underlying_close:.2f} vs strike {k}")

    def _finish(self, pos, now, action, price, f, proceeds, pnl, reason):
        self._close_stale_period(pos, now, f"position {action.lower()} ({reason})")
        self.s["cash"] = round(self.s["cash"] + proceeds, 2)
        self.s["positions"] = [p for p in self.s["positions"] if p["id"] != pos["id"]]
        self.s["last_exit"][pos["underlying"]] = now.isoformat()
        rec = {"id": pos["id"], "time": now.isoformat(), "action": action, "symbol": pos["symbol"],
               "underlying": pos["underlying"], "kind": pos["kind"], "strike": pos["strike"], "expiry": pos["expiry"],
               "qty": pos["qty"], "price": price, "fees": f["total"], "fee_detail": f, "cash_flow": proceeds,
               "pnl": pnl, "entry_cost": pos["entry_cost"], "entry_time": pos["entry_time"], "reason": reason}
        self.s["trades"].append(rec)
        log.info("PAPER %s %s @ %.4f fees $%.2f proceeds $%.2f P&L $%+.2f | %s | cash now $%.2f",
                 action, pos["symbol"], price, f["total"], proceeds, pnl, reason, self.s["cash"])
        self.save()
        return rec

    # ---------- strategy
    def signal(self, ticker, now):
        bars = self.p.intraday_bars(ticker)
        if bars is None or len(bars) < config.EMA_BARS:
            return None
        bars = bars[bars.index.date == now.date()]
        if len(bars) < config.EMA_BARS:
            return None
        o = float(bars["Open"].iloc[0])
        last = float(bars["Close"].iloc[-1])
        tp = (bars["High"] + bars["Low"] + bars["Close"]) / 3
        vol = bars["Volume"].astype(float)
        if vol.sum() <= 0 or o <= 0:
            return None
        vwap = float((tp * vol).cumsum().iloc[-1] / vol.cumsum().iloc[-1])
        ema = float(bars["Close"].ewm(span=config.EMA_BARS, adjust=False).mean().iloc[-1])
        move = last / o - 1
        th = config.MOVE_THRESHOLD_ETF if ticker in config.ETFS else config.MOVE_THRESHOLD_STOCK
        info = {"ticker": ticker, "open": round(o, 4), "last": round(last, 4), "vwap": round(vwap, 4),
                "ema": round(ema, 4), "move_pct": round(move * 100, 3), "threshold_pct": th * 100,
                "bar_time": bars.index[-1].isoformat()}
        if move >= th and last > vwap and last > ema:
            info["side"] = "call"
        elif move <= -th and last < vwap and last < ema:
            info["side"] = "put"
        else:
            info["side"] = None
        info["strength"] = abs(move) / th
        return info

    def candidate_contracts(self, ticker, side, spot, today: date, report=None):
        """Real quotes only. Returns (eligible list nearest-the-money first, rejected list)."""
        rejected, out = [], []
        exps = []
        for e in self.p.expiries(ticker):
            dte = (date.fromisoformat(e) - today).days
            if 0 <= dte <= config.MAX_DTE:
                exps.append(e)
        for e in sorted(exps):
            rows = [r for r in self.p.chain(ticker, e) if r["kind"] == side and r["strike"] is not None
                    and abs(r["strike"] / spot - 1) <= config.MAX_OTM_PCT]
            if side == "call":
                otm = sorted([r for r in rows if r["strike"] > spot], key=lambda r: r["strike"])
            else:
                otm = sorted([r for r in rows if r["strike"] < spot], key=lambda r: -r["strike"])
            for r in otm[:config.MAX_OTM_STRIKES]:
                why = self.liquidity_reject(r)
                if why:
                    rejected.append((r["symbol"], why))
                else:
                    out.append(r)  # ordered: nearest expiry first, then nearest-the-money
        return out, rejected

    @staticmethod
    def liquidity_reject(r):
        b, a = r.get("bid"), r.get("ask")
        if not VALID(b) or not VALID(a):
            return f"no quote (bid={b}, ask={a})"
        if a < config.MIN_ASK:
            return f"ask {a} < min {config.MIN_ASK}"
        if (r.get("volume") or 0) < config.MIN_VOLUME:
            return f"volume {r.get('volume')} < {config.MIN_VOLUME}"
        if (r.get("oi") or 0) < config.MIN_OPEN_INTEREST:
            return f"OI {r.get('oi')} < {config.MIN_OPEN_INTEREST}"
        if (a - b) / a > config.MAX_SPREAD_FRAC_OF_ASK:
            return f"spread {(a - b) / a:.0%} > {config.MAX_SPREAD_FRAC_OF_ASK:.0%}"
        return None

    def try_entry(self, now):
        sigs = []
        for t in config.UNIVERSE:
            le = self.s["last_exit"].get(t)
            if le and now - datetime.fromisoformat(le) < timedelta(minutes=config.REENTRY_COOLDOWN_MIN):
                continue
            try:
                sg = self.signal(t, now)
            except Exception as e:  # data hiccup for one ticker should not kill the tick
                log.warning("signal error %s: %s", t, e)
                continue
            if sg and sg["side"]:
                sigs.append(sg)
        self.s["status"]["signals"] = sigs
        for sg in sorted(sigs, key=lambda x: -x["strength"]):
            try:
                cands, rej = self.candidate_contracts(sg["ticker"], sg["side"], sg["last"], now.date())
            except Exception as e:
                log.warning("chain error %s: %s", sg["ticker"], e)
                continue
            for r in rej:
                log.info("skip %s: %s", *r)
            for q in cands:
                reason = (f"{sg['ticker']} {sg['move_pct']:+.2f}% from open, last {sg['last']} vs VWAP {sg['vwap']} "
                          f"EMA{config.EMA_BARS} {sg['ema']} (bar {sg['bar_time']})")
                pos, why = self.open_position(q, now, reason)
                if pos:
                    return pos
                log.info("skip %s ask %.2f: %s", q["symbol"], q["ask"], why)
        return None

    # ---------- position management
    def refresh_marks(self, now):
        """Fetch ONLY each open contract's own expiry chain (fresh, short timeouts). Returns True if all quotes good.
        On failure the last REAL mark is kept and flagged; no price is ever invented."""
        all_ok = True
        for pos in self.s["positions"]:
            err = None
            try:
                q = self.p.option_quote(pos["underlying"], pos["expiry"], pos["symbol"], fresh=True)
            except Exception as e:
                q, err = None, e
            if q and VALID(q.get("bid")) and VALID(q.get("ask")):
                self._close_stale_period(pos, now, "quote recovered")
                pos.update(mark_bid=q["bid"], mark_ask=q["ask"], mark_time=now.isoformat(), quote_ok=True,
                           fetch_failures=0)
            else:
                all_ok = False
                pos["quote_ok"] = False
                pos["fetch_failures"] = pos.get("fetch_failures", 0) + 1
                pos.setdefault("stale_since", pos.get("mark_time"))
                if pos.get("stale_since") is None:
                    pos["stale_since"] = pos.get("mark_time")
                why = f"fetch error: {err}" if err else f"bid={q and q.get('bid')} ask={q and q.get('ask')}"
                log.warning("no quote for open position %s (%s; failure #%d); keeping last real mark from %s",
                            pos["symbol"], why, pos["fetch_failures"], pos.get("mark_time"))
        return all_ok

    def quote_age_s(self, pos, now):
        mt = pos.get("mark_time")
        return (now - datetime.fromisoformat(mt)).total_seconds() if mt else None

    def stale_positions(self, now):
        """Open positions whose last GOOD quote is older than STALE_QUOTE_SECONDS (market hours only)."""
        if not mc.is_market_open(now):
            return []
        return [p for p in self.s["positions"] if (self.quote_age_s(p, now) or 0) > config.STALE_QUOTE_SECONDS]

    def _close_stale_period(self, pos, now, how):
        since = pos.pop("stale_since", None)
        if not since:
            return
        secs = (now - datetime.fromisoformat(since)).total_seconds()
        if secs > config.STALE_QUOTE_SECONDS:
            rec = {"symbol": pos["symbol"], "start": since, "end": now.isoformat(), "seconds": round(secs),
                   "failures": pos.get("fetch_failures", 0), "ended_by": how}
            self.s.setdefault("stale_periods", []).append(rec)
            del self.s["stale_periods"][:-config.GAP_HISTORY_MAX]
            log.warning("STALE QUOTE PERIOD %s: no good quote from %s to %s (%.0fs, %d failed fetches) - %s",
                        pos["symbol"], fmt_et(since), fmt_et(now.isoformat()), secs, pos.get("fetch_failures", 0), how)

    # ---------- loop health
    def note_loop(self, now, mono=None):
        """Call once per loop iteration BEFORE tick. Detects missed checks during market hours
        (also across process restarts, since the last iteration time is persisted)."""
        st = self.s["status"]
        prev = st.get("last_loop_wall")
        gap = None
        if prev:
            prev_dt = datetime.fromisoformat(prev)
            elapsed = (now - prev_dt).total_seconds()
            requested = st.get("last_requested_sleep") or config.POLL_SECONDS_MARKET
            threshold = max(config.GAP_FACTOR * config.POLL_SECONDS_MARKET, requested + config.POLL_SECONDS_MARKET)
            mkt = mc.market_seconds_between(prev_dt, now)
            if elapsed > threshold and mkt > config.POLL_SECONDS_MARKET:
                mono_el = None
                if mono is not None and st.get("last_loop_mono") is not None and st.get("pid") == os.getpid() \
                        and st.get("proc_id") == PROC_ID:
                    mono_el = round(mono - st["last_loop_mono"], 1)
                gap = {"start": prev, "end": now.isoformat(), "seconds": round(elapsed), "market_seconds": round(mkt),
                       "expected_s": requested, "monotonic_s": mono_el,
                       "open_positions": [p["symbol"] for p in self.s["positions"]]}
                self.s.setdefault("missed_checks", []).append(gap)
                del self.s["missed_checks"][:-config.GAP_HISTORY_MAX]
                log.warning("MISSED CHECKS: no engine iteration from %s to %s (%.0fs wall, %.0fs of market time, "
                            "expected <= %.0fs; monotonic %s s; open positions: %s). Box/process likely paused.",
                            fmt_et(prev), fmt_et(now.isoformat()), elapsed, mkt, threshold, mono_el,
                            gap["open_positions"] or "none")
        st["last_loop_wall"] = now.isoformat()
        st["last_loop_mono"] = mono
        st["pid"] = os.getpid()
        st["proc_id"] = PROC_ID
        return gap

    def next_delay(self, now):
        """Seconds until the next iteration: fast backoff retries while an open position lacks a fresh quote."""
        st = self.s["status"]
        if mc.is_market_open(now):
            needs_retry = any(not p.get("quote_ok") for p in self.s["positions"])
            if needs_retry:
                n = st.get("quote_retry_n", 0)
                st["quote_retry_n"] = n + 1
                d = config.QUOTE_RETRY_BACKOFF_S[min(n, len(config.QUOTE_RETRY_BACKOFF_S) - 1)]
            else:
                st["quote_retry_n"] = 0
                d = config.POLL_SECONDS_MARKET
        else:
            st["quote_retry_n"] = 0
            d = max(5, min(config.POLL_SECONDS_IDLE, (mc.next_open(now) - now).total_seconds()))
        st["last_requested_sleep"] = d
        return d

    def manage_positions(self, now, force_reason=None):
        for pos in list(self.s["positions"]):
            exp = date.fromisoformat(pos["expiry"])
            reason = force_reason
            if not reason and pos.get("quote_ok"):
                liq = self.position_liq_value(pos, now.date())
                ret = (liq - pos["entry_cost"]) / pos["entry_cost"]
                if ret >= config.TAKE_PROFIT_FRAC:
                    reason = f"take profit {ret:+.0%}"
                elif ret <= -config.STOP_LOSS_FRAC:
                    reason = f"stop loss {ret:+.0%}"
            if not reason and mc.time_stop_reached(now):
                held = mc.trading_days_between(datetime.fromisoformat(pos["entry_time"]).date(), now.date())
                if exp <= now.date():
                    reason = "time stop (expiry day)"
                elif held >= config.MAX_HOLD_TRADING_DAYS - 1 and held > 0:
                    reason = f"time stop (max hold {config.MAX_HOLD_TRADING_DAYS} trading days)"
            if reason:
                if pos.get("quote_ok"):
                    self.close_position(pos, pos["mark_bid"], now, reason)
                else:
                    log.warning("no quote: wanted to exit %s (%s) but no valid bid; holding", pos["symbol"], reason)

    def settle_expirations(self, now):
        for pos in list(self.s["positions"]):
            exp = date.fromisoformat(pos["expiry"])
            after_close = exp < now.date() or (exp == now.date() and now.time() >= mc.close_time(exp))
            if not after_close:
                continue
            try:
                px = self.p.close_on(pos["underlying"], exp)
            except Exception as e:
                px = None
                log.warning("close fetch error %s: %s", pos["underlying"], e)
            if px is None:
                log.warning("expired %s but no official close for %s on %s yet; will retry", pos["symbol"], pos["underlying"], exp)
                continue
            self.settle_expired(pos, px, now)

    # ---------- tick
    def tick(self, now=None):
        now = (now or datetime.now(ET)).astimezone(ET)
        self.p.new_tick()
        st = self.s["status"]
        st["last_tick"] = now.isoformat()
        st["market_open"] = mc.is_market_open(now)
        self.settle_expirations(now)
        if st["market_open"]:
            self.refresh_marks(now)
            self.roll_day(now)
            self.update_risk(now)
            day = self.s.get("day") or {}
            if self.s["full_stop"]:
                self.manage_positions(now, force_reason="full stop - liquidating")
            elif day.get("daily_cap_hit"):
                self.manage_positions(now, force_reason="daily loss cap - liquidating")
            else:
                self.manage_positions(now)
            st["entry_block"] = None  # recomputed below; never show a stale reason from a prior day
            if not self.s["positions"] and mc.entry_window_open(now):
                ok, why = self.can_open(0.0, now)
                if ok:
                    self.try_entry(now)
                else:
                    st["entry_block"] = why
            self.update_risk(now)
        eq = self.equity(now.date())
        if self.p.last_success:
            st["last_data_update"] = self.p.last_success.isoformat()
        d = now.date().isoformat()
        if d in self.s["daily"]:
            self.s["daily"][d]["last_equity"] = eq
        h = self.s["equity_history"]
        if st["market_open"] or not h or h[-1]["equity"] != eq or h[-1]["cash"] != self.s["cash"]:
            h.append({"t": now.isoformat(), "equity": eq, "cash": self.s["cash"]})
            del h[:-config.EQUITY_HISTORY_MAX]
        self.save()
        return eq


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    setup_logging()
    if a.reset:
        if os.path.exists(config.STATE_FILE):
            bk = config.STATE_FILE + datetime.now(ET).strftime(".bak-%Y%m%d-%H%M%S")
            shutil.copy(config.STATE_FILE, bk)
            log.info("state backed up to %s", bk)
        save_state(new_state(), config.STATE_FILE)
        log.info("state reset to $%.2f", config.STARTING_CASH)
        return
    lockf = open(config.LOCK_FILE, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("another engine instance holds %s; exiting", config.LOCK_FILE)
        sys.exit(3)
    from data import YahooProvider
    eng = Engine(YahooProvider())
    log.info("engine start (PAPER ONLY). %s. cash $%.2f, positions %d, full_stop=%s",
             config.DATA_SOURCE_LABEL, eng.s["cash"], len(eng.s["positions"]), eng.s["full_stop"])
    run_loop(eng, once=a.once)


def run_loop(eng, once=False, deadline=None, on_iter=None, stop_fn=None):
    """The engine loop. deadline (aware datetime): return once the wall clock passes it (used by the GitHub
    Actions runner, which must hand off before the 6 h job limit). on_iter(eng, now): optional hook called after
    every iteration (the runner uses it to commit and push state). stop_fn(eng, now) -> True ends the loop early."""
    while True:
        now = datetime.now(ET)
        try:
            eng.note_loop(now, _time.monotonic())
            eq = eng.tick(now)
            if mc.is_market_open(now):
                stale = eng.stale_positions(datetime.now(ET))
                log.info("tick equity $%.2f cash $%.2f open=%d%s [%s]", eq, eng.s["cash"], len(eng.s["positions"]),
                         f" STALE QUOTE: {[p['symbol'] for p in stale]}" if stale else "", config.DATA_SOURCE_LABEL)
        except Exception:
            log.exception("tick failed; will retry")
        if on_iter:
            try:
                on_iter(eng, datetime.now(ET))
            except Exception:
                log.exception("loop hook failed")
        if once or (stop_fn and stop_fn(eng, datetime.now(ET))):
            break
        try:
            delay = eng.next_delay(datetime.now(ET))
            eng.save()
        except Exception:
            log.exception("scheduling failed")
            delay = config.QUOTE_RETRY_BACKOFF_S[0]
        if eng.s["status"].get("quote_retry_n"):
            log.info("open position quote not fresh; fast retry in %ss (attempt %d)", delay, eng.s["status"]["quote_retry_n"])
        if deadline is not None:
            left = (deadline - datetime.now(ET)).total_seconds()
            if left <= 0:
                break
            delay = min(delay, left)
        robust_sleep(delay)


def robust_sleep(seconds, now_fn=lambda: datetime.now(ET), mono_fn=_time.monotonic, sleep_fn=_time.sleep,
                 chunk=None):
    """Sleep in short chunks; stop as soon as EITHER the wall-clock or the monotonic deadline passes.
    If the box is paused/frozen and the wall clock jumps ahead, we resume within one chunk."""
    chunk = chunk or config.SLEEP_CHUNK_S
    wall_deadline = now_fn() + timedelta(seconds=seconds)
    mono_deadline = mono_fn() + seconds
    n = 0
    while True:
        rem_wall = (wall_deadline - now_fn()).total_seconds()
        rem_mono = mono_deadline - mono_fn()
        rem = min(rem_wall, rem_mono)
        if rem <= 0:
            return n
        sleep_fn(min(chunk, rem))
        n += 1


if __name__ == "__main__":
    main()
