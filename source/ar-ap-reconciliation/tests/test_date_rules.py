from __future__ import annotations

import unittest
from datetime import date

from finance_platform.date_rules import add_months, calculate_terms, month_end


class DateRulesTest(unittest.TestCase):
    def test_calculates_due_date_by_term_days_without_grace(self) -> None:
        terms = calculate_terms(date(2026, 3, 1), term_days=90)

        self.assertEqual(terms.due_date, date(2026, 5, 30))
        self.assertEqual(terms.grace_deadline, date(2026, 5, 30))

    def test_add_months_clamps_to_month_end(self) -> None:
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2024, 1, 31), 1), date(2024, 2, 29))

    def test_month_end_returns_last_day_of_same_month(self) -> None:
        self.assertEqual(month_end(date(2026, 6, 1)), date(2026, 6, 30))
        self.assertEqual(month_end(date(2026, 2, 13)), date(2026, 2, 28))

    def test_month_term_is_converted_to_30_day_compatibility_value(self) -> None:
        terms = calculate_terms(date(2026, 3, 15), term_months=2)

        self.assertEqual(terms.due_date, date(2026, 5, 14))
        self.assertEqual(terms.grace_deadline, date(2026, 5, 14))


if __name__ == "__main__":
    unittest.main()
