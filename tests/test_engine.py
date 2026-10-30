"""Unit checks with a fake provider and a throwaway state file (never touches data/state.json)."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import fees  # noqa: E402
import market_calendar as mc  # noqa: E402
from engine import Engine  # noqa: E402
from market_calendar import ET  # noqa: E402

DAY = date(2026, 10, 28)  # a Wednesday, normal trading day


def at(h, m, d=DAY):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ET)


def q(symbol="SPY261028C00775000", kind="call", strike=775.0, bid=0.10, ask=0.12, vol=500, oi=1000,
      und="SPY", exp="2026-10-28"):
    return {"symbol": symbol, "kind": kind, "underlying": und, "expiry": exp, "strike": strike,
            "bid": bid, "ask": ask, "last": None, "volume": vol, "oi": oi}


class FakeProvider:
    def __init__(self):
        self.quotes, self.closes, self.bars, self.last_success = {}, {}, {}, None

    def new_tick(self):
        pass

    def intraday_bars(self, t):
        return self.bars.get(t)

    def expiries(self, t):
        return sorted({r["expiry"] for r in self.quotes.values() if r["underlying"] == t})

    def chain(self, t, e):
        return [r for r in self.quotes.values() if r["underlying"] == t and r["expiry"] == e]

    fail_next = 0
    calls = 0

    def option_quote(self, t, e, s, fresh=False):
        self.calls += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TimeoutError("simulated Yahoo timeout")
        return self.quotes.get(s)

    def close_on(self, t, d):
        return self.closes.get((t, d))


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        os.unlink(self.tmp)
        self.p = FakeProvider()
        self.e = Engine(self.p, state_path=self.tmp)
        self.e.roll_day(at(10, 5))

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.unlink(self.tmp)

    def test_buy_at_ask_with_fees(self):
        pos, why = self.e.open_position(q(bid=0.10, ask=0.12), at(10, 5), "t")
        self.assertEqual(why, "filled")
        self.assertEqual(pos["entry_price"], 0.12)
        # $12.00 premium + $0.04 ORF/OCC + $0 CAT (0.0003 rounds down) = 12.04
        self.assertAlmostEqual(pos["entry_cost"], 12.04)
        self.assertAlmostEqual(self.e.s["cash"], 50 - 12.04)

    def test_sell_at_bid_with_fees(self):
        pos, _ = self.e.open_position(q(bid=0.10, ask=0.12), at(10, 5), "t")
        rec = self.e.close_position(pos, 0.18, at(11, 0), "t")
        self.assertEqual(rec["price"], 0.18)
        # sell 0.18: $18 gross - ORF/OCC 0.04 - SEC ceil(18*20.6e-6=0.00037)=0.01 - TAF 0 (paused Oct-Dec 2026)
        self.assertAlmostEqual(rec["fees"], 0.05)
        self.assertAlmostEqual(rec["cash_flow"], 17.95)
        self.assertAlmostEqual(rec["pnl"], 17.95 - 12.04)
        self.assertAlmostEqual(self.e.s["cash"], 50 - 12.04 + 17.95)

    def test_taf_after_pause_and_rounding(self):
        f = fees.sell_fees(1, 0.18, date(2027, 1, 5))
        self.assertEqual(f["taf"], 0.0)          # 0.00329 < $0.01 -> rounds down to 0
        f = fees.sell_fees(10, 0.18, date(2027, 1, 5))
        self.assertEqual(f["taf"], 0.03)         # 0.0329 -> nearest penny
        f = fees.sell_fees(10, 0.18, date(2026, 11, 5))
        self.assertEqual(f["taf"], 0.0)          # paused
        self.assertEqual(fees.buy_fees(100)["cat"], 0.03)  # 0.03 CAT on 100 contracts

    def test_no_quote_no_fill(self):
        pos, why = self.e.open_position(q(bid=0.0, ask=0.12), at(10, 5), "t")
        self.assertIsNone(pos)
        pos, why = self.e.open_position(q(bid=0.10, ask=None), at(10, 5), "t")
        self.assertIsNone(pos)
        pos, _ = self.e.open_position(q(), at(10, 5), "t")
        self.assertIsNone(self.e.close_position(pos, 0.0, at(11, 0), "t"))
        self.assertEqual(len(self.e.s["positions"]), 1)

    def test_40pct_cap_blocks_oversized(self):
        # 40% of $50 = $20. ask 0.20 -> 20.04 > 20 -> blocked; ask 0.19 -> 19.04 ok
        pos, why = self.e.open_position(q(bid=0.18, ask=0.20), at(10, 5), "t")
        self.assertIsNone(pos)
        self.assertIn("40% cap", why)
        pos, why = self.e.open_position(q(bid=0.17, ask=0.19), at(10, 5), "t")
        self.assertIsNotNone(pos)

    def test_cap_uses_current_equity(self):
        self.e.s["cash"] = 30.0
        ok, why = self.e.can_open(12.5, at(10, 5))  # 40% of 30 = 12
        self.assertFalse(ok)
        ok, _ = self.e.can_open(11.9, at(10, 5))
        self.assertTrue(ok)

    def test_daily_cap_triggers_and_liquidates(self):
        sym = "SPY261028C00775000"
        self.p.quotes[sym] = q(bid=0.18, ask=0.19)
        pos, _ = self.e.open_position(self.p.quotes[sym], at(10, 5), "t")
        # option collapses: bid 0.05 -> equity = 30.96 + 5 - 0.05 = ~35.91 <= 42.5 floor
        self.p.quotes[sym] = q(bid=0.05, ask=0.06)
        self.e.tick(at(10, 30))
        self.assertTrue(self.e.s["day"]["daily_cap_hit"])
        self.assertEqual(self.e.s["positions"], [])  # liquidated at the bid
        ok, why = self.e.can_open(1.0, at(10, 35))
        self.assertFalse(ok)
        self.assertEqual(why, "daily loss cap hit")
        # next day resets daily cap
        nxt = date(2026, 10, 29)
        self.e.tick(at(10, 5, nxt))
        self.assertFalse(self.e.s["day"]["daily_cap_hit"])

    def test_full_stop(self):
        self.e.s["cash"] = 25.00
        self.e.tick(at(10, 30))
        self.assertTrue(self.e.s["full_stop"])
        ok, why = self.e.can_open(1.0, at(10, 31))
        self.assertEqual(why, "full stop active")
        self.e.tick(at(10, 5, date(2026, 10, 29)))  # persists across days
        self.assertTrue(self.e.s["full_stop"])

    def test_expiry_settles_at_intrinsic(self):
        sym = "SPY261028C00775000"
        self.p.quotes[sym] = q(bid=0.10, ask=0.12)
        pos, _ = self.e.open_position(self.p.quotes[sym], at(10, 5), "t")
        self.p.quotes[sym] = q(bid=0.0, ask=0.0)   # no quote -> cannot sell at time stop, held
        self.e.tick(at(15, 50))
        self.assertEqual(len(self.e.s["positions"]), 1)
        self.e.tick(at(16, 5))                     # no official close yet -> keep, don't guess
        self.assertEqual(len(self.e.s["positions"]), 1)
        self.p.closes[("SPY", DAY)] = 775.40
        self.e.tick(at(16, 10))
        rec = self.e.s["trades"][-1]
        self.assertEqual(rec["action"], "EXPIRE")
        self.assertAlmostEqual(rec["cash_flow"], 40.0)   # (775.40-775)*100
        self.assertAlmostEqual(rec["pnl"], 40.0 - 12.04)

    def test_expiry_otm_put_worthless(self):
        p = q(symbol="SPY261028P00760000", kind="put", strike=760.0, bid=0.10, ask=0.12)
        pos, _ = self.e.open_position(p, at(10, 5), "t")
        self.p.closes[("SPY", DAY)] = 770.0
        self.e.tick(at(16, 10))
        self.assertAlmostEqual(self.e.s["trades"][-1]["cash_flow"], 0.0)
        self.assertAlmostEqual(self.e.s["cash"], 50 - 12.04)

    def test_take_profit_and_time_stop(self):
        sym = "SPY261028C00775000"
        self.p.quotes[sym] = q(bid=0.10, ask=0.12)
        self.e.open_position(self.p.quotes[sym], at(10, 5), "t")
        self.p.quotes[sym] = q(bid=0.19, ask=0.20)  # 19-0.05=18.95 vs 12.04 = +57%
        self.e.tick(at(11, 0))
        self.assertIn("take profit", self.e.s["trades"][-1]["reason"])
        self.p.quotes[sym] = q(bid=0.10, ask=0.12)
        self.e.s["day"]["entries"] = 0
        self.e.open_position(self.p.quotes[sym], at(12, 0), "t")
        self.e.tick(at(15, 46))
        self.assertEqual(self.e.s["trades"][-1]["reason"], "time stop (expiry day)")

    def test_calendar(self):
        self.assertFalse(mc.is_trading_day(date(2026, 11, 26)))  # Thanksgiving
        self.assertFalse(mc.is_trading_day(date(2026, 10, 31)))  # Saturday
        self.assertTrue(mc.is_market_open(at(12, 30, date(2026, 11, 27))))
        self.assertFalse(mc.is_market_open(at(13, 0, date(2026, 11, 27))))  # early close
        self.assertFalse(mc.is_market_open(at(9, 29)))
        self.assertTrue(mc.is_market_open(at(9, 30)))
        self.assertFalse(mc.is_market_open(at(16, 0)))
        self.assertFalse(mc.entry_window_open(at(9, 59)))
        self.assertFalse(mc.entry_window_open(at(15, 0)))

    def test_risk_constants_untouched(self):
        self.assertEqual((config.MAX_POSITION_FRAC, config.DAILY_LOSS_CAP_FRAC, config.FULL_STOP_EQUITY,
                          config.STARTING_CASH), (0.40, 0.15, 25.0, 50.0))

    def test_signal_and_selection_end_to_end(self):
        idx = pd.date_range(at(9, 30), periods=40, freq="1min")
        px = [770 + i * 0.12 for i in range(40)]  # +0.62% trend up
        self.p.bars["SPY"] = pd.DataFrame({"Open": px, "High": [x + .05 for x in px], "Low": [x - .05 for x in px],
                                           "Close": px, "Volume": [1000] * 40}, index=idx)
        spot = px[-1]
        self.p.quotes["A"] = q(symbol="A", strike=775.0, bid=0.0, ask=0.15)            # no bid -> skip
        self.p.quotes["B"] = q(symbol="B", strike=776.0, bid=0.08, ask=0.15)           # spread 47% -> skip
        self.p.quotes["C"] = q(symbol="C", strike=777.0, bid=0.10, ask=0.12, vol=50)  # low volume -> skip
        self.p.quotes["D"] = q(symbol="D", strike=774.0, bid=0.14, ask=0.16)  # ok? strike 774 vs spot 774.68 -> ITM, not OTM
        self.p.quotes["E"] = q(symbol="E", strike=775.0, bid=0.11, ask=0.13, exp="2026-10-29")  # next expiry ok
        self.assertLess(774, spot)
        pos = self.e.try_entry(at(10, 10))
        self.assertEqual(pos["symbol"], "E")
        self.assertEqual(pos["entry_price"], 0.13)


    # ------------------------------------------------------------ robustness
    def test_retry_backoff_then_stop_loss_on_recovery(self):
        sym = "AMD261028P00537500"
        self.p.quotes[sym] = q(symbol=sym, kind="put", strike=537.5, und="AMD", bid=0.15, ask=0.17)
        self.e.open_position(self.p.quotes[sym], at(10, 26), "t")
        self.p.fail_next = 3
        self.e.tick(at(10, 56))                     # fetch fails -> keep last real mark, flagged
        pos = self.e.s["positions"][0]
        self.assertFalse(pos["quote_ok"])
        self.assertEqual(pos["mark_bid"], 0.15)     # never invented
        self.assertEqual(self.e.next_delay(at(10, 56)), 10)
        self.e.tick(at(10, 56, 10) if False else datetime(2026, 10, 28, 10, 56, 10, tzinfo=ET))
        self.assertEqual(self.e.next_delay(at(10, 56)), 12)
        self.e.tick(datetime(2026, 10, 28, 10, 56, 22, tzinfo=ET))
        self.assertEqual(self.e.next_delay(at(10, 56)), 15)
        self.assertEqual(self.e.next_delay(at(10, 56)), 15)  # stays at 15s, never back to 90s while failing
        # recovers with bid 0.10 -> net 9.95 vs 17.04 = -42% -> stop loss on the very next fast retry
        self.p.quotes[sym] = q(symbol=sym, kind="put", strike=537.5, und="AMD", bid=0.10, ask=0.11)
        self.e.tick(datetime(2026, 10, 28, 10, 56, 37, tzinfo=ET))
        self.assertEqual(self.e.s["positions"], [])
        self.assertIn("stop loss", self.e.s["trades"][-1]["reason"])
        self.assertEqual(self.e.s["trades"][-1]["price"], 0.10)  # sold at the real bid
        self.assertEqual(self.e.next_delay(datetime(2026, 10, 28, 10, 57, tzinfo=ET)), config.POLL_SECONDS_MARKET)
        # stale period (10:26 last good quote -> 10:56:37) recorded
        sp = self.e.s["stale_periods"][-1]
        self.assertEqual(sp["symbol"], sym)
        self.assertGreater(sp["seconds"], config.STALE_QUOTE_SECONDS)

    def test_guarded_hard_timeout(self):
        import time
        import data
        t0 = time.monotonic()
        with self.assertRaises(data.FetchTimeout):
            data.guarded(time.sleep, 3, timeout=0.2)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(data.guarded(lambda: 42, timeout=1), 42)
        with self.assertRaises(ValueError):
            data.guarded(lambda: (_ for _ in ()).throw(ValueError("x")), timeout=1)

    def test_gap_detection(self):
        self.assertIsNone(self.e.note_loop(at(11, 50)))
        self.e.next_delay(at(11, 50))                # requests 90s
        self.assertIsNone(self.e.note_loop(datetime(2026, 10, 28, 11, 51, 35, tzinfo=ET)))  # 95s: fine
        self.e.next_delay(at(11, 51))
        g = self.e.note_loop(at(14, 7))              # box paused 11:51 -> 14:07
        self.assertIsNotNone(g)
        self.assertGreater(g["seconds"], 8000)
        self.assertEqual(len(self.e.s["missed_checks"]), 1)
        # overnight/idle gaps outside market hours are not missed checks
        self.e.note_loop(at(15, 59)); self.e.next_delay(at(15, 59))
        self.assertEqual(len(self.e.s["missed_checks"]), 2)   # 14:07 -> 15:59 was itself a real gap
        self.assertIsNone(self.e.note_loop(at(16, 30)))       # only 60s of market time missed
        self.e.next_delay(at(16, 30))
        self.assertIsNone(self.e.note_loop(at(9, 0, date(2026, 10, 29))))
        # gap spanning the open IS counted (e.g. 09:00 -> 10:00 with 300s idle sleep requested)
        self.e.next_delay(at(9, 0, date(2026, 10, 29)))
        self.assertIsNotNone(self.e.note_loop(at(10, 0, date(2026, 10, 29))))
        self.assertEqual(len(self.e.s["missed_checks"]), 3)

    def test_stale_position_flag(self):
        sym = "SPY261028C00775000"
        self.p.quotes[sym] = q(bid=0.10, ask=0.12)
        self.e.open_position(self.p.quotes[sym], at(10, 5), "t")
        self.assertEqual(self.e.stale_positions(at(10, 7)), [])
        self.p.quotes[sym] = q(bid=0.0, ask=0.0)    # quotes go missing
        self.e.tick(at(10, 7))
        self.assertEqual(self.e.stale_positions(at(10, 7)), [])          # 2 min old: not yet stale
        self.assertEqual(len(self.e.stale_positions(at(10, 8, 30) if False else datetime(2026, 10, 28, 10, 8, 1, tzinfo=ET))), 1)
        import stats
        self.assertEqual(len(stats.stale_positions(self.e.s, datetime(2026, 10, 28, 10, 8, 1, tzinfo=ET))), 1)
        self.assertEqual(stats.stale_positions(self.e.s, at(16, 30)), [])  # market closed: not flagged

    def test_robust_sleep_wall_jump(self):
        from engine import robust_sleep
        clock = {"wall": at(11, 53), "mono": 1000.0}
        def sleep_fn(s):
            clock["mono"] += s
            clock["wall"] = clock["wall"] + __import__("datetime").timedelta(seconds=s)
            if clock["mono"] >= 1010:   # box frozen: wall clock leaps 2h, monotonic barely moves
                clock["wall"] = at(14, 7)
        n = robust_sleep(90, now_fn=lambda: clock["wall"], mono_fn=lambda: clock["mono"], sleep_fn=sleep_fn, chunk=5)
        self.assertEqual(n, 2)               # woke on the first chunk after the jump, not 90s later
        clock.update(wall=at(10, 0), mono=0.0)
        def sleep2(s):
            clock["mono"] += s; clock["wall"] += __import__("datetime").timedelta(seconds=s)
        self.assertEqual(robust_sleep(12, lambda: clock["wall"], lambda: clock["mono"], sleep2, chunk=5), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
