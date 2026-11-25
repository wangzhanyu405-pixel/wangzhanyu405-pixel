from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import (
    AG_GRID_LOCALE_CN,
    NAV_ITEMS,
    build_bank_review_grid_options,
    build_party_editor_grid_options,
    build_reconciliation_evidence,
    build_party_monthly_tree_rows,
    build_inline_manual_settlement_requests,
    filter_workbench_matches_by_scope,
    format_bank_rows,
    format_bank_ledger_rows,
    format_chinese_month,
    filter_source_rows,
    filter_other_rows,
    filter_statistic_rows,
    format_fuzzy_match_rows,
    format_invoice_rows,
    format_invoice_ledger_rows,
    format_workbench_detail_rows,
    format_party_edit_rows,
    export_matches_button,
    format_unmatched_bank_workbench_rows,
    match_export_context,
    party_monthly_activity,
    format_payment_candidate_rows,
    format_payment_profile_exception_rows,
    format_payment_template_rows,
    get_local_store,
    parse_decimal_input,
    reconciliation_scope_key,
    reconciliation_event_signature,
    render_brand_header,
    render_ledger_summary_html,
    render_metric_grid,
    render_record_cards_html,
    render_reconciliation_evidence,
    storage_mode_notice,
    sqlserver_credentials_ready,
    prepare_workbench_inline_rows,
    search_party_options,
    should_open_reconciliation_dialog,
    scoped_session_matches,
    unique_bank_review_selection,
)
from finance_platform.local_store import LocalStore
from finance_platform.matching import MatchResult
from finance_platform.payment_template import PaymentCandidate


