from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from finance_platform.balances import calculate_balance_rows
from finance_platform.local_store import LocalStore


class BalanceCalculationTest(unittest.TestCase):
    def test_payable_balance_uses_invoices_less_real_supplier_payments_and_adjustments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            supplier_id = store.upsert_party("supplier", "上海测试供应商", default_term_days=30)
            other_supplier_id = store.upsert_party("supplier", "上海其他供应商", default_term_days=30)
            store.insert_invoice(
                direction="payable",
                party_id=supplier_id,
                party_name_raw="上海测试供应商",
                invoice_no="P-1",
                invoice_date=date(2026, 1, 10),
                invoice_amount=Decimal("1000.00"),
                term_days=30,
            )
            store.insert_invoice(
                direction="payable",
                party_id=supplier_id,
                party_name_raw="上海测试供应商",
                invoice_no="P-2",
                invoice_date=date(2026, 2, 10),
                invoice_amount=Decimal("500.00"),
                term_days=30,
            )
            store.insert_invoice(
                direction="payable",
                party_id=None,
                party_name_raw="未建档供应商",
                invoice_no="P-UNKNOWN",
                invoice_date=date(2026, 2, 10),
                invoice_amount=Decimal("9999.00"),
                term_days=30,
            )
            store.insert_invoice(
                direction="payable",
                party_id=other_supplier_id,
                party_name_raw="上海其他供应商",
                invoice_no="P-LATE",
                invoice_date=date(2026, 8, 1),
                invoice_amount=Decimal("700.00"),
                term_days=30,
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 3, 1),
                income_amount=None,
                expense_amount=Decimal("300.00"),
                counterparty_name_raw="上海测试供应商",
                direction="outflow",
                party_id=supplier_id,
                summary="货款",
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 3, 2),
                income_amount=None,
                expense_amount=Decimal("2000.00"),
                counterparty_name_raw="上海测试供应商",
                direction="outflow",
                party_id=supplier_id,
                summary="股权投资款",
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 3, 3),
                income_amount=None,
                expense_amount=Decimal("88.00"),
                counterparty_name_raw="未建档供应商",
                direction="outflow",
            )
            store.insert_balance_adjustment(
                direction="payable",
                party_id=supplier_id,
                adjustment_type="acceptance_bill",
                adjustment_date=date(2026, 3, 5),
                amount=Decimal("-100.00"),
                remark="承兑支付",
            )

            rows = calculate_balance_rows(store, "payable", date(2026, 6, 30))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].party_name, "上海测试供应商")
            self.assertEqual(rows[0].invoice_total, Decimal("1500.00"))
            self.assertEqual(rows[0].bank_settlement_total, Decimal("300.00"))
            self.assertEqual(rows[0].adjustment_total, Decimal("-100.00"))
            self.assertEqual(rows[0].balance_amount, Decimal("1100.00"))

    def test_receivable_balance_uses_customer_receipts_and_adjustments_as_of_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            customer_id = store.upsert_party("customer", "上海测试客户", default_term_days=30)
            store.insert_invoice(
                direction="receivable",
                party_id=customer_id,
                party_name_raw="上海测试客户",
                invoice_no="R-1",
                invoice_date=date(2026, 1, 20),
                invoice_amount=Decimal("2000.00"),
                term_days=30,
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 2, 10),
                income_amount=Decimal("700.00"),
                expense_amount=None,
                counterparty_name_raw="上海测试客户",
                direction="inflow",
                party_id=customer_id,
                summary="货款",
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 7, 1),
                income_amount=Decimal("500.00"),
                expense_amount=None,
                counterparty_name_raw="上海测试客户",
                direction="inflow",
                party_id=customer_id,
                summary="货款",
            )
            store.insert_balance_adjustment(
                direction="receivable",
                party_id=customer_id,
                adjustment_type="cash",
                adjustment_date=date(2026, 2, 15),
                amount=Decimal("-200.00"),
                remark="现金收款",
            )

            rows = calculate_balance_rows(store, "receivable", date(2026, 6, 30))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].party_name, "上海测试客户")
            self.assertEqual(rows[0].invoice_total, Decimal("2000.00"))
            self.assertEqual(rows[0].bank_settlement_total, Decimal("700.00"))
            self.assertEqual(rows[0].adjustment_total, Decimal("-200.00"))
            self.assertEqual(rows[0].balance_amount, Decimal("1100.00"))

    def test_manual_payable_settlement_reduces_balance_without_bank_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            supplier_id = store.upsert_party("supplier", "上海现金供应商", default_term_days=30)
            invoice_id = store.insert_invoice(
                direction="payable",
                party_id=supplier_id,
                party_name_raw="上海现金供应商",
                invoice_no="P-CASH-1",
                invoice_date=date(2026, 1, 1),
                invoice_amount=Decimal("1000.00"),
                term_days=30,
            )

            store.save_manual_settlement(
                direction="payable",
                invoice_id=invoice_id,
                settlement_method="cash",
                settlement_date=date(2026, 1, 20),
                amount=Decimal("400.00"),
                remark="现金支付一部分",
            )

            rows = calculate_balance_rows(store, "payable", date(2026, 1, 31))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].invoice_total, Decimal("1000.00"))
            self.assertEqual(rows[0].bank_settlement_total, Decimal("0.00"))
            self.assertEqual(rows[0].adjustment_total, Decimal("-400.00"))
            self.assertEqual(rows[0].balance_amount, Decimal("600.00"))


if __name__ == "__main__":
    unittest.main()
