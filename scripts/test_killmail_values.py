"""Unit and admin-control checks for in-place killmail valuation."""

import unittest
from collections import defaultdict, deque
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from scripts import calculate_killmail_values as values
from scripts.test_first_loss_admin import jobs


class PriceFormulaTests(unittest.TestCase):
    def test_rolling_price_trims_like_zkill(self):
        price, fallback = values.rolling_price(
            [Decimal(number) for number in range(1, 35)]
        )
        self.assertEqual(price, Decimal("17.50"))
        self.assertFalse(fallback)

        price, fallback = values.rolling_price(
            [Decimal(number) for number in range(1, 8)]
        )
        self.assertEqual(price, Decimal("3.00"))
        self.assertFalse(fallback)

    def test_exact_day_price_caps_a_higher_average(self):
        price, fallback = values.rolling_price(
            [Decimal("10"), Decimal("12")], Decimal("4.25")
        )
        self.assertEqual(price, Decimal("4.25"))
        self.assertFalse(fallback)

    def test_no_history_is_one_cent_and_reported(self):
        self.assertEqual(
            values.rolling_price([], None), (Decimal("0.01"), True)
        )

    def test_concrete_special_rules_precede_history(self):
        metadata = {
            1: (None, 91, "SKIN"),
            2: (29, 6, "Capsule"),
            3: (4513, None, "Mordunium"),
            42126: (30, 6, "Vanquisher"),
        }
        self.assertEqual(
            values.effective_price(1, date(2026, 1, 1), [Decimal("50")], Decimal("50"), metadata)[0],
            Decimal("0.01"),
        )
        self.assertEqual(
            values.effective_price(2, date(2026, 1, 1), [], None, metadata)[0],
            Decimal("10000"),
        )
        self.assertEqual(
            values.effective_price(3, date(2026, 1, 1), [], None, metadata)[0],
            Decimal("200"),
        )
        self.assertEqual(
            values.effective_price(42126, date(2026, 1, 1), [], Decimal("9"), metadata)[0],
            Decimal("650000000000"),
        )

    def test_price_stream_retains_history_and_materializes_only_needed_pairs(self):
        class Cursor:
            def __init__(self, rows):
                self.rows = iter(rows)

            def fetchone(self):
                return next(self.rows, None)

        rows = [
            (date(2026, 1, 1), {"34": Decimal("4")}),
            (date(2026, 1, 2), {"34": Decimal("8"), "35": Decimal("10")}),
            (date(2026, 1, 3), {"34": Decimal("12")}),
        ]
        cursor = Cursor(rows[1:])
        histories = defaultdict(lambda: deque(maxlen=34))
        pending, materialized, last_day, exact = values.advance_prices(
            cursor,
            rows[0],
            date(2026, 1, 2),
            histories,
            {date(2026, 1, 2): [34, 35]},
            {},
        )
        self.assertEqual(pending[0], date(2026, 1, 3))
        self.assertEqual(last_day, date(2026, 1, 2))
        self.assertEqual(exact[35], Decimal("10"))
        self.assertEqual(materialized, [
            (date(2026, 1, 2), 34, 600, False),
            (date(2026, 1, 2), 35, 1000, False),
        ])


class AdminJobTests(unittest.TestCase):
    def test_job_is_passive_and_has_no_user_range(self):
        with patch.object(jobs, "_read_config_file", return_value={"jobs": []}), patch.object(
            jobs, "_start_process"
        ) as launch:
            job = jobs.get_job("calculate_killmail_values")
            self.assertEqual(job["type"], "killmail_values")
            launch.assert_not_called()
            jobs.run_killmail_values()
            launch.assert_called_once_with(job, job["command"])


if __name__ == "__main__":
    unittest.main()
