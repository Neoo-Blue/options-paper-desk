import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ci_runner  # noqa: E402
from market_calendar import ET  # noqa: E402


def at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


class DeadlineTest(unittest.TestCase):
    def test_morning_job_capped_below_6h(self):
        self.assertEqual(ci_runner.deadline_for(at(2026, 11, 2, 9, 15)), at(2026, 11, 2, 14, 55))

    def test_afternoon_job_runs_past_close_for_settlement(self):
        self.assertEqual(ci_runner.deadline_for(at(2026, 11, 2, 12, 40)), at(2026, 11, 2, 16, 15))

    def test_early_close(self):
        self.assertEqual(ci_runner.deadline_for(at(2026, 11, 27, 11, 0)), at(2026, 11, 27, 13, 15))

    def test_after_close_weekend_and_holiday_run_once(self):
        self.assertIsNone(ci_runner.deadline_for(at(2026, 11, 2, 16, 20)))
        self.assertIsNone(ci_runner.deadline_for(at(2026, 10, 31, 10, 0)))   # Saturday
        self.assertIsNone(ci_runner.deadline_for(at(2026, 11, 26, 10, 0)))   # Thanksgiving


class ChainTest(unittest.TestCase):
    H = 3600

    def test_chains_when_cap_hit_mid_session(self):
        self.assertTrue(ci_runner.should_chain(at(2026, 11, 3, 15, 15), 5.6 * self.H))
        self.assertTrue(ci_runner.should_chain(at(2026, 11, 3, 13, 50), 5.6 * self.H))

    def test_no_chain_after_close_weekend_holiday(self):
        self.assertFalse(ci_runner.should_chain(at(2026, 11, 3, 16, 15), 5.6 * self.H))
        self.assertFalse(ci_runner.should_chain(at(2026, 11, 3, 16, 12), 5.6 * self.H))   # < 5 min left
        self.assertFalse(ci_runner.should_chain(at(2026, 11, 7, 12, 0), 5.6 * self.H))    # Saturday
        self.assertFalse(ci_runner.should_chain(at(2026, 11, 26, 12, 0), 5.6 * self.H))   # Thanksgiving
        self.assertFalse(ci_runner.should_chain(at(2026, 11, 27, 13, 12), 5.6 * self.H))  # early close 13:00 (+15)

    def test_no_chain_after_short_run(self):
        self.assertFalse(ci_runner.should_chain(at(2026, 11, 3, 12, 0), 60))

    def test_settlement_pending(self):
        class E:
            s = {"positions": [{"expiry": "2026-11-03"}]}
        self.assertFalse(ci_runner.settlement_pending(E, at(2026, 11, 3, 15, 0)))
        self.assertTrue(ci_runner.settlement_pending(E, at(2026, 11, 3, 16, 5)))
        self.assertTrue(ci_runner.settlement_pending(E, at(2026, 11, 4, 9, 0)))
        E.s = {"positions": [{"expiry": "2026-11-06"}]}
        self.assertFalse(ci_runner.settlement_pending(E, at(2026, 11, 3, 16, 5)))


if __name__ == "__main__":
    unittest.main()
