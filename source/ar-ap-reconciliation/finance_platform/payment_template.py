from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from finance_platform.local_store import LocalStore
from finance_platform.normalization import clean_name, normalize_amount, normalize_date
from finance_platform.parsers import choose_excel_engine


ABC_UPLOAD_SHEET = "页一【批量转账文件】"
ABC_UPLOAD_COLUMNS = [
    "编号",
    "收款方账号",
    "收款方户名",
    "是否农行账户",
    "开户银行（行别）",
    "开户行大额行号",
    "开户行支行名称",
    "金额",
    "用途（附言）",
    "企业自制凭证号",
]
DEFAULT_PAYMENT_PURPOSE = "货款"


@dataclass(frozen=True)
class PaymentCandidate:
    party_id: int | None
    party_name: str
    amount: Decimal
    invoice_count: int
    invoice_ids: tuple[int, ...]
    earliest_grace_deadline: date
    latest_grace_deadline: date
    bank_account: str | None
    bank_name: str | None
    is_abc_account: bool
    missing_fields: tuple[str, ...]


@dataclass(frozen=True)
class PaymentTemplateConfig:
    bank_code: str
    bank_name: str
    template_name: str
    sheet_name: str
    default_purpose: str
    columns: list[str]


def infer_payment_template_config(path: str | Path, *, bank_code: str = "ABC") -> PaymentTemplateConfig:
    workbook_path = Path(path)
    pd = _load_pandas()
    engine = choose_excel_engine(workbook_path)
    best: tuple[int, str, list[str], str | None] | None = None
    with pd.ExcelFile(workbook_path, engine=engine) as excel:
        for sheet_name in excel.sheet_names:
            raw = pd.read_excel(excel, sheet_name=sheet_name, header=None, dtype=object, nrows=20)
            raw = raw.where(raw.notna(), None)
            for row_index, row in raw.iterrows():
                values = [_template_cell(value) for value in row.tolist()]
                columns = [value for value in values if value]
                if not columns:
                    continue
                score = _payment_template_header_score(columns)
                if score < 2:
                    continue
                title = _nearest_title_above(raw, int(row_index))
                if best is None or score > best[0]:
                    best = (score, sheet_name, columns, title)
    if best is None:
        raise ValueError("未能识别付款模板表头，请确认模板包含收款方账号、收款方户名、金额等列。")
    _, sheet_name, columns, title = best
    template_name = clean_name(title) or _template_name_from_file(workbook_path)
    return PaymentTemplateConfig(
        bank_code=bank_code,
        bank_name="中国农业银行" if bank_code == "ABC" else clean_name(bank_code),
        template_name=template_name,
        sheet_name=sheet_name,
        default_purpose=DEFAULT_PAYMENT_PURPOSE,
        columns=columns,
    )


def build_payment_candidates(
    store: LocalStore,
    start_date: date,
    end_date: date,
    *,
    include_statuses: tuple[str, ...] = ("unmatched", "partial"),
) -> list[PaymentCandidate]:
    """Group payable invoices whose due date falls in the selected payment window."""
    grouped: dict[tuple[int | None, str], dict[str, Any]] = {}
    for invoice in store.list_invoices("payable"):
        if invoice.get("party_id") is None:
            continue
        if invoice.get("status") not in include_statuses:
            continue
        due_date = normalize_date(invoice.get("due_date"))
        if due_date is None or due_date < start_date or due_date > end_date:
            continue
        amount = normalize_amount(invoice.get("invoice_amount")) or Decimal("0.00")
        if amount == 0:
            continue

        party_id = int(invoice["party_id"]) if invoice.get("party_id") is not None else None
        party_name = clean_name(invoice.get("party_name") or invoice.get("party_name_raw"))
        key = (party_id, party_name)
        bucket = grouped.setdefault(
            key,
            {
                "party_id": party_id,
                "party_name": party_name,
                "amount": Decimal("0.00"),
                "invoice_ids": [],
                "deadlines": [],
            },
        )
        bucket["amount"] += amount
        bucket["invoice_ids"].append(int(invoice["id"]))
        bucket["deadlines"].append(due_date)

    candidates: list[PaymentCandidate] = []
    for bucket in grouped.values():
        amount = bucket["amount"].quantize(Decimal("0.01"))
        if amount <= Decimal("0.00"):
            continue
        party = store.get_party(bucket["party_id"])
        bank_account = clean_name(party.get("bank_account")) if party else None
        bank_name = clean_name(party.get("bank_name")) if party else None
        missing = []
        if not bank_account:
            missing.append("收款方账号")
        if not bank_name:
            missing.append("开户行支行名称")
        candidates.append(
            PaymentCandidate(
                party_id=bucket["party_id"],
                party_name=bucket["party_name"],
                amount=amount,
                invoice_count=len(bucket["invoice_ids"]),
                invoice_ids=tuple(bucket["invoice_ids"]),
                earliest_grace_deadline=min(bucket["deadlines"]),
                latest_grace_deadline=max(bucket["deadlines"]),
                bank_account=bank_account or None,
                bank_name=bank_name or None,
                is_abc_account=is_abc_bank(bank_name),
                missing_fields=tuple(missing),
            )
        )
    return sorted(candidates, key=lambda row: (row.latest_grace_deadline, row.party_name))


