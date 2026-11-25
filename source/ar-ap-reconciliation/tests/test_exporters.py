from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook
import pandas as pd

from finance_platform.exporters import export_reconciliation_workbook
from finance_platform.matching import MatchResult


class ReconciliationExporterTest(unittest.TestCase):
    def test_reconciliation_export_contains_auditable_summary_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "reconciliation.xlsx"
            matches = [
                MatchResult(
                    match_type="exact",
                    status="matched",
                    party_id=1,
                    party_name="上海测试供应商",
                    invoice_ids=[1],
                    bank_transaction_ids=[10],
                    invoice_total=Decimal("100.00"),
                    bank_total=Decimal("100.00"),
                    difference_amount=Decimal("0.00"),
                    confidence=Decimal("1.00"),
                ),
                MatchResult(
                    match_type="unmatched",
                    status="unmatched_invoice",
                    party_id=2,
                    party_name="上海未付供应商",
                    invoice_ids=[2],
                    bank_transaction_ids=[],
                    invoice_total=Decimal("30.00"),
                    bank_total=Decimal("0.00"),
                    difference_amount=Decimal("30.00"),
                    confidence=Decimal("0.00"),
                ),
            ]

            export_reconciliation_workbook(
                output,
                payable_details=[
                    {
                        "发票ID": 1,
                        "往来单位": "上海测试供应商",
                        "发票日期": date(2026, 6, 1),
                        "发票金额": Decimal("100.00"),
                    }
                ],
                bank_details=[
                    {
                        "流水ID": 10,
                        "交易日期": date(2026, 6, 30),
                        "方向": "支出",
                        "金额": Decimal("100.00"),
                    }
                ],
                match_results=matches,
                exceptions=[matches[1]],
            )

            workbook = pd.ExcelFile(output)
            self.assertIn("汇总核对", workbook.sheet_names)
            summary = pd.read_excel(output, sheet_name="汇总核对")

            self.assertEqual(list(summary.columns), ["匹配状态", "笔数", "发票合计", "银行合计", "差额"])
            total = summary[summary["匹配状态"] == "全部"].iloc[0]
            matched = summary[summary["匹配状态"] == "matched"].iloc[0]
            unmatched = summary[summary["匹配状态"] == "unmatched_invoice"].iloc[0]

            self.assertEqual(int(total["笔数"]), 2)
            self.assertEqual(float(total["发票合计"]), 130.0)
            self.assertEqual(float(total["银行合计"]), 100.0)
            self.assertEqual(float(total["差额"]), 30.0)
            self.assertEqual(float(matched["差额"]), 0.0)
            self.assertEqual(float(unmatched["差额"]), 30.0)

    def test_reconciliation_export_uses_chinese_detail_headers_and_text_invoice_no(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "reconciliation.xlsx"
            long_invoice_no = "90000000000000300001"
            matches = [
                MatchResult(
                    match_type="exact",
                    status="matched",
                    party_id=1,
                    party_name="上海测试客户",
                    invoice_ids=[101],
                    bank_transaction_ids=[201],
                    invoice_total=Decimal("1310.60"),
                    bank_total=Decimal("1310.60"),
                    difference_amount=Decimal("0.00"),
                    confidence=Decimal("1.00"),
                )
            ]
            detail_rows = [
                {
                    "往来单位": "上海测试客户",
                    "发票号": long_invoice_no,
                    "发票日期": "2026-06-25",
                    "到期日": "2026-07-25",
                    "发票金额": "¥1,310.60",
                    "实收日期": "2026-06-30",
                    "收款金额": "¥1,310.60",
                    "差额": "¥0.00",
                    "状态": "已收款",
                    "匹配方式": "单张发票匹配",
                    "置信度": "100%",
                    "说明": "同一往来单位，金额一致，收款日期在允许窗口内",
                    "发票ID": "101",
                    "流水ID": "201",
                }
            ]

            export_reconciliation_workbook(
                output,
                payable_details=[],
                bank_details=[],
                match_results=matches,
                match_details=detail_rows,
                exceptions=[],
            )

            workbook = load_workbook(output)
            sheet = workbook["匹配结果"]
            headers = [cell.value for cell in sheet[1]]

            self.assertIn("发票号", headers)
            self.assertIn("往来单位", headers)
            self.assertNotIn("match_type", headers)
            self.assertNotIn("invoice_ids", headers)

            invoice_no_col = headers.index("发票号") + 1
            invoice_cell = sheet.cell(row=2, column=invoice_no_col)
            self.assertEqual(invoice_cell.value, long_invoice_no)
            self.assertEqual(invoice_cell.number_format, "@")

            amount_col = headers.index("发票金额") + 1
            amount_cell = sheet.cell(row=2, column=amount_col)
            self.assertEqual(amount_cell.value, 1310.6)
            self.assertNotIn("¥", str(amount_cell.value))
            self.assertNotIn("¥", amount_cell.number_format)


if __name__ == "__main__":
    unittest.main()
