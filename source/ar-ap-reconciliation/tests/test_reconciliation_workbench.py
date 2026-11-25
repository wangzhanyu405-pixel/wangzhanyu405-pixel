from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from app import (
    build_workbench_status_matches,
    default_invoice_lookback_start,
    format_unmatched_bank_workbench_rows,
    format_workbench_detail_rows,
    load_reconciliation_rows,
    load_reconciliation_status_rows,
    run_local_payable_match,
    run_local_receivable_match,
)
from finance_platform.local_store import LocalStore
from finance_platform.matching import MatchResult


class ReconciliationWorkbenchScopeTest(unittest.TestCase):
    def test_payable_workbench_uses_evidence_window_for_cross_year_bank_payment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            supplier_id = store.upsert_party("supplier", "上海例己实业有限公司", default_term_days=21)
            invoice_id = store.insert_invoice(
                "payable",
                supplier_id,
                "上海例己实业有限公司",
                "P-2026-001",
                date(2025, 12, 15),
                Decimal("250.25"),
                term_days=37,
            )
            store.insert_invoice(
                "payable",
                supplier_id,
                "上海例己实业有限公司",
                "P-NEXT",
                date(2026, 7, 1),
                Decimal("200.00"),
                term_days=30,
            )
            bank_id = store.insert_bank_transaction(
                date(2025, 12, 30),
                None,
                Decimal("250.25"),
                "上海例己实业有限公司",
                "outflow",
                party_id=supplier_id,
            )

            invoices, banks = load_reconciliation_rows(store, "payable", date(2026, 1, 1), date(2026, 7, 8))
            matches = run_local_payable_match(invoices, banks)

        matched = [row for row in matches if row.status == "matched"]
        self.assertEqual([row["id"] for row in invoices if row["invoice_no"] == "P-2026-001"], [invoice_id])
        self.assertIn(bank_id, [row["id"] for row in banks])
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].invoice_ids, [invoice_id])
        self.assertEqual(matched[0].bank_transaction_ids, [bank_id])

    def test_receivable_workbench_uses_evidence_window_for_prior_year_invoice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            customer_id = store.upsert_party("customer", "上海示例丙积木科技有限公司", default_term_days=30)
            invoice_id = store.insert_invoice(
                "receivable",
                customer_id,
                "上海示例丙积木科技有限公司",
                "R-2025-001",
                date(2025, 12, 31),
                Decimal("350.75"),
                term_days=30,
            )
            bank_id = store.insert_bank_transaction(
                date(2026, 1, 26),
                Decimal("350.75"),
                None,
                "上海示例丙积木科技有限公司",
                "inflow",
                party_id=customer_id,
            )

            invoices, banks = load_reconciliation_rows(store, "receivable", date(2026, 1, 1), date(2026, 7, 8))
            matches = run_local_receivable_match(invoices, banks)

        self.assertEqual([row["id"] for row in invoices], [invoice_id])
        self.assertEqual([row["id"] for row in banks], [bank_id])
        self.assertEqual([row.invoice_ids for row in matches if row.status == "matched"], [[invoice_id]])

    def test_workbench_excludes_confirmed_matched_rows_but_keeps_partial_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            supplier_id = store.upsert_party("supplier", "上海重复运行供应商", default_term_months=1, grace_months=1)
            matched_invoice_id = store.insert_invoice(
                "payable",
                supplier_id,
                "上海重复运行供应商",
                "P-MATCHED",
                date(2026, 4, 1),
                Decimal("100.00"),
                1,
                1,
            )
            partial_invoice_id = store.insert_invoice(
                "payable",
                supplier_id,
                "上海重复运行供应商",
                "P-PARTIAL",
                date(2026, 4, 2),
                Decimal("99.50"),
                1,
                1,
            )
            open_invoice_id = store.insert_invoice(
                "payable",
                supplier_id,
                "上海重复运行供应商",
                "P-OPEN",
                date(2026, 4, 3),
                Decimal("80.00"),
                1,
                1,
            )
            matched_bank_id = store.insert_bank_transaction(
                date(2026, 6, 20),
                None,
                Decimal("100.00"),
                "上海重复运行供应商",
                "outflow",
                party_id=supplier_id,
            )
            partial_bank_id = store.insert_bank_transaction(
                date(2026, 6, 21),
                None,
                Decimal("99.00"),
                "上海重复运行供应商",
                "outflow",
                party_id=supplier_id,
            )
            open_bank_id = store.insert_bank_transaction(
                date(2026, 6, 22),
                None,
                Decimal("80.00"),
                "上海重复运行供应商",
                "outflow",
                party_id=supplier_id,
            )
            store.save_match_results(
                "payable",
                [
                    MatchResult(
                        match_type="exact",
                        status="matched",
                        party_id=supplier_id,
                        party_name="上海重复运行供应商",
                        invoice_ids=[matched_invoice_id],
                        bank_transaction_ids=[matched_bank_id],
                        invoice_total=Decimal("100.00"),
                        bank_total=Decimal("100.00"),
                        difference_amount=Decimal("0.00"),
                        confidence=Decimal("1.00"),
                    ),
                    MatchResult(
                        match_type="probable",
                        status="probable",
                        party_id=supplier_id,
                        party_name="上海重复运行供应商",
                        invoice_ids=[partial_invoice_id],
                        bank_transaction_ids=[partial_bank_id],
                        invoice_total=Decimal("99.50"),
                        bank_total=Decimal("99.00"),
                        difference_amount=Decimal("0.50"),
                        confidence=Decimal("0.75"),
                    ),
                ],
            )

            invoices, banks = load_reconciliation_rows(store, "payable", date(2026, 6, 1), date(2026, 6, 30))

        self.assertEqual({row["id"] for row in invoices}, {partial_invoice_id, open_invoice_id})
        self.assertEqual({row["id"] for row in banks}, {partial_bank_id, open_bank_id})

    def test_default_invoice_lookback_starts_from_previous_year_beginning(self) -> None:
        self.assertEqual(default_invoice_lookback_start(date(2026, 6, 1)), date(2025, 1, 1))

    def test_payable_workbench_excludes_non_reconciliation_bank_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            supplier_id = store.upsert_party("supplier", "上海对账供应商", default_term_months=1, grace_months=1)
            store.insert_invoice(
                direction="payable",
                party_id=supplier_id,
                party_name_raw="上海对账供应商",
                invoice_no="P-1",
                invoice_date=date(2026, 5, 1),
                invoice_amount=Decimal("100.00"),
                term_months=1,
                grace_months=1,
            )
            business_bank_id = store.insert_bank_transaction(
                transaction_date=date(2026, 6, 20),
                income_amount=None,
                expense_amount=Decimal("100.00"),
                counterparty_name_raw="上海对账供应商",
                direction="outflow",
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 20),
                income_amount=None,
                expense_amount=Decimal("51.00"),
                counterparty_name_raw="待报解预算收入-国库信息处理系统扣税",
                direction="outflow",
                summary="公共缴费",
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 21),
                income_amount=None,
                expense_amount=Decimal("200.00"),
                counterparty_name_raw="李甲",
                direction="outflow",
                summary="转存",
            )

            _, banks = load_reconciliation_rows(store, "payable", date(2026, 6, 1), date(2026, 6, 30))

        self.assertEqual([row["id"] for row in banks], [business_bank_id])

    def test_payable_workbench_excludes_invoices_outside_party_master(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            supplier_id = store.upsert_party("supplier", "上海名单内供应商", default_term_months=1, grace_months=1)
            included_id = store.insert_invoice(
                direction="payable",
                party_id=supplier_id,
                party_name_raw="上海名单内供应商",
                invoice_no="P-IN",
                invoice_date=date(2026, 5, 1),
                invoice_amount=Decimal("100.00"),
                term_months=1,
                grace_months=1,
            )
            store.insert_invoice(
                direction="payable",
                party_id=None,
                party_name_raw="上海名单外一次性采购",
                invoice_no="P-OUT",
                invoice_date=date(2026, 5, 1),
                invoice_amount=Decimal("50.00"),
                term_months=1,
                grace_months=1,
            )

            invoices, _ = load_reconciliation_rows(store, "payable", date(2026, 6, 1), date(2026, 6, 30))

        self.assertEqual([row["id"] for row in invoices], [included_id])

    def test_status_view_keeps_confirmed_payable_match_visible_after_incremental_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            supplier_id = store.upsert_party("supplier", "上海已付供应商", default_term_months=1, grace_months=1)
            invoice_id = store.insert_invoice(
                direction="payable",
                party_id=supplier_id,
                party_name_raw="上海已付供应商",
                invoice_no="P-MATCHED",
                invoice_date=date(2026, 5, 1),
                invoice_amount=Decimal("100.00"),
                term_months=1,
                grace_months=1,
            )
            bank_id = store.insert_bank_transaction(
                transaction_date=date(2026, 6, 20),
                income_amount=None,
                expense_amount=Decimal("100.00"),
                counterparty_name_raw="上海已付供应商",
                direction="outflow",
                party_id=supplier_id,
            )
            store.save_match_results(
                "payable",
                [
                    MatchResult(
                        match_type="exact",
                        status="matched",
                        party_id=supplier_id,
                        party_name="上海已付供应商",
                        invoice_ids=[invoice_id],
                        bank_transaction_ids=[bank_id],
                        invoice_total=Decimal("100.00"),
                        bank_total=Decimal("100.00"),
                        difference_amount=Decimal("0.00"),
                        confidence=Decimal("1.00"),
                    )
                ],
            )

            incremental_invoices, incremental_banks = load_reconciliation_rows(
                store,
                "payable",
                date(2026, 6, 1),
                date(2026, 6, 30),
            )
            status_invoices, status_banks = load_reconciliation_status_rows(
                store,
                "payable",
                date(2026, 6, 1),
                date(2026, 6, 30),
            )
            status_matches = build_workbench_status_matches(
                "payable",
                status_invoices,
                status_banks,
                store.list_match_results("payable", limit=1000),
            )

        rows = format_workbench_detail_rows("payable", status_invoices, status_banks, status_matches)

        self.assertEqual(incremental_invoices, [])
        self.assertEqual(incremental_banks, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["往来单位"], "上海已付供应商")
        self.assertEqual(rows[0]["发票号"], "P-MATCHED")
        self.assertEqual(rows[0]["实付日期"], "2026-06-20")
        self.assertEqual(rows[0]["付款金额"], "¥100.00")
        self.assertEqual(rows[0]["状态"], "已付款")

    def test_status_view_shows_unmatched_receivable_invoice_and_bank_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            customer_id = store.upsert_party("customer", "上海待收客户", default_term_months=1, grace_months=1)
            store.insert_invoice(
                direction="receivable",
                party_id=customer_id,
                party_name_raw="上海待收客户",
                invoice_no="R-OPEN",
                invoice_date=date(2026, 5, 1),
                invoice_amount=Decimal("120.00"),
                term_months=1,
                grace_months=1,
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 20),
                income_amount=Decimal("80.00"),
                expense_amount=None,
                counterparty_name_raw="上海待收客户",
                direction="inflow",
                party_id=customer_id,
            )

            status_invoices, status_banks = load_reconciliation_status_rows(
                store,
                "receivable",
                date(2026, 6, 1),
                date(2026, 6, 30),
            )
            status_matches = build_workbench_status_matches(
                "receivable",
                status_invoices,
                status_banks,
                store.list_match_results("receivable", limit=1000),
            )

        rows = format_workbench_detail_rows("receivable", status_invoices, status_banks, status_matches)
        bank_only_rows = format_unmatched_bank_workbench_rows("receivable", status_banks, status_matches)
        statuses = {row["状态"] for row in rows}
        bank_only_statuses = {row["状态"] for row in bank_only_rows}

        self.assertEqual(len(rows), 1)
        self.assertEqual(len(bank_only_rows), 1)
        self.assertIn("未收款", statuses)
        self.assertIn("收款未找到发票", bank_only_statuses)


if __name__ == "__main__":
    unittest.main()
