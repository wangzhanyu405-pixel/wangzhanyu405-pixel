from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from finance_platform.local_store import LocalStore, insert_parsed_bank_transactions, insert_parsed_invoices
from finance_platform.matching import MatchResult
from finance_platform.payment_template import build_payment_candidates
from finance_platform.parsers import ParsedBankTransaction, ParsedInvoice


class LocalStoreTest(unittest.TestCase):
    def test_dashboard_summary_uses_local_invoices_and_bank_transactions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party("supplier", "上海测试供应商", default_term_months=2, grace_months=1)
            store.insert_invoice(
                direction="payable",
                party_id=party_id,
                party_name_raw="上海测试供应商",
                invoice_no="INV-1",
                invoice_date=date(2026, 3, 1),
                invoice_amount=Decimal("100.00"),
                term_months=2,
                grace_months=1,
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 30),
                income_amount=None,
                expense_amount=Decimal("100.00"),
                counterparty_name_raw="上海测试供应商",
                direction="outflow",
                party_id=party_id,
            )

            summary = store.dashboard_summary(date(2026, 3, 1), date(2026, 6, 30))

            self.assertEqual(summary["payable_amount"], Decimal("100.00"))
            self.assertEqual(summary["paid_amount"], Decimal("100.00"))
            self.assertEqual(summary["payable_count"], 1)
            self.assertEqual(summary["bank_outflow_count"], 1)

    def test_party_totals_use_invoice_direction_for_receivable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            customer_id = store.upsert_party("customer", "上海测试客户", default_term_days=30)
            store.insert_invoice(
                direction="receivable",
                party_id=customer_id,
                party_name_raw="上海测试客户",
                invoice_no="R-1",
                invoice_date=date(2026, 6, 1),
                invoice_amount=Decimal("1000.00"),
                term_days=30,
            )
            store.insert_invoice(
                direction="receivable",
                party_id=None,
                party_name_raw="未建档客户",
                invoice_no="R-OTHER",
                invoice_date=date(2026, 6, 2),
                invoice_amount=Decimal("9999.00"),
                term_days=30,
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 20),
                income_amount=Decimal("300.00"),
                expense_amount=None,
                counterparty_name_raw="上海测试客户",
                direction="inflow",
                party_id=customer_id,
            )

            rows = store.party_totals("receivable", date(2026, 6, 1), date(2026, 6, 30))

            self.assertEqual(rows, [{"往来单位": "上海测试客户", "金额": Decimal("1000.00")}])

    def test_party_totals_for_payments_filter_supplier_payment_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            supplier_id = store.upsert_party("supplier", "上海测试供应商", default_term_days=30)
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
                expense_amount=Decimal("0.90"),
                counterparty_name_raw="上海测试供应商",
                summary="费用外收",
                direction="outflow",
                party_id=supplier_id,
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 7),
                income_amount=None,
                expense_amount=Decimal("50.00"),
                counterparty_name_raw="待报解预算收入-国库信息处理系统扣税",
                summary="公共缴费",
                direction="outflow",
            )

            rows = store.party_totals("outflow", date(2026, 6, 1), date(2026, 6, 30))

            self.assertEqual(rows, [{"往来单位": "上海测试供应商", "金额": Decimal("100.00")}])

    def test_party_totals_for_receipts_filter_customer_receipt_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            customer_id = store.upsert_party("customer", "上海测试客户", default_term_days=30)
            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 5),
                income_amount=Decimal("200.00"),
                expense_amount=None,
                counterparty_name_raw="上海测试客户",
                direction="inflow",
                party_id=customer_id,
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 6),
                income_amount=Decimal("30.00"),
                expense_amount=None,
                counterparty_name_raw="上海未建档公司",
                direction="inflow",
            )

            rows = store.party_totals("inflow", date(2026, 6, 1), date(2026, 6, 30))

            self.assertEqual(rows, [{"往来单位": "上海测试客户", "金额": Decimal("200.00")}])

    def test_payment_template_can_be_deleted_and_reimported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            template = store.get_payment_template("ABC")

            store.delete_payment_template(int(template["id"]))
            self.assertEqual(store.list_payment_templates(), [])

            template_id = store.upsert_payment_template(
                bank_code="ABC",
                bank_name="中国农业银行",
                template_name=template["template_name"],
                sheet_name="页一【批量转账文件】",
                default_purpose="货款",
                columns=["编号", "收款方账号", "金额"],
            )
            templates = store.list_payment_templates()

            self.assertEqual(template_id, int(template["id"]))
            self.assertEqual(len(templates), 1)
            self.assertEqual(templates[0]["columns"], ["编号", "收款方账号", "金额"])

    def test_party_alias_resolves_bank_counterparty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party("supplier", "上海测试供应商", default_term_months=2, grace_months=1)
            store.add_alias(party_id, "测试供应商银行户名", "bank")

            self.assertEqual(store.resolve_party_id("测试供应商银行户名"), party_id)

    def test_save_match_results_records_history_and_updates_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party("supplier", "上海测试供应商", default_term_months=2, grace_months=1)
            invoice_id = store.insert_invoice(
                direction="payable",
                party_id=party_id,
                party_name_raw="上海测试供应商",
                invoice_no="INV-1",
                invoice_date=date(2026, 3, 1),
                invoice_amount=Decimal("100.00"),
                term_months=2,
                grace_months=1,
            )
            bank_id = store.insert_bank_transaction(
                transaction_date=date(2026, 6, 30),
                income_amount=None,
                expense_amount=Decimal("100.00"),
                counterparty_name_raw="上海测试供应商",
                direction="outflow",
                party_id=party_id,
            )
            match = MatchResult(
                match_type="exact",
                status="matched",
                party_id=party_id,
                party_name="上海测试供应商",
                invoice_ids=[invoice_id],
                bank_transaction_ids=[bank_id],
                invoice_total=Decimal("100.00"),
                bank_total=Decimal("100.00"),
                difference_amount=Decimal("0.00"),
                confidence=Decimal("1.00"),
                remark="测试匹配",
            )

            run_no = store.save_match_results("payable", [match])

            history = store.list_match_results("payable", limit=10)
            invoices = store.list_invoices("payable")
            banks = store.list_bank_transactions("outflow")
            candidates = build_payment_candidates(store, date(2026, 6, 1), date(2026, 6, 30))

            self.assertTrue(run_no.startswith("payable-"))
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["run_no"], run_no)
            self.assertEqual(invoices[0]["status"], "matched")
            self.assertEqual(banks[0]["match_status"], "matched")
            self.assertEqual(candidates, [])

    def test_save_manual_settlement_marks_invoice_and_excludes_payment_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party("supplier", "上海现金供应商", default_term_days=30)
            invoice_id = store.insert_invoice(
                direction="payable",
                party_id=party_id,
                party_name_raw="上海现金供应商",
                invoice_no="CASH-1",
                invoice_date=date(2026, 1, 1),
                invoice_amount=Decimal("500.00"),
                term_days=30,
            )

            run_no = store.save_manual_settlement(
                direction="payable",
                invoice_id=invoice_id,
                settlement_method="cash",
                settlement_date=date(2026, 1, 20),
                amount=Decimal("500.00"),
                remark="现金支付",
            )

            invoices = store.list_invoices("payable")
            history = store.list_match_results("payable", limit=10)
            candidates = build_payment_candidates(store, date(2026, 1, 1), date(2026, 1, 31))

        self.assertTrue(run_no.startswith("payable-manual-"))
        self.assertEqual(invoices[0]["status"], "matched")
        self.assertEqual(history[0]["match_type"], "manual_cash")
        self.assertEqual(history[0]["settlement_method"], "cash")
        self.assertEqual(history[0]["settlement_date"], "2026-01-20")
        self.assertEqual(candidates, [])

    def test_unmatched_result_does_not_downgrade_confirmed_match_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party("supplier", "上海测试供应商", default_term_months=2, grace_months=1)
            invoice_id = store.insert_invoice(
                direction="payable",
                party_id=party_id,
                party_name_raw="上海测试供应商",
                invoice_no="INV-1",
                invoice_date=date(2026, 3, 1),
                invoice_amount=Decimal("100.00"),
                term_months=2,
                grace_months=1,
            )
            bank_id = store.insert_bank_transaction(
                transaction_date=date(2026, 6, 30),
                income_amount=None,
                expense_amount=Decimal("100.00"),
                counterparty_name_raw="上海测试供应商",
                direction="outflow",
                party_id=party_id,
            )
            matched = MatchResult(
                match_type="exact",
                status="matched",
                party_id=party_id,
                party_name="上海测试供应商",
                invoice_ids=[invoice_id],
                bank_transaction_ids=[bank_id],
                invoice_total=Decimal("100.00"),
                bank_total=Decimal("100.00"),
                difference_amount=Decimal("0.00"),
                confidence=Decimal("1.00"),
            )
            unmatched = MatchResult(
                match_type="unmatched",
                status="unmatched_invoice",
                party_id=party_id,
                party_name="上海测试供应商",
                invoice_ids=[invoice_id],
                bank_transaction_ids=[],
                invoice_total=Decimal("100.00"),
                bank_total=Decimal("0.00"),
                difference_amount=Decimal("100.00"),
                confidence=Decimal("0.00"),
            )

            store.save_match_results("payable", [matched], run_no="matched-run")
            store.save_match_results("payable", [unmatched], run_no="narrow-window-run")

            invoices = store.list_invoices("payable")
            banks = store.list_bank_transactions("outflow")
            candidates = build_payment_candidates(store, date(2026, 6, 1), date(2026, 6, 30))

            self.assertEqual(invoices[0]["status"], "matched")
            self.assertEqual(banks[0]["match_status"], "matched")
            self.assertEqual(candidates, [])

    def test_update_party_fields_supports_batch_maintenance_and_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party("supplier", "上海测试供应商", default_term_days=30)

            store.update_party_fields(
                party_id,
                default_term_days=90,
                bank_account="123456",
                bank_name="中国农业银行股份有限公司上海测试支行",
                alias_name="测试供应商新别名",
            )

            party = store.get_party(party_id)
            self.assertEqual(party["default_term_days"], 90)
            self.assertEqual(party["bank_account"], "123456")
            self.assertEqual(party["bank_name"], "中国农业银行股份有限公司上海测试支行")
            self.assertEqual(store.resolve_party_id("测试供应商新别名"), party_id)

    def test_update_party_identity_supports_direct_name_and_type_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party("supplier", "上海旧名称供应商", default_term_days=30)

            store.update_party_identity(party_id, party_type="customer", name="上海新名称客户")

            party = store.get_party(party_id)
            self.assertEqual(party["party_type"], "customer")
            self.assertEqual(party["name"], "上海新名称客户")
            self.assertEqual(store.resolve_party_id("上海新名称客户", preferred_party_type="customer"), party_id)

    def test_invoice_due_date_uses_term_days_without_grace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party("supplier", "上海账期天数供应商", default_term_days=90)

            store.insert_invoice(
                direction="payable",
                party_id=party_id,
                party_name_raw="上海账期天数供应商",
                invoice_no="P-DAYS",
                invoice_date=date(2026, 3, 1),
                invoice_amount=Decimal("100.00"),
                term_days=30,
            )

            invoice = store.list_invoices("payable")[0]
            self.assertEqual(invoice["term_days"], 90)
            self.assertEqual(invoice["due_date"], "2026-05-30")
            self.assertEqual(invoice["grace_deadline"], "2026-05-30")

    def test_insert_parsed_receivable_invoice_keeps_unknown_party_unlinked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            row = ParsedInvoice(
                direction="receivable",
                party_name_raw="上海测试客户",
                invoice_no="R-1",
                invoice_date=date(2026, 4, 1),
                invoice_amount=Decimal("100.00"),
                term_months=1,
                grace_months=1,
                due_date=date(2026, 5, 1),
                grace_deadline=date(2026, 6, 30),
                source_sheet="Sheet1",
                source_row_no=1,
            )

            insert_parsed_invoices(store, [row])

            invoices = store.list_invoices("receivable")
            self.assertIsNone(store.resolve_party_id("上海测试客户"))
            self.assertIsNone(invoices[0]["party_id"])
            self.assertEqual(invoices[0]["party_name_raw"], "上海测试客户")

    def test_insert_parsed_invoice_links_existing_formal_party_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party("customer", "上海测试客户")
            row = ParsedInvoice(
                direction="receivable",
                party_name_raw="上海测试客户",
                invoice_no="R-1",
                invoice_date=date(2026, 4, 1),
                invoice_amount=Decimal("100.00"),
                term_months=1,
                grace_months=1,
                due_date=date(2026, 5, 1),
                grace_deadline=date(2026, 6, 30),
                source_sheet="Sheet1",
                source_row_no=1,
            )

            insert_parsed_invoices(store, [row])

            invoices = store.list_invoices("receivable")
            self.assertEqual(invoices[0]["party_id"], party_id)

    def test_insert_parsed_invoice_persists_tax_export_red_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            row = ParsedInvoice(
                direction="payable",
                party_name_raw="上海测试供应商",
                invoice_no="RED-1",
                invoice_date=date(2026, 5, 20),
                invoice_amount=Decimal("-123.45"),
                term_months=1,
                grace_months=1,
                due_date=date(2026, 6, 20),
                grace_deadline=date(2026, 7, 31),
                source_sheet="发票基础信息",
                source_row_no=3,
                invoice_status_raw="已红冲-全额",
                is_positive=False,
                original_invoice_no="BLUE-1",
                red_confirmation_no="90000000000000000016",
                invoice_type="电子发票",
                amount_without_tax=Decimal("-109.25"),
                tax_amount=Decimal("-14.20"),
                remark="红冲测试",
            )

            insert_parsed_invoices(store, [row])

            invoice = store.list_invoices("payable")[0]
            self.assertEqual(invoice["invoice_status_raw"], "已红冲-全额")
            self.assertEqual(invoice["is_positive"], 0)
            self.assertEqual(invoice["original_invoice_no"], "BLUE-1")
            self.assertEqual(invoice["red_confirmation_no"], "90000000000000000016")
            self.assertEqual(invoice["invoice_type"], "电子发票")
            self.assertEqual(invoice["amount_without_tax"], "-109.25")
            self.assertEqual(invoice["tax_amount"], "-14.20")
            self.assertEqual(invoice["remark"], "红冲测试")

    def test_insert_parsed_receivable_invoice_does_not_create_customer_when_only_supplier_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            supplier_id = store.upsert_party("supplier", "上海双向往来单位")
            row = ParsedInvoice(
                direction="receivable",
                party_name_raw="上海双向往来单位",
                invoice_no="R-2",
                invoice_date=date(2026, 5, 1),
                invoice_amount=Decimal("120.00"),
                term_months=1,
                grace_months=1,
                due_date=date(2026, 6, 1),
                grace_deadline=date(2026, 7, 31),
                source_sheet="Sheet1",
                source_row_no=1,
            )

            insert_parsed_invoices(store, [row])

            invoices = store.list_invoices("receivable")
            self.assertIsNone(invoices[0]["party_id"])
            self.assertEqual(store.resolve_party_id("上海双向往来单位", preferred_party_type="supplier"), supplier_id)

    def test_rebuild_party_links_only_uses_existing_formal_parties(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party("customer", "上海测试客户")
            store.insert_invoice(
                direction="receivable",
                party_id=None,
                party_name_raw="上海测试客户",
                invoice_no="R-OLD",
                invoice_date=date(2026, 4, 1),
                invoice_amount=Decimal("100.00"),
                term_months=1,
                grace_months=1,
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 1),
                income_amount=Decimal("100.00"),
                expense_amount=None,
                counterparty_name_raw="上海测试客户",
                direction="inflow",
            )

            store.rebuild_party_links()

            invoice = store.list_invoices("receivable")[0]
            bank = store.list_bank_transactions("inflow")[0]
            self.assertEqual(invoice["party_id"], party_id)
            self.assertEqual(bank["party_id"], party_id)
            self.assertEqual(bank["business_category"], "customer_receipt")

    def test_rebuild_party_links_returns_visible_relink_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            store.insert_invoice(
                direction="payable",
                party_id=None,
                party_name_raw="上海测试供应商",
                invoice_no="P-OLD",
                invoice_date=date(2026, 4, 1),
                invoice_amount=Decimal("100.00"),
                term_days=30,
            )
            store.insert_invoice(
                direction="receivable",
                party_id=None,
                party_name_raw="上海测试客户",
                invoice_no="R-OLD",
                invoice_date=date(2026, 4, 1),
                invoice_amount=Decimal("200.00"),
                term_days=30,
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 5, 1),
                income_amount=None,
                expense_amount=Decimal("100.00"),
                counterparty_name_raw="上海测试供应商",
                direction="outflow",
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 5, 2),
                income_amount=Decimal("200.00"),
                expense_amount=None,
                counterparty_name_raw="上海测试客户",
                direction="inflow",
            )
            store.upsert_party("supplier", "上海测试供应商")
            store.upsert_party("customer", "上海测试客户")

            result = store.rebuild_party_links()

            self.assertEqual(
                result,
                {
                    "payable_invoices": 1,
                    "receivable_invoices": 1,
                    "bank_transactions": 2,
                },
            )

    def test_rebuild_party_links_does_not_create_missing_parties(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            store.insert_invoice(
                direction="receivable",
                party_id=None,
                party_name_raw="上海名单外客户",
                invoice_no="R-OLD",
                invoice_date=date(2026, 4, 1),
                invoice_amount=Decimal("100.00"),
                term_months=1,
                grace_months=1,
            )

            store.rebuild_party_links()

            invoice = store.list_invoices("receivable")[0]
            self.assertIsNone(store.resolve_party_id("上海名单外客户"))
            self.assertIsNone(invoice["party_id"])

    def test_archive_party_removes_formal_party_without_deleting_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party("supplier", "上海一次性采购")
            invoice_id = store.insert_invoice(
                direction="payable",
                party_id=party_id,
                party_name_raw="上海一次性采购",
                invoice_no="P-1",
                invoice_date=date(2026, 4, 1),
                invoice_amount=Decimal("100.00"),
                term_months=1,
                grace_months=1,
            )

            store.archive_party(party_id)

            self.assertEqual(store.list_parties(), [])
            invoice = store.list_invoices("payable")[0]
            self.assertEqual(invoice["id"], invoice_id)
            self.assertIsNone(invoice["party_id"])

    def test_archive_all_parties_clears_formal_master_without_deleting_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            supplier_id = store.upsert_party("supplier", "上海测试供应商")
            customer_id = store.upsert_party("customer", "上海测试客户")
            store.insert_invoice(
                direction="payable",
                party_id=supplier_id,
                party_name_raw="上海测试供应商",
                invoice_no="P-1",
                invoice_date=date(2026, 4, 1),
                invoice_amount=Decimal("100.00"),
                term_months=1,
                grace_months=1,
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 1),
                income_amount=Decimal("200.00"),
                expense_amount=None,
                counterparty_name_raw="上海测试客户",
                direction="inflow",
                party_id=customer_id,
            )

            archived = store.archive_all_parties()

            self.assertEqual(archived, 2)
            self.assertEqual(store.list_parties(), [])
            self.assertIsNone(store.list_invoices("payable")[0]["party_id"])
            self.assertIsNone(store.list_bank_transactions("inflow")[0]["party_id"])

    def test_bank_inflow_prefers_customer_when_party_name_exists_as_supplier_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            store.upsert_party("supplier", "上海双向往来单位")
            customer_id = store.upsert_party("customer", "上海双向往来单位")

            store.insert_bank_transaction(
                transaction_date=date(2026, 5, 25),
                income_amount=Decimal("100.00"),
                expense_amount=None,
                counterparty_name_raw="上海双向往来单位",
                direction="inflow",
            )

            bank = store.list_bank_transactions("inflow")[0]
            self.assertEqual(bank["party_id"], customer_id)
            self.assertEqual(bank["party_type"], "customer")
            self.assertEqual(bank["business_category"], "customer_receipt")

    def test_unidentified_bank_fuzzy_candidate_requires_manual_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            customer_id = store.upsert_party("customer", "示例甲包装品贸易（上海）有限公司")
            bank_id = store.insert_bank_transaction(
                transaction_date=date(2026, 5, 25),
                income_amount=Decimal("2100.50"),
                expense_amount=None,
                counterparty_name_raw="示例甲包装（上海）有限公司",
                direction="inflow",
            )

            before = store.list_bank_transactions("inflow")[0]
            candidates = store.unidentified_bank_fuzzy_matches(date(2026, 5, 1), date(2026, 5, 31))

            self.assertIsNone(before["party_id"])
            self.assertEqual(before["business_category"], "company_candidate")
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["bank_transaction_id"], bank_id)
            self.assertEqual(candidates[0]["suggested_party_id"], customer_id)
            self.assertEqual(candidates[0]["suggested_party_type"], "customer")
            self.assertGreaterEqual(candidates[0]["score"], 0.72)

            store.confirm_bank_fuzzy_match(bank_id, customer_id)

            after = store.list_bank_transactions("inflow")[0]
            self.assertEqual(after["party_id"], customer_id)
            self.assertEqual(after["party_type"], "customer")
            self.assertEqual(after["business_category"], "customer_receipt")
            self.assertEqual(store.resolve_party_id("示例甲包装（上海）有限公司", preferred_party_type="customer"), customer_id)

    def test_promote_bank_counterparty_creates_party_and_relinks_bank(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            bank_id = store.insert_bank_transaction(
                transaction_date=date(2026, 5, 25),
                income_amount=None,
                expense_amount=Decimal("100.00"),
                counterparty_name_raw="上海新建材料有限公司",
                counterparty_account="6222001",
                counterparty_bank="中国农业银行上海支行",
                direction="outflow",
            )

            party_id = store.promote_bank_counterparty(bank_id, "supplier", term_days=60)
            bank = store.list_bank_transactions("outflow")[0]
            party = store.get_party(party_id)

            self.assertEqual(party["party_type"], "supplier")
            self.assertEqual(party["name"], "上海新建材料有限公司")
            self.assertEqual(party["default_term_days"], 60)
            self.assertEqual(party["bank_account"], "6222001")
            self.assertEqual(bank["party_id"], party_id)
            self.assertEqual(bank["business_category"], "supplier_payment")

    def test_promote_bank_counterparty_group_relinks_same_name_same_direction_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            selected_id = store.insert_bank_transaction(
                transaction_date=date(2026, 5, 25),
                income_amount=None,
                expense_amount=Decimal("100.00"),
                counterparty_name_raw="上海批量供应商有限公司",
                counterparty_account="6222001",
                counterparty_bank="中国农业银行上海支行",
                direction="outflow",
            )
            other_same_name = store.insert_bank_transaction(
                transaction_date=date(2026, 5, 26),
                income_amount=None,
                expense_amount=Decimal("200.00"),
                counterparty_name_raw="上海批量供应商有限公司",
                counterparty_account="6222001",
                counterparty_bank="中国农业银行上海支行",
                direction="outflow",
            )
            inbound_same_name = store.insert_bank_transaction(
                transaction_date=date(2026, 5, 27),
                income_amount=Decimal("300.00"),
                expense_amount=None,
                counterparty_name_raw="上海批量供应商有限公司",
                direction="inflow",
            )

            result = store.promote_bank_counterparty_group(selected_id, "supplier", term_days=60)
            outflows = store.list_bank_transactions("outflow")
            inflow = store.list_bank_transactions("inflow")[0]

            self.assertEqual(result["linked_bank_transactions"], 2)
            self.assertEqual({row["id"] for row in outflows}, {selected_id, other_same_name})
            self.assertTrue(all(row["party_id"] == result["party_id"] for row in outflows))
            self.assertTrue(all(row["business_category"] == "supplier_payment" for row in outflows))
            self.assertIsNone(inflow["party_id"])
            self.assertEqual(inflow["id"], inbound_same_name)

    def test_payment_template_config_persists_abc_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "finance.db"
            store = LocalStore(db_path)
            store.init()

            template = store.get_payment_template("ABC")

            self.assertEqual(template["bank_code"], "ABC")
            self.assertEqual(template["bank_name"], "中国农业银行")
            self.assertIn("收款方账号", template["columns"])

            store.save_payment_template(
                int(template["id"]),
                template_name="农行批量付款-生产",
                sheet_name="页一【批量转账文件】",
                default_purpose="货款",
                columns=["收款方户名", "收款方账号", "金额", "用途（附言）"],
            )

            reopened = LocalStore(db_path)
            reopened.init()
            saved = reopened.get_payment_template("ABC")

            self.assertEqual(saved["template_name"], "农行批量付款-生产")
            self.assertEqual(saved["default_purpose"], "货款")
            self.assertEqual(saved["columns"], ["收款方户名", "收款方账号", "金额", "用途（附言）"])

    def test_import_bank_transactions_skips_cross_file_duplicate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            source_one = store.record_import_file("2026-06", "bank_statement", "全年流水.xlsx", None, "hash-one", 2, 2, 0)
            source_two = store.record_import_file("2026-06", "bank_statement", "六月流水.xls", None, "hash-two", 1, 1, 0)
            row = ParsedBankTransaction(
                transaction_date=date(2026, 6, 25),
                income_amount=Decimal("1515.50"),
                expense_amount=None,
                counterparty_account="000000000000013",
                counterparty_name_raw="上海示例丙积木科技有限公司",
                counterparty_bank="招商银行股份有限公司上海测试支行",
                subject="",
                summary="转存",
                direction="inflow",
                source_sheet="Sheet1",
                source_row_no=1,
            )

            inserted_one = insert_parsed_bank_transactions(store, [row, row], source_one)
            inserted_two = insert_parsed_bank_transactions(store, [row], source_two)

            banks = store.list_bank_transactions("inflow", date(2026, 6, 1), date(2026, 6, 30))
            self.assertEqual(inserted_one, 2)
            self.assertEqual(inserted_two, 0)
            self.assertEqual(len(banks), 2)

    def test_import_detail_row_count_tracks_failed_and_completed_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            bank_source = store.record_import_file("2026-06", "bank_statement", "银行流水.xlsx", None, "hash-bank", 1, 1, 0)
            invoice_source = store.record_import_file("2026-06", "payable_invoice", "发票.xlsx", None, "hash-invoice", 1, 1, 0)

            self.assertEqual(store.count_import_detail_rows("bank_statement", bank_source), 0)
            self.assertEqual(store.count_import_detail_rows("payable_invoice", invoice_source), 0)

            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 25),
                income_amount=Decimal("100.00"),
                expense_amount=None,
                counterparty_name_raw="上海测试客户",
                direction="inflow",
                source_file_id=bank_source,
            )
            store.insert_invoice(
                direction="payable",
                party_id=None,
                party_name_raw="上海测试供应商",
                invoice_no="INV-1",
                invoice_date=date(2026, 6, 1),
                invoice_amount=Decimal("80.00"),
                term_months=1,
                grace_months=1,
                source_file_id=invoice_source,
            )

            self.assertEqual(store.count_import_detail_rows("bank_statement", bank_source), 1)
            self.assertEqual(store.count_import_detail_rows("payable_invoice", invoice_source), 1)

    def test_import_saved_count_is_visible_in_recent_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            source_id = store.record_import_file("2026-06", "bank_statement", "银行流水.xlsx", None, "hash-bank", 10, 9, 0)

            self.assertEqual(store.recent_imports(1)[0]["saved_count"], 0)

            store.update_import_saved_count(source_id, 7)

            self.assertEqual(store.recent_imports(1)[0]["saved_count"], 7)

    def test_import_parse_counts_can_be_refreshed_for_existing_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            source_id = store.record_import_file("2026-06", "bank_statement", "银行流水.xlsx", "old.xlsx", "hash-bank", 10, 9, 0)

            store.update_import_parse_counts(
                source_id,
                period_month="2026-07",
                stored_path="new.xlsx",
                row_count=12,
                parsed_count=11,
                error_count=1,
            )

            row = store.recent_imports(1)[0]
            self.assertEqual(row["period_month"], "2026-07")
            self.assertEqual(row["stored_path"], "new.xlsx")
            self.assertEqual(row["row_count"], 12)
            self.assertEqual(row["parsed_count"], 11)
            self.assertEqual(row["error_count"], 1)

    def test_delete_bank_transactions_in_date_range_refreshes_saved_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            source_id = store.record_import_file("2026-06", "bank_statement", "银行流水.xlsx", None, "hash-bank", 2, 2, 0)
            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 1),
                income_amount=Decimal("10.00"),
                expense_amount=None,
                counterparty_name_raw="上海测试客户",
                direction="inflow",
                source_file_id=source_id,
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 7, 1),
                income_amount=Decimal("20.00"),
                expense_amount=None,
                counterparty_name_raw="上海测试客户",
                direction="inflow",
                source_file_id=source_id,
            )
            store.update_import_saved_count(source_id, 2)

            removed = store.delete_bank_transactions_in_date_range(date(2026, 6, 1), date(2026, 6, 30))

            self.assertEqual(removed, 1)
            self.assertEqual(store.count_import_detail_rows("bank_statement", source_id), 1)
            self.assertEqual(store.recent_imports(1)[0]["saved_count"], 1)

    def test_deduplicate_cross_file_bank_transactions_keeps_same_file_repeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            source_one = store.record_import_file("2026-06", "bank_statement", "全年流水.xlsx", None, "hash-one", 2, 2, 0)
            source_two = store.record_import_file("2026-06", "bank_statement", "六月流水.xls", None, "hash-two", 1, 1, 0)
            for source_id in (source_one, source_one, source_two):
                store.insert_bank_transaction(
                    transaction_date=date(2026, 6, 25),
                    income_amount=Decimal("2580.25"),
                    expense_amount=None,
                    counterparty_name_raw="上海示例丙积木科技有限公司",
                    direction="inflow",
                    counterparty_account="000000000000013",
                    counterparty_bank="招商银行股份有限公司上海测试支行",
                    subject="",
                    summary="转存",
                    source_file_id=source_id,
                )

            removed = store.deduplicate_cross_file_bank_transactions()

            banks = store.list_bank_transactions("inflow", date(2026, 6, 1), date(2026, 6, 30))
            self.assertEqual(removed, 1)
            self.assertEqual(len(banks), 2)
            self.assertEqual({row["source_file_id"] for row in banks}, {source_one})

    def test_deduplicate_cross_file_bank_transactions_refreshes_saved_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            source_one = store.record_import_file("2026-06", "bank_statement", "全年流水.xlsx", None, "hash-one", 1, 1, 0)
            source_two = store.record_import_file("2026-06", "bank_statement", "六月流水.xls", None, "hash-two", 1, 1, 0)
            for source_id in (source_one, source_two):
                store.insert_bank_transaction(
                    transaction_date=date(2026, 6, 25),
                    income_amount=Decimal("123.45"),
                    expense_amount=None,
                    counterparty_name_raw="上海测试客户",
                    direction="inflow",
                    source_file_id=source_id,
                )
                store.update_import_saved_count(source_id, 1)

            removed = store.deduplicate_cross_file_bank_transactions()
            imports = {row["id"]: row["saved_count"] for row in store.recent_imports(10)}

            self.assertEqual(removed, 1)
            self.assertEqual(imports[source_one], 1)
            self.assertEqual(imports[source_two], 0)

    def test_parsed_bank_import_uses_single_connection_for_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            source_id = store.record_import_file("2026-06", "bank_statement", "流水.xlsx", None, "hash", 2, 2, 0)
            rows = [
                ParsedBankTransaction(
                    transaction_date=date(2026, 6, 1),
                    income_amount=Decimal("10.00"),
                    expense_amount=None,
                    counterparty_account="1001",
                    counterparty_name_raw="上海测试客户",
                    counterparty_bank="测试银行",
                    subject="",
                    summary="转账",
                    direction="inflow",
                    source_sheet="Sheet1",
                    source_row_no=2,
                ),
                ParsedBankTransaction(
                    transaction_date=date(2026, 6, 2),
                    income_amount=Decimal("20.00"),
                    expense_amount=None,
                    counterparty_account="1002",
                    counterparty_name_raw="上海测试客户",
                    counterparty_bank="测试银行",
                    subject="",
                    summary="转账",
                    direction="inflow",
                    source_sheet="Sheet1",
                    source_row_no=3,
                ),
            ]
            original_connect = store.connect
            connect_count = 0

            def counted_connect():
                nonlocal connect_count
                connect_count += 1
                return original_connect()

            store.connect = counted_connect  # type: ignore[method-assign]

            saved = insert_parsed_bank_transactions(store, rows, source_id)

            self.assertEqual(saved, 2)
            self.assertLessEqual(connect_count, 1)


if __name__ == "__main__":
    unittest.main()
