from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from finance_platform.config import configured_company_name
from finance_platform.date_rules import calculate_terms
from finance_platform.normalization import (
    clean_name,
    detect_bank_direction,
    normalize_amount,
    normalize_date,
)


HEADER_KEYWORDS = [
    "日期",
    "时间",
    "金额",
    "发票",
    "名称",
    "单位",
    "户名",
    "收入",
    "支出",
    "余额",
    "摘要",
    "账期",
]

DEFAULT_COMPANY_NAME = configured_company_name()


@dataclass(frozen=True)
class ParsedParty:
    party_type: str
    name: str
    short_name: str | None
    default_term_months: int
    grace_months: int
    source_row_no: int
    bank_account: str | None = None
    bank_name: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def default_term_days(self) -> int:
        return int(self.default_term_months)


@dataclass(frozen=True)
class ParsedInvoice:
    direction: str
    party_name_raw: str
    invoice_no: str | None
    invoice_date: date
    invoice_amount: Decimal
    term_months: int
    grace_months: int
    due_date: date
    grace_deadline: date
    source_sheet: str
    source_row_no: int
    invoice_status_raw: str | None = None
    is_positive: bool | None = None
    original_invoice_no: str | None = None
    red_confirmation_no: str | None = None
    invoice_type: str | None = None
    amount_without_tax: Decimal | None = None
    tax_amount: Decimal | None = None
    remark: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def term_days(self) -> int:
        return int(self.term_months)


@dataclass(frozen=True)
class ParsedBankTransaction:
    transaction_date: date
    income_amount: Decimal | None
    expense_amount: Decimal | None
    counterparty_account: str | None
    counterparty_name_raw: str
    counterparty_bank: str | None
    subject: str | None
    summary: str | None
    direction: str
    source_sheet: str
    source_row_no: int
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ParseResult:
    rows: list[Any]
    warnings: list[str]
    row_count: int

    @property
    def parsed_count(self) -> int:
        return len(self.rows)

    @property
    def error_count(self) -> int:
        return len(self.warnings)


def is_excel_temp_file(path: Path) -> bool:
    return path.name.startswith("~$")


