from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from io import BytesIO

import pandas as pd

from finance_platform.ledger_tools import (
    BankLedgerFilter,
    InvoiceLedgerFilter,
    excel_bytes_from_rows,
    filter_bank_rows,
    filter_invoice_rows,
    summarize_bank_rows,
    summarize_invoice_rows,
)


class LedgerToolsTest(unittest.TestCase):
    def test_filter_bank_rows_by_finance_search_fields(self) -> None:
        rows = [
            {
                "transaction_date": "2026-06-02",
                "direction": "outflow",
                "income_amount": None,
                "expense_amount": "2500.10",
                "counterparty_account": "000015",
                "counterparty_name_raw": "待报解预算收入-国库信息处理系统扣税",
                "counterparty_bank": "中国农业银行",
                "summary": "公共缴费",
                "subject": "",
                "business_category": "tax_payment",
                "business_category_label": "税费",
            },
            {
                "transaction_date": "2026-06-15",
                "direction": "outflow",
                "income_amount": None,
                "expense_amount": "160000.00",
                "counterparty_account": "000016",
                "counterparty_name_raw": "上海例丁实业有限公司",
                "counterparty_bank": "中国建设银行",
                "summary": "转账",
                "subject": "后道货款",
                "business_category": "supplier_payment",
                "business_category_label": "供应商付款",
            },
            {
                "transaction_date": "2026-06-20",
                "direction": "inflow",
                "income_amount": "15000.00",
                "expense_amount": None,
                "counterparty_account": "000017",
                "counterparty_name_raw": "上海例乙印刷有限公司",
                "counterparty_bank": "中国银行",
                "summary": "转存",
                "subject": "",
                "business_category": "customer_receipt",
                "business_category_label": "客户回款",
            },
        ]

        result = filter_bank_rows(
            rows,
            BankLedgerFilter(
                direction="outflow",
                counterparty_name="例丁",
                counterparty_account="0000",
                text="货款",
                categories=("supplier_payment",),
                min_amount=Decimal("100000"),
                max_amount=Decimal("400000"),
            ),
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["counterparty_name_raw"], "上海例丁实业有限公司")

    def test_summarize_bank_rows_counts_amounts_and_identification(self) -> None:
        rows = [
            {"direction": "outflow", "income_amount": None, "expense_amount": "100.00", "business_category": "supplier_payment"},
            {"direction": "inflow", "income_amount": "80.00", "expense_amount": None, "business_category": "customer_receipt"},
            {"direction": "inflow", "income_amount": "20.00", "expense_amount": None, "business_category": "unidentified_inflow"},
        ]

        summary = summarize_bank_rows(rows)

        self.assertEqual(summary["row_count"], 3)
        self.assertEqual(summary["income_total"], Decimal("100.00"))
        self.assertEqual(summary["expense_total"], Decimal("100.00"))
        self.assertEqual(summary["net_amount"], Decimal("0.00"))
        self.assertEqual(summary["identified_count"], 2)
        self.assertEqual(summary["unidentified_count"], 1)

    def test_filter_and_summarize_invoice_rows(self) -> None:
        rows = [
            {
                "direction": "payable",
                "party_name": "上海供应商",
                "party_name_raw": "上海供应商",
                "invoice_no": "P-1",
                "invoice_date": "2026-05-01",
                "invoice_amount": "100.00",
                "status": "unmatched",
            },
            {
                "direction": "receivable",
                "party_name": "上海客户",
                "party_name_raw": "上海客户",
                "invoice_no": "R-1",
                "invoice_date": "2026-05-20",
                "invoice_amount": "300.00",
                "status": "matched",
            },
        ]

        result = filter_invoice_rows(
            rows,
            InvoiceLedgerFilter(
                direction="receivable",
                party_name="客户",
                invoice_no="R-",
                status=("matched",),
                min_amount=Decimal("200"),
                max_amount=Decimal("400"),
            ),
        )
        summary = summarize_invoice_rows(result)

        self.assertEqual(len(result), 1)
        self.assertEqual(summary["row_count"], 1)
        self.assertEqual(summary["amount_total"], Decimal("300.00"))
        self.assertEqual(summary["matched_count"], 1)

    def test_partial_invoice_status_is_not_counted_as_matched_in_summary(self) -> None:
        summary = summarize_invoice_rows(
            [
                {
                    "direction": "payable",
                    "party_name": "上海疑似供应商",
                    "party_name_raw": "上海疑似供应商",
                    "invoice_no": "P-1",
                    "invoice_amount": "100.00",
                    "status": "partial",
                },
                {
                    "direction": "payable",
                    "party_name": "上海已匹配供应商",
                    "party_name_raw": "上海已匹配供应商",
                    "invoice_no": "P-2",
                    "invoice_amount": "60.00",
                    "status": "matched",
                },
            ]
        )

        self.assertEqual(summary["row_count"], 2)
        self.assertEqual(summary["matched_count"], 1)
        self.assertEqual(summary["unmatched_count"], 1)

    def test_excel_bytes_from_rows_uses_selected_column_names(self) -> None:
        data = excel_bytes_from_rows(
            [{"交易日期": "2026-06-02", "对方户名": "上海测试", "金额": "100.00", "隐藏列": "不要导出"}],
            ["交易日期", "对方户名", "金额"],
            sheet_name="银行流水",
        )

        workbook = pd.read_excel(BytesIO(data), sheet_name="银行流水")

        self.assertEqual(list(workbook.columns), ["交易日期", "对方户名", "金额"])
        self.assertEqual(workbook.iloc[0]["对方户名"], "上海测试")


if __name__ == "__main__":
    unittest.main()
