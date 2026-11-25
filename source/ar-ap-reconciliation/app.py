from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
try:
    from st_aggrid import AgGrid, DataReturnMode, GridOptionsBuilder, JsCode
except Exception:  # pragma: no cover - deployment fallback when optional component is missing
    AgGrid = None
    DataReturnMode = None
    GridOptionsBuilder = None
    JsCode = None

from finance_platform.balances import calculate_balance_rows
from finance_platform.bank_classification import bank_review_action, classify_counterparty_nature
from finance_platform.config import configured_company_name, get_settings
from finance_platform.db import init_db, test_connection
from finance_platform.exporters import export_reconciliation_workbook
from finance_platform.ledger_tools import (
    BankLedgerFilter,
    InvoiceLedgerFilter,
    excel_bytes_from_rows,
    filter_bank_rows,
    filter_invoice_rows,
    summarize_bank_rows,
    summarize_invoice_rows,
)
from finance_platform.local_store import (
    LocalStore,
    insert_parsed_bank_transactions,
    insert_parsed_invoices,
    insert_parsed_parties,
)
from finance_platform.matching import BankRecord, InvoiceRecord, MatchResult, match_payables, match_receivables
from finance_platform.models import FILE_ROLE_LABELS, FILE_ROLE_OPTIONS
from finance_platform.payment_template import (
    DEFAULT_PAYMENT_PURPOSE,
    PaymentCandidate,
    build_payment_candidates,
    build_payment_profile_exceptions,
    export_bank_payment_workbook,
    infer_payment_template_config,
)
from finance_platform.party_io import (
    PARTY_IMPORT_COLUMNS,
    build_party_template_rows,
    parse_party_import_workbook,
)
from finance_platform.parsers import ParsedBankTransaction, ParsedInvoice
from finance_platform.services import (
    parse_file_by_role,
    repair_local_invoice_import_directions,
    resolve_upload_file_role,
    save_upload,
    sha256_file,
)
from finance_platform.store import create_store


NAV_ITEMS = ["看板", "银行流水", "发票台账", "付款模板", "数据导入", "应付工作台", "应收工作台", "往来单位", "统计分析", "异常处理", "系统"]

STATUS_LABELS = {
    "matched": "已匹配",
    "unmatched": "未匹配",
    "partial": "待人工确认",
    "probable": "待人工确认",
    "unmatched_invoice": "发票未匹配",
    "unmatched_bank": "银行流水未匹配",
    "created": "已创建",
    "uploaded": "已上传",
    "parsed": "已解析",
    "failed": "失败",
}

MATCH_TYPE_LABELS = {
    "exact": "单张发票匹配",
    "exact_late": "单张发票逾期匹配",
    "monthly_summary": "月度汇总匹配",
    "invoice_subset": "多张发票合并匹配",
    "invoice_subset_late": "多张发票逾期合并匹配",
    "red_offset": "红字发票冲销",
    "early_payment": "疑似预收/预付",
    "probable": "疑似匹配",
    "unmatched": "未匹配",
    "manual_abc_bank": "人工确认农业银行",
    "manual_acceptance_bill": "人工确认承兑汇票",
    "manual_cash": "人工确认现金",
    "manual_other": "人工确认其他",
}

SETTLEMENT_METHOD_LABELS = {
    "abc_bank": "农业银行",
    "acceptance_bill": "承兑汇票",
    "cash": "现金",
    "other": "其他",
}
INLINE_MANUAL_METHOD_LABELS = {
    "农业银行": "abc_bank",
    "承兑汇票": "acceptance_bill",
    "现金": "cash",
    "其他": "other",
}
INLINE_MANUAL_METHOD_OPTIONS = ["", *INLINE_MANUAL_METHOD_LABELS.keys()]
MANUAL_EDIT_COLUMNS = {"人工处理方式", "人工日期", "人工金额", "人工备注"}

AG_GRID_LOCALE_CN = {
    "selectAll": "全选",
    "selectAllSearchResults": "全选搜索结果",
    "searchOoo": "搜索...",
    "blanks": "空白",
    "noMatches": "无匹配",
    "filterOoo": "筛选...",
    "equals": "等于",
    "notEqual": "不等于",
    "blank": "为空",
    "notBlank": "不为空",
    "empty": "请选择",
    "lessThan": "小于",
    "greaterThan": "大于",
    "lessThanOrEqual": "小于等于",
    "greaterThanOrEqual": "大于等于",
    "inRange": "介于",
    "contains": "包含",
    "notContains": "不包含",
    "startsWith": "开头是",
    "endsWith": "结尾是",
    "dateFormatOoo": "日期格式...",
    "before": "早于",
    "after": "晚于",
    "andCondition": "且",
    "orCondition": "或",
    "applyFilter": "应用",
    "resetFilter": "重置",
    "clearFilter": "清空",
    "cancelFilter": "取消",
    "columns": "列",
    "filters": "筛选",
    "rowGroupColumnsEmptyMessage": "拖到这里分组",
    "valueColumnsEmptyMessage": "拖到这里汇总",
    "pinColumn": "固定列",
    "pinLeft": "固定在左侧",
    "pinRight": "固定在右侧",
    "noPin": "取消固定",
    "autosizeThiscolumn": "自动调整本列",
    "autosizeThisColumn": "自动调整本列",
    "autoSizeThis": "自动调整本列",
    "autosizeAllColumns": "自动调整全部列",
    "autoSizeAll": "自动调整全部列",
    "resetColumns": "重置列",
    "expandAll": "全部展开",
    "collapseAll": "全部折叠",
    "copy": "复制",
    "copyWithHeaders": "复制含表头",
    "paste": "粘贴",
    "export": "导出",
    "csvExport": "导出 CSV",
    "excelExport": "导出 Excel",
    "sortAscending": "升序",
    "sortDescending": "降序",
    "sortUnSort": "取消排序",
    "hideColumn": "隐藏列",
    "loadingOoo": "加载中...",
    "noRowsToShow": "暂无数据",
}

WORKBENCH_STATUS_OPTIONS = {
    "payable": ["已付款", "已付款（逾期）", "已付款（农业银行）", "已付款（承兑汇票）", "已付款（现金）", "已付款（其他）", "已冲销", "未付款", "付款未找到发票", "待人工确认"],
    "receivable": ["已收款", "已收款（逾期）", "已收款（农业银行）", "已收款（承兑汇票）", "已收款（现金）", "已收款（其他）", "已冲销", "未收款", "收款未找到发票", "待人工确认"],
}

DISPLAY_VALUE_LABELS = {
    **STATUS_LABELS,
    **MATCH_TYPE_LABELS,
    "payable": "应付",
    "receivable": "应收",
    "outflow": "支出",
    "inflow": "收入",
    "supplier": "供应商",
    "customer": "客户",
    "party_directory": "供应商/客户目录",
    "invoice_auto": "发票明细（自动识别）",
    "payable_invoice": "应付发票明细",
    "receivable_invoice": "应收开票明细",
    "bank_statement": "银行流水",
    "name": "名称",
    "manual": "人工",
}

DISPLAY_KEY_LABELS = {
    "id": "ID",
    "period_month": "所属月份",
    "file_role": "文件类型",
    "original_filename": "原文件名",
    "stored_path": "保存路径",
    "file_hash": "文件指纹",
    "row_count": "原始行数",
    "parsed_count": "识别行数",
    "error_count": "错误行数",
    "status": "状态",
    "uploaded_at": "上传时间",
    "created_at": "创建时间",
    "updated_at": "更新时间",
    "party_id": "往来单位ID",
    "alias_name": "别名",
    "source_type": "来源",
    "is_active": "启用",
}

MATCH_EVIDENCE_LOOKBACK_YEARS = 1


def main() -> None:
    st.set_page_config(page_title="财务智能对账平台", page_icon="账", layout="wide")
    apply_theme()
    store = get_app_store()

    selected = render_top_nav()
    if selected == "看板":
        page_dashboard(store)
    elif selected == "银行流水":
        page_bank_ledger(store)
    elif selected == "发票台账":
        page_invoice_ledger(store)
    elif selected == "付款模板":
        page_payment_templates(store)
    elif selected == "数据导入":
        page_import(store)
    elif selected == "应付工作台":
        page_payable(store)
    elif selected == "应收工作台":
        page_receivable(store)
    elif selected == "往来单位":
        page_parties(store)
    elif selected == "统计分析":
        page_statistics(store)
    elif selected == "异常处理":
        page_exceptions(store)
    else:
        page_system(store)


def get_local_store() -> LocalStore:
    settings = get_settings()
    store = LocalStore(local_db_path(settings))
    store.init()
    store.rebuild_party_links()
    return store


def get_app_store() -> LocalStore:
    return create_store(get_settings())


def sqlserver_credentials_ready(settings: Any) -> bool:
    return bool(
        getattr(settings, "db_server", "")
        and getattr(settings, "db_name", "")
        and getattr(settings, "has_database_credentials", False)
    )


def storage_mode_notice(settings: Any) -> str:
    if getattr(settings, "uses_sqlserver_storage", False):
        return "当前处于 SQL Server 正式库模式，导入、维护和对账结果会直接写入 SQL Server。"
    if sqlserver_credentials_ready(settings):
        return "当前仍是本地测试库模式，页面可测试 SQL 连接，但导入和维护数据不会写入 SQL Server。"
    return "当前处于本地测试库模式，未配置完整 SQL Server 账号。"


def sync_after_write(store: LocalStore) -> None:
    return None


def local_db_path(settings: Any) -> Path:
    return Path(getattr(settings, "local_db_path", "data/local_finance.db"))


