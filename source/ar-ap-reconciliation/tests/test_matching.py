from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from finance_platform.matching import BankRecord, InvoiceRecord, match_payables, match_receivables


class MatchingTest(unittest.TestCase):
    def test_exact_match_by_party_amount_and_date_window(self) -> None:
        invoice = InvoiceRecord(
            id=1,
            party_id=10,
            party_name="上海测试供应商",
            amount=Decimal("100.00"),
            invoice_date=date(2026, 3, 1),
            due_date=date(2026, 4, 1),
            grace_deadline=date(2026, 5, 31),
        )
        bank = BankRecord(
            id=20,
            party_id=10,
            counterparty_name="上海测试供应商",
            amount=Decimal("100.00"),
            transaction_date=date(2026, 5, 20),
            direction="outflow",
        )

        results = match_payables([invoice], [bank])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].match_type, "exact")
        self.assertEqual(results[0].status, "matched")
        self.assertEqual(results[0].invoice_ids, [1])
        self.assertEqual(results[0].bank_transaction_ids, [20])

    def test_monthly_summary_match_groups_same_party_and_month(self) -> None:
        invoices = [
            self._invoice(1, Decimal("60.00")),
            self._invoice(2, Decimal("40.00")),
        ]
        banks = [
            self._bank(11, Decimal("50.00")),
            self._bank(12, Decimal("50.00")),
        ]

        results = match_payables(invoices, banks)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].match_type, "monthly_summary")
        self.assertEqual(results[0].invoice_total, Decimal("100.00"))
        self.assertEqual(results[0].bank_total, Decimal("100.00"))
        self.assertCountEqual(results[0].invoice_ids, [1, 2])
        self.assertCountEqual(results[0].bank_transaction_ids, [11, 12])

    def test_unmatched_invoice_and_bank_are_reported(self) -> None:
        invoice = self._invoice(1, Decimal("100.00"))
        bank = self._bank(11, Decimal("80.00"))

        results = match_payables([invoice], [bank], probable_tolerance=Decimal("1.00"))
        statuses = [result.status for result in results]

        self.assertIn("unmatched_invoice", statuses)
        self.assertIn("unmatched_bank", statuses)

    def test_probable_match_for_small_amount_difference(self) -> None:
        invoice = self._invoice(1, Decimal("100.00"))
        bank = self._bank(11, Decimal("99.50"))

        results = match_payables([invoice], [bank], probable_tolerance=Decimal("1.00"))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].match_type, "probable")
        self.assertEqual(results[0].status, "probable")
        self.assertEqual(results[0].difference_amount, Decimal("0.50"))

    def test_late_exact_payment_still_matches_as_paid(self) -> None:
        invoice = self._invoice(1, Decimal("100.00"))
        bank = BankRecord(
            id=11,
            party_id=10,
            counterparty_name="上海测试供应商",
            amount=Decimal("100.00"),
            transaction_date=date(2026, 6, 25),
            direction="outflow",
        )

        results = match_payables([invoice], [bank])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].match_type, "exact_late")
        self.assertEqual(results[0].status, "matched")
        self.assertEqual(results[0].invoice_ids, [1])
        self.assertEqual(results[0].bank_transaction_ids, [11])

    def test_single_bank_payment_can_match_unique_multi_invoice_subset(self) -> None:
        invoices = [
            self._invoice(1, Decimal("55.00")),
            self._invoice(2, Decimal("45.00")),
            self._invoice(3, Decimal("20.00")),
        ]
        bank = self._bank(11, Decimal("100.00"))

        results = match_payables(invoices, [bank])
        matched = [result for result in results if result.status == "matched"]

        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].match_type, "invoice_subset")
        self.assertCountEqual(matched[0].invoice_ids, [1, 2])
        self.assertEqual(matched[0].bank_transaction_ids, [11])
        self.assertEqual(matched[0].invoice_total, Decimal("100.00"))
        self.assertEqual(matched[0].bank_total, Decimal("100.00"))

    def test_bank_payment_can_match_positive_and_negative_invoice_net_subset(self) -> None:
        invoices = [
            self._invoice(1, Decimal("100.00")),
            self._invoice(2, Decimal("-20.00")),
        ]
        bank = BankRecord(
            id=11,
            party_id=10,
            counterparty_name="上海测试供应商",
            amount=Decimal("80.00"),
            transaction_date=date(2026, 6, 25),
            direction="outflow",
        )

        results = match_payables(invoices, [bank])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "matched")
        self.assertEqual(results[0].match_type, "invoice_subset_late")
        self.assertCountEqual(results[0].invoice_ids, [1, 2])
        self.assertEqual(results[0].bank_transaction_ids, [11])
        self.assertEqual(results[0].invoice_total, Decimal("80.00"))
        self.assertEqual(results[0].bank_total, Decimal("80.00"))

    def test_equal_positive_and_negative_invoices_are_offset_before_bank_matching(self) -> None:
        invoices = [
            self._invoice(1, Decimal("290.12")),
            self._invoice(2, Decimal("-290.12")),
        ]

        results = match_receivables(invoices, [])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "matched")
        self.assertEqual(results[0].match_type, "red_offset")
        self.assertCountEqual(results[0].invoice_ids, [1, 2])
        self.assertEqual(results[0].bank_transaction_ids, [])
        self.assertEqual(results[0].invoice_total, Decimal("0.00"))
        self.assertEqual(results[0].bank_total, Decimal("0.00"))

    def test_same_party_same_amount_bank_before_invoice_is_probable_prepayment(self) -> None:
        invoice = InvoiceRecord(
            id=1,
            party_id=10,
            party_name="上海测试客户",
            amount=Decimal("410.80"),
            invoice_date=date(2026, 4, 3),
            due_date=date(2026, 5, 3),
            grace_deadline=date(2026, 5, 3),
        )
        bank = BankRecord(
            id=11,
            party_id=10,
            counterparty_name="上海测试客户",
            amount=Decimal("410.80"),
            transaction_date=date(2026, 3, 28),
            direction="inflow",
        )

        results = match_receivables([invoice], [bank])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "probable")
        self.assertEqual(results[0].match_type, "early_payment")
        self.assertEqual(results[0].invoice_ids, [1])
        self.assertEqual(results[0].bank_transaction_ids, [11])
        self.assertIn("早于发票日期", results[0].remark)

    def test_ambiguous_multi_invoice_subset_stays_for_manual_review(self) -> None:
        invoices = [
            self._invoice(1, Decimal("60.00")),
            self._invoice(2, Decimal("40.00")),
            self._invoice(3, Decimal("70.00")),
            self._invoice(4, Decimal("30.00")),
        ]
        bank = self._bank(11, Decimal("100.00"))

        results = match_payables(invoices, [bank])

        self.assertNotIn("invoice_subset", [result.match_type for result in results])
        self.assertIn("unmatched_bank", [result.status for result in results])

    def test_receivable_match_uses_bank_inflows_only(self) -> None:
        invoice = InvoiceRecord(
            id=1,
            party_id=10,
            party_name="上海测试客户",
            amount=Decimal("100.00"),
            invoice_date=date(2026, 3, 1),
            due_date=date(2026, 4, 1),
            grace_deadline=date(2026, 5, 31),
        )
        inflow = BankRecord(
            id=20,
            party_id=10,
            counterparty_name="上海测试客户",
            amount=Decimal("100.00"),
            transaction_date=date(2026, 5, 20),
            direction="inflow",
        )
        outflow = BankRecord(
            id=21,
            party_id=10,
            counterparty_name="上海测试客户",
            amount=Decimal("100.00"),
            transaction_date=date(2026, 5, 20),
            direction="outflow",
        )

        results = match_receivables([invoice], [outflow, inflow])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].match_type, "exact")
        self.assertEqual(results[0].status, "matched")
        self.assertEqual(results[0].bank_transaction_ids, [20])

    def _invoice(self, id_: int, amount: Decimal) -> InvoiceRecord:
        return InvoiceRecord(
            id=id_,
            party_id=10,
            party_name="上海测试供应商",
            amount=amount,
            invoice_date=date(2026, 3, 1),
            due_date=date(2026, 4, 1),
            grace_deadline=date(2026, 5, 31),
        )

    def _bank(self, id_: int, amount: Decimal) -> BankRecord:
        return BankRecord(
            id=id_,
            party_id=10,
            counterparty_name="上海测试供应商",
            amount=amount,
            transaction_date=date(2026, 5, 25),
            direction="outflow",
        )


if __name__ == "__main__":
    unittest.main()
