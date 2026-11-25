from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from finance_platform.normalization import normalize_amount


INTERNAL_MATCH_COLUMN_LABELS = {
    "match_type": "匹配方式",
    "status": "状态",
    "party_id": "往来单位ID",
    "party_name": "往来单位",
    "invoice_ids": "发票ID",
    "bank_transaction_ids": "流水ID",
    "invoice_total": "发票金额",
    "bank_total": "银行金额",
    "difference_amount": "差额",
    "confidence": "置信度",
    "remark": "说明",
    "settlement_method": "处理方式",
    "settlement_date": "处理日期",
}

TEXT_COLUMN_KEYWORDS = (
    "发票号",
    "发票号码",
    "发票ID",
    "流水ID",
    "账号",
    "账户",
    "税号",
    "编号",
    "invoice_no",
    "invoice_ids",
    "bank_transaction_ids",
    "counterparty_account",
)

AMOUNT_COLUMN_KEYWORDS = (
    "金额",
    "合计",
    "差额",
    "总额",
    "余额",
    "invoice_total",
    "bank_total",
    "difference_amount",
)


def export_reconciliation_workbook(
    output_path: str | Path,
    payable_details: list[dict[str, Any]],
    bank_details: list[dict[str, Any]],
    match_results: list[Any],
    match_details: list[dict[str, Any]] | None = None,
    exceptions: list[dict[str, Any]] | None = None,
) -> Path:
    pd = _load_pandas()
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    match_rows = _record_rows(match_results, localize=False)
    match_detail_rows = _record_rows(match_details if match_details is not None else match_results, localize=True)
    exception_rows = _record_rows(exceptions or [], localize=True)
    summary_rows = _build_reconciliation_summary_rows(match_rows)
    with pd.ExcelWriter(output, engine="xlsxwriter", engine_kwargs={"options": {"strings_to_numbers": False}}) as writer:
        _write_sheet(pd, writer, "汇总核对", summary_rows)
        _write_sheet(pd, writer, "发票明细", payable_details)
        _write_sheet(pd, writer, "银行流水明细", bank_details)
        _write_sheet(pd, writer, "匹配结果", match_detail_rows)
        _write_sheet(pd, writer, "异常明细", exception_rows)
    return output


def _record_rows(records: list[Any], *, localize: bool) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        if hasattr(record, "__dataclass_fields__"):
            row = asdict(record)
        else:
            row = dict(record)
        rows.append(_localize_internal_match_row(row) if localize else row)
    return rows


def _localize_internal_match_row(row: dict[str, Any]) -> dict[str, Any]:
    if not any(key in row for key in INTERNAL_MATCH_COLUMN_LABELS):
        return row
    return {INTERNAL_MATCH_COLUMN_LABELS.get(key, key): value for key, value in row.items()}


def _write_sheet(pd: Any, writer: Any, sheet_name: str, rows: list[dict[str, Any]]) -> None:
    safe_rows = [_prepare_export_row(row) for row in rows]
    dataframe = pd.DataFrame(safe_rows)
    safe_sheet_name = sheet_name[:31]
    dataframe.to_excel(writer, sheet_name=safe_sheet_name, index=False)
    worksheet = writer.sheets[safe_sheet_name]
    text_format = writer.book.add_format({"num_format": "@"})
    amount_format = writer.book.add_format({"num_format": "#,##0.00"})

    for column_index, column_name in enumerate(dataframe.columns):
        width = _column_width(dataframe, column_name)
        if _is_text_column(column_name):
            worksheet.set_column(column_index, column_index, width, text_format)
        elif _is_amount_column(column_name):
            worksheet.set_column(column_index, column_index, width, amount_format)
        else:
            worksheet.set_column(column_index, column_index, width)


def _prepare_export_row(row: dict[str, Any]) -> dict[str, Any]:
    output = {}
    for key, value in row.items():
        if _is_text_column(key):
            output[key] = _as_identifier_text(value)
        elif _is_amount_column(key):
            output[key] = _as_plain_amount(value)
        elif isinstance(value, Decimal):
            output[key] = float(value)
        elif isinstance(value, (list, tuple, set)):
            output[key] = "、".join(str(item) for item in value)
        else:
            output[key] = value
    return output


def _as_identifier_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "、".join(str(item) for item in value)
    return str(value)


def _as_plain_amount(value: Any) -> Any:
    if value in (None, ""):
        return ""
    amount = normalize_amount(value)
    if amount is None:
        return value
    return float(amount)


def _is_text_column(column_name: Any) -> bool:
    text = str(column_name)
    return any(keyword in text for keyword in TEXT_COLUMN_KEYWORDS)


def _is_amount_column(column_name: Any) -> bool:
    text = str(column_name)
    return any(keyword in text for keyword in AMOUNT_COLUMN_KEYWORDS)


def _column_width(dataframe: Any, column_name: Any) -> int:
    values = [str(column_name)]
    if not dataframe.empty:
        values.extend(str(value) for value in dataframe[column_name].head(200).tolist() if value is not None)
    return max(10, min(max(len(value) for value in values) + 2, 42))


def _build_reconciliation_summary_rows(match_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"count": 0, "invoice_total": Decimal("0.00"), "bank_total": Decimal("0.00")}
    )
    for row in match_rows:
        status = str(row.get("status") or "unknown")
        invoice_total = _to_decimal(row.get("invoice_total"))
        bank_total = _to_decimal(row.get("bank_total"))
        for key in ("全部", status):
            grouped[key]["count"] = int(grouped[key]["count"]) + 1
            grouped[key]["invoice_total"] = Decimal(grouped[key]["invoice_total"]) + invoice_total
            grouped[key]["bank_total"] = Decimal(grouped[key]["bank_total"]) + bank_total

    ordered_statuses = ["全部", *sorted(key for key in grouped.keys() if key != "全部")]
    output = []
    for status in ordered_statuses:
        bucket = grouped[status]
        invoice_total = Decimal(bucket["invoice_total"]).quantize(Decimal("0.01"))
        bank_total = Decimal(bucket["bank_total"]).quantize(Decimal("0.01"))
        output.append(
            {
                "匹配状态": status,
                "笔数": int(bucket["count"]),
                "发票合计": float(invoice_total),
                "银行合计": float(bank_total),
                "差额": float((invoice_total - bank_total).quantize(Decimal("0.01"))),
            }
        )
    return output


def _to_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _load_pandas():
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - depends on runtime
        raise RuntimeError("缺少 pandas，请先执行 pip install -r requirements.txt") from exc
    return pd
