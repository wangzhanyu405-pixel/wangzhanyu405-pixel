from __future__ import annotations

import unittest
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from finance_platform.parsers import (
    choose_excel_engine,
    detect_header_row,
    infer_invoice_file_role,
    is_excel_temp_file,
    parse_bank_statement,
    parse_payable_invoices,
    parse_party_directory,
    parse_receivable_invoices,
    pick_column,
)


class ParserHelpersTest(unittest.TestCase):
    def test_detect_header_row_scores_business_keywords(self) -> None:
        rows = [
            ["示例企业2026年开票收款明细", "", ""],
            ["开票日期", "发票号码", "付款单位", "金额", "收款日期"],
            ["2026-03-01", "123", "客户A", "100", ""],
        ]

        self.assertEqual(detect_header_row(rows), 1)

    def test_pick_column_returns_first_matching_keyword(self) -> None:
        columns = ["交易时间", "收入金额", "支出金额", "对方户名"]

        self.assertEqual(pick_column(columns, ["付款单位", "对方户名"]), "对方户名")
        self.assertIsNone(pick_column(columns, ["发票号码"]))

    def test_choose_excel_engine_by_suffix(self) -> None:
        self.assertEqual(choose_excel_engine(Path("a.xlsx")), "openpyxl")
        self.assertEqual(choose_excel_engine(Path("a.XLS")), "xlrd")

    def test_is_excel_temp_file(self) -> None:
        self.assertTrue(is_excel_temp_file(Path("~$26年农行明细.xlsx")))
        self.assertFalse(is_excel_temp_file(Path("26年农行明细.xlsx")))

    def test_party_directory_extracts_payment_account_and_bank(self) -> None:
        import pandas as pd

        with tempfile.TemporaryDirectory() as tmp_dir:
            fixture = Path(tmp_dir) / "应付款明细.xlsx"
            pd.DataFrame(
                [
                    {
                        "收款方户名": "上海测试纸业有限公司",
                        "账期": "90天",
                        "收款方账号": "6222001",
                        "开户行支行名称": "中国农业银行上海支行",
                    }
                ]
            ).to_excel(fixture, index=False)

            result = parse_party_directory(fixture)

        self.assertGreater(result.parsed_count, 0)
        first = result.rows[0]
        self.assertTrue(first.bank_account)
        self.assertTrue(first.bank_name)

    def test_bank_statement_keeps_amount_rows_without_counterparty_name(self) -> None:
        import pandas as pd

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "bank.xlsx"
            rows = [
                ["账户明细", None, None, None, None, None, None, None],
                ["账号:00-000000000000014", "户名:示例核算主体", "币种:人民币", None, None, None, None, None],
                ["交易时间", "收入金额", "支出金额", "账户余额", "对方账号", "对方户名", "对方开户行", "摘要"],
                ["2026-01-07 11:00:00", None, 60, 1940.00, "00000000000000011", None, "0007", "转取"],
                ["2026-03-21 10:00:00", 1.25, None, 2001.25, None, None, None, "批量结息"],
                ["2026-01-08 09:30:00", None, 100, 1840.00, "10001", "上海测试供应商", "中国农业银行", "转取"],
            ]
            pd.DataFrame(rows).to_excel(path, header=False, index=False)

            result = parse_bank_statement(path)

        self.assertEqual(result.parsed_count, 3)
        self.assertEqual(result.rows[0].counterparty_name_raw, "00000000000000011")
        self.assertEqual(result.rows[0].expense_amount, Decimal("60.00"))
        self.assertEqual(result.rows[1].counterparty_name_raw, "批量结息")
        self.assertEqual(result.rows[1].income_amount, Decimal("1.25"))
        self.assertEqual(result.rows[2].counterparty_name_raw, "上海测试供应商")

    def test_tax_export_invoice_parser_uses_base_info_sheet_and_red_metadata(self) -> None:
        import pandas as pd

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "取得发票.xlsx"
            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                pd.DataFrame(
                    [
                        {
                            "发票号码": "DETAIL-DUP",
                            "销方名称": "明细供应商",
                            "开票日期": date(2026, 1, 1),
                            "金额": 50,
                        },
                        {
                            "发票号码": "DETAIL-DUP",
                            "销方名称": "明细供应商",
                            "开票日期": date(2026, 1, 1),
                            "金额": 50,
                        },
                    ]
                ).to_excel(writer, sheet_name="信息汇总表", index=False)
                pd.DataFrame(
                    [
                        {
                            "数电发票号码": "R-RED",
                            "销方名称": "上海供应商",
                            "开票日期": date(2026, 5, 20),
                            "金额": -109.25,
                            "税额": -14.20,
                            "价税合计": -123.45,
                            "发票状态": "已红冲-全额",
                            "是否正数发票": "否",
                            "发票票种": "电子发票",
                            "备注": "被红冲蓝字数电发票号码：B-001 红字发票信息确认单编号：90000000000000000016",
                        }
                    ]
                ).to_excel(writer, sheet_name="发票基础信息", index=False)

            result = parse_payable_invoices(path)

        self.assertEqual(result.parsed_count, 1)
        invoice = result.rows[0]
        self.assertEqual(invoice.source_sheet, "发票基础信息")
        self.assertEqual(invoice.party_name_raw, "上海供应商")
        self.assertEqual(invoice.invoice_no, "R-RED")
        self.assertEqual(invoice.invoice_amount, Decimal("-123.45"))
        self.assertEqual(invoice.invoice_status_raw, "已红冲-全额")
        self.assertFalse(invoice.is_positive)
        self.assertEqual(invoice.original_invoice_no, "B-001")
        self.assertEqual(invoice.red_confirmation_no, "90000000000000000016")
        self.assertEqual(invoice.invoice_type, "电子发票")
        self.assertEqual(invoice.amount_without_tax, Decimal("-109.25"))
        self.assertEqual(invoice.tax_amount, Decimal("-14.20"))

    def test_invoice_role_inference_prefers_issued_invoice_as_receivable(self) -> None:
        import pandas as pd

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "26年开具发票.xlsx"
            pd.DataFrame(
                [
                    {
                        "数电发票号码": "OUT-1",
                        "销方名称": "示例核算主体",
                        "购买方名称": "上海测试客户有限公司",
                        "开票日期": date(2026, 6, 1),
                        "价税合计": 1200,
                    }
                ]
            ).to_excel(path, sheet_name="发票基础信息", index=False)

            inferred_role = infer_invoice_file_role(path, fallback_role="payable_invoice")
            result = parse_receivable_invoices(path)

        self.assertEqual(inferred_role, "receivable_invoice")
        self.assertEqual(result.rows[0].direction, "receivable")
        self.assertEqual(result.rows[0].party_name_raw, "上海测试客户有限公司")


if __name__ == "__main__":
    unittest.main()
