from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from finance_platform.local_store import LocalStore


class FinancialControlsTest(unittest.TestCase):
    def test_settlement_summary_uses_due_date_and_match_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            supplier_id = store.upsert_party("supplier", "上海测试供应商", default_term_days=90)
            customer_id = store.upsert_party("customer", "上海测试客户", default_term_days=30)
            store.insert_invoice(
                direction="payable",
                party_id=supplier_id,
                party_name_raw="上海测试供应商",
                invoice_no="P-1",
                invoice_date=date(2026, 3, 1),
                invoice_amount=Decimal("100.00"),
                term_days=90,
                status="unmatched",
            )
            store.insert_invoice(
                direction="payable",
                party_id=supplier_id,
                party_name_raw="上海测试供应商",
                invoice_no="P-2",
                invoice_date=date(2026, 3, 2),
                invoice_amount=Decimal("60.00"),
                term_days=90,
                status="matched",
            )
            store.insert_invoice(
                direction="receivable",
                party_id=customer_id,
                party_name_raw="上海测试客户",
                invoice_no="R-1",
                invoice_date=date(2026, 4, 1),
                invoice_amount=Decimal("80.00"),
                term_days=30,
                status="unmatched",
            )

            summary = store.settlement_summary(date(2026, 5, 1), date(2026, 5, 31), as_of=date(2026, 7, 6))

            self.assertEqual(summary["payable_due_amount"], Decimal("160.00"))
            self.assertEqual(summary["payable_settled_amount"], Decimal("60.00"))
            self.assertEqual(summary["payable_pending_amount"], Decimal("100.00"))
            self.assertEqual(summary["payable_overdue_amount"], Decimal("100.00"))
            self.assertEqual(summary["receivable_due_amount"], Decimal("80.00"))
            self.assertEqual(summary["receivable_pending_amount"], Decimal("80.00"))
            self.assertEqual(summary["receivable_overdue_amount"], Decimal("80.00"))

    def test_partial_status_remains_pending_until_confirmed_matched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            supplier_id = store.upsert_party("supplier", "上海疑似供应商", default_term_days=30)
            store.insert_invoice(
                direction="payable",
                party_id=supplier_id,
                party_name_raw="上海疑似供应商",
                invoice_no="P-PARTIAL",
                invoice_date=date(2026, 4, 1),
                invoice_amount=Decimal("100.00"),
                term_days=30,
                status="partial",
            )

            summary = store.settlement_summary(date(2026, 5, 1), date(2026, 5, 31), as_of=date(2026, 7, 6))

            self.assertEqual(summary["payable_settled_amount"], Decimal("0.00"))
            self.assertEqual(summary["payable_pending_amount"], Decimal("100.00"))
            self.assertEqual(summary["payable_overdue_amount"], Decimal("100.00"))

    def test_bank_category_totals_separate_supplier_payment_from_tax(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            supplier_id = store.upsert_party("supplier", "上海测试供应商")
            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 5),
                income_amount=None,
                expense_amount=Decimal("100.00"),
                counterparty_name_raw="上海测试供应商",
                direction="outflow",
                party_id=supplier_id,
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 6),
                income_amount=None,
                expense_amount=Decimal("50.00"),
                counterparty_name_raw="待报解预算收入-国库信息处理系统扣税",
                direction="outflow",
            )

            rows = store.list_bank_transactions("outflow", date(2026, 6, 1), date(2026, 6, 30))
            totals = store.bank_category_totals(date(2026, 6, 1), date(2026, 6, 30))

            categories = {row["counterparty_name_raw"]: row["business_category"] for row in rows}
            self.assertEqual(categories["上海测试供应商"], "supplier_payment")
            self.assertEqual(categories["待报解预算收入-国库信息处理系统扣税"], "tax_payment")
            self.assertEqual(totals["supplier_payment"], Decimal("100.00"))
            self.assertEqual(totals["tax_payment"], Decimal("50.00"))

    def test_import_file_hash_lookup_prevents_duplicate_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            first_id = store.record_import_file(
                period_month="2026-06",
                file_role="bank_statement",
                original_filename="bank.xlsx",
                stored_path="data/uploads/bank.xlsx",
                file_hash="abc123",
                row_count=10,
                parsed_count=10,
                error_count=0,
            )

            duplicate = store.find_import_by_hash("abc123", "bank_statement")

            self.assertEqual(duplicate["id"], first_id)


if __name__ == "__main__":
    unittest.main()
