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


if __name__ == "__main__":
    unittest.main()
