from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from finance_platform.normalization import clean_name
from finance_platform.parsers import _iter_excel_records, parse_term_days, pick_column


PARTY_IMPORT_COLUMNS = [
    "类型",
    "往来单位",
    "简称",
    "账期天数",
    "收款账号",
    "开户行",
    "别名",
    "备注",
]


@dataclass(frozen=True)
class ParsedPartyImportRow:
    party_type: str
    name: str
    short_name: str | None
    default_term_months: int
    grace_months: int
    bank_account: str | None = None
    bank_name: str | None = None
    alias_name: str | None = None
    remark: str | None = None
    source_sheet: str | None = None
    source_row_no: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def default_term_days(self) -> int:
        return int(self.default_term_months)


@dataclass(frozen=True)
class PartyImportResult:
    rows: list[ParsedPartyImportRow]
    warnings: list[str]
    row_count: int

    @property
    def parsed_count(self) -> int:
        return len(self.rows)


def build_party_template_rows() -> list[dict[str, Any]]:
    return [
        {
            "类型": "供应商",
            "往来单位": "上海示例纸业有限公司",
            "简称": "示例纸业",
            "账期天数": 30,
            "收款账号": "请填写收款账号",
            "开户行": "请填写开户行",
            "别名": "银行流水里的户名",
            "备注": "",
        },
        {
            "类型": "客户",
            "往来单位": "上海示例客户有限公司",
            "简称": "示例客户",
            "账期天数": 60,
            "收款账号": "",
            "开户行": "",
            "别名": "",
            "备注": "",
        },
    ]


def parse_party_import_workbook(path: str | Path, default_party_type: str = "supplier") -> PartyImportResult:
    parsed_rows: list[ParsedPartyImportRow] = []
    warnings: list[str] = []
    total_rows = 0
    for sheet_name, records in _iter_excel_records(Path(path)):
        sheet_default_party_type = _infer_party_type_from_sheet_name(sheet_name) or default_party_type
        total_rows += len(records)
        if not records:
            continue
        columns = list(records[0].keys())
        type_col = pick_column(columns, ["类型", "类别", "单位类型", "供应商客户", "往来类型"])
        name_col = pick_column(
            columns,
            [
                "往来单位",
                "供应商名称",
                "客户名称",
                "付款单位",
                "收款方户名",
                "付款方户名",
                "对方户名",
                "单位名称",
                "名称",
            ],
        )
        short_col = pick_column(columns, ["简称", "简码", "短名称"])
        term_col = pick_column(columns, ["账期天数", "账期", "付款账期", "收款账期", "账期月数"])
        account_col = pick_column(columns, ["收款账号", "收款方账号", "银行账号", "账号", "对方账号", "账户"])
        bank_col = pick_column(columns, ["开户行支行名称", "开户银行", "开户行", "银行名称"])
        alias_col = pick_column(columns, ["别名", "银行户名", "额外别名"])
        remark_col = pick_column(columns, ["备注", "说明"])
        if not name_col:
            warnings.append(f"{sheet_name}: 未找到往来单位名称列")
            continue
        for row_no, record in enumerate(records, start=1):
            name = clean_name(record.get(name_col))
            if not name:
                continue
            parsed_rows.append(
                ParsedPartyImportRow(
                    party_type=_parse_party_type(record.get(type_col) if type_col else None, sheet_default_party_type),
                    name=name,
                    short_name=clean_name(record.get(short_col)) if short_col else None,
                    default_term_months=_parse_days(record.get(term_col) if term_col else None, default=30),
                    grace_months=0,
                    bank_account=clean_name(record.get(account_col)) if account_col else None,
                    bank_name=clean_name(record.get(bank_col)) if bank_col else None,
                    alias_name=clean_name(record.get(alias_col)) if alias_col else None,
                    remark=clean_name(record.get(remark_col)) if remark_col else None,
                    source_sheet=sheet_name,
                    source_row_no=row_no,
                )
            )
    return PartyImportResult(rows=parsed_rows, warnings=warnings, row_count=total_rows)


def _infer_party_type_from_sheet_name(sheet_name: str) -> str | None:
    text = clean_name(sheet_name)
    if "应收" in text or "客户" in text or "收款" in text:
        return "customer"
    if "应付" in text or "供应" in text or "付款" in text:
        return "supplier"
    return None


def _parse_party_type(value: Any, default_party_type: str) -> str:
    text = clean_name(value)
    if "客户" in text or "应收" in text or text.lower() == "customer":
        return "customer"
    if "供应" in text or "应付" in text or text.lower() == "supplier":
        return "supplier"
    return default_party_type if default_party_type in {"supplier", "customer"} else "supplier"


def _parse_days(value: Any, default: int) -> int:
    text = clean_name(value)
    if not text:
        return default
    return parse_term_days(text)