def build_payment_profile_exceptions(
    store: LocalStore,
    start_date: date,
    end_date: date,
) -> list[PaymentCandidate]:
    """List due payable candidates whose supplier payment profile is incomplete."""
    return [
        candidate
        for candidate in build_payment_candidates(store, start_date, end_date)
        if candidate.missing_fields
    ]


def _payment_template_header_score(columns: list[str]) -> int:
    score = 0
    for column in columns:
        if column in ABC_UPLOAD_COLUMNS:
            score += 2
        elif any(column in known or known in column for known in ABC_UPLOAD_COLUMNS):
            score += 1
    required_hits = sum(1 for required in ("收款方账号", "收款方户名", "金额") if required in columns)
    return score + required_hits


def _nearest_title_above(raw: Any, row_index: int) -> str | None:
    for idx in range(row_index - 1, -1, -1):
        values = [_template_cell(value) for value in raw.iloc[idx].tolist()]
        values = [value for value in values if value]
        if len(values) == 1:
            return values[0]
    return None


def _template_name_from_file(path: Path) -> str:
    name = clean_name(path.stem)
    if "农业银行" in name or "农行" in name:
        return "农业银行批量付款模板"
    return name or "批量付款模板"


def _template_cell(value: Any) -> str:
    text = clean_name(value)
    if not text:
        return ""
    for known in ABC_UPLOAD_COLUMNS:
        if text == clean_name(known):
            return known
    return text


