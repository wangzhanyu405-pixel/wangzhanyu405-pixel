from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from finance_platform.normalization import (
    clean_name,
    detect_bank_direction,
    normalize_amount,
    normalize_date,
    period_month,
)


class NormalizationTest(unittest.TestCase):
    def test_clean_name_normalizes_spaces(self) -> None:
        self.assertEqual(clean_name(" 上海\u3000示例壬胶粘制品有限公司 "), "上海 示例壬胶粘制品有限公司")
        self.assertEqual(clean_name(None), "")

    def test_clean_name_normalizes_full_width_parentheses(self) -> None:
        self.assertEqual(clean_name("例癸（上海）生物科技有限公司"), "例癸(上海)生物科技有限公司")

    def test_normalize_amount_handles_commas_and_empty_values(self) -> None:
        self.assertEqual(normalize_amount("1,234.50"), Decimal("1234.50"))
        self.assertEqual(normalize_amount("￥ 88.1"), Decimal("88.10"))
        self.assertIsNone(normalize_amount(""))
        self.assertIsNone(normalize_amount("不是金额"))

    def test_normalize_date_handles_date_string_and_excel_serial(self) -> None:
        self.assertEqual(normalize_date("2026-03-01"), date(2026, 3, 1))
        self.assertEqual(normalize_date("2026/03/01"), date(2026, 3, 1))
        self.assertEqual(normalize_date(46083), date(2026, 3, 2))
        self.assertIsNone(normalize_date(""))

    def test_detect_bank_direction(self) -> None:
        self.assertEqual(detect_bank_direction(Decimal("10.00"), None), "inflow")
        self.assertEqual(detect_bank_direction(None, Decimal("10.00")), "outflow")
        self.assertEqual(detect_bank_direction(Decimal("0"), Decimal("0")), "unknown")

    def test_period_month(self) -> None:
        self.assertEqual(period_month(date(2026, 7, 6)), "2026-07")


if __name__ == "__main__":
    unittest.main()