def choose_excel_engine(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return "openpyxl"
    if suffix == ".xls":
        return "xlrd"
    raise ValueError(f"不支持的 Excel 文件类型: {path.suffix}")


def detect_header_row(rows: list[list[Any]], max_scan_rows: int = 12) -> int:
    best_index = 0
    best_score = -1.0
    for idx, row in enumerate(rows[:max_scan_rows]):
        values = [clean_name(cell) for cell in row if clean_name(cell)]
        score = len(values) * 0.05
        for value in values:
            score += sum(1 for keyword in HEADER_KEYWORDS if keyword in value)
        if score > best_score:
            best_index = idx
            best_score = score
    return best_index


def pick_column(columns: list[str], keywords: list[str]) -> str | None:
    for keyword in keywords:
        for column in columns:
            if keyword == column or keyword in column:
                return column
    return None


def parse_party_directory(path: str | Path, party_type: str = "supplier") -> ParseResult:
    rows: list[ParsedParty] = []
    warnings: list[str] = []
    total_rows = 0
    for sheet_name, records in _iter_excel_records(Path(path)):
        total_rows += len(records)
        if not records:
            continue
        columns = list(records[0].keys())
        name_col = pick_column(columns, ["供应商名称", "客户名称", "收款方户名", "对方户名", "单位名称", "往来单位", "名称"])
        short_col = pick_column(columns, ["简称", "简码", "短名称"])
        term_col = pick_column(columns, ["账期", "付款账期", "收款账期"])
        account_col = pick_column(columns, ["收款方账号", "银行账号", "账号", "对方账号", "账户"])
        bank_col = pick_column(columns, ["开户行支行名称", "开户银行", "开户行", "银行名称"])
        if not name_col:
            warnings.append(f"{sheet_name}: 未找到供应商/客户名称列")
            continue
        for row_no, record in enumerate(records, start=1):
            name = clean_name(record.get(name_col))
            if not name:
                continue
            term_days = parse_term_days(record.get(term_col)) if term_col else 30
            rows.append(
                ParsedParty(
                    party_type=party_type,
                    name=name,
                    short_name=clean_name(record.get(short_col)) if short_col else None,
                    default_term_months=term_days,
                    grace_months=0,
                    source_row_no=row_no,
                    bank_account=clean_name(record.get(account_col)) if account_col else None,
                    bank_name=clean_name(record.get(bank_col)) if bank_col else None,
                )
            )
    return ParseResult(rows=rows, warnings=warnings, row_count=total_rows)


def parse_payable_invoices(path: str | Path) -> ParseResult:
    return _parse_invoices(
        Path(path),
        direction="payable",
        party_keywords=["销售方名称", "供应商名称", "销方名称", "名称"],
        date_keywords=["发票日期", "开票日期"],
        amount_keywords=["价税合计", "发票金额", "金额"],
    )


def parse_receivable_invoices(path: str | Path) -> ParseResult:
    return _parse_invoices(
        Path(path),
        direction="receivable",
        party_keywords=["付款单位", "客户名称", "购买方名称", "购方名称", "单位"],
        date_keywords=["开票日期", "发票日期"],
        amount_keywords=["价税合计", "发票金额", "金额"],
    )


def infer_invoice_file_role(
    path: str | Path,
    *,
    fallback_role: str | None = None,
    company_name: str = DEFAULT_COMPANY_NAME,
) -> str:
    """Infer whether a tax-office invoice export is payable or receivable.

    "取得发票" means the company is buyer and the counterparty is supplier.
    "开具发票" means the company is seller and the counterparty is customer.
    """
    invoice_path = Path(path)
    filename = clean_name(invoice_path.name)
    if any(keyword in filename for keyword in ("开具", "销项", "开出")):
        return "receivable_invoice"
    if any(keyword in filename for keyword in ("取得", "进项", "收到", "采购")):
        return "payable_invoice"

    content_role = _infer_invoice_role_from_content(invoice_path, company_name=company_name)
    if content_role:
        return content_role
    if fallback_role in {"payable_invoice", "receivable_invoice"}:
        return fallback_role
    return "payable_invoice"


def parse_bank_statement(path: str | Path) -> ParseResult:
    rows: list[ParsedBankTransaction] = []
    warnings: list[str] = []
    total_rows = 0
    for sheet_name, records in _iter_excel_records(Path(path)):
        total_rows += len(records)
        if not records:
            continue
        columns = list(records[0].keys())
        date_col = pick_column(columns, ["交易时间", "交易日期", "日期"])
        income_col = pick_column(columns, ["收入金额", "贷方金额", "收入"])
        expense_col = pick_column(columns, ["支出金额", "借方金额", "支出"])
        account_col = pick_column(columns, ["对方账号", "对方账户", "账号"])
        name_col = pick_column(columns, ["对方户名", "对方名称", "户名", "客户名称"])
        bank_col = pick_column(columns, ["对方开户行", "开户行"])
        subject_col = pick_column(columns, ["科目"])
        summary_col = pick_column(columns, ["摘要", "用途"])
        if not date_col or not name_col:
            warnings.append(f"{sheet_name}: 未找到交易日期或对方户名列")
            continue
        for row_no, record in enumerate(records, start=1):
            transaction_date = normalize_date(record.get(date_col))
            income = normalize_amount(record.get(income_col)) if income_col else None
            expense = normalize_amount(record.get(expense_col)) if expense_col else None
            counterparty_account = clean_name(record.get(account_col)) if account_col else None
            summary = clean_name(record.get(summary_col)) if summary_col else None
            counterparty_name = _bank_counterparty_name(
                record.get(name_col),
                counterparty_account=counterparty_account,
                summary=summary,
            )
            if not transaction_date or (income is None and expense is None):
                continue
            direction = detect_bank_direction(income, expense)
            rows.append(
                ParsedBankTransaction(
                    transaction_date=transaction_date,
                    income_amount=income,
                    expense_amount=expense,
                    counterparty_account=counterparty_account,
                    counterparty_name_raw=counterparty_name,
                    counterparty_bank=clean_name(record.get(bank_col)) if bank_col else None,
                    subject=clean_name(record.get(subject_col)) if subject_col else None,
                    summary=summary,
                    direction=direction,
                    source_sheet=sheet_name,
                    source_row_no=row_no,
                )
            )
    return ParseResult(rows=rows, warnings=warnings, row_count=total_rows)


def parse_term_days(value: Any) -> int:
    text = clean_name(value)
    if not text:
        return 30
    month_match = re.search(r"(\d+)\s*个?月", text)
    if month_match:
        return max(0, int(month_match.group(1)) * 30)
    day_match = re.search(r"(\d+)\s*天", text)
    if day_match:
        return max(0, int(day_match.group(1)))
    number_match = re.search(r"\d+", text)
    if number_match:
        value_int = int(number_match.group(0))
        return max(0, value_int)
    return 30


def parse_term_months(value: Any) -> int:
    return parse_term_days(value)


def _bank_counterparty_name(
    value: Any,
    *,
    counterparty_account: str | None,
    summary: str | None,
) -> str:
    return clean_name(value) or clean_name(counterparty_account) or clean_name(summary) or "未提供户名"


def _parse_invoices(
    path: Path,
    direction: str,
    party_keywords: list[str],
    date_keywords: list[str],
    amount_keywords: list[str],
) -> ParseResult:
    rows: list[ParsedInvoice] = []
    warnings: list[str] = []
    total_rows = 0
    for sheet_name, records in _iter_invoice_records(path):
        total_rows += len(records)
        if not records:
            continue
        columns = list(records[0].keys())
        party_col = pick_column(columns, party_keywords)
        invoice_no_col = pick_column(columns, ["数电发票号码", "发票号码", "发票号", "票号"])
        date_col = pick_column(columns, date_keywords)
        amount_col = pick_column(columns, amount_keywords)
        term_col = pick_column(columns, ["账期"])
        status_col = pick_column(columns, ["发票状态"])
        positive_col = pick_column(columns, ["是否正数发票"])
        type_col = pick_column(columns, ["发票票种"])
        amount_without_tax_col = pick_column(columns, ["金额"])
        tax_amount_col = pick_column(columns, ["税额"])
        remark_col = pick_column(columns, ["备注"])
        if not party_col or not date_col or not amount_col:
            warnings.append(f"{sheet_name}: 未找到往来单位、日期或金额列")
            continue
        for row_no, record in enumerate(records, start=1):
            party_name = clean_name(record.get(party_col))
            invoice_date = normalize_date(record.get(date_col))
            invoice_amount = normalize_amount(record.get(amount_col))
            if not party_name or not invoice_date or invoice_amount is None:
                continue
            positive_text = clean_name(record.get(positive_col)) if positive_col else ""
            is_positive = _parse_positive_invoice_flag(positive_text)
            if is_positive is False and invoice_amount > 0:
                invoice_amount = -invoice_amount
            term_days = parse_term_days(record.get(term_col)) if term_col else 30
            terms = calculate_terms(invoice_date, term_days=term_days)
            remark = clean_name(record.get(remark_col)) if remark_col else None
            rows.append(
                ParsedInvoice(
                    direction=direction,
                    party_name_raw=party_name,
                    invoice_no=clean_name(record.get(invoice_no_col)) if invoice_no_col else None,
                    invoice_date=invoice_date,
                    invoice_amount=invoice_amount,
                    term_months=term_days,
                    grace_months=0,
                    due_date=terms.due_date,
                    grace_deadline=terms.grace_deadline,
                    source_sheet=sheet_name,
                    source_row_no=row_no,
                    invoice_status_raw=clean_name(record.get(status_col)) if status_col else None,
                    is_positive=is_positive,
                    original_invoice_no=_extract_original_invoice_no(remark),
                    red_confirmation_no=_extract_red_confirmation_no(remark),
                    invoice_type=clean_name(record.get(type_col)) if type_col else None,
                    amount_without_tax=normalize_amount(record.get(amount_without_tax_col)) if amount_without_tax_col else None,
                    tax_amount=normalize_amount(record.get(tax_amount_col)) if tax_amount_col else None,
                    remark=remark,
                )
            )
    return ParseResult(rows=rows, warnings=warnings, row_count=total_rows)


def _iter_invoice_records(path: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    sheets = _iter_excel_records(path)
    base_info = [sheet for sheet in sheets if clean_name(sheet[0]) == "发票基础信息"]
    return base_info or sheets


def _infer_invoice_role_from_content(path: Path, *, company_name: str) -> str | None:
    company = clean_name(company_name)
    if not company:
        return None
    payable_score = 0
    receivable_score = 0
    try:
        sheets = _iter_invoice_records(path)
    except Exception:
        return None
    for _, records in sheets:
        if not records:
            continue
        columns = list(records[0].keys())
        seller_col = pick_column(columns, ["销方名称", "销售方名称", "供应商名称"])
        buyer_col = pick_column(columns, ["购买方名称", "购方名称", "付款单位", "客户名称"])
        for record in records[:50]:
            seller = clean_name(record.get(seller_col)) if seller_col else ""
            buyer = clean_name(record.get(buyer_col)) if buyer_col else ""
            if company and company in seller:
                receivable_score += 1
            if company and company in buyer:
                payable_score += 1
    if receivable_score > payable_score:
        return "receivable_invoice"
    if payable_score > receivable_score:
        return "payable_invoice"
    return None


def _parse_positive_invoice_flag(value: str) -> bool | None:
    if not value:
        return None
    if value in {"是", "正数", "正常", "Y", "YES", "true", "True", "1"}:
        return True
    if value in {"否", "负数", "红字", "N", "NO", "false", "False", "0"}:
        return False
    return None


def _extract_original_invoice_no(remark: str | None) -> str | None:
    text = clean_name(remark)
    if not text:
        return None
    patterns = [
        r"被红冲蓝字(?:数电)?发票号码[:：]\s*([A-Za-z0-9-]+)",
        r"被红冲蓝字数电票号码[:：]\s*([A-Za-z0-9-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _extract_red_confirmation_no(remark: str | None) -> str | None:
    text = clean_name(remark)
    if not text:
        return None
    match = re.search(r"红字发票信息确认单编号[:：]\s*([A-Za-z0-9-]+)", text)
    return match.group(1) if match else None


def _iter_excel_records(path: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    if is_excel_temp_file(path):
        return []
    pd = _load_pandas()
    engine = choose_excel_engine(path)
    sheets: list[tuple[str, list[dict[str, Any]]]] = []
    with pd.ExcelFile(path, engine=engine) as excel:
        for sheet_name in excel.sheet_names:
            raw = pd.read_excel(excel, sheet_name=sheet_name, header=None, dtype=object)
            raw = raw.dropna(how="all")
            if raw.empty:
                sheets.append((sheet_name, []))
                continue
            preview_rows = raw.head(12).where(raw.notna(), None).values.tolist()
            header_idx = detect_header_row(preview_rows)
            header_values = [clean_name(value) or f"Unnamed_{idx}" for idx, value in enumerate(raw.iloc[header_idx].tolist())]
            data = raw.iloc[header_idx + 1 :].copy()
            data.columns = _dedupe_columns(header_values)
            data = data.dropna(how="all")
            records = data.where(data.notna(), None).to_dict(orient="records")
            sheets.append((sheet_name, records))
    return sheets


def _dedupe_columns(columns: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for column in columns:
        base = column or "Unnamed"
        count = seen.get(base, 0)
        seen[base] = count + 1
        result.append(base if count == 0 else f"{base}.{count}")
    return result


def _load_pandas():
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - depends on runtime
        raise RuntimeError("缺少 pandas，请先执行 pip install -r requirements.txt") from exc
    return pd