class AppRenderingTest(unittest.TestCase):
    def test_brand_header_carries_company_context(self) -> None:
        html = render_brand_header("data/local_finance.db")

        self.assertIn("财务智能对账平台", html)
        self.assertIn("财务应收应付对账平台", html)
        self.assertIn("本地测试库", html)
        self.assertNotIn("—", html)
        self.assertNotIn("–", html)

    def test_metric_grid_html_is_not_indented_as_markdown_code_block(self) -> None:
        html = render_metric_grid(
            {
                "payable_due_amount": Decimal("100.00"),
                "payable_settled_amount": Decimal("50.00"),
                "payable_pending_amount": Decimal("50.00"),
                "payable_overdue_amount": Decimal("0.00"),
                "supplier_payment_amount": Decimal("45.00"),
                "payable_due_count": 1,
                "payable_pending_count": 1,
                "payable_overdue_count": 0,
                "receivable_due_amount": Decimal("80.00"),
                "receivable_settled_amount": Decimal("40.00"),
                "receivable_pending_amount": Decimal("40.00"),
                "receivable_overdue_amount": Decimal("0.00"),
                "customer_receipt_amount": Decimal("35.00"),
                "receivable_due_count": 1,
                "receivable_pending_count": 1,
                "receivable_overdue_count": 0,
            }
        )

        self.assertNotRegex(html, r"\n\s{4,}<div")
        self.assertIn('class="metric-card"', html)
        self.assertIn('data-tone="danger"', html)
        self.assertIn("已导入付款", html)
        self.assertIn("已导入收款", html)
        self.assertIn("¥45.00", html)
        self.assertIn("¥35.00", html)
        self.assertNotIn("—", html)
        self.assertNotIn("–", html)

    def test_fuzzy_match_rows_are_finance_readable(self) -> None:
        rows = format_fuzzy_match_rows(
            [
                {
                    "bank_transaction_id": 7,
                    "transaction_date": "2026-05-25",
                    "direction_label": "收入",
                    "counterparty_name_raw": "示例甲包装(上海)有限公司",
                    "amount": Decimal("2100.50"),
                    "business_category_label": "未识别收入",
                    "suggested_party_id": 3,
                    "suggested_party_name": "示例甲包装品贸易(上海)有限公司",
                    "suggested_party_type_label": "客户",
                    "matched_name": "示例甲包装品贸易(上海)有限公司",
                    "score": 0.8,
                    "candidate_count": 1,
                }
            ]
        )

        self.assertEqual(rows[0]["选择"], False)
        self.assertEqual(rows[0]["流水ID"], 7)
        self.assertEqual(rows[0]["银行户名"], "示例甲包装(上海)有限公司")
        self.assertEqual(rows[0]["建议往来单位"], "示例甲包装品贸易(上海)有限公司")
        self.assertEqual(rows[0]["单位类型"], "客户")
        self.assertEqual(rows[0]["相似度"], "80.0%")
        self.assertEqual(rows[0]["金额"], "¥2,100.50")

    def test_statistic_rows_can_be_filtered_by_party_keyword(self) -> None:
        rows = [
            {"往来单位": "上海例庚包装印刷有限公司", "金额": "100.00"},
            {"往来单位": "上海例丁实业有限公司", "金额": "200.00"},
        ]

        filtered = filter_statistic_rows(rows, "例庚")

        self.assertEqual(filtered, [{"往来单位": "上海例庚包装印刷有限公司", "金额": "100.00"}])

    def test_nav_exposes_data_center_pages(self) -> None:
        self.assertIn("银行流水", NAV_ITEMS)
        self.assertIn("发票台账", NAV_ITEMS)
        self.assertIn("付款模板", NAV_ITEMS)

    def test_match_export_context_prefers_original_run_rows_after_reload(self) -> None:
        session_state = {
            "payable_match_invoices": [{"id": 1, "status": "matched"}],
            "payable_match_banks": [{"id": 10, "match_status": "matched"}],
        }
        current_invoices = [{"id": 2, "status": "unmatched"}]
        current_banks = [{"id": 20, "match_status": "unmatched"}]

        invoices, banks = match_export_context("payable", session_state, current_invoices, current_banks)

        self.assertEqual(invoices, [{"id": 1, "status": "matched"}])
        self.assertEqual(banks, [{"id": 10, "match_status": "matched"}])

    def test_scoped_match_state_is_hidden_when_filter_scope_changes(self) -> None:
        june_scope = reconciliation_scope_key("payable", date(2026, 6, 1), date(2026, 6, 30), date(2026, 1, 1))
        may_scope = reconciliation_scope_key("payable", date(2026, 5, 1), date(2026, 5, 31), date(2026, 1, 1))
        session_state = {
            "payable_match_scope": june_scope,
            "payable_matches": ["old-june-match"],
            "payable_match_invoices": [{"id": 1}],
            "payable_match_banks": [{"id": 10}],
        }
        current_invoices = [{"id": 2}]
        current_banks = [{"id": 20}]

        matches = scoped_session_matches("payable", session_state, may_scope)
        invoices, banks = match_export_context("payable", session_state, current_invoices, current_banks, scope_key=may_scope)

        self.assertEqual(matches, [])
        self.assertEqual(invoices, current_invoices)
        self.assertEqual(banks, current_banks)

    def test_local_store_factory_is_not_streamlit_cached(self) -> None:
        self.assertFalse(hasattr(get_local_store, "clear"))

    def test_storage_notice_warns_when_sql_credentials_exist_but_mode_is_local(self) -> None:
        settings = SimpleNamespace(
            uses_sqlserver_storage=False,
            has_database_credentials=True,
            db_server="localhost",
            db_name="demo_finance",
        )

        self.assertTrue(sqlserver_credentials_ready(settings))
        self.assertIn("不会写入 SQL Server", storage_mode_notice(settings))

    def test_ledger_summary_html_keeps_long_money_visible(self) -> None:
        html = render_ledger_summary_html(
            [
                ("收入合计", "¥1,900,250.75"),
                ("支出合计", "¥0.00"),
            ]
        )

        self.assertIn("¥1,900,250.75", html)
        self.assertIn("ledger-summary-value", html)
        self.assertNotIn("...", html)

    def test_ag_grid_locale_uses_chinese_filter_words(self) -> None:
        self.assertEqual(AG_GRID_LOCALE_CN["contains"], "包含")
        self.assertEqual(AG_GRID_LOCALE_CN["notContains"], "不包含")
        self.assertEqual(AG_GRID_LOCALE_CN["equals"], "等于")
        self.assertEqual(AG_GRID_LOCALE_CN["filterOoo"], "筛选...")
        self.assertEqual(AG_GRID_LOCALE_CN["sortAscending"], "升序")
        self.assertEqual(AG_GRID_LOCALE_CN["hideColumn"], "隐藏列")

    def test_ledger_summary_html_marks_finance_risk_tones(self) -> None:
        html = render_ledger_summary_html(
            [
                ("应付发票总额", "¥100.00"),
                ("已付金额", "¥80.00"),
                ("未付金额", "¥20.00"),
                ("付款未找到发票", "¥5.00"),
            ]
        )

        self.assertIn('data-tone="total"', html)
        self.assertIn('data-tone="settled"', html)
        self.assertIn('data-tone="pending"', html)
        self.assertIn('data-tone="danger"', html)

    def test_bank_ledger_rows_are_selectable_and_readable(self) -> None:
        rows = format_bank_ledger_rows(
            [
                {
                    "id": 9,
                    "transaction_date": "2026-06-02",
                    "direction": "outflow",
                    "income_amount": None,
                    "expense_amount": "2500.10",
                    "counterparty_account": "000015",
                    "counterparty_name_raw": "待报解预算收入",
                    "counterparty_bank": "中国农业银行",
                    "summary": "公共缴费",
                    "subject": "",
                    "business_category_label": "税费",
                    "match_status": "unmatched",
                }
            ]
        )

        self.assertEqual(rows[0]["选择"], False)
        self.assertEqual(rows[0]["流水ID"], 9)
        self.assertEqual(rows[0]["方向"], "支出")
        self.assertEqual(rows[0]["金额"], "¥2,500.10")
        self.assertEqual(rows[0]["对方户名"], "待报解预算收入")
        self.assertEqual(rows[0]["匹配状态"], "未匹配")

    def test_workbench_detail_rows_show_invoice_status_by_finance_terms(self) -> None:
        invoices = [
            {
                "id": 4,
                "direction": "payable",
                "party_name": "上海供应商",
                "party_name_raw": "上海供应商",
                "invoice_no": "P-1",
                "invoice_date": "2026-05-20",
                "invoice_amount": "300.00",
                "term_days": 30,
                "due_date": "2026-06-19",
            },
            {
                "id": 5,
                "direction": "payable",
                "party_name": "上海供应商",
                "party_name_raw": "上海供应商",
                "invoice_no": "P-2",
                "invoice_date": "2026-06-01",
                "invoice_amount": "200.00",
                "term_days": 30,
                "due_date": "2026-07-01",
            },
        ]
        banks = [
            {
                "id": 9,
                "transaction_date": "2026-06-20",
                "direction": "outflow",
                "expense_amount": "300.00",
                "income_amount": None,
                "counterparty_name_raw": "上海供应商",
            }
        ]
        matches = [
            MatchResult(
                match_type="exact_late",
                status="matched",
                party_id=1,
                party_name="上海供应商",
                invoice_ids=[4],
                bank_transaction_ids=[9],
                invoice_total=Decimal("300.00"),
                bank_total=Decimal("300.00"),
                difference_amount=Decimal("0.00"),
                confidence=Decimal("0.90"),
                remark="同一往来单位、金额一致，付款晚于到期日，按逾期付款匹配",
            ),
            MatchResult(
                match_type="unmatched",
                status="unmatched_invoice",
                party_id=1,
                party_name="上海供应商",
                invoice_ids=[5],
                bank_transaction_ids=[],
                invoice_total=Decimal("200.00"),
                bank_total=Decimal("0.00"),
                difference_amount=Decimal("200.00"),
                confidence=Decimal("0.00"),
                remark="发票未匹配到付款流水",
            ),
        ]

        rows = format_workbench_detail_rows("payable", invoices, banks, matches)

        self.assertEqual(rows[0]["往来单位"], "上海供应商")
        self.assertEqual(rows[0]["发票号"], "P-1")
        self.assertEqual(rows[0]["到期日"], "2026-06-19")
        self.assertEqual(rows[0]["实付日期"], "2026-06-20")
        self.assertEqual(rows[0]["状态"], "已付款（逾期）")
        self.assertEqual(rows[0]["匹配方式"], "单张发票逾期匹配")
        self.assertNotIn("invoice_ids", rows[0])
        self.assertNotIn("exact_late", str(rows))
        self.assertEqual(rows[1]["状态"], "未付款")
        self.assertEqual(rows[1]["实付日期"], "")

    def test_export_matches_button_uses_label_for_exception_status(self) -> None:
        captured: dict[str, object] = {}

        def fake_export(output: Path, **kwargs: object) -> None:
            captured.update(kwargs)
            output.write_bytes(b"xlsx")

        match = MatchResult(
            match_type="unmatched",
            status="unmatched_invoice",
            party_id=1,
            party_name="上海供应商",
            invoice_ids=[4],
            bank_transaction_ids=[],
            invoice_total=Decimal("300.00"),
            bank_total=Decimal("0.00"),
            difference_amount=Decimal("300.00"),
            confidence=Decimal("0.00"),
            remark="发票未匹配到付款流水",
        )
        invoices = [
            {
                "id": 4,
                "direction": "payable",
                "party_name": "上海供应商",
                "party_name_raw": "上海供应商",
                "invoice_no": "90000000000000300001",
                "invoice_date": "2026-05-20",
                "invoice_amount": "300.00",
                "term_days": 30,
                "due_date": "2026-06-19",
            }
        ]

        with patch("app.get_settings", return_value=SimpleNamespace(export_dir=Path("/tmp"))), \
             patch("app.export_reconciliation_workbook", side_effect=fake_export), \
             patch("app.st.download_button"):
            export_matches_button("应付", invoices, [], [match])

        self.assertEqual(captured["exceptions"][0]["状态"], "未付款")
        self.assertEqual(captured["match_details"][0]["发票号"], "90000000000000300001")
        self.assertNotIn("match_type", captured["match_details"][0])
        self.assertNotIn("invoice_ids", captured["match_details"][0])

    def test_receivable_workbench_detail_rows_use_receipt_words(self) -> None:
        rows = format_workbench_detail_rows(
            "receivable",
            [
                {
                    "id": 8,
                    "direction": "receivable",
                    "party_name": "上海客户",
                    "party_name_raw": "上海客户",
                    "invoice_no": "R-1",
                    "invoice_date": "2026-05-20",
                    "invoice_amount": "500.00",
                    "term_days": 30,
                    "due_date": "2026-06-19",
                }
            ],
            [],
            [
                MatchResult(
                    match_type="unmatched",
                    status="unmatched_invoice",
                    party_id=2,
                    party_name="上海客户",
                    invoice_ids=[8],
                    bank_transaction_ids=[],
                    invoice_total=Decimal("500.00"),
                    bank_total=Decimal("0.00"),
                    difference_amount=Decimal("500.00"),
                    confidence=Decimal("0.00"),
                    remark="发票未匹配到收款流水",
                )
            ],
        )

        self.assertIn("实收日期", rows[0])
        self.assertIn("收款金额", rows[0])
        self.assertEqual(rows[0]["状态"], "未收款")

    def test_workbench_detail_rows_keep_bank_only_rows_out_of_invoice_main_table(self) -> None:
        invoices = [
            {
                "id": 8,
                "direction": "receivable",
                "party_name": "上海客户",
                "party_name_raw": "上海客户",
                "invoice_no": "R-1",
                "invoice_date": "2026-05-20",
                "invoice_amount": "500.00",
                "term_days": 30,
                "due_date": "2026-06-19",
            }
        ]
        banks = [
            {
                "id": 18,
                "transaction_date": "2026-06-20",
                "direction": "inflow",
                "income_amount": "800.00",
                "expense_amount": None,
                "counterparty_name_raw": "上海客户",
                "summary": "货款",
                "business_category_label": "客户回款",
            }
        ]
        matches = [
            MatchResult(
                match_type="unmatched",
                status="unmatched_invoice",
                party_id=2,
                party_name="上海客户",
                invoice_ids=[8],
                bank_transaction_ids=[],
                invoice_total=Decimal("500.00"),
                bank_total=Decimal("0.00"),
                difference_amount=Decimal("500.00"),
                confidence=Decimal("0.00"),
                remark="发票未匹配到收款流水",
            ),
            MatchResult(
                match_type="unmatched",
                status="unmatched_bank",
                party_id=2,
                party_name="上海客户",
                invoice_ids=[],
                bank_transaction_ids=[18],
                invoice_total=Decimal("0.00"),
                bank_total=Decimal("800.00"),
                difference_amount=Decimal("800.00"),
                confidence=Decimal("0.00"),
                remark="银行收款未匹配到发票",
            ),
        ]

        detail_rows = format_workbench_detail_rows("receivable", invoices, banks, matches)
        bank_only_rows = format_unmatched_bank_workbench_rows("receivable", banks, matches)

        self.assertEqual(len(detail_rows), 1)
        self.assertEqual(detail_rows[0]["状态"], "未收款")
        self.assertEqual(len(bank_only_rows), 1)
        self.assertEqual(bank_only_rows[0]["状态"], "收款未找到发票")
        self.assertEqual(bank_only_rows[0]["流水ID"], "18")

    def test_reconciliation_evidence_resolves_clicked_workbench_row(self) -> None:
        invoices = [
            {
                "id": 8,
                "direction": "receivable",
                "party_name": "上海客户",
                "party_name_raw": "上海客户",
                "invoice_no": "90000000000000500001",
                "invoice_date": "2026-05-20",
                "invoice_amount": "500.00",
                "term_days": 30,
                "due_date": "2026-06-19",
                "status": "matched",
            }
        ]
        banks = [
            {
                "id": 18,
                "transaction_date": "2026-06-20",
                "direction": "inflow",
                "income_amount": "500.00",
                "expense_amount": None,
                "counterparty_name_raw": "上海客户",
                "summary": "货款",
                "business_category_label": "客户回款",
                "match_status": "matched",
            }
        ]
        row = {
            "往来单位": "上海客户",
            "发票号": "90000000000000500001",
            "发票ID": "8",
            "流水ID": "18",
            "状态": "已收款",
            "匹配方式": "单张发票匹配",
            "说明": "同一往来单位、金额一致、付款日期在允许窗口内",
        }

        evidence = build_reconciliation_evidence("receivable", row, invoices, banks)

        self.assertEqual(evidence["title"], "上海客户 对账依据")
        self.assertEqual(evidence["conclusion"]["状态"], "已收款")
        self.assertEqual(evidence["invoices"][0]["发票号"], "90000000000000500001")
        self.assertEqual(evidence["banks"][0]["流水ID"], 18)
        self.assertEqual(evidence["missing_bank_message"], "")

    def test_reconciliation_evidence_explains_missing_bank_rows(self) -> None:
        row = {
            "往来单位": "上海供应商",
            "发票ID": "9",
            "流水ID": "",
            "状态": "未付款",
            "匹配方式": "未匹配",
            "说明": "发票未匹配到付款流水",
        }
        invoices = [
            {
                "id": 9,
                "direction": "payable",
                "party_name": "上海供应商",
                "party_name_raw": "上海供应商",
                "invoice_no": "P-9",
                "invoice_date": "2026-05-20",
                "invoice_amount": "800.00",
                "term_days": 30,
                "due_date": "2026-06-19",
                "status": "unmatched",
            }
        ]

        evidence = build_reconciliation_evidence("payable", row, invoices, [])

        self.assertEqual(evidence["banks"], [])
        self.assertEqual(evidence["missing_bank_message"], "未找到该公司的相关农业银行付款流水。")

    def test_record_cards_html_uses_compact_excel_like_rows(self) -> None:
        html = render_record_cards_html(
            [
                {
                    "发票ID": 30000,
                    "往来单位": "上海例甲印务有限公司",
                    "发票号": "90000000000000100001",
                    "发票金额": "¥5,500.50",
                }
            ],
            empty_message="暂无数据",
        )

        self.assertIn('class="record-sheet-list"', html)
        self.assertIn("<th>发票号</th>", html)
        self.assertIn("<td>90000000000000100001</td>", html)
        self.assertNotIn("record-card", html)

    def test_source_rows_filter_by_named_fields(self) -> None:
        invoice_rows = [
            {
                "id": 2113,
                "party_name": "上海例戊包装材料有限公司",
                "invoice_no": "90000000000000400001",
                "invoice_date": "2026-06-30",
                "invoice_amount": "3400.00",
                "status": "unmatched",
            },
            {
                "id": 3007,
                "party_name": "上海例甲印务有限公司",
                "invoice_no": "90000000000000200001",
                "invoice_date": "2026-03-26",
                "invoice_amount": "3450.15",
                "status": "matched",
            },
        ]
        bank_rows = [
            {
                "id": 1008,
                "counterparty_name_raw": "上海例戊包装材料有限公司",
                "transaction_date": "2026-07-08",
                "expense_amount": "3400.00",
                "summary": "转账",
                "match_status": "unmatched",
            },
            {
                "id": 457,
                "counterparty_name_raw": "上海例甲印务有限公司",
                "transaction_date": "2026-03-31",
                "expense_amount": "5500.50",
                "summary": "转账",
                "match_status": "matched",
            },
        ]
        filters = {
            "party": "例戊",
            "number": "900000000000004",
            "amount": "3400",
            "date": "2026-06",
            "status": "未匹配",
        }

        matched_invoices = filter_source_rows(invoice_rows, filters, "invoice")
        matched_banks = filter_source_rows(bank_rows, {"party": "例戊", "amount": "3400", "date": "2026-07"}, "bank")

        self.assertEqual([row["id"] for row in matched_invoices], [2113])
        self.assertEqual([row["id"] for row in matched_banks], [1008])

    def test_workbench_scope_filters_matches_by_party_and_status(self) -> None:
        invoices = [
            {
                "id": 4,
                "party_id": 1,
                "party_name": "上海供应商甲",
                "party_name_raw": "上海供应商甲",
                "invoice_no": "P-A",
                "invoice_date": "2026-05-20",
                "invoice_amount": "300.00",
                "term_days": 30,
                "due_date": "2026-06-19",
            },
            {
                "id": 5,
                "party_id": 2,
                "party_name": "上海供应商乙",
                "party_name_raw": "上海供应商乙",
                "invoice_no": "P-B",
                "invoice_date": "2026-05-21",
                "invoice_amount": "200.00",
                "term_days": 30,
                "due_date": "2026-06-20",
            },
        ]
        matches = [
            MatchResult(
                match_type="exact",
                status="matched",
                party_id=1,
                party_name="上海供应商甲",
                invoice_ids=[4],
                bank_transaction_ids=[9],
                invoice_total=Decimal("300.00"),
                bank_total=Decimal("300.00"),
                difference_amount=Decimal("0.00"),
                confidence=Decimal("1.00"),
            ),
            MatchResult(
                match_type="unmatched",
                status="unmatched_invoice",
                party_id=2,
                party_name="上海供应商乙",
                invoice_ids=[5],
                bank_transaction_ids=[],
                invoice_total=Decimal("200.00"),
                bank_total=Decimal("0.00"),
                difference_amount=Decimal("200.00"),
                confidence=Decimal("0.00"),
            ),
        ]

        filtered = filter_workbench_matches_by_scope(
            "payable",
            matches,
            invoices,
            [],
            party_query="乙",
            selected_party_ids=[],
            selected_statuses=["未付款"],
        )

        self.assertEqual([row.party_name for row in filtered], ["上海供应商乙"])

    def test_workbench_rows_keep_inline_manual_fields_blank_until_user_edits(self) -> None:
        rows = prepare_workbench_inline_rows(
            "payable",
            [
                {
                    "往来单位": "上海供应商",
                    "发票号": "P-9",
                    "发票日期": "2026-05-20",
                    "到期日": "2026-06-19",
                    "发票金额": "¥800.00",
                    "实付日期": "",
                    "付款金额": "¥0.00",
                    "差额": "¥800.00",
                    "状态": "未付款",
                    "匹配方式": "未匹配",
                    "说明": "发票未匹配到付款流水",
                    "发票ID": "9",
                    "流水ID": "",
                }
            ],
            default_date=date(2026, 7, 8),
        )

        keys = list(rows[0].keys())
        self.assertLess(keys.index("人工处理方式"), keys.index("发票号"))
        self.assertEqual(rows[0]["人工处理方式"], "")
        self.assertEqual(rows[0]["人工日期"], "")
        self.assertEqual(rows[0]["人工金额"], "")

    def test_workbench_rows_do_not_offer_manual_fields_for_bank_only_rows(self) -> None:
        rows = prepare_workbench_inline_rows(
            "payable",
            [
                {
                    "往来单位": "上海供应商",
                    "发票号": "",
                    "发票金额": "¥0.00",
                    "实付日期": "2026-07-08",
                    "付款金额": "¥800.00",
                    "差额": "¥800.00",
                    "状态": "付款未找到发票",
                    "发票ID": "",
                    "流水ID": "18",
                }
            ],
            default_date=date(2026, 7, 8),
        )

        self.assertEqual(rows[0]["人工日期"], "")
        self.assertEqual(rows[0]["人工金额"], "")

    def test_double_click_manual_columns_does_not_open_evidence_dialog(self) -> None:
        event = {
            "event": "double_click",
            "column": "人工处理方式",
            "event_id": "manual-1",
            "row": {"发票ID": "9", "流水ID": ""},
        }

        self.assertFalse(should_open_reconciliation_dialog(event))

    def test_double_click_normal_column_opens_evidence_only_once(self) -> None:
        event = {
            "event": "double_click",
            "column": "往来单位",
            "event_id": "party-1",
            "row": {"发票ID": "9", "流水ID": "18"},
        }

        signature = reconciliation_event_signature(event)

        self.assertTrue(should_open_reconciliation_dialog(event))
        self.assertFalse(should_open_reconciliation_dialog(event, signature))

    def test_reconciliation_evidence_uses_excel_like_tables(self) -> None:
        evidence = {
            "conclusion": {"往来单位": "上海客户", "状态": "已收款"},
            "invoices": [{"发票ID": 1, "发票号": "90000000000000500001"}],
            "banks": [{"流水ID": 2, "对方户名": "上海客户"}],
            "missing_bank_message": "",
        }

        with (
            patch("app.render_section_heading"),
            patch("app.show_table") as show_table_mock,
            patch("app.st.warning"),
            patch("app.st.markdown") as markdown_mock,
        ):
            render_reconciliation_evidence(evidence)

        self.assertEqual(show_table_mock.call_count, 3)
        markdown_mock.assert_not_called()

    def test_parse_decimal_input_accepts_finance_formatting(self) -> None:
        self.assertEqual(parse_decimal_input("¥1,234.50"), Decimal("1234.50"))
        self.assertEqual(parse_decimal_input("￥800"), Decimal("800"))
        self.assertIsNone(parse_decimal_input(""))

    def test_inline_manual_settlement_requests_parse_grid_rows(self) -> None:
        requests, warnings = build_inline_manual_settlement_requests(
            "payable",
            [
                {
                    "往来单位": "上海供应商",
                    "发票ID": "9",
                    "状态": "未付款",
                    "人工处理方式": "承兑汇票",
                    "人工日期": "2026-07-08",
                    "人工金额": "800.00",
                    "人工备注": "承兑支付",
                }
            ],
        )

        self.assertEqual(warnings, [])
        self.assertEqual(requests[0]["invoice_id"], 9)
        self.assertEqual(requests[0]["settlement_method"], "acceptance_bill")
        self.assertEqual(requests[0]["settlement_date"], date(2026, 7, 8))
        self.assertEqual(requests[0]["amount"], Decimal("800.00"))

    def test_reconciliation_export_rows_include_source_ids_for_audit(self) -> None:
        invoice_rows = format_invoice_rows(
            [
                {
                    "id": 4,
                    "direction": "payable",
                    "party_name": "上海供应商",
                    "party_name_raw": "上海供应商",
                    "invoice_no": "P-1",
                    "invoice_date": "2026-05-20",
                    "invoice_amount": "300.00",
                    "term_months": 1,
                    "due_date": "2026-06-20",
                    "grace_deadline": "2026-07-31",
                    "status": "matched",
                }
            ]
        )
        bank_rows = format_bank_rows(
            [
                {
                    "id": 9,
                    "transaction_date": "2026-06-20",
                    "direction": "outflow",
                    "income_amount": None,
                    "expense_amount": "300.00",
                    "counterparty_name_raw": "上海供应商",
                    "summary": "货款",
                    "business_category_label": "供应商付款",
                    "match_status": "matched",
                }
            ]
        )

        self.assertEqual(invoice_rows[0]["发票ID"], 4)
        self.assertEqual(bank_rows[0]["流水ID"], 9)
        self.assertEqual(invoice_rows[0]["状态"], "已匹配")
        self.assertEqual(bank_rows[0]["状态"], "已匹配")

    def test_invoice_ledger_rows_are_selectable_and_readable(self) -> None:
        rows = format_invoice_ledger_rows(
            [
                {
                    "id": 4,
                    "direction": "receivable",
                    "party_name": "上海客户",
                    "party_name_raw": "上海客户",
                    "invoice_no": "R-1",
                    "invoice_date": "2026-05-20",
                    "invoice_amount": "300.00",
                    "term_months": 1,
                    "grace_deadline": "2026-07-31",
                    "status": "matched",
                }
            ]
        )

        self.assertEqual(rows[0]["选择"], False)
        self.assertEqual(rows[0]["发票ID"], 4)
        self.assertEqual(rows[0]["方向"], "应收")
        self.assertEqual(rows[0]["往来单位"], "上海客户")
        self.assertEqual(rows[0]["发票金额"], "¥300.00")
        self.assertEqual(rows[0]["匹配状态"], "已匹配")

    def test_invoice_ledger_rows_show_tax_export_red_metadata(self) -> None:
        rows = format_invoice_ledger_rows(
            [
                {
                    "id": 5,
                    "direction": "payable",
                    "party_name": "上海供应商",
                    "party_name_raw": "上海供应商",
                    "invoice_no": "RED-1",
                    "invoice_date": "2026-05-20",
                    "invoice_amount": "-123.45",
                    "term_months": 1,
                    "grace_months": 1,
                    "due_date": "2026-06-20",
                    "grace_deadline": "2026-07-31",
                    "status": "unmatched",
                    "invoice_status_raw": "已红冲-全额",
                    "is_positive": 0,
                    "original_invoice_no": "BLUE-1",
                    "red_confirmation_no": "90000000000000000016",
                }
            ]
        )

        self.assertEqual(rows[0]["税局状态"], "已红冲-全额")
        self.assertEqual(rows[0]["正数发票"], "否")
        self.assertEqual(rows[0]["原蓝字发票"], "BLUE-1")
        self.assertEqual(rows[0]["红字确认单"], "90000000000000000016")

    def test_payment_template_rows_do_not_expose_historical_purpose(self) -> None:
        rows = format_payment_template_rows(
            [
                {
                    "id": 1,
                    "bank_name": "中国农业银行",
                    "template_name": "农业银行批量付款模板",
                    "sheet_name": "页一【批量转账文件】",
                    "default_purpose": "丰登路后道厂-货款",
                    "columns": ["编号", "收款方账号", "金额"],
                }
            ]
        )

        self.assertNotIn("默认用途", rows[0])
        self.assertIn("导出列", rows[0])
        self.assertNotIn("丰登路后道厂", str(rows[0]))

    def test_payment_candidates_default_select_only_complete_bank_info(self) -> None:
        rows = format_payment_candidate_rows(
            [
                PaymentCandidate(
                    party_id=1,
                    party_name="上海完整供应商",
                    amount=Decimal("100.00"),
                    invoice_count=1,
                    invoice_ids=(1,),
                    earliest_grace_deadline=date(2026, 6, 30),
                    latest_grace_deadline=date(2026, 6, 30),
                    bank_account="1234567890",
                    bank_name="中国农业银行股份有限公司上海测试支行",
                    is_abc_account=True,
                    missing_fields=(),
                ),
                PaymentCandidate(
                    party_id=2,
                    party_name="上海缺资料供应商",
                    amount=Decimal("200.00"),
                    invoice_count=1,
                    invoice_ids=(2,),
                    earliest_grace_deadline=date(2026, 6, 30),
                    latest_grace_deadline=date(2026, 6, 30),
                    bank_account=None,
                    bank_name=None,
                    is_abc_account=False,
                    missing_fields=("收款方账号", "开户行支行名称"),
                ),
            ]
        )

        self.assertEqual(rows[0]["资料状态"], "完整")
        self.assertEqual(rows[0]["选择"], True)
        self.assertEqual(rows[1]["资料状态"], "缺 收款方账号、开户行支行名称")
        self.assertEqual(rows[1]["选择"], False)

    def test_chinese_month_and_party_search_helpers(self) -> None:
        self.assertEqual(format_chinese_month("2026-01"), "2026年1月")
        parties = [
            {"id": 1, "party_type": "supplier", "name": "上海例丁实业有限公司"},
            {"id": 2, "party_type": "customer", "name": "上海示例丙积木科技有限公司"},
        ]

        options = search_party_options(parties, "海")

        self.assertEqual([option["名称"] for option in options], ["上海例丁实业有限公司", "上海示例丙积木科技有限公司"])
        self.assertEqual(options[0]["类型"], "供应商")

    def test_party_monthly_tree_rows_use_chinese_months(self) -> None:
        rows = build_party_monthly_tree_rows(
            [
                {
                    "月份": "2026-01",
                    "应付": Decimal("100.00"),
                    "付款": Decimal("80.00"),
                    "应收": Decimal("50.00"),
                    "收款": Decimal("20.00"),
                },
                {
                    "月份": "2026-02",
                    "应付": Decimal("0.00"),
                    "付款": Decimal("10.00"),
                    "应收": Decimal("0.00"),
                    "收款": Decimal("0.00"),
                },
            ]
        )

        self.assertEqual(rows[0]["月份"], "2026年1月")
        self.assertEqual(rows[0]["项目"], "应付")
        self.assertEqual(rows[0]["金额"], "¥100.00")
        self.assertEqual(rows[-1]["月份"], "2026年2月")

    def test_party_monthly_activity_filters_bank_flow_by_reconciliation_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalStore(Path(tmp_dir) / "finance.db")
            store.init()
            supplier_id = store.upsert_party("supplier", "上海测试供应商", default_term_days=30)
            store.insert_invoice(
                direction="payable",
                party_id=supplier_id,
                party_name_raw="上海测试供应商",
                invoice_no="P-1",
                invoice_date=date(2026, 6, 1),
                invoice_amount=Decimal("200.00"),
                term_days=30,
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 20),
                income_amount=None,
                expense_amount=Decimal("100.00"),
                counterparty_name_raw="上海测试供应商",
                direction="outflow",
                party_id=supplier_id,
            )
            store.insert_bank_transaction(
                transaction_date=date(2026, 6, 20),
                income_amount=None,
                expense_amount=Decimal("0.90"),
                counterparty_name_raw="上海测试供应商",
                summary="费用外收",
                direction="outflow",
                party_id=supplier_id,
            )

            rows = party_monthly_activity(store, supplier_id, date(2026, 6, 1), date(2026, 6, 30))

            self.assertEqual(rows[0]["应付"], Decimal("200.00"))
            self.assertEqual(rows[0]["付款"], Decimal("100.00"))

    def test_filter_other_rows_searches_unit_invoice_and_summary(self) -> None:
        rows = [
            {"party_name_raw": "上海一次性采购", "invoice_no": "P-1", "summary": ""},
            {"party_name_raw": "上海名单外客户", "invoice_no": "R-1", "summary": "服务费"},
        ]

        result = filter_other_rows(rows, "采购")

        self.assertEqual(result, [rows[0]])

    def test_party_edit_rows_include_delete_column(self) -> None:
        rows = format_party_edit_rows(
            [
                {
                    "id": 1,
                    "party_type": "supplier",
                    "name": "上海一次性采购",
                    "short_name": "",
                    "default_term_months": 1,
                    "grace_months": 1,
                    "bank_account": "",
                    "bank_name": "",
                    "remark": "",
                }
            ]
        )

        self.assertIn("删除", rows[0])
        self.assertFalse(rows[0]["删除"])

    def test_party_editor_grid_options_support_header_filtering_and_editing(self) -> None:
        options = build_party_editor_grid_options(["ID", "类型", "名称", "删除", "账期天数", "收款账号", "开户行"])
        columns = {column["field"]: column for column in options["columnDefs"]}

        self.assertEqual(options["localeText"]["filterOoo"], "筛选...")
        self.assertTrue(options["defaultColDef"]["filter"])
        self.assertTrue(options["defaultColDef"]["floatingFilter"])
        self.assertFalse(columns["ID"]["editable"])
        self.assertTrue(columns["名称"]["filter"])
        self.assertTrue(columns["名称"]["editable"])
        self.assertEqual(columns["删除"]["cellEditor"], "agCheckboxCellEditor")

    def test_bank_review_grid_options_support_chinese_header_search(self) -> None:
        options = build_bank_review_grid_options(["选择", "流水ID", "交易日期", "方向", "对方户名", "金额", "识别类型"])
        columns = {column["field"]: column for column in options["columnDefs"]}

        self.assertEqual(options["localeText"]["contains"], "包含")
        self.assertTrue(options["defaultColDef"]["floatingFilter"])
        self.assertEqual(columns["选择"]["cellEditor"], "agCheckboxCellEditor")
        self.assertEqual(columns["对方户名"]["pinned"], "left")
        self.assertEqual(columns["金额"]["filter"], "agTextColumnFilter")

    def test_unique_bank_review_selection_keeps_one_row_per_name_and_direction(self) -> None:
        rows = [
            {"流水ID": 1, "对方户名": "上海批量供应商有限公司", "方向": "支出"},
            {"流水ID": 2, "对方户名": "上海批量供应商有限公司", "方向": "支出"},
            {"流水ID": 3, "对方户名": "上海批量供应商有限公司", "方向": "收入"},
            {"流水ID": 4, "对方户名": "上海另一家公司", "方向": "支出"},
        ]

        unique_rows = unique_bank_review_selection(rows)

        self.assertEqual([row["流水ID"] for row in unique_rows], [1, 3, 4])

    def test_payment_profile_exception_rows_show_missing_bank_fields(self) -> None:
        candidate = PaymentCandidate(
            party_id=5,
            party_name="上海缺资料供应商",
            amount=Decimal("1200.50"),
            invoice_count=2,
            invoice_ids=(1, 2),
            earliest_grace_deadline=date(2026, 6, 1),
            latest_grace_deadline=date(2026, 6, 30),
            bank_account=None,
            bank_name=None,
            is_abc_account=False,
            missing_fields=("收款方账号", "开户行支行名称"),
        )

        rows = format_payment_profile_exception_rows([candidate])

        self.assertEqual(rows[0]["供应商"], "上海缺资料供应商")
        self.assertEqual(rows[0]["缺少字段"], "收款方账号、开户行支行名称")
        self.assertEqual(rows[0]["到期应付金额"], "¥1,200.50")


if __name__ == "__main__":
    unittest.main()