def apply_theme() -> None:
    st.markdown(
        """
        <style>
        :root {
            --ink: #111827;
            --muted: #667085;
            --subtle: #98a2b3;
            --line: #d9dee8;
            --page: #f5f7fb;
            --panel: #ffffff;
            --panel-soft: #f8fafc;
            --brand: #0b3b3c;
            --accent: #0f766e;
            --warn: #b54708;
            --danger: #b42318;
            --good: #047857;
            --radius: 14px;
            --radius-sm: 10px;
            --shadow: 0 18px 44px rgba(16, 24, 40, .08);
            --shadow-soft: 0 8px 24px rgba(16, 24, 40, .055);
        }
        html, body, [class*="css"] {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                "Hiragino Sans GB", "Microsoft YaHei", Arial, sans-serif;
        }
        .stApp {
            background: linear-gradient(180deg, #f8fafc 0%, #eef2f6 58%, #f5f7fb 100%);
            color: var(--ink);
        }
        [data-testid="stSidebar"] { display: none; }
        [data-testid="stHeader"] {
            background: rgba(248, 250, 252, .88);
            backdrop-filter: blur(10px);
        }
        .block-container {
            padding-top: 1.25rem;
            padding-bottom: 3rem;
            padding-left: 2.2rem;
            padding-right: 2.2rem;
            max-width: 1440px;
        }
        .topbar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 20px;
            padding: 4px 0 16px;
            margin-bottom: 8px;
            border-bottom: 1px solid rgba(17, 24, 39, .08);
        }
        .brand-lockup {
            display: flex;
            align-items: center;
            gap: 14px;
            min-width: 0;
        }
        .brand-lockup > div:last-child {
            min-width: 300px;
        }
        .brand-mark {
            width: 44px;
            height: 44px;
            border-radius: 12px;
            background: linear-gradient(135deg, #0b3b3c, #0f766e);
            color: #ffffff;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-weight: 820;
            letter-spacing: 0;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.22), 0 12px 28px rgba(11, 59, 60, .20);
            flex: 0 0 auto;
        }
        .brand-kicker {
            color: var(--accent);
            font-size: 12px;
            font-weight: 760;
            line-height: 1.2;
        }
        .brand-title {
            font-size: 24px;
            font-weight: 780;
            letter-spacing: 0;
            color: var(--ink);
            line-height: 1.1;
            margin-top: 3px;
        }
        .brand-subtitle {
            color: var(--muted);
            font-size: 13px;
            margin-top: 7px;
            line-height: 1.45;
            word-break: break-word;
        }
        .header-actions {
            display: flex;
            align-items: center;
            justify-content: flex-end;
            gap: 8px;
            flex-wrap: wrap;
        }
        .status-pill {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 34px;
            padding: 7px 11px;
            border: 1px solid rgba(17, 24, 39, .10);
            border-radius: 999px;
            background: rgba(255,255,255,.78);
            color: var(--muted);
            font-size: 12px;
            font-weight: 650;
            white-space: nowrap;
        }
        .status-pill.strong {
            background: #0b3b3c;
            border-color: #0b3b3c;
            color: #ffffff;
        }
        .hero-panel {
            border: 1px solid var(--line);
            border-radius: var(--radius);
            padding: 20px;
            background: rgba(255,255,255,.92);
            box-shadow: var(--shadow-soft);
            margin: 14px 0 16px;
        }
        .dashboard-command {
            border: 1px solid var(--line);
            border-radius: var(--radius);
            padding: 20px;
            background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);
            box-shadow: var(--shadow-soft);
            margin: 14px 0 14px;
        }
        .section-title {
            font-size: 19px;
            font-weight: 760;
            letter-spacing: 0;
            margin: 2px 0 10px 0;
            color: var(--ink);
        }
        .section-caption {
            color: var(--muted);
            font-size: 13px;
            line-height: 1.6;
            margin: -2px 0 12px;
            max-width: 72ch;
        }
        .metric-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 12px;
            margin: 14px 0 18px;
        }
        .metric-card {
            border: 1px solid var(--line);
            border-radius: var(--radius);
            padding: 16px;
            background: var(--panel);
            box-shadow: var(--shadow-soft);
            min-height: 124px;
            position: relative;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }
        .metric-card::before {
            content: "";
            position: absolute;
            inset: 0 0 auto 0;
            height: 3px;
            background: var(--accent);
        }
        .metric-card[data-tone="income"]::before { background: #2563eb; }
        .metric-card[data-tone="danger"]::before { background: var(--danger); }
        .metric-card[data-tone="settled"]::before { background: var(--good); }
        .metric-card[data-tone="pending"]::before { background: var(--warn); }
        .metric-card:hover {
            transform: translateY(-1px);
            box-shadow: var(--shadow);
        }
        .metric-label {
            font-size: 13px;
            color: var(--muted);
            font-weight: 700;
            margin-bottom: 8px;
        }
        .metric-value {
            font-size: clamp(24px, 2.2vw, 31px);
            font-weight: 800;
            line-height: 1.05;
            color: var(--ink);
            font-variant-numeric: tabular-nums;
        }
        .record-sheet-list {
            display: grid;
            grid-template-columns: 1fr;
            gap: 8px;
            margin: 6px 0 10px;
        }
        .record-sheet {
            border: 1px solid var(--line);
            border-radius: var(--radius-sm);
            background: #ffffff;
            overflow: hidden;
        }
        .record-sheet-title {
            color: var(--ink);
            font-size: 13px;
            font-weight: 760;
            padding: 7px 10px;
            background: #f8fafc;
            border-bottom: 1px solid var(--line);
        }
        .record-sheet-table {
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
            font-size: 13px;
        }
        .record-sheet-table th,
        .record-sheet-table td {
            border-bottom: 1px solid #edf1f5;
            padding: 6px 10px;
            text-align: left;
            vertical-align: top;
            line-height: 1.35;
            overflow-wrap: anywhere;
        }
        .record-sheet-table tr:last-child th,
        .record-sheet-table tr:last-child td {
            border-bottom: 0;
        }
        .record-sheet-table th {
            width: 128px;
            color: var(--muted);
            font-weight: 700;
            background: #fbfcfe;
        }
        .record-sheet-table td {
            color: var(--ink);
        }
        .metric-note {
            margin-top: 10px;
            font-size: 12px;
            color: var(--muted);
            line-height: 1.35;
        }
        .ledger-summary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 12px;
            margin: 16px 0 4px;
        }
        .ledger-summary-card {
            border: 1px solid var(--line);
            border-radius: var(--radius-sm);
            background: rgba(255,255,255,.86);
            padding: 13px 14px;
            min-width: 0;
            border-top: 4px solid #94a3b8;
            box-shadow: 0 10px 24px rgba(15, 23, 42, .05);
        }
        .ledger-summary-card[data-tone="settled"] {
            background: linear-gradient(180deg, rgba(236,253,245,.96), rgba(255,255,255,.92));
            border-top-color: var(--good);
        }
        .ledger-summary-card[data-tone="pending"] {
            background: linear-gradient(180deg, rgba(255,247,237,.96), rgba(255,255,255,.92));
            border-top-color: var(--warn);
        }
        .ledger-summary-card[data-tone="danger"] {
            background: linear-gradient(180deg, rgba(254,242,242,.96), rgba(255,255,255,.92));
            border-top-color: var(--danger);
        }
        .ledger-summary-card[data-tone="total"] {
            background: linear-gradient(180deg, rgba(239,246,255,.96), rgba(255,255,255,.92));
            border-top-color: #2563eb;
        }
        .ledger-summary-label {
            color: var(--muted);
            font-size: 13px;
            font-weight: 700;
            margin-bottom: 8px;
        }
        .ledger-summary-value {
            color: var(--ink);
            font-size: 25px;
            line-height: 1.15;
            font-weight: 760;
            letter-spacing: 0;
            white-space: normal;
            overflow-wrap: anywhere;
            font-variant-numeric: tabular-nums;
        }
        .workspace-card {
            border: 1px solid var(--line);
            border-radius: var(--radius);
            padding: 17px;
            background: rgba(255,255,255,.96);
            box-shadow: var(--shadow-soft);
            margin-bottom: 16px;
        }
        [data-testid="stVerticalBlockBorderWrapper"] {
            border-color: var(--line) !important;
            border-radius: var(--radius) !important;
            background: rgba(255,255,255,.94) !important;
            box-shadow: var(--shadow-soft);
        }
        [data-testid="stVerticalBlockBorderWrapper"] > div {
            border-radius: var(--radius) !important;
        }
        .action-strip {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-top: 12px;
        }
        div[data-testid="stSegmentedControl"] {
            background: rgba(255,255,255,.86);
            border: 1px solid var(--line);
            border-radius: 12px;
            padding: 5px;
            box-shadow: 0 4px 14px rgba(16, 24, 40, .045);
            overflow-x: auto;
        }
        div[data-testid="stSegmentedControl"] label {
            border-radius: 9px;
            min-height: 34px;
            font-weight: 680;
            white-space: nowrap;
        }
        div[data-testid="stMetric"] {
            background: transparent;
        }
        .stButton > button, .stDownloadButton > button {
            border-radius: var(--radius-sm);
            border: 1px solid #111827;
            background: #111827;
            color: #ffffff;
            font-weight: 680;
            min-height: 38px;
            padding: .5rem .9rem;
            box-shadow: 0 8px 18px rgba(17, 24, 39, .10);
            transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
        }
        .stButton > button:hover, .stDownloadButton > button:hover {
            border-color: #0b3b3c;
            background: #0b3b3c;
            color: #ffffff;
            transform: translateY(-1px);
            box-shadow: 0 10px 22px rgba(11, 59, 60, .16);
        }
        .stButton > button:active, .stDownloadButton > button:active {
            transform: translateY(0);
            box-shadow: 0 4px 10px rgba(17, 24, 39, .10);
        }
        [data-testid="stFileUploader"] section,
        [data-testid="stTextInput"] input,
        [data-testid="stNumberInput"] input,
        [data-testid="stDateInput"] input,
        [data-baseweb="select"] > div {
            border-radius: var(--radius-sm);
        }
        .stDataFrame {
            border-radius: var(--radius-sm);
            overflow: hidden;
            border: 1px solid rgba(17, 24, 39, .08);
        }
        [data-testid="stAlert"] {
            border-radius: var(--radius-sm);
            border: 1px solid rgba(17, 24, 39, .08);
        }
        @media (prefers-reduced-motion: no-preference) {
            .metric-card,
            .stButton > button,
            .stDownloadButton > button {
                transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease, background .16s ease;
            }
        }
        @media (max-width: 900px) {
            .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .topbar { align-items: flex-start; flex-direction: column; }
            .header-actions { justify-content: flex-start; }
            .block-container { padding-left: 1rem; padding-right: 1rem; }
        }
        @media (max-width: 560px) {
            .metric-grid { grid-template-columns: 1fr; }
            .brand-title { font-size: 21px; }
            .brand-mark { width: 40px; height: 40px; }
            .brand-lockup > div:last-child { min-width: 0; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_brand_header(db_path: str | Path, *, storage_label: str = "本地测试库", status_label: str = "测试环境") -> str:
    return (
        '<div class="topbar">'
        '<div class="brand-lockup">'
        '<div class="brand-mark">账</div>'
        '<div>'
        '<div class="brand-kicker">FINANCE WORKBENCH</div>'
        f'<div class="brand-title">{escape(configured_company_name() or "财务智能对账平台")}</div>'
        f'<div class="brand-subtitle">财务应收应付对账平台，当前使用{storage_label}：{db_path}</div>'
        '</div>'
        '</div>'
        '<div class="header-actions">'
        f'<div class="status-pill strong">{status_label}</div>'
        '<div class="status-pill">应付对账</div>'
        '<div class="status-pill">应收回款</div>'
        '<div class="status-pill">付款模板</div>'
        '</div>'
        '</div>'
    )


def render_top_nav() -> str:
    settings = get_settings()
    if settings.uses_sqlserver_storage:
        storage_value = f"{settings.db_server}:{settings.db_port}/{settings.db_name}"
        st.markdown(render_brand_header(storage_value, storage_label="SQL Server 正式库", status_label="正式库"), unsafe_allow_html=True)
    else:
        st.markdown(render_brand_header(local_db_path(settings)), unsafe_allow_html=True)
    render_relink_notice()
    return st.segmented_control("功能区", NAV_ITEMS, default="看板", label_visibility="collapsed")


def relink_stats_message(stats: dict[str, int]) -> str:
    return (
        "已重新识别应付发票 "
        f"{int(stats.get('payable_invoices', 0))} 条、应收发票 "
        f"{int(stats.get('receivable_invoices', 0))} 条、银行流水 "
        f"{int(stats.get('bank_transactions', 0))} 条。"
    )


def render_relink_notice() -> None:
    notice = st.session_state.pop("relink_notice", None)
    if notice:
        st.success(str(notice))


def flash_relink_stats(stats: dict[str, int]) -> None:
    st.session_state["relink_notice"] = relink_stats_message(stats)


def render_section_heading(title: str, caption: str | None = None) -> None:
    caption_html = f'<div class="section-caption">{caption}</div>' if caption else ""
    st.markdown(f'<div class="section-title">{title}</div>{caption_html}', unsafe_allow_html=True)


def page_dashboard(store: LocalStore) -> None:
    with st.container(border=True):
        render_section_heading("经营看板", "按账期天数和到期日查看应付、应收、逾期与银行流水分类，先看风险，再进入工作台处理明细。")
        start_date, end_date = render_date_filter("dashboard")
        summary = store.settlement_summary(start_date, end_date)
        settings = get_settings()
        storage_label = "正式库数据" if settings.uses_sqlserver_storage else "本地测试数据"

        action_cols = st.columns([1.05, .9, 5.4])
        if action_cols[0].button(f"清空{storage_label}", use_container_width=True):
            store.clear_all()
            sync_after_write(store)
            st.success(f"{storage_label}已清空。")
            st.rerun()
        if action_cols[1].button("刷新看板", use_container_width=True):
            st.rerun()

    st.markdown(render_metric_grid(summary), unsafe_allow_html=True)

    left, right = st.columns([1.35, 1])
    with left:
        with st.container(border=True):
            render_section_heading("月度变化")
            monthly = store.monthly_series(start_date, end_date)
            if monthly:
                chart_df = dataframe_for_chart(monthly).set_index("月份")
                st.line_chart(chart_df, height=320)
            else:
                st.info("还没有本地数据。可以先点“导入样例数据”。")

    with right:
        with st.container(border=True):
            render_section_heading("银行流水分类")
            show_table(format_category_rows(store.bank_category_rows(start_date, end_date)[:8]), "暂无银行流水")

    left2, right2 = st.columns([1, 1])
    with left2:
        with st.container(border=True):
            render_section_heading("到期待付 Top 供应商")
            show_table(format_amount_rows(store.party_due_totals("payable", start_date, end_date)[:8]), "暂无到期待付数据")
    with right2:
        with st.container(border=True):
            render_section_heading("逾期待收 Top 客户")
            show_table(format_amount_rows(store.party_due_totals("receivable", start_date, end_date, overdue_only=True)[:8]), "暂无逾期待收数据")


def page_bank_ledger(store: LocalStore) -> None:
    with st.container(border=True):
        render_section_heading("银行流水", "保存历年银行流水，按交易日期、对方户名、账号、摘要、收支方向和金额快速筛选，并导出当前结果。")
        c1, c2, c3, c4 = st.columns([1, 1.2, 2, 1.1])
        bank_name = c1.selectbox("银行模板", ["农业银行"], key="bank_ledger_bank")
        period_month = c2.text_input("上传月份", value=date.today().strftime("%Y-%m"), key="bank_ledger_period")
        uploaded_files = c3.file_uploader("上传银行流水 Excel", type=["xls", "xlsx"], accept_multiple_files=True, key="bank_ledger_upload")
        c4.button("新增银行流水模板", disabled=True, use_container_width=True)
        import_mode = st.segmented_control(
            "导入方式",
            ["追加导入", "覆盖文件日期范围"],
            default="追加导入",
            key="bank_ledger_import_mode",
        )
        st.caption("历史总表建议使用“覆盖文件日期范围”，避免和之前导入的同日期流水叠加。")
        if st.button("导入银行流水", key="bank_ledger_import"):
            import_uploaded_files_to_store(
                store,
                period_month,
                "bank_statement",
                uploaded_files,
                replace_bank_date_range=import_mode == "覆盖文件日期范围",
            )
        clean_cols = st.columns([1.2, 4])
        if clean_cols[0].button("清理跨文件重复流水", key="bank_ledger_dedupe", use_container_width=True):
            removed = store.deduplicate_cross_file_bank_transactions()
            sync_after_write(store)
            st.success(f"已清理 {removed} 条跨文件重复流水。")
            st.rerun()
        clean_cols[1].caption("只清理来自不同上传文件、关键字段完全一致的流水；同一文件内重复出现的手续费等记录会保留。")

    with st.container(border=True):
        render_section_heading("流水筛选")
        start_date, end_date = render_date_filter("bank_ledger")
        raw_rows = store.list_bank_transactions(start_date=start_date, end_date=end_date)
        category_labels = {row["business_category"]: row["business_category_label"] for row in raw_rows}

        f1, f2, f3, f4 = st.columns(4)
        direction_choice = f1.segmented_control("收支方向", ["全部", "收入", "支出"], default="全部", key="bank_ledger_direction")
        counterparty_name = f2.text_input("对方户名", key="bank_ledger_name")
        counterparty_account = f3.text_input("对方账号", key="bank_ledger_account")
        text = f4.text_input("摘要/用途", key="bank_ledger_text")
        f5, f6, f7 = st.columns([1.2, 1, 1])
        categories = f5.multiselect(
            "流水分类",
            options=sorted(category_labels),
            format_func=lambda value: category_labels.get(value, value),
            key="bank_ledger_categories",
        )
        min_amount = _optional_decimal(f6.text_input("最小金额", key="bank_ledger_min_amount"))
        max_amount = _optional_decimal(f7.text_input("最大金额", key="bank_ledger_max_amount"))

        direction = {"全部": "all", "收入": "inflow", "支出": "outflow"}[direction_choice]
        rows = filter_bank_rows(
            raw_rows,
            BankLedgerFilter(
                direction=direction,
                counterparty_name=counterparty_name,
                counterparty_account=counterparty_account,
                text=text,
                categories=tuple(categories),
                min_amount=min_amount,
                max_amount=max_amount,
            ),
        )
        summary = summarize_bank_rows(rows)
        render_ledger_summary(
            [
                ("行数", summary["row_count"]),
                ("收入合计", money(summary["income_total"])),
                ("支出合计", money(summary["expense_total"])),
                ("净额", money(summary["net_amount"])),
                ("未识别", summary["unidentified_count"]),
            ]
        )

    with st.container(border=True):
        render_section_heading(f"{bank_name}流水明细")
        formatted_rows = format_bank_ledger_rows(rows)
        selected_rows = render_selectable_export_table(
            formatted_rows,
            table_key="bank_ledger_table",
            export_key="bank_ledger_export",
            sheet_name="银行流水",
            filename=f"银行流水_{date.today():%Y%m%d}.xlsx",
        )
        if selected_rows:
            st.caption(f"已勾选 {len(selected_rows)} 条流水。")


def page_invoice_ledger(store: LocalStore) -> None:
    with st.container(border=True):
        render_section_heading("发票台账", "保存应付和应收发票，按开票日期、往来单位、发票号、方向、金额和状态筛选查找。")
        c1, c2, c3 = st.columns([1.2, 1.1, 2])
        role = c1.selectbox(
            "发票类型",
            ["invoice_auto", "payable_invoice", "receivable_invoice"],
            format_func=lambda value: {
                "invoice_auto": "自动识别取得/开具",
                "payable_invoice": "应付发票",
                "receivable_invoice": "应收发票",
            }[value],
            key="invoice_ledger_role",
        )
        period_month = c2.text_input("上传月份", value=date.today().strftime("%Y-%m"), key="invoice_ledger_period")
        uploaded_files = c3.file_uploader("上传发票 Excel", type=["xls", "xlsx"], accept_multiple_files=True, key="invoice_ledger_upload")
        b1, b2 = st.columns([1, 1])
        if b1.button("导入发票台账", key="invoice_ledger_import"):
            import_uploaded_files_to_store(store, period_month, role, uploaded_files)
        if b2.button("自动修复已导入发票方向", key="invoice_repair_direction"):
            report = repair_local_invoice_import_directions(store)
            sync_after_write(store)
            st.success(f"已修复 {report['repaired_files']} 个源文件，重建 {report['repaired_rows']} 张发票。")
            st.rerun()

    with st.container(border=True):
        render_section_heading("发票筛选")
        start_date, end_date = render_date_filter("invoice_ledger")
        raw_rows = store.list_invoices(start_date=start_date, end_date=end_date)
        statuses = sorted({row.get("status") or "" for row in raw_rows if row.get("status")})
        f1, f2, f3, f4 = st.columns(4)
        direction_choice = f1.segmented_control("发票方向", ["全部", "应付", "应收"], default="全部", key="invoice_ledger_direction")
        party_name = f2.text_input("往来单位", key="invoice_ledger_party")
        invoice_no = f3.text_input("发票号", key="invoice_ledger_no")
        status = f4.multiselect("匹配状态", statuses, format_func=status_label, key="invoice_ledger_status")
        f5, f6 = st.columns(2)
        min_amount = _optional_decimal(f5.text_input("最小金额", key="invoice_ledger_min_amount"))
        max_amount = _optional_decimal(f6.text_input("最大金额", key="invoice_ledger_max_amount"))

        direction = {"全部": "all", "应付": "payable", "应收": "receivable"}[direction_choice]
        rows = filter_invoice_rows(
            raw_rows,
            InvoiceLedgerFilter(
                direction=direction,
                party_name=party_name,
                invoice_no=invoice_no,
                status=tuple(status),
                min_amount=min_amount,
                max_amount=max_amount,
            ),
        )
        summary = summarize_invoice_rows(rows)
        render_ledger_summary(
            [
                ("发票数", summary["row_count"]),
                ("金额合计", money(summary["amount_total"])),
                ("已处理", summary["matched_count"]),
                ("未处理", summary["unmatched_count"]),
            ]
        )

    with st.container(border=True):
        render_section_heading("发票明细")
        render_selectable_export_table(
            format_invoice_ledger_rows(rows),
            table_key="invoice_ledger_table",
            export_key="invoice_ledger_export",
            sheet_name="发票台账",
            filename=f"发票台账_{date.today():%Y%m%d}.xlsx",
        )


def page_payment_templates(store: LocalStore) -> None:
    with st.container(border=True):
        render_section_heading("付款模板", "集中维护银行批量付款上传模板。当前先启用农业银行，其他银行保留新增入口。")
        c1, c2, c3 = st.columns([1, 1, 4])
        c1.button("新增付款模板", disabled=True, use_container_width=True)
        c2.button("新增银行", disabled=True, use_container_width=True)
        c3.info("其他银行模板后续接入；本期先把农业银行模板维护和导出跑通。")
        uploaded_template = st.file_uploader(
            "导入并识别付款模板 Excel",
            type=["xls", "xlsx"],
            key="payment_template_import_upload",
        )
        if st.button("识别并保存付款模板", key="payment_template_import_button"):
            if not uploaded_template:
                st.warning("请先选择付款模板文件。")
            else:
                try:
                    uploaded_template.seek(0)
                    path = save_upload(uploaded_template, uploaded_template.name, "payment_templates", get_settings())
                    config = infer_payment_template_config(path)
                    template_id = store.upsert_payment_template(
                        bank_code=config.bank_code,
                        bank_name=config.bank_name,
                        template_name=config.template_name,
                        sheet_name=config.sheet_name,
                        default_purpose=config.default_purpose,
                        columns=config.columns,
                    )
                    sync_after_write(store)
                    st.success(f"已识别并保存模板 #{template_id}：{config.sheet_name}，{len(config.columns)} 列。")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

    try:
        template = store.get_payment_template("ABC")
    except ValueError:
        template = None
    with st.container(border=True):
        render_section_heading("农业银行批量付款模板", "这里只维护网银上传文件的结构。已经付款过的历史行不作为模板数据。")
        if template is None:
            st.info("当前没有启用的农业银行付款模板，请上传模板文件识别保存。")
        else:
            with st.form("abc_payment_template_form"):
                c1, c2 = st.columns(2)
                template_name = c1.text_input("模板名称", value=template["template_name"])
                sheet_name = c2.text_input("Sheet 名称", value=template["sheet_name"])
                columns_text = st.text_area("导出列顺序（每行一列）", value="\n".join(template["columns"]), height=220)
                submitted = st.form_submit_button("保存模板配置")
            if submitted:
                columns = [line.strip() for line in columns_text.splitlines() if line.strip()]
                try:
                    store.save_payment_template(
                        int(template["id"]),
                        template_name=template_name,
                        sheet_name=sheet_name,
                        default_purpose=DEFAULT_PAYMENT_PURPOSE,
                        columns=columns,
                    )
                    sync_after_write(store)
                    st.success("付款模板配置已保存。")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    with st.container(border=True):
        render_section_heading("已维护模板")
        templates = store.list_payment_templates()
        show_table(format_payment_template_rows(templates), "暂无付款模板")
        if templates:
            options = {f"#{row['id']} {row['bank_name']} - {row['template_name']}": int(row["id"]) for row in templates}
            selected_label = st.selectbox("删除模板", list(options.keys()), key="payment_template_delete_select")
            if st.button("删除所选模板", key="payment_template_delete_button"):
                store.delete_payment_template(options[selected_label])
                sync_after_write(store)
                st.success("付款模板已删除。")
                st.rerun()


def import_uploaded_files_to_store(
    store: LocalStore,
    period_month: str,
    file_role: str,
    uploaded_files: list[Any] | None,
    *,
    replace_bank_date_range: bool = False,
) -> None:
    if not uploaded_files:
        st.warning("请先选择文件。")
        return
    wrote_any = False
    for uploaded in uploaded_files:
        try:
            uploaded.seek(0)
            path = save_upload(uploaded, uploaded.name, period_month, get_settings())
            actual_file_role = resolve_upload_file_role(path, file_role)
            if actual_file_role != file_role and file_role != "invoice_auto":
                st.info(f"{uploaded.name}: 已识别为 {FILE_ROLE_LABELS.get(actual_file_role, actual_file_role)}。")
            file_hash = sha256_file(path)
            existing = store.find_import_by_hash(file_hash, actual_file_role)
            if existing is None and actual_file_role in {"payable_invoice", "receivable_invoice"}:
                existing = store.find_import_by_hash(file_hash)
            if existing and not replace_bank_date_range:
                source_file_id = int(existing["id"])
                existing_role = str(existing.get("file_role") or actual_file_role)
                saved_before = store.count_import_detail_rows(existing_role, source_file_id)
                if saved_before > 0 and existing_role == actual_file_role:
                    st.warning(
                        f"{uploaded.name}: 与已导入文件 {existing['original_filename']} 内容相同，"
                        f"已有明细 {saved_before} 行，已跳过。"
                    )
                    continue
                if saved_before > 0 and existing_role in {"payable_invoice", "receivable_invoice"}:
                    removed = store.delete_invoices_for_import(source_file_id)
                    st.info(f"{uploaded.name}: 发现同文件曾按错误发票方向导入，已删除旧明细 {removed} 行并重建。")
            result = parse_file_by_role(path, actual_file_role)
            replace_range = replace_bank_date_range and actual_file_role == "bank_statement" and result.rows
            if existing:
                source_file_id = int(existing["id"])
                saved_before = store.count_import_detail_rows(actual_file_role, source_file_id)
                store.update_import_parse_counts(
                    source_file_id,
                    period_month=period_month,
                    stored_path=str(path),
                    row_count=result.row_count,
                    parsed_count=result.parsed_count,
                    error_count=result.error_count,
                    file_role=actual_file_role,
                )
                if saved_before == 0:
                    st.info(f"{uploaded.name}: 找到上次未完成的导入记录，正在补写明细。")
            else:
                source_file_id = store.record_import_file(
                    period_month,
                    actual_file_role,
                    uploaded.name,
                    str(path),
                    file_hash,
                    result.row_count,
                    result.parsed_count,
                    result.error_count,
                )
            if replace_range:
                start_date = min(row.transaction_date for row in result.rows)
                end_date = max(row.transaction_date for row in result.rows)
                removed = store.delete_bank_transactions_in_date_range(start_date, end_date)
                st.info(f"{uploaded.name}: 覆盖导入已先删除 {start_date} 至 {end_date} 的旧银行流水 {removed} 行。")
            saved = persist_local_result(store, actual_file_role, result.rows, source_file_id)
            store.update_import_saved_count(source_file_id, saved)
            store.refresh_import_saved_counts()
            wrote_any = True
            st.success(
                f"{uploaded.name}: {FILE_ROLE_LABELS.get(actual_file_role, actual_file_role)}，"
                f"解析 {result.parsed_count} 行，写入 {saved} 行。"
            )
            if result.warnings:
                st.warning("；".join(result.warnings[:5]))
        except Exception as exc:
            st.error(str(exc))
    if wrote_any:
        sync_after_write(store)


def render_ledger_summary(items: list[tuple[str, Any]]) -> None:
    st.markdown(render_ledger_summary_html(items), unsafe_allow_html=True)


def render_ledger_summary_html(items: list[tuple[str, Any]]) -> str:
    cards = ['<div class="ledger-summary-grid">']
    for label, value in items:
        tone = ledger_summary_tone(str(label))
        cards.append(
            f'<div class="ledger-summary-card" data-tone="{tone}">'
            f'<div class="ledger-summary-label">{escape(str(label))}</div>'
            f'<div class="ledger-summary-value">{escape(str(value))}</div>'
            '</div>'
        )
    cards.append("</div>")
    return "".join(cards)


def ledger_summary_tone(label: str) -> str:
    if "未找到发票" in label or "异常" in label or "逾期" in label:
        return "danger"
    if label.startswith("已付") or label.startswith("已收") or "已处理" in label:
        return "settled"
    if label.startswith("未付") or label.startswith("未收") or "待" in label:
        return "pending"
    if "总额" in label or "合计" in label:
        return "total"
    return "neutral"


def render_selectable_export_table(
    rows: list[dict[str, Any]],
    *,
    table_key: str,
    export_key: str,
    sheet_name: str,
    filename: str,
) -> list[dict[str, Any]]:
    if not rows:
        st.info("暂无数据")
        return []

    edited = st.data_editor(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
        disabled=[column for column in rows[0].keys() if column != "选择"],
        column_config={"选择": st.column_config.CheckboxColumn("选择", default=False)},
        key=table_key,
    )
    selected_rows = [row.to_dict() for _, row in edited.iterrows() if bool(row["选择"])]
    exportable_columns = [column for column in rows[0].keys() if column != "选择"]
    with st.expander("导出 Excel", expanded=False):
        c1, c2 = st.columns([1.2, 2.8])
        export_scope = c1.segmented_control("导出范围", ["当前筛选", "勾选行"], default="当前筛选", key=f"{export_key}_scope")
        export_columns = c2.multiselect("导出字段", exportable_columns, default=exportable_columns, key=f"{export_key}_columns")
        export_rows = selected_rows if export_scope == "勾选行" else rows
        if export_scope == "勾选行" and not selected_rows:
            st.warning("当前没有勾选行，下载文件会只有表头。")
        st.download_button(
            "下载 Excel",
            data=excel_bytes_from_rows(export_rows, export_columns, sheet_name=sheet_name),
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{export_key}_download",
        )
    return selected_rows


def prepare_workbench_inline_rows(
    direction: str,
    rows: list[dict[str, Any]],
    *,
    default_date: date | None = None,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    leading_columns = ["往来单位", "人工处理方式", "人工日期", "人工金额", "人工备注"]
    for row in rows:
        copied = dict(row)
        copied["人工处理方式"] = ""
        copied["人工日期"] = ""
        copied["人工金额"] = ""
        copied["人工备注"] = ""
        ordered: dict[str, Any] = {}
        for column in leading_columns:
            if column in copied:
                ordered[column] = copied[column]
        for column, value in copied.items():
            if column not in ordered:
                ordered[column] = value
        output.append(ordered)
    return output


def build_inline_manual_settlement_requests(
    direction: str,
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    action = "付款" if direction == "payable" else "收款"
    requests: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_invoice_ids: set[int] = set()
    for row in rows:
        method_label = _cell_text(row.get("人工处理方式"))
        if not method_label:
            continue
        if not _is_open_workbench_status(direction, str(row.get("状态") or "")):
            warnings.append(f"{row.get('往来单位') or '当前行'} 已结清或不可人工确认，已跳过。")
            continue
        method = INLINE_MANUAL_METHOD_LABELS.get(method_label)
        if not method:
            warnings.append(f"{row.get('往来单位') or '当前行'} 的人工处理方式无效，已跳过。")
            continue
        invoice_ids = parse_display_id_list(row.get("发票ID"))
        if len(invoice_ids) != 1:
            warnings.append(f"{row.get('往来单位') or '当前行'} 需要单张发票才能人工确认{action}，已跳过。")
            continue
        invoice_id = invoice_ids[0]
        if invoice_id in seen_invoice_ids:
            continue
        settlement_date = _parse_date_input(row.get("人工日期"))
        if settlement_date is None:
            warnings.append(f"{row.get('往来单位') or '当前行'} 缺少有效人工{action}日期，已跳过。")
            continue
        amount = parse_decimal_input(row.get("人工金额"))
        if amount is None or amount <= Decimal("0.00"):
            warnings.append(f"{row.get('往来单位') or '当前行'} 缺少有效人工{action}金额，已跳过。")
            continue
        requests.append(
            {
                "invoice_id": invoice_id,
                "settlement_method": method,
                "settlement_date": settlement_date,
                "amount": amount,
                "remark": _cell_text(row.get("人工备注")),
            }
        )
        seen_invoice_ids.add(invoice_id)
    return requests, warnings


def apply_inline_manual_settlements(
    store: LocalStore,
    direction: str,
    rows: list[dict[str, Any]],
) -> tuple[int, list[str]]:
    requests, warnings = build_inline_manual_settlement_requests(direction, rows)
    saved = 0
    for request in requests:
        try:
            store.save_manual_settlement(direction=direction, **request)
            saved += 1
        except Exception as exc:
            warnings.append(str(exc))
    if saved:
        sync_after_write(store)
    return saved, warnings


def render_record_cards_html(rows: list[dict[str, Any]], empty_message: str) -> str:
    if not rows:
        return f'<div class="record-sheet-list"><div class="record-sheet"><div class="record-sheet-title">{escape(empty_message)}</div></div></div>'
    cards = ['<div class="record-sheet-list">']
    for index, row in enumerate(rows, start=1):
        cards.append('<div class="record-sheet">')
        cards.append(f'<div class="record-sheet-title">第 {index} 条</div>')
        cards.append('<table class="record-sheet-table"><tbody>')
        for key, value in row.items():
            cards.append(
                "<tr>"
                f"<th>{escape(str(key))}</th>"
                f"<td>{escape(display_value_label(value))}</td>"
                "</tr>"
            )
        cards.append("</tbody></table></div>")
    cards.append("</div>")
    return "".join(cards)


def reconciliation_event_signature(event: dict[str, Any] | None) -> str:
    if not isinstance(event, dict):
        return ""
    explicit_id = event.get("event_id")
    if explicit_id:
        return str(explicit_id)
    row = event.get("row")
    if not isinstance(row, dict):
        row = {}
    row_parts = [
        row.get("发票ID") or "",
        row.get("流水ID") or "",
        row.get("往来单位") or "",
        row.get("发票号") or "",
        row.get("状态") or "",
        row.get("匹配方式") or "",
    ]
    return "|".join([str(event.get("event") or ""), str(event.get("column") or ""), *[str(part) for part in row_parts]])


def should_open_reconciliation_dialog(event: dict[str, Any] | None, last_signature: str | None = None) -> bool:
    if not isinstance(event, dict):
        return False
    if event.get("event") != "double_click":
        return False
    if not isinstance(event.get("row"), dict):
        return False
    if str(event.get("column") or "") in MANUAL_EDIT_COLUMNS:
        return False
    signature = reconciliation_event_signature(event)
    if signature and last_signature and signature == last_signature:
        return False
    return True


def render_workbench_detail_table(
    store: LocalStore,
    direction: str,
    invoices: list[dict[str, Any]],
    banks: list[dict[str, Any]],
    matches: list[MatchResult],
) -> list[dict[str, Any]]:
    detail_rows = format_workbench_detail_rows(direction, invoices, banks, matches)
    if not detail_rows:
        st.info("暂无应付对账数据" if direction == "payable" else "暂无应收对账数据")
        return []

    rows = prepare_workbench_inline_rows(direction, detail_rows)
    state_key = f"{direction}_workbench_inline_rows"
    event = render_double_click_grid(
        rows,
        table_key=f"{direction}_workbench_detail_grid",
        hidden_columns=["发票ID", "流水ID"],
        editable_columns=["人工处理方式", "人工日期", "人工金额", "人工备注"],
    )
    if event and event.get("data"):
        st.session_state[state_key] = event.get("data") or rows
    last_event_key = f"{direction}_last_evidence_event"
    if should_open_reconciliation_dialog(event, st.session_state.get(last_event_key)):
        st.session_state[last_event_key] = reconciliation_event_signature(event)
        evidence = build_reconciliation_evidence(direction, event["row"], invoices, banks)
        render_reconciliation_evidence_dialog(evidence)

    c1, c2 = st.columns([1.1, 4])
    if c1.button("保存行内人工处理", key=f"{direction}_inline_manual_save", use_container_width=True):
        saved, warnings = apply_inline_manual_settlements(store, direction, st.session_state.get(state_key, rows))
        if saved:
            st.success(f"已保存 {saved} 条人工处理记录。")
            st.rerun()
        elif not warnings:
            st.info("没有需要保存的人工处理记录。")
        if warnings:
            st.warning("；".join(warnings[:6]))
    render_unmatched_bank_workbench_expander(direction, banks, matches)
    return detail_rows


def render_unmatched_bank_workbench_expander(
    direction: str,
    banks: list[dict[str, Any]],
    matches: list[MatchResult],
) -> None:
    rows = format_unmatched_bank_workbench_rows(direction, banks, matches)
    if not rows:
        return
    title = "付款未找到发票" if direction == "payable" else "收款未找到发票"
    with st.expander(f"{title}（{len(rows)} 条）", expanded=False):
        show_table(rows, f"暂无{title}")


def render_double_click_grid(
    rows: list[dict[str, Any]],
    *,
    table_key: str,
    hidden_columns: list[str] | None = None,
    editable_columns: list[str] | None = None,
) -> dict[str, Any] | None:
    if not rows:
        return None
    if AgGrid is None or DataReturnMode is None or GridOptionsBuilder is None or JsCode is None:
        show_table(rows)
        st.info("当前环境未安装双击表格组件，先展示为普通表格。")
        return None

    dataframe = pd.DataFrame(rows)
    grid_builder = GridOptionsBuilder.from_dataframe(dataframe)
    grid_builder.configure_default_column(
        sortable=True,
        filter=True,
        resizable=True,
        wrapText=True,
        autoHeight=False,
    )
    grid_builder.configure_grid_options(
        rowSelection="single",
        suppressRowClickSelection=False,
        animateRows=False,
        singleClickEdit=True,
        stopEditingWhenCellsLoseFocus=True,
        localeText=AG_GRID_LOCALE_CN,
    )
    for column in hidden_columns or []:
        if column in dataframe.columns:
            grid_builder.configure_column(column, hide=True)
    for column in editable_columns or []:
        if column in dataframe.columns:
            grid_builder.configure_column(column, editable=True)
    if "人工处理方式" in dataframe.columns:
        grid_builder.configure_column(
            "人工处理方式",
            editable=True,
            cellEditor="agSelectCellEditor",
            cellEditorParams={"values": INLINE_MANUAL_METHOD_OPTIONS},
            minWidth=130,
            pinned="left",
        )
    for column, width in [
        ("往来单位", 240),
        ("人工日期", 120),
        ("人工金额", 120),
        ("人工备注", 180),
        ("发票号", 190),
        ("发票日期", 120),
        ("到期日", 120),
        ("发票金额", 130),
        ("实付日期", 120),
        ("实收日期", 120),
        ("付款金额", 130),
        ("收款金额", 130),
        ("差额", 130),
        ("状态", 130),
        ("匹配方式", 150),
        ("处理方式", 130),
        ("置信度", 90),
        ("说明", 300),
    ]:
        if column in dataframe.columns:
            grid_builder.configure_column(column, minWidth=width)
    if "往来单位" in dataframe.columns:
        grid_builder.configure_column("往来单位", pinned="left")

    collect_double_clicked_row = JsCode(
        """
        function({streamlitRerunEventTriggerName, eventData}) {
            if (!eventData || !eventData.api) {
                return null;
            }
            let rows = [];
            eventData.api.forEachNodeAfterFilterAndSort(function(node) {
                rows.push(node.data);
            });
            let column = "";
            if (eventData.column) {
                if (typeof eventData.column.getColId === "function") {
                    column = eventData.column.getColId();
                } else {
                    column = eventData.column.colId || eventData.column.field || "";
                }
            }
            const rowKey = [
                eventData.data ? (eventData.data["发票ID"] || "") : "",
                eventData.data ? (eventData.data["流水ID"] || "") : "",
                eventData.data ? (eventData.data["往来单位"] || "") : "",
                eventData.data ? (eventData.data["发票号"] || "") : ""
            ].join("|");
            if (streamlitRerunEventTriggerName === 'cellDoubleClicked' && eventData.data) {
                return {
                    event: 'double_click',
                    row: eventData.data,
                    data: rows,
                    column: column,
                    event_id: ['double_click', column, rowKey, Date.now()].join('|')
                };
            }
            if (streamlitRerunEventTriggerName === 'cellValueChanged') {
                return {event: 'edit', row: eventData.data, data: rows, column: column};
            }
            return null;
        }
        """
    )
    should_return = JsCode(
        """
        function({streamlitRerunEventTriggerName, eventData}) {
            return (streamlitRerunEventTriggerName === 'cellDoubleClicked' && !!eventData && !!eventData.data)
                || streamlitRerunEventTriggerName === 'cellValueChanged';
        }
        """
    )
    response = AgGrid(
        dataframe,
        gridOptions=grid_builder.build(),
        height=520,
        theme="streamlit",
        key=table_key,
        update_on=["cellDoubleClicked", "cellValueChanged"],
        data_return_mode=DataReturnMode.CUSTOM,
        custom_jscode_for_grid_return=collect_double_clicked_row,
        should_grid_return=should_return,
        allow_unsafe_jscode=True,
        show_toolbar=True,
        show_search=True,
        show_download_button=False,
        use_json_serialization=True,
    )
    raw_data = getattr(response, "raw_data", None)
    if isinstance(raw_data, dict):
        return raw_data
    if isinstance(response, dict):
        return response
    return None


def build_party_editor_grid_options(columns: list[str]) -> dict[str, Any]:
    width_map = {
        "ID": 90,
        "类型": 110,
        "名称": 300,
        "删除": 90,
        "简称": 140,
        "账期天数": 120,
        "收款账号": 220,
        "开户行": 320,
        "备注": 180,
        "新增别名": 180,
    }
    column_defs: list[dict[str, Any]] = []
    for column in columns:
        col_def: dict[str, Any] = {
            "field": column,
            "headerName": column,
            "editable": column not in {"ID"},
            "filter": True,
            "floatingFilter": True,
            "resizable": True,
            "sortable": True,
            "minWidth": width_map.get(column, 140),
        }
        if column in {"ID", "类型", "名称"}:
            col_def["pinned"] = "left"
        if column == "ID":
            col_def["filter"] = "agNumberColumnFilter"
        if column == "类型":
            col_def["cellEditor"] = "agSelectCellEditor"
            col_def["cellEditorParams"] = {"values": ["供应商", "客户"]}
        if column == "删除":
            col_def["cellRenderer"] = "agCheckboxCellRenderer"
            col_def["cellEditor"] = "agCheckboxCellEditor"
            col_def["filter"] = "agSetColumnFilter"
        if column == "账期天数":
            col_def["filter"] = "agNumberColumnFilter"
            col_def["cellEditor"] = "agNumberCellEditor"
        column_defs.append(col_def)
    return {
        "defaultColDef": {
            "filter": True,
            "floatingFilter": True,
            "sortable": True,
            "resizable": True,
            "editable": True,
        },
        "columnDefs": column_defs,
        "localeText": AG_GRID_LOCALE_CN,
        "singleClickEdit": True,
        "stopEditingWhenCellsLoseFocus": True,
        "suppressRowClickSelection": True,
        "animateRows": False,
    }


def render_party_batch_grid(rows: list[dict[str, Any]]) -> pd.DataFrame:
    dataframe = pd.DataFrame(rows)
    if AgGrid is None or DataReturnMode is None:
        return st.data_editor(
            dataframe,
            width="stretch",
            hide_index=True,
            disabled=["ID"],
            column_config={
                "账期天数": st.column_config.NumberColumn("账期天数", min_value=0, max_value=365, step=1),
                "ID": st.column_config.NumberColumn("ID", width="small"),
                "类型": st.column_config.SelectboxColumn("类型", options=["供应商", "客户"], width="small"),
                "名称": st.column_config.TextColumn("名称", width="large"),
                "删除": st.column_config.CheckboxColumn("删除", default=False),
                "简称": st.column_config.TextColumn("简称", width="medium"),
                "收款账号": st.column_config.TextColumn("收款账号", width="medium"),
                "开户行": st.column_config.TextColumn("开户行", width="large"),
                "备注": st.column_config.TextColumn("备注", width="medium"),
                "新增别名": st.column_config.TextColumn("新增别名", width="medium"),
            },
            height=640,
            key="party_batch_editor",
        )

    response = AgGrid(
        dataframe,
        gridOptions=build_party_editor_grid_options(list(dataframe.columns)),
        height=640,
        theme="streamlit",
        key="party_batch_editor",
        update_on=["cellValueChanged", "filterChanged", "sortChanged"],
        data_return_mode=DataReturnMode.AS_INPUT,
        allow_unsafe_jscode=True,
        fit_columns_on_grid_load=False,
        show_toolbar=True,
        show_search=True,
        show_download_button=False,
        use_json_serialization=True,
    )
    data = getattr(response, "data", None)
    if data is None and isinstance(response, dict):
        data = response.get("data")
    if data is None:
        return dataframe
    return pd.DataFrame(data)


def build_bank_review_grid_options(columns: list[str]) -> dict[str, Any]:
    width_map = {
        "选择": 90,
        "流水ID": 100,
        "交易日期": 130,
        "方向": 90,
        "对方户名": 320,
        "识别类型": 140,
        "建议动作": 200,
        "当前分类": 140,
        "金额": 140,
        "对方账号": 220,
        "对方开户行": 300,
        "摘要": 180,
        "匹配状态": 130,
    }
    column_defs: list[dict[str, Any]] = []
    for column in columns:
        col_def: dict[str, Any] = {
            "field": column,
            "headerName": column,
            "editable": column == "选择",
            "filter": True,
            "floatingFilter": True,
            "resizable": True,
            "sortable": True,
            "minWidth": width_map.get(column, 140),
        }
        if column == "选择":
            col_def["pinned"] = "left"
            col_def["cellRenderer"] = "agCheckboxCellRenderer"
            col_def["cellEditor"] = "agCheckboxCellEditor"
            col_def["filter"] = "agSetColumnFilter"
        if column == "对方户名":
            col_def["pinned"] = "left"
        if column == "流水ID":
            col_def["filter"] = "agNumberColumnFilter"
        if column == "金额":
            col_def["filter"] = "agTextColumnFilter"
        column_defs.append(col_def)
    return {
        "defaultColDef": {
            "filter": True,
            "floatingFilter": True,
            "sortable": True,
            "resizable": True,
            "editable": False,
        },
        "columnDefs": column_defs,
        "localeText": AG_GRID_LOCALE_CN,
        "singleClickEdit": True,
        "stopEditingWhenCellsLoseFocus": True,
        "suppressRowClickSelection": True,
        "animateRows": False,
    }


def render_bank_review_grid(rows: list[dict[str, Any]], *, key_prefix: str) -> pd.DataFrame:
    dataframe = pd.DataFrame(rows)
    if AgGrid is None or DataReturnMode is None:
        return st.data_editor(
            dataframe,
            width="stretch",
            hide_index=True,
            disabled=[column for column in rows[0].keys() if column != "选择"],
            column_config={"选择": st.column_config.CheckboxColumn("选择", default=False)},
            key=f"{key_prefix}_editor",
        )

    response = AgGrid(
        dataframe,
        gridOptions=build_bank_review_grid_options(list(dataframe.columns)),
        height=520,
        theme="streamlit",
        key=f"{key_prefix}_editor",
        update_on=["cellValueChanged", "filterChanged", "sortChanged"],
        data_return_mode=DataReturnMode.AS_INPUT,
        allow_unsafe_jscode=True,
        fit_columns_on_grid_load=False,
        show_toolbar=True,
        show_search=True,
        show_download_button=False,
        use_json_serialization=True,
    )
    data = getattr(response, "data", None)
    if data is None and isinstance(response, dict):
        data = response.get("data")
    if data is None:
        return dataframe
    return pd.DataFrame(data)


def unique_bank_review_selection(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        name_key = "".join(str(row.get("对方户名") or "").split())
        key = (name_key, str(row.get("方向") or "").strip())
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(row)
    return unique_rows


def build_reconciliation_evidence(
    direction: str,
    clicked_row: dict[str, Any],
    invoices: list[dict[str, Any]],
    banks: list[dict[str, Any]],
) -> dict[str, Any]:
    is_payable = direction == "payable"
    party_name = str(clicked_row.get("往来单位") or "")
    invoice_ids = parse_display_id_list(clicked_row.get("发票ID"))
    bank_ids = parse_display_id_list(clicked_row.get("流水ID"))
    invoice_map = {int(row["id"]): row for row in invoices}
    bank_map = {int(row["id"]): row for row in banks}
    linked_invoices = [invoice_map[invoice_id] for invoice_id in invoice_ids if invoice_id in invoice_map]
    linked_banks = [bank_map[bank_id] for bank_id in bank_ids if bank_id in bank_map]

    missing_bank_message = ""
    if not linked_banks:
        action = "付款" if is_payable else "收款"
        missing_bank_message = f"未找到该公司的相关农业银行{action}流水。"

    return {
        "title": f"{party_name or '当前行'} 对账依据",
        "conclusion": {
            "往来单位": party_name,
            "状态": clicked_row.get("状态") or "",
            "匹配方式": clicked_row.get("匹配方式") or "",
            "处理方式": clicked_row.get("处理方式") or "",
            "置信度": clicked_row.get("置信度") or "",
            "说明": clicked_row.get("说明") or "",
        },
        "invoices": format_invoice_rows(linked_invoices),
        "banks": format_bank_rows(linked_banks),
        "missing_bank_message": missing_bank_message,
    }


def parse_display_id_list(value: Any) -> list[int]:
    if value in (None, ""):
        return []
    if isinstance(value, (int, float)):
        return [int(value)]
    if isinstance(value, list):
        source_values = value
    else:
        text = str(value).strip()
        if text.startswith("["):
            return _json_int_list(text)
        source_values = text.replace(",", "、").replace("，", "、").split("、")
    output: list[int] = []
    for item in source_values:
        try:
            output.append(int(str(item).strip()))
        except (TypeError, ValueError):
            continue
    return output


def _is_open_workbench_status(direction: str, status: str) -> bool:
    if direction == "payable":
        return status in {"未付款", "待人工确认"}
    return status in {"未收款", "待人工确认"}


def _parse_date_input(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = _cell_text(value)
    if not text:
        return None
    for candidate in [text, text.replace("/", "-")]:
        try:
            return date.fromisoformat(candidate[:10])
        except ValueError:
            continue
    return None


def render_reconciliation_evidence_dialog(evidence: dict[str, Any]) -> None:
    title = str(evidence.get("title") or "对账依据")

    if hasattr(st, "dialog"):
        @st.dialog(title, width="large")
        def _dialog() -> None:
            render_reconciliation_evidence(evidence)

        _dialog()
    else:
        with st.expander(title, expanded=True):
            render_reconciliation_evidence(evidence)


def render_reconciliation_evidence(evidence: dict[str, Any]) -> None:
    conclusion = evidence.get("conclusion") or {}
    render_section_heading("系统判断")
    show_table([conclusion] if conclusion else [], "暂无判断信息")

    missing_bank_message = str(evidence.get("missing_bank_message") or "")
    if missing_bank_message:
        st.warning(missing_bank_message)

    render_section_heading("关联发票")
    show_table(evidence.get("invoices") or [], "这笔判断没有关联发票。")
    render_section_heading("关联银行流水")
    show_table(evidence.get("banks") or [], "这笔判断没有关联农业银行流水。")


def page_import(store: LocalStore) -> None:
    settings = get_settings()
    storage_label = "SQL Server 正式库" if settings.uses_sqlserver_storage else "本地测试库"
    with st.container(border=True):
        render_section_heading("数据导入", f"当前导入目标：{storage_label}。")

    with st.container():
        col1, col2, col3 = st.columns([1.2, 1, 1.2])
        period_month = col1.text_input("所属月份", value=date.today().strftime("%Y-%m"))
        file_role = col2.selectbox("文件类型", FILE_ROLE_OPTIONS, format_func=lambda value: FILE_ROLE_LABELS[value])
        uploaded_files = col3.file_uploader("选择 Excel", type=["xls", "xlsx"], accept_multiple_files=True)
        if st.button("导入数据"):
            import_uploaded_files_to_store(store, period_month, file_role, uploaded_files)

    with st.container(border=True):
        render_section_heading("最近导入记录")
        show_table(store.recent_imports(20), "暂无导入记录")


def page_payable(store: LocalStore) -> None:
    with st.container(border=True):
        render_section_heading("应付工作台", "查看供应商发票、银行支出流水，运行本地匹配并生成付款模板。")
        start_date, end_date = render_date_filter("payable")
        invoice_start_date = render_invoice_lookback_filter("payable", start_date, end_date)
        workbench_filters = render_workbench_scope_filters(store, "payable")
        scope_key = reconciliation_scope_key("payable", start_date, end_date, invoice_start_date)
        match_invoices, match_banks = load_reconciliation_rows(store, "payable", start_date, end_date, invoice_start_date)
        st.write(
            f"当前付款期间：待匹配银行支出 {len(match_banks)} 行；"
            f"发票回溯 {invoice_start_date:%Y-%m-%d} 至 {end_date:%Y-%m-%d}，待匹配应付发票 {len(match_invoices)} 行。"
        )
        if st.button("运行本地应付对账"):
            matches = run_local_payable_match(match_invoices, match_banks)
            run_no = store.save_match_results("payable", matches)
            sync_after_write(store)
            st.session_state["payable_matches"] = matches
            st.session_state["payable_match_invoices"] = match_invoices
            st.session_state["payable_match_banks"] = match_banks
            st.session_state["payable_match_scope"] = scope_key
            st.session_state["payable_run_no"] = run_no
            match_invoices, match_banks = load_reconciliation_rows(store, "payable", start_date, end_date, invoice_start_date)
            st.success(f"生成并保存匹配结果 {len(matches)} 条，批次 {run_no}。")

    status_invoices, status_banks = load_reconciliation_status_rows(store, "payable", start_date, end_date, invoice_start_date)
    status_matches = build_workbench_status_matches(
        "payable",
        status_invoices,
        status_banks,
        store.list_match_results("payable", limit=100000),
    )
    status_matches = filter_workbench_status_matches(status_matches, status_invoices, status_banks, start_date, end_date)
    status_matches = filter_workbench_matches_by_scope("payable", status_matches, status_invoices, status_banks, **workbench_filters)
    with st.container(border=True):
        render_section_heading("应付对账明细", "按供应商、发票、到期日、实付日期和状态展开。")
        render_ledger_summary(workbench_status_summary("payable", status_matches))
        render_workbench_detail_table(store, "payable", status_invoices, status_banks, status_matches)
        export_matches_button("应付", status_invoices, status_banks, status_matches)

    with st.expander("最近应付匹配批次", expanded=False):
        show_table(format_match_history_rows(store.list_match_results("payable", 8)), "暂无应付匹配历史")

    with st.container(border=True):
        render_section_heading("农行批量付款模板")
        render_payment_template_tool(store, start_date, end_date)

    render_reconciliation_source_expander("payable", status_invoices, status_banks)

    render_other_flow_expander(store, "payable", start_date, end_date, invoice_start_date)


def page_receivable(store: LocalStore) -> None:
    with st.container(border=True):
        render_section_heading("应收工作台", "查看客户开票和银行收入流水，运行本地匹配并沉淀回款状态。")
        start_date, end_date = render_date_filter("receivable")
        invoice_start_date = render_invoice_lookback_filter("receivable", start_date, end_date)
        workbench_filters = render_workbench_scope_filters(store, "receivable")
        scope_key = reconciliation_scope_key("receivable", start_date, end_date, invoice_start_date)
        match_invoices, match_banks = load_reconciliation_rows(store, "receivable", start_date, end_date, invoice_start_date)
        st.write(
            f"当前回款期间：待匹配银行收入 {len(match_banks)} 行；"
            f"发票回溯 {invoice_start_date:%Y-%m-%d} 至 {end_date:%Y-%m-%d}，待匹配应收发票 {len(match_invoices)} 行。"
        )
        if st.button("运行本地应收对账"):
            matches = run_local_receivable_match(match_invoices, match_banks)
            run_no = store.save_match_results("receivable", matches)
            sync_after_write(store)
            st.session_state["receivable_matches"] = matches
            st.session_state["receivable_match_invoices"] = match_invoices
            st.session_state["receivable_match_banks"] = match_banks
            st.session_state["receivable_match_scope"] = scope_key
            st.session_state["receivable_run_no"] = run_no
            match_invoices, match_banks = load_reconciliation_rows(store, "receivable", start_date, end_date, invoice_start_date)
            st.success(f"生成并保存匹配结果 {len(matches)} 条，批次 {run_no}。")

    status_invoices, status_banks = load_reconciliation_status_rows(store, "receivable", start_date, end_date, invoice_start_date)
    status_matches = build_workbench_status_matches(
        "receivable",
        status_invoices,
        status_banks,
        store.list_match_results("receivable", limit=100000),
    )
    status_matches = filter_workbench_status_matches(status_matches, status_invoices, status_banks, start_date, end_date)
    status_matches = filter_workbench_matches_by_scope("receivable", status_matches, status_invoices, status_banks, **workbench_filters)
    with st.container(border=True):
        render_section_heading("应收对账明细", "按客户、发票、到期日、实收日期和状态展开。")
        render_ledger_summary(workbench_status_summary("receivable", status_matches))
        render_workbench_detail_table(store, "receivable", status_invoices, status_banks, status_matches)
        export_matches_button("应收", status_invoices, status_banks, status_matches)

    with st.expander("最近应收匹配批次", expanded=False):
        show_table(format_match_history_rows(store.list_match_results("receivable", 8)), "暂无应收匹配历史")

    render_reconciliation_source_expander("receivable", status_invoices, status_banks)

    render_other_flow_expander(store, "receivable", start_date, end_date, invoice_start_date)


def page_parties(store: LocalStore) -> None:
    with st.container(border=True):
        render_section_heading("往来单位", "维护供应商和客户的账期天数、账户信息和银行户名别名。")
        with st.form("party_form"):
            cols = st.columns([1, 2, 1, 1.4, 2])
            party_type = cols[0].selectbox("类型", ["supplier", "customer"], format_func=lambda value: "供应商" if value == "supplier" else "客户")
            name = cols[1].text_input("名称")
            term_days = cols[2].number_input("账期天数", min_value=0, max_value=365, value=30, step=1)
            bank_account = cols[3].text_input("收款账号")
            bank_name = cols[4].text_input("开户行")
            alias = st.text_input("额外别名/银行户名")
            submitted = st.form_submit_button("保存")
        if submitted:
            if not name.strip():
                st.warning("名称不能为空。")
            else:
                party_id = store.upsert_party(
                    party_type,
                    name.strip(),
                    default_term_days=int(term_days),
                    bank_account=bank_account,
                    bank_name=bank_name,
                )
                if alias.strip():
                    store.add_alias(party_id, alias.strip(), "manual")
                if bank_name.strip():
                    store.add_alias(party_id, bank_name.strip(), "bank")
                stats = store.rebuild_party_links()
                sync_after_write(store)
                st.success(f"已保存。{relink_stats_message(stats)}")

    with st.container(border=True):
        render_party_import_export(store)

    with st.container(border=True):
        render_section_heading("基础信息批量维护")
        render_party_batch_editor(store)

    with st.container(border=True):
        render_section_heading("别名映射", "银行户名、简称和人工确认过的相似名称都会在这里形成映射，供后续流水识别使用。")
        show_table(store.list_aliases(), "暂无别名")


def page_statistics(store: LocalStore) -> None:
    with st.container(border=True):
        render_section_heading("统计分析", "余额按结束日期计算；开票和银行流水按所选日期区间统计。")
        start_date, end_date = render_date_filter("stats")
        c1, c2 = st.columns([1.2, 2.8])
        direction = c1.segmented_control(
            "统计口径",
            ["应付余额", "付款流水", "应收余额", "收款流水", "应付开票", "应收开票"],
            default="应付余额",
        )
        party_query = c2.text_input("搜索往来单位", placeholder="输入一个字即可筛选公司名称、简称", key="stats_party_query")
        balance_key = {"应付余额": "payable", "应收余额": "receivable"}.get(direction)
        total_key = {"应付开票": "payable", "付款流水": "outflow", "应收开票": "receivable", "收款流水": "inflow"}.get(direction)
        if balance_key:
            rows = format_balance_rows(calculate_balance_rows(store, balance_key, end_date))
            section_title = f"{direction}排行（截至 {format_chinese_date(end_date)}）"
        else:
            rows = format_amount_rows(store.party_totals(total_key, start_date, end_date))
            section_title = f"{direction}排行（{format_chinese_date(start_date)} 至 {format_chinese_date(end_date)}）"
        rows = filter_statistic_rows(rows, party_query)

        party_options = search_party_options(store.list_parties(), party_query)
        selected_party_id = None
        if party_query:
            if party_options:
                option_labels = {f"{row['类型']}｜{row['名称']}": row["ID"] for row in party_options}
                selected_label = st.selectbox("选择往来单位查看明细", list(option_labels), key="stats_party_selected")
                selected_party_id = option_labels[selected_label]
            else:
                st.info("没有找到包含该关键字的往来单位。")

    if selected_party_id:
        party = store.get_party(selected_party_id)
        monthly_rows = party_monthly_activity(store, selected_party_id, start_date, end_date)
        with st.container(border=True):
            render_section_heading(f"{party['name']} 往来概览")
            totals = {
                "应付": sum((row["应付"] for row in monthly_rows), Decimal("0.00")),
                "付款": sum((row["付款"] for row in monthly_rows), Decimal("0.00")),
                "应收": sum((row["应收"] for row in monthly_rows), Decimal("0.00")),
                "收款": sum((row["收款"] for row in monthly_rows), Decimal("0.00")),
            }
            render_ledger_summary([(label, money(amount)) for label, amount in totals.items()])
            if monthly_rows:
                chart_rows = [
                    {
                        "月份": format_chinese_month(row["月份"]),
                        "应付": row["应付"],
                        "付款": row["付款"],
                        "应收": row["应收"],
                        "收款": row["收款"],
                    }
                    for row in monthly_rows
                ]
                chart_df = dataframe_for_chart(chart_rows).set_index("月份")
                st.bar_chart(chart_df, height=320)
                render_section_heading("月份树状明细")
                show_table(build_party_monthly_tree_rows(monthly_rows), "暂无月份明细")
            else:
                st.info("当前时间范围内没有这家往来单位的发票或银行流水。")
    with st.container(border=True):
        render_section_heading(section_title)
        show_table(rows, "暂无统计数据")


def page_exceptions(store: LocalStore) -> None:
    with st.container(border=True):
        render_section_heading("异常处理", "集中查看逾期未处理发票和无法识别的银行户名。")
        start_date, end_date = render_date_filter("exceptions")
        invoices = store.list_invoices(start_date=start_date, end_date=end_date)
        overdue = [row for row in invoices if row["due_date"] < date.today().isoformat() and row["status"] != "matched"]
        bank_rows = store.list_bank_transactions(start_date=start_date, end_date=end_date)
        candidate_bank = [
            row
            for row in bank_rows
            if row["business_category"] in {"company_candidate", "unidentified_outflow", "unidentified_inflow", "unknown"}
        ]
        personal_bank = [row for row in bank_rows if row["business_category"] == "personal_flow"]
        non_recon_bank = [
            row
            for row in bank_rows
            if row["business_category"] in {
                "tax_payment",
                "payroll",
                "bank_fee",
                "internal_transfer",
                "refund",
                "non_reconciliation",
            }
        ]
        payment_profile_exceptions = build_payment_profile_exceptions(store, start_date, end_date)
        fuzzy_matches = store.unidentified_bank_fuzzy_matches(start_date, end_date)
        st.write(
            f"逾期未处理发票 {len(overdue)} 条，"
            f"公司/未识别候选 {len(candidate_bank)} 条，"
            f"个人流水 {len(personal_bank)} 条，"
            f"非对账流水 {len(non_recon_bank)} 条，"
            f"付款资料不完整 {len(payment_profile_exceptions)} 条，"
            f"名称候选 {len(fuzzy_matches)} 条。"
        )

    with st.container(border=True):
        render_fuzzy_match_review(store, fuzzy_matches)

    with st.container(border=True):
        render_section_heading("付款资料不完整", "供应商已进入应付候选，但缺收款账号或开户行，付款前需要补齐。")
        show_table(format_payment_profile_exception_rows(payment_profile_exceptions), "暂无付款资料不完整")

    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            render_section_heading("逾期发票")
            show_table(format_invoice_rows(overdue), "暂无逾期发票")
    with right:
        with st.container(border=True):
            render_bank_candidate_review(store, candidate_bank[:300], key_prefix="candidate_bank")

    with st.expander("个人名流水", expanded=False):
        render_bank_candidate_review(store, personal_bank[:300], key_prefix="personal_bank")

    with st.expander("非对账流水", expanded=False):
        show_table(format_bank_review_rows(non_recon_bank[:300]), "暂无非对账流水")


def render_fuzzy_match_review(store: LocalStore, matches: list[dict[str, Any]]) -> None:
    render_section_heading("名称模糊匹配审核", "系统只给出相似公司建议；勾选确认后，银行户名才会写入该往来单位别名。")
    rows = format_fuzzy_match_rows(matches)
    if not rows:
        st.info("当前筛选范围内没有需要审核的相似名称候选。")
        return

    edited = st.data_editor(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
        disabled=[column for column in rows[0].keys() if column != "选择"],
        column_config={
            "选择": st.column_config.CheckboxColumn("选择", default=False),
        },
        key="fuzzy_match_review_editor",
    )
    selected = [row for _, row in edited.iterrows() if bool(row["选择"])]
    st.write(f"已选择 {len(selected)} 条候选。")
    if st.button("确认所选名称别名", key="confirm_fuzzy_matches"):
        if not selected:
            st.warning("请先勾选需要确认的候选。")
            return
        confirmed = 0
        errors: list[str] = []
        for _, row in edited.iterrows():
            if not bool(row["选择"]):
                continue
            try:
                store.confirm_bank_fuzzy_match(int(row["流水ID"]), int(row["建议ID"]))
                confirmed += 1
            except ValueError as exc:
                errors.append(f"流水 {row['流水ID']}：{exc}")
        if confirmed:
            stats = store.rebuild_party_links()
            sync_after_write(store)
            flash_relink_stats(stats)
            st.success(f"已确认 {confirmed} 条名称别名。")
        for error in errors:
            st.error(error)
        if confirmed:
            st.rerun()


def render_bank_candidate_review(store: LocalStore, bank_rows: list[dict[str, Any]], *, key_prefix: str) -> None:
    render_section_heading("银行户名人工处理", "公司候选可建为往来单位；个人名可查询历史流水，也可按需要转为供应商或客户。")
    rows = format_bank_review_rows(bank_rows)
    if not rows:
        st.info("暂无需要人工处理的银行流水。")
        return
    edited = render_bank_review_grid(rows, key_prefix=key_prefix)
    selected = [row for _, row in edited.iterrows() if bool(row["选择"])]
    st.write(f"已选择 {len(selected)} 条流水。")
    c1, c2, c3 = st.columns([1, 1, 1])
    target_type = c1.segmented_control("转为", ["供应商", "客户"], default="供应商", key=f"{key_prefix}_target_type")
    term_days = c2.number_input("默认账期天数", min_value=0, max_value=365, value=30, step=1, key=f"{key_prefix}_term")
    if st.button("转为往来单位", key=f"{key_prefix}_promote"):
        if not selected:
            st.warning("请先勾选需要转为往来单位的流水。")
            return
        party_type = "supplier" if target_type == "供应商" else "customer"
        unique_selected = unique_bank_review_selection(selected)
        promoted = 0
        linked = 0
        for row in unique_selected:
            result = store.promote_bank_counterparty_group(
                int(row["流水ID"]),
                party_type,
                term_days=int(term_days),
            )
            promoted += 1
            linked += int(result.get("linked_bank_transactions") or 0)
        stats = store.rebuild_party_links()
        sync_after_write(store)
        flash_relink_stats(stats)
        st.success(f"已处理 {promoted} 组户名，归并同户名同方向流水 {linked} 条，并写入往来单位/别名。")
        st.rerun()


def page_system(store: LocalStore) -> None:
    with st.container(border=True):
        settings = get_settings()
        storage_text = "SQL Server 正式库" if settings.uses_sqlserver_storage else "本地测试库"
        render_section_heading("系统", f"当前存储模式：{storage_text}。正式库模式下，导入、维护和对账结果直接写入 SQL Server。")
        notice = storage_mode_notice(settings)
        if settings.uses_sqlserver_storage:
            st.success(notice)
        elif sqlserver_credentials_ready(settings):
            st.warning(notice)
        else:
            st.info(notice)
        show_table([
            {"项目": "存储模式", "值": storage_text},
            {"项目": "本地库（仅测试模式）", "值": str(local_db_path(settings))},
            {"项目": "上传目录", "值": str(settings.upload_dir)},
            {"项目": "导出目录", "值": str(settings.export_dir)},
            {"项目": "SQL 驱动", "值": settings.db_driver},
            {"项目": "SQL Server", "值": f"{settings.db_server}:{settings.db_port}/{settings.db_name}"},
            {"项目": "SQL 用户", "值": settings.db_user or "未配置"},
        ])
        cols = st.columns(3)
        if cols[0].button("测试 SQL 连接"):
            ok, message = test_connection(settings)
            st.success(message) if ok else st.error(message)
        if cols[1].button("初始化 SQL 表"):
            ok, message = init_db(settings)
            st.success(message) if ok else st.error(message)
        clear_label = "清空正式库数据" if settings.uses_sqlserver_storage else "清空本地库"
        clear_message = "正式库数据已清空。" if settings.uses_sqlserver_storage else "本地数据已清空。"
        if cols[2].button(clear_label):
            store.clear_all()
            st.success(clear_message)



def render_date_filter(key_prefix: str) -> tuple[date, date]:
    today = date.today()
    mode = st.segmented_control(
        "时间范围",
        ["今年", "近三个月", "本月", "自定义"],
        default="今年",
        key=f"{key_prefix}_mode",
    )
    if mode == "本月":
        start = date(today.year, today.month, 1)
        end = today
    elif mode == "近三个月":
        start = today - timedelta(days=90)
        end = today
    elif mode == "自定义":
        c1, c2 = st.columns(2)
        start = c1.date_input("开始日期", value=date(today.year, 1, 1), key=f"{key_prefix}_start", format="YYYY/MM/DD")
        end = c2.date_input("结束日期", value=today, key=f"{key_prefix}_end", format="YYYY/MM/DD")
    else:
        start = date(today.year, 1, 1)
        end = today
    st.caption(f"当前范围：{format_chinese_date(start)} 至 {format_chinese_date(end)}")
    return start, end


def render_invoice_lookback_filter(key_prefix: str, start_date: date, end_date: date) -> date:
    c1, c2 = st.columns([1.1, 3])
    invoice_start = c1.date_input(
        "发票回溯开始日期",
        value=default_invoice_lookback_start(start_date),
        key=f"{key_prefix}_invoice_lookback_start",
        format="YYYY/MM/DD",
    )
    c2.caption(
        f"页面显示范围为 {format_chinese_date(start_date)} 至 {format_chinese_date(end_date)}；"
        "系统会自动向前取跨年证据，用来覆盖跨期付款或收款匹配。"
    )
    return invoice_start


def default_invoice_lookback_start(start_date: date) -> date:
    return reconciliation_evidence_start_date(start_date)


def reconciliation_evidence_start_date(start_date: date) -> date:
    return date(start_date.year - MATCH_EVIDENCE_LOOKBACK_YEARS, 1, 1)


def load_reconciliation_rows(
    store: LocalStore,
    direction: str,
    start_date: date,
    end_date: date,
    invoice_start_date: date | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    invoice_start = invoice_start_date or default_invoice_lookback_start(start_date)
    evidence_start = min(invoice_start, reconciliation_evidence_start_date(start_date))
    if direction == "payable":
        return (
            open_invoice_rows(store.list_invoices("payable", evidence_start, end_date)),
            reconciliation_bank_rows(open_bank_rows(store.list_bank_transactions("outflow", evidence_start, end_date)), "payable"),
        )
    if direction == "receivable":
        return (
            open_invoice_rows(store.list_invoices("receivable", evidence_start, end_date)),
            reconciliation_bank_rows(open_bank_rows(store.list_bank_transactions("inflow", evidence_start, end_date)), "receivable"),
        )
    raise ValueError(f"不支持的对账方向: {direction}")


def load_reconciliation_status_rows(
    store: LocalStore,
    direction: str,
    start_date: date,
    end_date: date,
    invoice_start_date: date | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    invoice_start = invoice_start_date or default_invoice_lookback_start(start_date)
    evidence_start = min(invoice_start, reconciliation_evidence_start_date(start_date))
    if direction == "payable":
        return (
            formal_invoice_rows(store.list_invoices("payable", evidence_start, end_date)),
            reconciliation_bank_rows(store.list_bank_transactions("outflow", evidence_start, end_date), "payable"),
        )
    if direction == "receivable":
        return (
            formal_invoice_rows(store.list_invoices("receivable", evidence_start, end_date)),
            reconciliation_bank_rows(store.list_bank_transactions("inflow", evidence_start, end_date), "receivable"),
        )
    raise ValueError(f"不支持的对账方向: {direction}")


def build_workbench_status_matches(
    direction: str,
    invoices: list[dict[str, Any]],
    banks: list[dict[str, Any]],
    saved_results: list[dict[str, Any]],
) -> list[MatchResult]:
    invoice_map = {int(row["id"]): row for row in invoices}
    bank_map = {int(row["id"]): row for row in banks}
    covered_invoice_ids: set[int] = set()
    covered_bank_ids: set[int] = set()
    matches: list[MatchResult] = []

    for row in saved_results:
        status = str(row.get("status") or "")
        if status not in {"matched", "probable"}:
            continue
        invoice_ids = [value for value in _json_int_list(row.get("invoice_ids")) if value in invoice_map]
        bank_ids = [value for value in _json_int_list(row.get("bank_transaction_ids")) if value in bank_map]
        if not invoice_ids and not bank_ids:
            continue
        if covered_invoice_ids.intersection(invoice_ids) or covered_bank_ids.intersection(bank_ids):
            continue
        matches.append(_history_row_to_match_result(row, invoice_ids, bank_ids))
        covered_invoice_ids.update(invoice_ids)
        covered_bank_ids.update(bank_ids)

    for invoice in invoices:
        invoice_id = int(invoice["id"])
        if invoice_id in covered_invoice_ids:
            continue
        invoice_status = str(invoice.get("status") or "unmatched")
        if invoice_status == "matched":
            matches.append(_synthetic_matched_invoice_result(direction, invoice))
            covered_invoice_ids.add(invoice_id)
            continue
        if invoice_status in {"partial", "probable"}:
            matches.append(_synthetic_probable_invoice_result(direction, invoice))
            covered_invoice_ids.add(invoice_id)
            continue
        matches.append(_synthetic_unmatched_invoice_result(direction, invoice))
        covered_invoice_ids.add(invoice_id)

    for bank in banks:
        bank_id = int(bank["id"])
        if bank_id in covered_bank_ids:
            continue
        bank_status = str(bank.get("match_status") or "unmatched")
        if bank_status in {"matched", "partial", "probable"}:
            continue
        matches.append(_synthetic_unmatched_bank_result(direction, bank))
        covered_bank_ids.add(bank_id)

    return matches


def filter_workbench_status_matches(
    matches: list[MatchResult],
    invoices: list[dict[str, Any]],
    banks: list[dict[str, Any]],
    start_date: date,
    end_date: date,
) -> list[MatchResult]:
    invoice_map = {int(row["id"]): row for row in invoices}
    bank_map = {int(row["id"]): row for row in banks}
    visible: list[MatchResult] = []
    for match in matches:
        invoice_ids = [int(value) for value in _match_attr(match, "invoice_ids", [])]
        bank_ids = [int(value) for value in _match_attr(match, "bank_transaction_ids", [])]
        linked_invoices = [invoice_map[invoice_id] for invoice_id in invoice_ids if invoice_id in invoice_map]
        linked_banks = [bank_map[bank_id] for bank_id in bank_ids if bank_id in bank_map]
        if any(_invoice_in_display_range(invoice, start_date, end_date) for invoice in linked_invoices):
            visible.append(match)
            continue
        if any(_bank_in_display_range(bank, start_date, end_date) for bank in linked_banks):
            visible.append(match)
    return visible


def render_workbench_scope_filters(store: LocalStore, direction: str) -> dict[str, Any]:
    is_payable = direction == "payable"
    party_type = "supplier" if is_payable else "customer"
    party_label = "供应商" if is_payable else "客户"
    parties = store.list_parties(party_type)
    option_map = {str(row["name"]): int(row["id"]) for row in parties}
    c1, c2, c3 = st.columns([1.7, 1.3, 1.5])
    selected_names = c1.multiselect(
        f"选择{party_label}",
        list(option_map.keys()),
        key=f"{direction}_scope_party_ids",
        placeholder=f"可输入搜索并多选{party_label}",
    )
    party_query = c2.text_input(
        f"{party_label}关键字",
        key=f"{direction}_scope_party_query",
        placeholder="例如：例甲、布鲁、例戊",
    )
    statuses = c3.multiselect(
        "状态",
        WORKBENCH_STATUS_OPTIONS[direction],
        key=f"{direction}_scope_statuses",
        placeholder="可多选状态",
    )
    return {
        "selected_party_ids": [option_map[name] for name in selected_names],
        "party_query": party_query,
        "selected_statuses": statuses,
    }


def filter_workbench_matches_by_scope(
    direction: str,
    matches: list[MatchResult],
    invoices: list[dict[str, Any]],
    banks: list[dict[str, Any]],
    *,
    party_query: str = "",
    selected_party_ids: list[int] | tuple[int, ...] | set[int] | None = None,
    selected_statuses: list[str] | tuple[str, ...] | set[str] | None = None,
) -> list[MatchResult]:
    selected_ids = {int(value) for value in selected_party_ids or []}
    statuses = {_cell_text(value) for value in selected_statuses or [] if _cell_text(value)}
    query = _normal_source_search_text(party_query)
    if not selected_ids and not statuses and not query:
        return matches

    invoice_map = {int(row["id"]): row for row in invoices}
    bank_map = {int(row["id"]): row for row in banks}
    filtered: list[MatchResult] = []
    for match in matches:
        invoice_ids = [int(value) for value in _match_attr(match, "invoice_ids", [])]
        bank_ids = [int(value) for value in _match_attr(match, "bank_transaction_ids", [])]
        linked_invoices = [invoice_map[invoice_id] for invoice_id in invoice_ids if invoice_id in invoice_map]
        linked_banks = [bank_map[bank_id] for bank_id in bank_ids if bank_id in bank_map]
        party_ids = _workbench_match_party_ids(match, linked_invoices, linked_banks)
        party_text = _normal_source_search_text(
            " ".join(_workbench_match_party_names(match, linked_invoices, linked_banks))
        )
        status_text = invoice_settlement_status(
            direction,
            str(_match_attr(match, "status", "") or ""),
            str(_match_attr(match, "match_type", "") or ""),
            str(_match_attr(match, "settlement_method", "") or ""),
        )

        if selected_ids and not party_ids.intersection(selected_ids):
            continue
        if query and query not in party_text:
            continue
        if statuses and status_text not in statuses:
            continue
        filtered.append(match)
    return filtered


def _workbench_match_party_ids(
    match: MatchResult,
    invoices: list[dict[str, Any]],
    banks: list[dict[str, Any]],
) -> set[int]:
    output: set[int] = set()
    match_party_id = _match_attr(match, "party_id")
    if match_party_id not in (None, ""):
        output.add(int(match_party_id))
    for row in [*invoices, *banks]:
        party_id = row.get("party_id")
        if party_id not in (None, ""):
            output.add(int(party_id))
    return output


def _workbench_match_party_names(
    match: MatchResult,
    invoices: list[dict[str, Any]],
    banks: list[dict[str, Any]],
) -> list[str]:
    names = [str(_match_attr(match, "party_name", "") or "")]
    for invoice in invoices:
        names.append(str(invoice.get("party_name") or invoice.get("party_name_raw") or ""))
    for bank in banks:
        names.append(str(bank.get("party_name") or bank.get("counterparty_name_raw") or ""))
    return [name for name in names if name]


def _invoice_in_display_range(invoice: dict[str, Any], start_date: date, end_date: date) -> bool:
    invoice_date = _date_from_row(invoice.get("invoice_date"))
    due_date = _date_from_row(invoice.get("due_date"))
    return (
        (invoice_date is not None and start_date <= invoice_date <= end_date)
        or (due_date is not None and start_date <= due_date <= end_date)
    )


def _bank_in_display_range(bank: dict[str, Any], start_date: date, end_date: date) -> bool:
    transaction_date = _date_from_row(bank.get("transaction_date"))
    return transaction_date is not None and start_date <= transaction_date <= end_date


def _date_from_row(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def formal_invoice_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("party_id") is not None]


def open_invoice_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("party_id") is not None and row.get("status") != "matched"]


def other_invoice_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("party_id") is None and row.get("status") != "matched"]


def open_bank_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("match_status") != "matched"]


def reconciliation_bank_rows(rows: list[dict[str, Any]], direction: str) -> list[dict[str, Any]]:
    expected_category = "supplier_payment" if direction == "payable" else "customer_receipt"
    return [row for row in rows if row.get("business_category") == expected_category]


def render_metric_grid(summary: dict[str, Any]) -> str:
    cards = [
        ("到期应付", money(summary["payable_due_amount"]), f"{summary['payable_due_count']} 张，按到期日", "payable"),
        ("已导入付款", money(summary["supplier_payment_amount"]), "已导入银行流水中的供应商付款", "settled"),
        ("待确认付款", money(summary["payable_pending_amount"]), f"{summary['payable_pending_count']} 张未匹配付款证据", "pending"),
        ("逾期应付", money(summary["payable_overdue_amount"]), f"{summary['payable_overdue_count']} 张超期", "danger"),
        ("到期应收", money(summary["receivable_due_amount"]), f"{summary['receivable_due_count']} 张，按到期日", "income"),
        ("已导入收款", money(summary["customer_receipt_amount"]), "已导入银行流水中的客户回款", "settled"),
        ("待确认收款", money(summary["receivable_pending_amount"]), f"{summary['receivable_pending_count']} 张未匹配收款证据", "pending"),
        ("逾期应收", money(summary["receivable_overdue_amount"]), f"{summary['receivable_overdue_count']} 张超期", "danger"),
    ]
    html = ['<div class="metric-grid">']
    for label, value, note, tone in cards:
        html.append(
            f'<div class="metric-card" data-tone="{tone}">'
            f'<div class="metric-label">{label}</div>'
            f'<div class="metric-value">{value}</div>'
            f'<div class="metric-note">{note}</div>'
            f'</div>'
        )
    html.append("</div>")
    return "".join(html)


def render_payment_template_tool(store: LocalStore, start_date: date, end_date: date) -> None:
    candidates = build_payment_candidates(store, start_date, end_date)
    if not candidates:
        st.info("当前时间范围内没有到期的应付候选。")
        return

    try:
        template = store.get_payment_template("ABC")
    except ValueError:
        st.warning("当前没有启用的农业银行付款模板，请先到“付款模板”页导入或保存模板。")
        return
    purpose = st.text_input("付款用途（附言）", value=DEFAULT_PAYMENT_PURPOSE, key="payment_template_purpose")
    rows = format_payment_candidate_rows(candidates)
    edited = st.data_editor(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
        column_config={
            "选择": st.column_config.CheckboxColumn("选择", default=True),
            "金额": st.column_config.TextColumn("金额", disabled=True),
        },
        disabled=[column for column in rows[0].keys() if column != "选择"],
        key="payment_template_candidates",
    )
    selected_indexes = [int(row["候选编号"]) - 1 for _, row in edited.iterrows() if bool(row["选择"])]
    selected = [candidates[index] for index in selected_indexes]
    total = sum((candidate.amount for candidate in selected), Decimal("0.00"))
    st.write(f"已选择 {len(selected)} 家供应商，合计 {money(total)}。")
    missing = [candidate for candidate in selected if candidate.missing_fields]
    if missing:
        st.warning("部分供应商缺少收款账号或开户行，导出的模板会留空，请先到“往来单位”补齐后再上传网银。")

    if st.button("生成农行批量付款模板"):
        if not selected:
            st.warning("请至少选择一家供应商。")
            return
        settings = get_settings()
        output = settings.export_dir / f"农行批量转账上传_{date.today():%Y%m%d}.xls"
        export_bank_payment_workbook(
            output,
            selected,
            purpose=purpose,
            columns=template.get("columns"),
            sheet_name=template.get("sheet_name"),
        )
        st.success(f"已生成 {output.name}")
        st.download_button(
            "下载农行上传文件",
            data=output.read_bytes(),
            file_name=output.name,
            mime="application/vnd.ms-excel",
        )


def render_manual_settlement_tool(
    store: LocalStore,
    direction: str,
    invoices: list[dict[str, Any]],
    start_date: date,
    end_date: date,
) -> None:
    is_payable = direction == "payable"
    title = "人工确认付款" if is_payable else "人工确认收款"
    action = "付款" if is_payable else "收款"
    open_invoices = [
        row for row in invoices
        if row.get("status") != "matched" and _invoice_in_display_range(row, start_date, end_date)
    ]
    with st.expander(title, expanded=False):
        if not open_invoices:
            st.info(f"当前范围内没有需要人工确认{action}的发票。")
            return
        options = {manual_settlement_invoice_label(row): int(row["id"]) for row in open_invoices}
        c1, c2, c3 = st.columns([2.2, 1.2, 1])
        selected_label = c1.selectbox("选择发票", list(options.keys()), key=f"{direction}_manual_invoice")
        method = c2.selectbox(
            "处理方式",
            ["abc_bank", "acceptance_bill", "cash", "other"],
            key=f"{direction}_manual_method",
            format_func=lambda value: f"{settlement_method_label(value)}{action}",
        )
        settlement_date = c3.date_input(
            f"实{action}日期",
            value=date.today(),
            key=f"{direction}_manual_date",
            format="YYYY/MM/DD",
        )
        selected_invoice = next(row for row in open_invoices if int(row["id"]) == options[selected_label])
        amount_default = str(Decimal(str(selected_invoice.get("invoice_amount") or "0.00")).quantize(Decimal("0.01")))
        c4, c5 = st.columns([1, 2.5])
        amount_text = c4.text_input(f"实{action}金额", value=amount_default, key=f"{direction}_manual_amount")
        remark = c5.text_input("备注", value="", key=f"{direction}_manual_remark")
        if st.button(f"确认人工{action}", key=f"{direction}_manual_submit"):
            amount = parse_decimal_input(amount_text)
            if amount is None or amount <= Decimal("0.00"):
                st.warning(f"请输入有效的实{action}金额。")
                return
            run_no = store.save_manual_settlement(
                direction=direction,
                invoice_id=options[selected_label],
                settlement_method=method,
                settlement_date=settlement_date,
                amount=amount,
                remark=remark,
            )
            sync_after_write(store)
            st.success(f"已保存人工{action}记录，批次 {run_no}。")
            st.rerun()


def manual_settlement_invoice_label(row: dict[str, Any]) -> str:
    amount = money(row.get("invoice_amount") or "0")
    return " | ".join(
        [
            str(row.get("party_name") or row.get("party_name_raw") or ""),
            str(row.get("invoice_no") or "无发票号"),
            str(row.get("invoice_date") or ""),
            amount,
        ]
    )


def render_other_flow_expander(
    store: LocalStore,
    direction: str,
    start_date: date,
    end_date: date,
    invoice_start_date: date,
) -> None:
    is_payable = direction == "payable"
    invoice_direction = "payable" if is_payable else "receivable"
    bank_direction = "outflow" if is_payable else "inflow"
    title = "其他付款/名单外支出" if is_payable else "其他收款/名单外收入"
    invoice_title = "名单外应付发票" if is_payable else "名单外应收发票"
    bank_title = "其他支出流水" if is_payable else "其他收入流水"
    with st.expander(title, expanded=False):
        query = st.text_input("搜索其他记录", key=f"{direction}_other_search")
        other_invoices = other_invoice_rows(store.list_invoices(invoice_direction, invoice_start_date, end_date))
        other_banks = [
            row
            for row in open_bank_rows(store.list_bank_transactions(bank_direction, start_date, end_date))
            if row.get("business_category") not in {"supplier_payment", "customer_receipt"}
        ]
        matched_invoices = filter_other_rows(other_invoices, query)
        matched_banks = filter_other_rows(other_banks, query)
        left, right = st.columns(2)
        with left:
            render_section_heading(invoice_title)
            show_table(format_invoice_rows(matched_invoices[:200]), "暂无名单外发票")
        with right:
            render_section_heading(bank_title)
            show_table(format_bank_rows(matched_banks[:200]), "暂无其他流水")


def render_reconciliation_source_expander(
    direction: str,
    invoices: list[dict[str, Any]],
    banks: list[dict[str, Any]],
) -> None:
    is_payable = direction == "payable"
    title = "原始应付发票和银行支出流水" if is_payable else "原始应收发票和银行收入流水"
    invoice_title = "应付发票明细" if is_payable else "应收开票明细"
    bank_title = "银行支出流水" if is_payable else "银行收入流水"
    with st.expander(title, expanded=False):
        filters = render_source_filters(direction)
        matched_invoices = filter_source_rows(invoices, filters, "invoice")
        matched_banks = filter_source_rows(banks, filters, "bank")
        render_section_heading(invoice_title)
        show_table(format_invoice_rows(matched_invoices[:300]), f"暂无{invoice_title}")
        render_section_heading(bank_title)
        show_table(format_bank_rows(matched_banks[:300]), f"暂无{bank_title}")


def render_source_filters(direction: str) -> dict[str, str]:
    c1, c2, c3 = st.columns([1.4, 1.4, 1])
    c4, c5 = st.columns([1, 1.8])
    return {
        "party": c1.text_input("往来单位/银行户名", key=f"{direction}_source_party", placeholder="例如：例戊、例甲、布鲁"),
        "number": c2.text_input("发票号/流水ID/账号", key=f"{direction}_source_number", placeholder="例如：900000 或 1008"),
        "amount": c3.text_input("金额", key=f"{direction}_source_amount", placeholder="例如：3400"),
        "date": c4.text_input("日期", key=f"{direction}_source_date", placeholder="例如：2026-06"),
        "status": c5.text_input("状态/摘要/分类", key=f"{direction}_source_status", placeholder="例如：未匹配、已匹配、转账"),
    }


def render_party_import_export(store: LocalStore) -> None:
    render_section_heading("往来单位导入导出", "用 Excel 批量维护供应商、客户、账期天数、账号、开户行和别名。")
    c1, c2, c3 = st.columns([1.1, 1.3, 2.6])
    default_type = c1.segmented_control("默认类型", ["供应商", "客户"], default="供应商", key="party_import_default_type")
    import_strategy = c2.selectbox("导入策略", ["覆盖全部字段", "只新增", "只补空字段"], key="party_import_strategy")
    uploaded_files = c3.file_uploader(
        "导入往来单位 Excel",
        type=["xls", "xlsx"],
        accept_multiple_files=True,
        key="party_import_upload",
    )
    c4, c5 = st.columns([1, 4])
    c4.download_button(
        "下载导入模板",
        data=excel_bytes_from_rows(build_party_template_rows(), PARTY_IMPORT_COLUMNS, sheet_name="往来单位模板"),
        file_name="往来单位导入模板.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
    c5.caption("导入时会自动识别“月结30天”“90天”“2个月”等账期写法，并统一保存为天数。")
    if st.button("导入往来单位", key="party_import_button"):
        if not uploaded_files:
            st.warning("请先选择往来单位 Excel。")
        else:
            total_saved = 0
            total_skipped = 0
            warnings: list[str] = []
            default_party_type = "supplier" if default_type == "供应商" else "customer"
            for uploaded in uploaded_files:
                try:
                    uploaded.seek(0)
                    path = save_upload(uploaded, uploaded.name, "party_directory", get_settings())
                    result = parse_party_import_workbook(path, default_party_type=default_party_type)
                    saved, skipped = persist_party_import_rows(store, result.rows, strategy=import_strategy)
                    total_saved += saved
                    total_skipped += skipped
                    warnings.extend(result.warnings)
                    st.success(f"{uploaded.name}: 识别 {result.parsed_count} 行，保存 {saved} 行，跳过 {skipped} 行。")
                except Exception as exc:
                    st.error(f"{uploaded.name}: {exc}")
            if warnings:
                st.warning("；".join(warnings[:5]))
            if total_saved or total_skipped:
                st.info(f"本次合计保存 {total_saved} 行，跳过 {total_skipped} 行。")
                stats = store.rebuild_party_links()
                sync_after_write(store)
                st.success(relink_stats_message(stats))
                current_party_count = len(store.list_parties())
                if current_party_count:
                    st.success(f"当前页面已读到往来单位 {current_party_count} 条。")
                elif total_saved:
                    st.error("导入流程报告已保存，但当前页面仍读不到往来单位。请检查当前存储模式和当前数据库连接。")

    with st.expander("导出往来单位", expanded=False):
        export_rows = format_party_export_rows(store.list_parties())
        export_columns = st.multiselect(
            "导出字段",
            PARTY_IMPORT_COLUMNS,
            default=PARTY_IMPORT_COLUMNS,
            key="party_export_columns",
        )
        st.download_button(
            "下载当前往来单位",
            data=excel_bytes_from_rows(export_rows, export_columns, sheet_name="往来单位"),
            file_name=f"往来单位_{date.today():%Y%m%d}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="party_export_download",
        )


def persist_party_import_rows(store: LocalStore, rows: list[Any], *, strategy: str) -> tuple[int, int]:
    existing = {(row["party_type"], row["name"]): row for row in store.list_parties()}
    saved = 0
    skipped = 0
    for row in rows:
        key = (row.party_type, row.name)
        current = existing.get(key)
        if strategy == "只新增" and current is not None:
            skipped += 1
            continue
        if strategy == "只补空字段" and current is not None:
            values: dict[str, Any] = {}
            if not current.get("short_name") and row.short_name:
                values["short_name"] = row.short_name
            if not current.get("bank_account") and row.bank_account:
                values["bank_account"] = row.bank_account
            if not current.get("bank_name") and row.bank_name:
                values["bank_name"] = row.bank_name
            if not current.get("remark") and row.remark:
                values["remark"] = row.remark
            if values:
                store.update_party_fields(int(current["id"]), **values)
                saved += 1
            else:
                skipped += 1
            if row.alias_name:
                store.add_alias(int(current["id"]), row.alias_name, "import")
            continue
        party_id = store.upsert_party(
            row.party_type,
            row.name,
            short_name=row.short_name,
            default_term_days=row.default_term_days,
            bank_account=row.bank_account,
            bank_name=row.bank_name,
            remark=row.remark,
        )
        if row.alias_name:
            store.add_alias(party_id, row.alias_name, "import")
        if row.bank_name:
            store.add_alias(party_id, row.bank_name, "bank")
        existing[key] = store.get_party(party_id) or {}
        saved += 1
    return saved, skipped


def render_party_batch_editor(store: LocalStore) -> None:
    parties = store.list_parties()
    if not parties:
        st.info("暂无往来单位")
        return
    rows = format_party_edit_rows(parties)
    edited = render_party_batch_grid(rows)
    action_cols = st.columns([1, 1, 1, 4])
    save_changes = action_cols[0].button("保存批量修改", key="party_save_changes", use_container_width=True)
    delete_selected = action_cols[1].button("删除勾选单位", key="party_delete_selected", use_container_width=True)
    delete_all = action_cols[2].button("一键清空往来单位", key="party_delete_all", use_container_width=True)
    if save_changes or delete_selected:
        changed = 0
        archived = 0
        errors: list[str] = []
        for _, row in edited.iterrows():
            party_id = int(row["ID"])
            original = next(item for item in rows if int(item["ID"]) == party_id)
            if bool(row.get("删除")):
                store.archive_party(party_id)
                archived += 1
                continue
            if delete_selected:
                continue
            try:
                if _party_identity_changed(row, original):
                    store.update_party_identity(
                        party_id,
                        party_type=_party_type_key(row["类型"]),
                        name=_cell_text(row["名称"]),
                    )
                    changed += 1
                values = {
                    "default_term_days": _safe_int(row["账期天数"], int(original["账期天数"])),
                    "bank_account": _cell_text(row["收款账号"]),
                    "bank_name": _cell_text(row["开户行"]),
                    "short_name": _cell_text(row["简称"]),
                    "remark": _cell_text(row["备注"]),
                    "alias_name": _cell_text(row["新增别名"]),
                }
                if _party_row_changed(row, original):
                    store.update_party_fields(party_id, **values)
                    changed += 1
            except ValueError as exc:
                errors.append(f"{original['名称']}：{exc}")
        if delete_selected:
            sync_after_write(store)
            st.success(f"已删除勾选往来单位 {archived} 条。")
        else:
            stats = store.rebuild_party_links()
            sync_after_write(store)
            flash_relink_stats(stats)
            st.success(f"已保存 {changed} 条修改，删除 {archived} 条。")
        for error in errors[:8]:
            st.error(error)
        if not errors:
            st.rerun()
    if delete_all:
        archived = store.archive_all_parties()
        sync_after_write(store)
        st.success(f"已清空往来单位 {archived} 条。")
        st.rerun()


def persist_local_result(store: LocalStore, file_role: str, rows: list[Any], source_file_id: int | None) -> int:
    if file_role == "party_directory":
        return insert_parsed_parties(store, rows)
    if file_role in ("payable_invoice", "receivable_invoice"):
        return insert_parsed_invoices(store, rows, source_file_id)
    if file_role == "bank_statement":
        return insert_parsed_bank_transactions(store, rows, source_file_id)
    return 0


def run_local_payable_match(invoices: list[dict[str, Any]], banks: list[dict[str, Any]]) -> list[MatchResult]:
    return match_payables(_invoice_records_from_rows(invoices), _bank_records_from_rows(banks, "expense_amount"))


def run_local_receivable_match(invoices: list[dict[str, Any]], banks: list[dict[str, Any]]) -> list[MatchResult]:
    return match_receivables(_invoice_records_from_rows(invoices), _bank_records_from_rows(banks, "income_amount"))


def _invoice_records_from_rows(rows: list[dict[str, Any]]) -> list[InvoiceRecord]:
    return [
        InvoiceRecord(
            id=int(row["id"]),
            party_id=row["party_id"],
            party_name=row["party_name"] or row["party_name_raw"],
            amount=Decimal(row["invoice_amount"]),
            invoice_date=date.fromisoformat(row["invoice_date"]),
            due_date=date.fromisoformat(row["due_date"]),
            grace_deadline=date.fromisoformat(row["due_date"]),
        )
        for row in rows
    ]


def _bank_records_from_rows(rows: list[dict[str, Any]], amount_key: str) -> list[BankRecord]:
    return [
        BankRecord(
            id=int(row["id"]),
            party_id=row["party_id"],
            counterparty_name=row["party_name"] or row["counterparty_name_raw"] or "未识别",
            amount=Decimal(row[amount_key] or "0.00"),
            transaction_date=date.fromisoformat(row["transaction_date"]),
            direction=row["direction"],
        )
        for row in rows
    ]


def export_matches_button(
    label: str,
    invoices: list[dict[str, Any]],
    banks: list[dict[str, Any]],
    matches: list[MatchResult],
) -> None:
    settings = get_settings()
    output = settings.export_dir / f"{label}对账结果_{date.today():%Y%m%d}.xlsx"
    direction = "payable" if label == "应付" else "receivable"
    detail_rows = format_workbench_detail_rows(direction, invoices, banks, matches)
    export_reconciliation_workbook(
        output,
        payable_details=format_invoice_rows(invoices),
        bank_details=format_bank_rows(banks),
        match_results=matches,
        match_details=detail_rows,
        exceptions=[row for row in detail_rows if not _is_settled_display_status(direction, row.get("状态"))],
    )
    st.download_button(
        f"导出当前{label}匹配结果",
        data=output.read_bytes(),
        file_name=output.name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _is_settled_display_status(direction: str, status: Any) -> bool:
    prefix = "已付款" if direction == "payable" else "已收款"
    return str(status or "").startswith(prefix)


def reconciliation_scope_key(direction: str, start_date: date, end_date: date, invoice_start_date: date) -> str:
    return "|".join(
        [
            direction,
            start_date.isoformat(),
            end_date.isoformat(),
            invoice_start_date.isoformat(),
        ]
    )


def scoped_session_matches(key_prefix: str, session_state: Any, scope_key: str) -> list[Any]:
    if session_state.get(f"{key_prefix}_match_scope") != scope_key:
        return []
    return session_state.get(f"{key_prefix}_matches", [])


def match_export_context(
    key_prefix: str,
    session_state: Any,
    current_invoices: list[dict[str, Any]],
    current_banks: list[dict[str, Any]],
    *,
    scope_key: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if scope_key is not None and session_state.get(f"{key_prefix}_match_scope") != scope_key:
        return current_invoices, current_banks
    return (
        session_state.get(f"{key_prefix}_match_invoices", current_invoices),
        session_state.get(f"{key_prefix}_match_banks", current_banks),
    )


def dataframe_for_chart(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for column in df.columns:
        if column != "月份":
            df[column] = df[column].apply(lambda value: float(value or 0))
    return df


def format_chinese_date(value: date | str) -> str:
    if isinstance(value, str):
        value = date.fromisoformat(value[:10])
    return f"{value.year}年{value.month}月{value.day}日"


def format_chinese_month(value: str) -> str:
    year, month = value.split("-")[:2]
    return f"{int(year)}年{int(month)}月"


def search_party_options(parties: list[dict[str, Any]], keyword: str) -> list[dict[str, Any]]:
    query = _cell_text(keyword)
    rows = []
    for party in parties:
        name = _cell_text(party.get("name"))
        short_name = _cell_text(party.get("short_name"))
        if query and query not in name and query not in short_name:
            continue
        rows.append(
            {
                "ID": int(party["id"]),
                "类型": "供应商" if party.get("party_type") == "supplier" else "客户",
                "名称": name,
                "简称": short_name,
            }
        )
    return rows


def filter_statistic_rows(rows: list[dict[str, Any]], keyword: str) -> list[dict[str, Any]]:
    query = _cell_text(keyword)
    if not query:
        return rows
    return [
        row
        for row in rows
        if query in _cell_text(row.get("往来单位")) or query in _cell_text(row.get("名称"))
    ]


def party_monthly_activity(store: LocalStore, party_id: int, start_date: date, end_date: date) -> list[dict[str, Any]]:
    months: dict[str, dict[str, Decimal]] = {}
    for invoice in store.list_invoices(start_date=start_date, end_date=end_date):
        if int(invoice.get("party_id") or 0) != int(party_id):
            continue
        month = str(invoice["invoice_date"])[:7]
        bucket = months.setdefault(month, _empty_month_bucket(month))
        amount = Decimal(str(invoice.get("invoice_amount") or "0"))
        if invoice.get("direction") == "payable":
            bucket["应付"] += amount
        else:
            bucket["应收"] += amount
    for bank in store.list_bank_transactions(start_date=start_date, end_date=end_date):
        if int(bank.get("party_id") or 0) != int(party_id):
            continue
        if bank.get("direction") == "outflow" and bank.get("business_category") != "supplier_payment":
            continue
        if bank.get("direction") == "inflow" and bank.get("business_category") != "customer_receipt":
            continue
        month = str(bank["transaction_date"])[:7]
        bucket = months.setdefault(month, _empty_month_bucket(month))
        if bank.get("direction") == "outflow":
            bucket["付款"] += Decimal(str(bank.get("expense_amount") or "0"))
        else:
            bucket["收款"] += Decimal(str(bank.get("income_amount") or "0"))
    return [months[month] for month in sorted(months)]


def filter_other_rows(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    text = _cell_text(query)
    if not text:
        return rows
    fields = (
        "party_name",
        "party_name_raw",
        "counterparty_name_raw",
        "invoice_no",
        "summary",
        "subject",
        "counterparty_account",
        "counterparty_bank",
    )
    output = []
    for row in rows:
        haystack = " ".join(_cell_text(row.get(field)) for field in fields)
        if text in haystack:
            output.append(row)
    return output


SOURCE_FILTER_FIELDS = {
    "invoice": {
        "party": ["party_name", "party_name_raw", "customer_name", "supplier_name", "buyer_name", "seller_name"],
        "number": ["id", "invoice_no", "red_confirm_no"],
        "amount": ["invoice_amount", "amount_without_tax", "tax_amount"],
        "date": ["invoice_date", "due_date", "grace_deadline"],
        "status": ["status", "tax_status", "is_positive", "is_blue_invoice"],
    },
    "bank": {
        "party": ["counterparty_name_raw", "counterparty_name", "matched_party_name"],
        "number": ["id", "counterparty_account", "counterparty_bank"],
        "amount": ["income_amount", "expense_amount", "amount"],
        "date": ["transaction_date"],
        "status": ["match_status", "business_category", "business_category_label", "summary", "subject"],
    },
}


def filter_source_rows(rows: list[dict[str, Any]], filters: dict[str, Any], row_kind: str) -> list[dict[str, Any]]:
    active_filters = {key: _cell_text(value) for key, value in filters.items() if _cell_text(value)}
    if not active_filters:
        return rows
    field_map = SOURCE_FILTER_FIELDS[row_kind]
    output: list[dict[str, Any]] = []
    for row in rows:
        if all(_source_row_matches(row, field_map.get(key, []), query) for key, query in active_filters.items()):
            output.append(row)
    return output


def _source_row_matches(row: dict[str, Any], fields: list[str], query: str) -> bool:
    query_text = _normal_source_search_text(query)
    if not query_text:
        return True
    for field in fields:
        if field not in row:
            continue
        value = row.get(field)
        source_text = _normal_source_search_text(value)
        label_text = _normal_source_search_text(display_value_label(value))
        if query_text in source_text or query_text in label_text:
            return True
    return False


def _normal_source_search_text(value: Any) -> str:
    return (
        display_value_label(value)
        .replace(" ", "")
        .replace(",", "")
        .replace("，", "")
        .replace("¥", "")
        .replace("￥", "")
        .lower()
    )


def build_party_monthly_tree_rows(monthly_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in monthly_rows:
        month = format_chinese_month(str(row["月份"]))
        for item in ["应付", "付款", "应收", "收款"]:
            amount = Decimal(str(row.get(item) or "0"))
            output.append({"月份": month, "项目": item, "金额": money(amount)})
    return output


def _empty_month_bucket(month: str) -> dict[str, Any]:
    return {
        "月份": month,
        "应付": Decimal("0.00"),
        "付款": Decimal("0.00"),
        "应收": Decimal("0.00"),
        "收款": Decimal("0.00"),
    }


def show_table(rows: list[dict[str, Any]], empty_message: str = "暂无数据") -> None:
    if rows:
        safe_rows = []
        for row in rows:
            safe_rows.append({
                display_key_label(key): display_value_label(value)
                for key, value in row.items()
            })
        st.dataframe(safe_rows, width="stretch", hide_index=True)
    else:
        st.info(empty_message)


def display_key_label(value: Any) -> str:
    text = str(value)
    return DISPLAY_KEY_LABELS.get(text, text)


def display_value_label(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return DISPLAY_VALUE_LABELS.get(text, text)


def status_label(value: Any) -> str:
    return STATUS_LABELS.get(str(value or ""), str(value or ""))


def match_type_label(value: Any) -> str:
    return MATCH_TYPE_LABELS.get(str(value or ""), str(value or ""))


def settlement_method_label(value: Any) -> str:
    return SETTLEMENT_METHOD_LABELS.get(str(value or ""), str(value or ""))


def invoice_settlement_status(
    direction: str,
    match_status: str,
    match_type: str = "",
    settlement_method: str = "",
) -> str:
    is_payable = direction == "payable"
    if match_type == "red_offset":
        return "已冲销"
    if match_status == "matched":
        settled = "已付款" if is_payable else "已收款"
        if settlement_method:
            return f"{settled}（{settlement_method_label(settlement_method)}）"
        return f"{settled}（逾期）" if "late" in match_type else settled
    if match_status == "probable":
        return "待人工确认"
    if match_status == "unmatched_invoice":
        return "未付款" if is_payable else "未收款"
    if match_status == "unmatched_bank":
        return "付款未找到发票" if is_payable else "收款未找到发票"
    return status_label(match_status)


def format_invoice_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        output.append({
            "发票ID": int(row["id"]),
            "方向": "应付" if row["direction"] == "payable" else "应收",
            "往来单位": row.get("party_name") or row.get("party_name_raw"),
            "发票号": row.get("invoice_no"),
            "发票日期": row.get("invoice_date"),
            "发票金额": money(Decimal(row["invoice_amount"])),
            "税局状态": row.get("invoice_status_raw") or "",
            "正数发票": _positive_invoice_label(row.get("is_positive")),
            "原蓝字发票": row.get("original_invoice_no") or "",
            "账期天数": row.get("term_days"),
            "到期日": row.get("due_date"),
            "状态": status_label(row.get("status")),
        })
    return output


def format_bank_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        amount = row.get("expense_amount") if row["direction"] == "outflow" else row.get("income_amount")
        output.append({
            "流水ID": int(row["id"]),
            "方向": "支出" if row["direction"] == "outflow" else "收入",
            "交易日期": row.get("transaction_date"),
            "对方户名": row.get("party_name") or row.get("counterparty_name_raw"),
            "金额": money(Decimal(amount or "0.00")),
            "分类": row.get("business_category_label") or "",
            "摘要": row.get("summary"),
            "状态": status_label(row.get("match_status")),
        })
    return output


def format_bank_ledger_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        amount = row.get("expense_amount") if row["direction"] == "outflow" else row.get("income_amount")
        output.append({
            "选择": False,
            "流水ID": int(row["id"]),
            "银行": "农业银行",
            "交易日期": row.get("transaction_date"),
            "方向": "支出" if row["direction"] == "outflow" else "收入",
            "对方户名": row.get("counterparty_name_raw") or "",
            "识别往来单位": row.get("party_name") or "",
            "对方账号": row.get("counterparty_account") or "",
            "对方开户行": row.get("counterparty_bank") or "",
            "金额": money(Decimal(amount or "0.00")),
            "分类": row.get("business_category_label") or "",
            "摘要": row.get("summary") or "",
            "用途/科目": row.get("subject") or "",
            "匹配状态": status_label(row.get("match_status")),
        })
    return output


def format_bank_review_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        amount = row.get("expense_amount") if row["direction"] == "outflow" else row.get("income_amount")
        nature = classify_counterparty_nature(
            row.get("counterparty_name_raw"),
            summary=row.get("summary"),
            bank=row.get("counterparty_bank"),
        )
        output.append(
            {
                "选择": False,
                "流水ID": int(row["id"]),
                "交易日期": row.get("transaction_date"),
                "方向": "支出" if row["direction"] == "outflow" else "收入",
                "对方户名": row.get("counterparty_name_raw") or "",
                "识别类型": nature.label,
                "建议动作": bank_review_action(nature),
                "当前分类": row.get("business_category_label") or "",
                "金额": money(Decimal(amount or "0.00")),
                "对方账号": row.get("counterparty_account") or "",
                "摘要": row.get("summary") or "",
                "匹配状态": status_label(row.get("match_status")),
            }
        )
    return output


def format_invoice_ledger_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        output.append({
            "选择": False,
            "发票ID": int(row["id"]),
            "方向": "应付" if row["direction"] == "payable" else "应收",
            "往来单位": row.get("party_name") or row.get("party_name_raw"),
            "发票号": row.get("invoice_no") or "",
            "发票日期": row.get("invoice_date"),
            "发票金额": money(Decimal(row["invoice_amount"])),
            "税局状态": row.get("invoice_status_raw") or "",
            "正数发票": _positive_invoice_label(row.get("is_positive")),
            "原蓝字发票": row.get("original_invoice_no") or "",
            "红字确认单": row.get("red_confirmation_no") or "",
            "账期天数": row.get("term_days"),
            "到期日": row.get("due_date"),
            "匹配状态": status_label(row.get("status")),
        })
    return output


def format_fuzzy_match_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        score = float(row.get("score") or 0)
        output.append({
            "选择": False,
            "流水ID": int(row["bank_transaction_id"]),
            "交易日期": row.get("transaction_date"),
            "方向": row.get("direction_label"),
            "银行户名": row.get("counterparty_name_raw"),
            "金额": money(row.get("amount") or Decimal("0.00")),
            "当前分类": row.get("business_category_label"),
            "建议ID": int(row["suggested_party_id"]),
            "建议往来单位": row.get("suggested_party_name"),
            "单位类型": row.get("suggested_party_type_label"),
            "相似度": f"{score:.1%}",
            "候选数": row.get("candidate_count"),
            "匹配依据": row.get("matched_name"),
        })
    return output


def format_amount_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"往来单位": row["往来单位"], "金额": money(row["金额"])} for row in rows]


def format_balance_rows(rows: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "往来单位": row.party_name,
            "余额": money(row.balance_amount),
            "发票合计": money(row.invoice_total),
            "银行结算": money(row.bank_settlement_total),
            "调整合计": money(row.adjustment_total),
            "发票数": row.invoice_count,
            "银行流水数": row.bank_count,
            "调整数": row.adjustment_count,
        }
        for row in rows
    ]


def format_category_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"分类": row["分类"], "金额": money(row["金额"])} for row in rows]


def workbench_status_summary(direction: str, matches: list[MatchResult]) -> list[tuple[str, Any]]:
    is_payable = direction == "payable"
    invoice_total = Decimal("0.00")
    settled_total = Decimal("0.00")
    pending_total = Decimal("0.00")
    unmatched_bank_total = Decimal("0.00")

    for match in matches:
        status = str(_match_attr(match, "status", ""))
        invoice_ids = list(_match_attr(match, "invoice_ids", []) or [])
        bank_ids = list(_match_attr(match, "bank_transaction_ids", []) or [])
        invoice_amount = Decimal(str(_match_attr(match, "invoice_total", Decimal("0.00")) or "0.00"))
        bank_amount = Decimal(str(_match_attr(match, "bank_total", Decimal("0.00")) or "0.00"))
        difference = Decimal(str(_match_attr(match, "difference_amount", Decimal("0.00")) or "0.00"))

        if invoice_ids:
            invoice_total += invoice_amount
        if status == "matched":
            settled_total += bank_amount
        elif status in {"unmatched_invoice", "probable"} and invoice_ids:
            pending_total += difference
        elif status == "unmatched_bank" and bank_ids:
            unmatched_bank_total += bank_amount

    if is_payable:
        return [
            ("应付发票总额", money(invoice_total)),
            ("已付金额", money(settled_total)),
            ("未付金额", money(pending_total)),
            ("付款未找到发票", money(unmatched_bank_total)),
        ]
    return [
        ("应收发票总额", money(invoice_total)),
        ("已收金额", money(settled_total)),
        ("未收金额", money(pending_total)),
        ("收款未找到发票", money(unmatched_bank_total)),
    ]


def format_workbench_detail_rows(
    direction: str,
    invoices: list[dict[str, Any]],
    banks: list[dict[str, Any]],
    matches: list[MatchResult],
) -> list[dict[str, Any]]:
    invoice_map = {int(row["id"]): row for row in invoices}
    bank_map = {int(row["id"]): row for row in banks}
    is_payable = direction == "payable"
    date_label = "实付日期" if is_payable else "实收日期"
    amount_label = "付款金额" if is_payable else "收款金额"
    rows: list[dict[str, Any]] = []

    for match in matches:
        match_type = str(_match_attr(match, "match_type", ""))
        status = str(_match_attr(match, "status", ""))
        settlement_method = str(_match_attr(match, "settlement_method", "") or "")
        settlement_date = str(_match_attr(match, "settlement_date", "") or "")
        invoice_ids = [int(value) for value in _match_attr(match, "invoice_ids", [])]
        bank_ids = [int(value) for value in _match_attr(match, "bank_transaction_ids", [])]
        linked_banks = [bank_map[bank_id] for bank_id in bank_ids if bank_id in bank_map]
        bank_dates = _joined_dates(bank.get("transaction_date") for bank in linked_banks)
        bank_names = _joined_text(
            bank.get("party_name") or bank.get("counterparty_name_raw")
            for bank in linked_banks
        )
        base = {
            date_label: bank_dates or settlement_date,
            amount_label: money(_match_attr(match, "bank_total", Decimal("0.00"))),
            "差额": money(_match_attr(match, "difference_amount", Decimal("0.00"))),
            "状态": invoice_settlement_status(direction, status, match_type, settlement_method),
            "匹配方式": match_type_label(match_type),
            "处理方式": settlement_method_label(settlement_method),
            "置信度": confidence_label(_match_attr(match, "confidence", Decimal("0.00"))),
            "说明": str(_match_attr(match, "remark", "") or ""),
            "发票ID": _joined_text(invoice_ids),
            "流水ID": _joined_text(bank_ids),
        }
        if not invoice_ids:
            continue
        for invoice_id in invoice_ids:
            invoice = invoice_map.get(invoice_id)
            rows.append({
                "往来单位": _invoice_party_name(invoice, match),
                "发票号": invoice.get("invoice_no") if invoice else "",
                "发票日期": invoice.get("invoice_date") if invoice else "",
                "到期日": invoice.get("due_date") if invoice else "",
                "发票金额": money(invoice.get("invoice_amount") if invoice else _match_attr(match, "invoice_total", Decimal("0.00"))),
                **base,
            })

    return sorted(rows, key=lambda row: (row.get("往来单位", ""), row.get("到期日", ""), row.get("发票日期", ""), row.get("发票号", "")))


def format_unmatched_bank_workbench_rows(
    direction: str,
    banks: list[dict[str, Any]],
    matches: list[MatchResult],
) -> list[dict[str, Any]]:
    bank_map = {int(row["id"]): row for row in banks}
    rows: list[dict[str, Any]] = []
    for match in matches:
        status = str(_match_attr(match, "status", ""))
        invoice_ids = [int(value) for value in _match_attr(match, "invoice_ids", [])]
        bank_ids = [int(value) for value in _match_attr(match, "bank_transaction_ids", [])]
        if status != "unmatched_bank" or invoice_ids or not bank_ids:
            continue
        for bank_id in bank_ids:
            bank = bank_map.get(bank_id)
            amount = _bank_row_amount(bank) if bank else Decimal(str(_match_attr(match, "bank_total", Decimal("0.00")) or "0.00"))
            rows.append(
                {
                    "流水ID": str(bank_id),
                    "交易日期": bank.get("transaction_date") if bank else "",
                    "方向": "支出" if direction == "payable" else "收入",
                    "往来单位": str(_match_attr(match, "party_name", "") or (bank.get("party_name") if bank else "") or (bank.get("counterparty_name_raw") if bank else "")),
                    "金额": money(amount),
                    "状态": invoice_settlement_status(direction, status, "unmatched"),
                    "摘要": bank.get("summary") if bank else "",
                    "分类": bank.get("business_category_label") if bank else "",
                    "对方账号": bank.get("counterparty_account") if bank else "",
                    "对方开户行": bank.get("counterparty_bank") if bank else "",
                    "说明": str(_match_attr(match, "remark", "") or ""),
                }
            )
    return sorted(rows, key=lambda row: (row.get("往来单位", ""), row.get("交易日期", ""), row.get("流水ID", "")))


def _history_row_to_match_result(row: dict[str, Any], invoice_ids: list[int], bank_ids: list[int]) -> MatchResult:
    return MatchResult(
        match_type=str(row.get("match_type") or ""),
        status=str(row.get("status") or ""),
        party_id=row.get("party_id"),
        party_name=str(row.get("party_name") or ""),
        invoice_ids=invoice_ids,
        bank_transaction_ids=bank_ids,
        invoice_total=Decimal(str(row.get("invoice_total") or "0.00")),
        bank_total=Decimal(str(row.get("bank_total") or "0.00")),
        difference_amount=Decimal(str(row.get("difference_amount") or "0.00")),
        confidence=Decimal(str(row.get("confidence") or "0.00")),
        remark=str(row.get("remark") or ""),
        settlement_method=str(row.get("settlement_method") or ""),
        settlement_date=str(row.get("settlement_date") or ""),
    )


def _synthetic_unmatched_invoice_result(direction: str, invoice: dict[str, Any]) -> MatchResult:
    amount = Decimal(str(invoice.get("invoice_amount") or "0.00"))
    return MatchResult(
        match_type="unmatched",
        status="unmatched_invoice",
        party_id=invoice.get("party_id"),
        party_name=str(invoice.get("party_name") or invoice.get("party_name_raw") or ""),
        invoice_ids=[int(invoice["id"])],
        bank_transaction_ids=[],
        invoice_total=amount,
        bank_total=Decimal("0.00"),
        difference_amount=amount,
        confidence=Decimal("0.00"),
        remark="发票未匹配到付款流水" if direction == "payable" else "发票未匹配到收款流水",
    )


def _synthetic_unmatched_bank_result(direction: str, bank: dict[str, Any]) -> MatchResult:
    amount = _bank_row_amount(bank)
    return MatchResult(
        match_type="unmatched",
        status="unmatched_bank",
        party_id=bank.get("party_id"),
        party_name=str(bank.get("party_name") or bank.get("counterparty_name_raw") or ""),
        invoice_ids=[],
        bank_transaction_ids=[int(bank["id"])],
        invoice_total=Decimal("0.00"),
        bank_total=amount,
        difference_amount=amount,
        confidence=Decimal("0.00"),
        remark="银行付款未匹配到发票" if direction == "payable" else "银行收款未匹配到发票",
    )


def _synthetic_probable_invoice_result(direction: str, invoice: dict[str, Any]) -> MatchResult:
    amount = Decimal(str(invoice.get("invoice_amount") or "0.00"))
    return MatchResult(
        match_type="probable",
        status="probable",
        party_id=invoice.get("party_id"),
        party_name=str(invoice.get("party_name") or invoice.get("party_name_raw") or ""),
        invoice_ids=[int(invoice["id"])],
        bank_transaction_ids=[],
        invoice_total=amount,
        bank_total=Decimal("0.00"),
        difference_amount=amount,
        confidence=Decimal("0.75"),
        remark="历史已标记待人工确认，当前范围未找到对应流水明细",
    )


def _synthetic_matched_invoice_result(direction: str, invoice: dict[str, Any]) -> MatchResult:
    amount = Decimal(str(invoice.get("invoice_amount") or "0.00"))
    return MatchResult(
        match_type="exact",
        status="matched",
        party_id=invoice.get("party_id"),
        party_name=str(invoice.get("party_name") or invoice.get("party_name_raw") or ""),
        invoice_ids=[int(invoice["id"])],
        bank_transaction_ids=[],
        invoice_total=amount,
        bank_total=amount,
        difference_amount=Decimal("0.00"),
        confidence=Decimal("1.00"),
        remark="历史已标记匹配，当前范围未找到匹配批次明细",
    )


def _bank_row_amount(bank: dict[str, Any]) -> Decimal:
    key = "expense_amount" if bank.get("direction") == "outflow" else "income_amount"
    return Decimal(str(bank.get(key) or "0.00"))


def _json_int_list(value: Any) -> list[int]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        raw_values = value
    else:
        try:
            raw_values = json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return []
    output: list[int] = []
    for item in raw_values:
        try:
            output.append(int(item))
        except (TypeError, ValueError):
            continue
    return output


def confidence_label(value: Any) -> str:
    try:
        return f"{Decimal(str(value or 0)):.0%}"
    except Exception:
        return ""


def _joined_dates(values: Any) -> str:
    return _joined_text(sorted({str(value) for value in values if value}))


def _joined_text(values: Any) -> str:
    return "、".join(str(value) for value in values if value not in (None, ""))


def _invoice_party_name(invoice: dict[str, Any] | None, match: MatchResult) -> str:
    if invoice:
        return str(invoice.get("party_name") or invoice.get("party_name_raw") or "")
    return str(_match_attr(match, "party_name", "") or "")


def _match_attr(match: Any, name: str, default: Any = None) -> Any:
    if hasattr(match, name):
        return getattr(match, name)
    if isinstance(match, dict):
        return match.get(name, default)
    return default


def format_match_history_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        output.append(
            {
                "批次": row.get("run_no"),
                "方向": "应付" if row.get("direction") == "payable" else "应收",
                "往来单位": row.get("party_name"),
                "类型": match_type_label(row.get("match_type")),
                "状态": invoice_settlement_status(
                    row.get("direction"),
                    row.get("status"),
                    row.get("match_type"),
                    row.get("settlement_method"),
                ),
                "发票金额": money(row.get("invoice_total") or "0"),
                "银行金额": money(row.get("bank_total") or "0"),
                "差额": money(row.get("difference_amount") or "0"),
                "处理方式": settlement_method_label(row.get("settlement_method")),
                "时间": row.get("created_at"),
            }
        )
    return output


def format_party_edit_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        output.append(
            {
                "ID": row["id"],
                "类型": "供应商" if row["party_type"] == "supplier" else "客户",
                "名称": row["name"],
                "删除": False,
                "简称": row.get("short_name") or "",
                "账期天数": int(row.get("default_term_days") or 0),
                "收款账号": row.get("bank_account") or "",
                "开户行": row.get("bank_name") or "",
                "备注": row.get("remark") or "",
                "新增别名": "",
            }
        )
    return output


def format_party_export_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        output.append(
            {
                "类型": "供应商" if row["party_type"] == "supplier" else "客户",
                "往来单位": row["name"],
                "简称": row.get("short_name") or "",
                "账期天数": int(row.get("default_term_days") or 0),
                "收款账号": row.get("bank_account") or "",
                "开户行": row.get("bank_name") or "",
                "别名": "",
                "备注": row.get("remark") or "",
            }
        )
    return output


def format_payment_candidate_rows(candidates: list[PaymentCandidate]) -> list[dict[str, Any]]:
    output = []
    for index, candidate in enumerate(candidates, start=1):
        output.append(
            {
                "选择": not candidate.missing_fields,
                "候选编号": index,
                "供应商": candidate.party_name,
                "发票数": candidate.invoice_count,
                "金额": money(candidate.amount),
                "最早到期日": candidate.earliest_grace_deadline.isoformat(),
                "最晚到期日": candidate.latest_grace_deadline.isoformat(),
                "收款账号": candidate.bank_account or "",
                "开户行": candidate.bank_name or "",
                "资料状态": "缺 " + "、".join(candidate.missing_fields) if candidate.missing_fields else "完整",
            }
        )
    return output


def format_payment_profile_exception_rows(candidates: list[PaymentCandidate]) -> list[dict[str, Any]]:
    output = []
    for candidate in candidates:
        output.append(
            {
                "供应商": candidate.party_name,
                "缺少字段": "、".join(candidate.missing_fields),
                "到期应付金额": money(candidate.amount),
                "发票数": candidate.invoice_count,
                "最早到期日": candidate.earliest_grace_deadline.isoformat(),
                "最晚到期日": candidate.latest_grace_deadline.isoformat(),
                "收款账号": candidate.bank_account or "",
                "开户行": candidate.bank_name or "",
            }
        )
    return output


def format_payment_template_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        output.append({
            "模板ID": row["id"],
            "银行": row["bank_name"],
            "模板名称": row["template_name"],
            "Sheet": row["sheet_name"],
            "列数": len(row.get("columns") or []),
            "导出列": "、".join(row.get("columns") or []),
        })
    return output


def _positive_invoice_label(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return "否" if value.strip() in {"0", "False", "false", "否"} else "是"
    return "是" if bool(value) else "否"


def _party_row_changed(row: pd.Series, original: dict[str, Any]) -> bool:
    for column in ["简称", "账期天数", "收款账号", "开户行", "备注", "新增别名"]:
        if _cell_text(row[column]) != _cell_text(original[column]):
            return True
    return False


def _party_identity_changed(row: pd.Series, original: dict[str, Any]) -> bool:
    return _cell_text(row["类型"]) != _cell_text(original["类型"]) or _cell_text(row["名称"]) != _cell_text(original["名称"])


def _party_type_key(value: Any) -> str:
    text = _cell_text(value)
    if text == "供应商":
        return "supplier"
    if text == "客户":
        return "customer"
    raise ValueError("类型只能填写供应商或客户")


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    return str(value).strip()


def _safe_int(value: Any, default: int) -> int:
    text = _cell_text(value)
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def _optional_decimal(value: Any) -> Decimal | None:
    text = _cell_text(value)
    if not text:
        return None
    try:
        return Decimal(text.replace(",", "").replace("，", "").replace("¥", "").replace("￥", ""))
    except Exception:
        return None


def parse_decimal_input(value: Any) -> Decimal | None:
    return _optional_decimal(value)


def match_to_row(row: MatchResult, direction: str = "应付") -> dict[str, Any]:
    direction_key = "payable" if direction == "应付" else "receivable"
    return {
        "匹配方式": match_type_label(row.match_type),
        "状态": invoice_settlement_status(direction_key, row.status, row.match_type, row.settlement_method),
        "往来单位": row.party_name,
        "发票ID": _joined_text(row.invoice_ids),
        "流水ID": _joined_text(row.bank_transaction_ids),
        "发票金额": money(row.invoice_total),
        "银行金额": money(row.bank_total),
        "差额": money(row.difference_amount),
        "置信度": confidence_label(row.confidence),
        "说明": row.remark,
    }


def money(value: Decimal | int | float | str) -> str:
    amount = Decimal(str(value or 0))
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    return f"{sign}¥{amount:,.2f}"


if __name__ == "__main__":
    main()
