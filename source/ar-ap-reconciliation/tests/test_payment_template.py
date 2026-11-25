from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from finance_platform.local_store import LocalStore
from finance_platform.payment_template import (
    DEFAULT_PAYMENT_PURPOSE,
    build_payment_candidates,
    build_payment_profile_exceptions,
    export_bank_payment_workbook,
    infer_payment_template_config,
)


class PaymentTemplateTest(unittest.TestCase):
    def test_default_payment_purpose_is_generic_not_historical_upload_data(self) -> None:
        self.assertEqual(DEFAULT_PAYMENT_PURPOSE, "货款")
        self.assertNotIn("丰登路后道厂", DEFAULT_PAYMENT_PURPOSE)

    def test_build_payment_candidates_groups_due_payables_with_party_bank_info(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party(
                "supplier",
                "上海测试供应商",
                default_term_days=60,
                bank_account="1234567890",
                bank_name="中国农业银行股份有限公司上海测试支行",
            )
            store.insert_invoice(
                direction="payable",
                party_id=party_id,
                party_name_raw="上海测试供应商",
                invoice_no="INV-1",
                invoice_date=date(2026, 3, 1),
                invoice_amount=Decimal("100.00"),
                term_days=60,
            )

            candidates = build_payment_candidates(store, date(2026, 4, 1), date(2026, 4, 30))

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate.party_name, "上海测试供应商")
            self.assertEqual(candidate.amount, Decimal("100.00"))
            self.assertEqual(candidate.bank_account, "1234567890")
            self.assertEqual(candidate.bank_name, "中国农业银行股份有限公司上海测试支行")
            self.assertTrue(candidate.is_abc_account)

    def test_build_payment_candidates_nets_negative_payable_invoices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party(
                "supplier",
                "上海红冲供应商",
                default_term_days=30,
                bank_account="1234567890",
                bank_name="中国农业银行股份有限公司上海测试支行",
            )
            store.insert_invoice(
                direction="payable",
                party_id=party_id,
                party_name_raw="上海红冲供应商",
                invoice_no="BLUE-1",
                invoice_date=date(2026, 4, 1),
                invoice_amount=Decimal("100.00"),
                term_days=30,
            )
            store.insert_invoice(
                direction="payable",
                party_id=party_id,
                party_name_raw="上海红冲供应商",
                invoice_no="RED-1",
                invoice_date=date(2026, 4, 20),
                invoice_amount=Decimal("-20.00"),
                term_days=30,
                is_positive=False,
                original_invoice_no="BLUE-1",
            )

            candidates = build_payment_candidates(store, date(2026, 5, 1), date(2026, 5, 31))

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].amount, Decimal("80.00"))
            self.assertEqual(candidates[0].invoice_count, 2)
            self.assertEqual(set(candidates[0].invoice_ids), {1, 2})

    def test_build_payment_candidates_omits_non_positive_net_payables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party("supplier", "上海全额红冲供应商", default_term_months=1, grace_months=1)
            store.insert_invoice(
                direction="payable",
                party_id=party_id,
                party_name_raw="上海全额红冲供应商",
                invoice_no="BLUE-1",
                invoice_date=date(2026, 4, 1),
                invoice_amount=Decimal("100.00"),
                term_months=1,
                grace_months=1,
            )
            store.insert_invoice(
                direction="payable",
                party_id=party_id,
                party_name_raw="上海全额红冲供应商",
                invoice_no="RED-1",
                invoice_date=date(2026, 4, 20),
                invoice_amount=Decimal("-100.00"),
                term_months=1,
                grace_months=1,
                is_positive=False,
                original_invoice_no="BLUE-1",
            )

            candidates = build_payment_candidates(store, date(2026, 5, 1), date(2026, 5, 31))

            self.assertEqual(candidates, [])

    def test_build_payment_candidates_excludes_payables_outside_party_master(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            store.insert_invoice(
                direction="payable",
                party_id=None,
                party_name_raw="上海名单外一次性采购",
                invoice_no="P-OUT",
                invoice_date=date(2026, 4, 1),
                invoice_amount=Decimal("100.00"),
                term_months=1,
                grace_months=1,
            )

            candidates = build_payment_candidates(store, date(2026, 6, 1), date(2026, 6, 30))

            self.assertEqual(candidates, [])

    def test_build_payment_profile_exceptions_lists_due_payables_missing_bank_info(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            missing_id = store.upsert_party("supplier", "上海缺资料供应商", default_term_days=30)
            complete_id = store.upsert_party(
                "supplier",
                "上海资料完整供应商",
                default_term_days=30,
                bank_account="123456",
                bank_name="中国农业银行上海支行",
            )
            store.insert_invoice(
                direction="payable",
                party_id=missing_id,
                party_name_raw="上海缺资料供应商",
                invoice_no="MISS-1",
                invoice_date=date(2026, 5, 1),
                invoice_amount=Decimal("100.00"),
                term_days=30,
            )
            store.insert_invoice(
                direction="payable",
                party_id=complete_id,
                party_name_raw="上海资料完整供应商",
                invoice_no="OK-1",
                invoice_date=date(2026, 5, 1),
                invoice_amount=Decimal("200.00"),
                term_days=30,
            )

            exceptions = build_payment_profile_exceptions(store, date(2026, 5, 1), date(2026, 5, 31))

            self.assertEqual(len(exceptions), 1)
            self.assertEqual(exceptions[0].party_name, "上海缺资料供应商")
            self.assertEqual(exceptions[0].amount, Decimal("100.00"))
            self.assertEqual(exceptions[0].missing_fields, ("收款方账号", "开户行支行名称"))

    def test_export_bank_payment_workbook_uses_abc_upload_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party(
                "supplier",
                "上海测试供应商",
                default_term_days=30,
                bank_account="9876543210",
                bank_name="中国工商银行股份有限公司上海测试支行",
            )
            store.insert_invoice(
                direction="payable",
                party_id=party_id,
                party_name_raw="上海测试供应商",
                invoice_no="INV-2",
                invoice_date=date(2026, 4, 1),
                invoice_amount=Decimal("250.50"),
                term_days=30,
            )
            candidates = build_payment_candidates(store, date(2026, 5, 1), date(2026, 5, 31))
            output = Path(tmp_dir) / "abc_payment.xls"

            export_bank_payment_workbook(output, candidates, purpose="丰登路后道厂-货款")

            exported = pd.read_excel(output, sheet_name="页一【批量转账文件】", header=1, engine="xlrd", dtype=object)
            self.assertEqual(exported.loc[0, "编号"], 1)
            self.assertEqual(str(exported.loc[0, "收款方账号"]), "9876543210")
            self.assertEqual(exported.loc[0, "收款方户名"], "上海测试供应商")
            self.assertEqual(exported.loc[0, "是否农行账户"], "否")
            self.assertEqual(exported.loc[0, "开户行支行名称"], "中国工商银行股份有限公司上海测试支行")
            self.assertEqual(float(exported.loc[0, "金额"]), 250.5)
            self.assertEqual(exported.loc[0, "用途（附言）"], "丰登路后道厂-货款")

    def test_export_bank_payment_workbook_honors_template_columns_and_sheet_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            party_id = store.upsert_party(
                "supplier",
                "上海模板供应商",
                default_term_days=0,
                bank_account="111222333",
                bank_name="中国农业银行股份有限公司上海支行",
            )
            store.insert_invoice(
                direction="payable",
                party_id=party_id,
                party_name_raw="上海模板供应商",
                invoice_no="INV-3",
                invoice_date=date(2026, 5, 1),
                invoice_amount=Decimal("88.00"),
                term_days=0,
            )
            candidates = build_payment_candidates(store, date(2026, 5, 1), date(2026, 5, 31))
            output = Path(tmp_dir) / "abc_payment_custom.xlsx"

            export_bank_payment_workbook(
                output,
                candidates,
                purpose="模板用途",
                columns=["收款方户名", "收款方账号", "金额", "用途（附言）"],
                sheet_name="自定义付款",
            )

            exported = pd.read_excel(output, sheet_name="自定义付款", header=1, dtype=object)
            self.assertEqual(list(exported.columns), ["收款方户名", "收款方账号", "金额", "用途（附言）"])
            self.assertEqual(exported.loc[0, "用途（附言）"], "模板用途")

    def test_import_payment_template_detects_columns_without_historical_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "后道厂中国农业银行企业网银批量转账上传文件.xlsx"
            columns = ["编号", "收款方账号", "收款方户名", "是否农行账户", "开户行支行名称", "金额", "用途（附言）"]
            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                pd.DataFrame(
                    [
                        ["中国农业银行批量转账上传文件"],
                        columns,
                        [1, "6222", "历史供应商", "是", "历史支行", 100, "历史用途"],
                    ]
                ).to_excel(
                    writer,
                    sheet_name="页一【批量转账文件】",
                    header=False,
                    index=False,
                )

            config = infer_payment_template_config(path)

        self.assertEqual(config.sheet_name, "页一【批量转账文件】")
        self.assertEqual(config.columns, columns)
        self.assertEqual(config.default_purpose, DEFAULT_PAYMENT_PURPOSE)
        self.assertNotIn("历史供应商", config.template_name)
        self.assertNotIn("历史用途", "\n".join(config.columns))


if __name__ == "__main__":
    unittest.main()
