from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from typing import Any

import pandas as pd

from finance_platform.normalization import clean_name, normalize_amount


UNIDENTIFIED_CATEGORIES = {"company_candidate", "unidentified_outflow", "unidentified_inflow", "unknown"}
MATCHED_STATUSES = {"matched"}


@dataclass(frozen=True)
class BankLedgerFilter:
    direction: str = "all"
    counterparty_name: str = ""
    counterparty_account: str = ""
    text: str = ""
    categories: tuple[str, ...] = ()
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None


@dataclass(frozen=True)
class InvoiceLedgerFilter:
    direction: str = "all"
    party_name: str = ""
    invoice_no: str = ""
    status: tuple[str, ...] = ()
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None


def filter_bank_rows(rows: list[dict[str, Any]], filters: BankLedgerFilter) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        if filters.direction != "all" and row.get("direction") != filters.direction:
            continue
        if filters.categories and row.get("business_category") not in filters.categories:
            continue
        amount = _bank_amount(row)
        if filters.min_amount is not None and amount < filters.min_amount:
            continue
        if filters.max_amount is not None and amount > filters.max_amount:
            continue
        if not _contains(row.get("counterparty_name_raw"), filters.counterparty_name):
            continue
        if not _contains(row.get("counterparty_account"), filters.counterparty_account):
            continue
        haystack = " ".join(
            clean_name(row.get(key))
            for key in ("summary", "subject", "counterparty_bank", "counterparty_name_raw")
        )
        if filters.text and clean_name(filters.text) not in haystack:
            continue
        output.append(row)
    return output


def summarize_bank_rows(rows: list[dict[str, Any]]) -> dict[str, Decimal | int]:
    income_total = Decimal("0.00")
    expense_total = Decimal("0.00")
    unidentified_count = 0
    for row in rows:
        income_total += normalize_amount(row.get("income_amount")) or Decimal("0.00")
        expense_total += normalize_amount(row.get("expense_amount")) or Decimal("0.00")
        if row.get("business_category") in UNIDENTIFIED_CATEGORIES:
            unidentified_count += 1
    return {
        "row_count": len(rows),
        "income_total": income_total,
        "expense_total": expense_total,
        "net_amount": income_total - expense_total,
        "identified_count": len(rows) - unidentified_count,
        "unidentified_count": unidentified_count,
    }


def filter_invoice_rows(rows: list[dict[str, Any]], filters: InvoiceLedgerFilter) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        if filters.direction != "all" and row.get("direction") != filters.direction:
            continue
        if filters.status and row.get("status") not in filters.status:
            continue
        amount = normalize_amount(row.get("invoice_amount")) or Decimal("0.00")
        if filters.min_amount is not None and amount < filters.min_amount:
            continue
        if filters.max_amount is not None and amount > filters.max_amount:
            continue
        party_name = row.get("party_name") or row.get("party_name_raw")
        if not _contains(party_name, filters.party_name):
            continue
        if not _contains(row.get("invoice_no"), filters.invoice_no):
            continue
        output.append(row)
    return output


def summarize_invoice_rows(rows: list[dict[str, Any]]) -> dict[str, Decimal | int]:
    matched_count = 0
    total = Decimal("0.00")
    for row in rows:
        total += normalize_amount(row.get("invoice_amount")) or Decimal("0.00")
        if clean_name(row.get("status")) in MATCHED_STATUSES:
            matched_count += 1
    return {
        "row_count": len(rows),
        "amount_total": total,
        "matched_count": matched_count,
        "unmatched_count": len(rows) - matched_count,
    }


def excel_bytes_from_rows(rows: list[dict[str, Any]], columns: list[str], *, sheet_name: str) -> bytes:
    output = BytesIO()
    safe_rows = [{column: row.get(column, "") for column in columns} for row in rows]
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        pd.DataFrame(safe_rows, columns=columns).to_excel(writer, sheet_name=sheet_name[:31], index=False)
    return output.getvalue()


def _bank_amount(row: dict[str, Any]) -> Decimal:
    if row.get("direction") == "outflow":
        return normalize_amount(row.get("expense_amount")) or Decimal("0.00")
    return normalize_amount(row.get("income_amount")) or Decimal("0.00")


def _contains(value: Any, needle: str) -> bool:
    text = clean_name(value)
    query = clean_name(needle)
    if not query:
        return True
    return query in text