def export_bank_payment_workbook(
    output_path: str | Path,
    candidates: list[PaymentCandidate],
    *,
    purpose: str = DEFAULT_PAYMENT_PURPOSE,
    columns: list[str] | None = None,
    sheet_name: str | None = None,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = payment_candidates_to_upload_rows(candidates, purpose=purpose)
    export_columns = columns or ABC_UPLOAD_COLUMNS
    export_sheet = sheet_name or ABC_UPLOAD_SHEET
    if output.suffix.lower() == ".xls":
        _export_xls(output, rows, export_columns, export_sheet)
    else:
        _export_xlsx(output, rows, export_columns, export_sheet)
    return output


def _export_xlsx(output: Path, rows: list[dict[str, Any]], columns: list[str], sheet_name: str) -> None:
    pd = _load_pandas()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df = pd.DataFrame(rows, columns=columns)
        df.to_excel(writer, sheet_name=sheet_name, startrow=1, index=False)

        workbook = writer.book
        worksheet = writer.sheets[sheet_name]
        title_fmt = workbook.add_format({"bold": True, "align": "center", "valign": "vcenter", "font_size": 14})
        header_fmt = workbook.add_format({"bold": True, "align": "center", "valign": "vcenter", "border": 1})
        text_fmt = workbook.add_format({"num_format": "@", "border": 1})
        money_fmt = workbook.add_format({"num_format": "#,##0.00", "border": 1})
        normal_fmt = workbook.add_format({"border": 1})
        worksheet.merge_range(0, 0, 0, len(columns) - 1, "中国农业银行批量转账上传文件", title_fmt)
        for col_idx, column in enumerate(columns):
            worksheet.write(1, col_idx, column, header_fmt)
        worksheet.set_column(0, 0, 8, normal_fmt)
        worksheet.set_column(1, 1, 26, text_fmt)
        worksheet.set_column(2, 2, 28, text_fmt)
        worksheet.set_column(3, 5, 16, text_fmt)
        worksheet.set_column(6, 6, 36, text_fmt)
        worksheet.set_column(7, 7, 14, money_fmt)
        worksheet.set_column(8, 9, 22, text_fmt)

        instruction = pd.DataFrame(
            [
                ["注意事项：", "请勿删除第一页前两行表头；账号请保留为文本；金额请核对后再上传网银。"],
                ["生成规则：", "系统按所选时间段内到期日落入范围的应付发票，汇总到供应商维度。"],
            ],
            columns=["项目", "说明"],
        )
        instruction.to_excel(writer, sheet_name="页二  【使用说明】", index=False)


def _export_xls(output: Path, rows: list[dict[str, Any]], columns: list[str], sheet_name: str) -> None:
    try:
        import xlwt
    except ImportError as exc:  # pragma: no cover - depends on runtime
        raise RuntimeError("缺少 xlwt，无法生成农行 .xls 上传文件，请先执行 pip install -r requirements.txt") from exc

    workbook = xlwt.Workbook(encoding="utf-8")
    worksheet = workbook.add_sheet(sheet_name)
    title_style = xlwt.easyxf("font: bold on, height 280; align: horiz center, vert center;")
    header_style = xlwt.easyxf(
        "font: bold on; align: horiz center, vert center; borders: left thin, right thin, top thin, bottom thin;"
    )
    text_style = xlwt.easyxf("borders: left thin, right thin, top thin, bottom thin;")
    money_style = xlwt.easyxf("borders: left thin, right thin, top thin, bottom thin;", num_format_str="#,##0.00")

    worksheet.write_merge(0, 0, 0, len(columns) - 1, "中国农业银行批量转账上传文件", title_style)
    for col_idx, column in enumerate(columns):
        worksheet.write(1, col_idx, column, header_style)
    for row_idx, row in enumerate(rows, start=2):
        for col_idx, column in enumerate(columns):
            value = row.get(column, "")
            style = money_style if column == "金额" else text_style
            worksheet.write(row_idx, col_idx, value, style)

    widths = [8, 26, 28, 14, 16, 16, 36, 14, 22, 22]
    for col_idx, width in enumerate(widths):
        worksheet.col(col_idx).width = width * 256

    instruction = workbook.add_sheet("页二  【使用说明】")
    instruction.write(0, 0, "项目", header_style)
    instruction.write(0, 1, "说明", header_style)
    instruction.write(1, 0, "注意事项：", text_style)
    instruction.write(1, 1, "请勿删除第一页前两行表头；账号请保留为文本；金额请核对后再上传网银。", text_style)
    instruction.write(2, 0, "生成规则：", text_style)
    instruction.write(2, 1, "系统按所选时间段内到期日落入范围的应付发票，汇总到供应商维度。", text_style)
    instruction.col(0).width = 16 * 256
    instruction.col(1).width = 80 * 256
    workbook.save(str(output))


def payment_candidates_to_upload_rows(
    candidates: list[PaymentCandidate],
    *,
    purpose: str = DEFAULT_PAYMENT_PURPOSE,
) -> list[dict[str, Any]]:
    rows = []
    for index, candidate in enumerate(candidates, start=1):
        rows.append(
            {
                "编号": index,
                "收款方账号": candidate.bank_account or "",
                "收款方户名": candidate.party_name,
                "是否农行账户": "是" if candidate.is_abc_account else "否",
                "开户银行（行别）": "",
                "开户行大额行号": "",
                "开户行支行名称": candidate.bank_name or "",
                "金额": float(candidate.amount),
                "用途（附言）": purpose,
                "企业自制凭证号": "",
            }
        )
    return rows


def is_abc_bank(bank_name: str | None) -> bool:
    text = clean_name(bank_name)
    return "农业银行" in text or "农行" in text


def _load_pandas():
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - depends on runtime
        raise RuntimeError("缺少 pandas，请先执行 pip install -r requirements.txt") from exc
    return pd
