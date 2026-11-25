from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


EXCEL_DATE_BASE = date(1899, 12, 30)
AMOUNT_QUANT = Decimal("0.01")


def clean_name(value: Any) -> str:
    if value is None:
        return ""
    text = (
        str(value)
        .replace("\u3000", " ")
        .replace("（", "(")
        .replace("）", ")")
        .strip()
    )
    return re.sub(r"\s+", " ", text)


def normalize_amount(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        amount = value
    else:
        text = str(value).strip()
        if not text:
            return None
        text = (
            text.replace(",", "")
            .replace("，", "")
            .replace("￥", "")
            .replace("¥", "")
            .replace("元", "")
            .strip()
        )
        if not text:
            return None
        try:
            amount = Decimal(text)
        except InvalidOperation:
            return None
    return amount.quantize(AMOUNT_QUANT, rounding=ROUND_HALF_UP)


def normalize_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        serial = int(value)
        if 20000 <= serial <= 80000:
            return EXCEL_DATE_BASE + timedelta(days=serial)
        return None

    text = str(value).strip()
    if not text:
        return None
    text = text.replace("年", "-").replace("月", "-").replace("日", "")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def detect_bank_direction(income_amount: Decimal | None, expense_amount: Decimal | None) -> str:
    income = income_amount or Decimal("0")
    expense = expense_amount or Decimal("0")
    if income > 0 and expense <= 0:
        return "inflow"
    if expense > 0 and income <= 0:
        return "outflow"
    return "unknown"


def period_month(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"
