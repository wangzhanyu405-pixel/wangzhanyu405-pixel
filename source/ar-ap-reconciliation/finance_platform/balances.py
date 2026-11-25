from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from finance_platform.normalization import normalize_amount


ZERO = Decimal("0.00")
SETTLEMENT_CATEGORY = {
    "payable": "supplier_payment",
    "receivable": "customer_receipt",
}
BANK_DIRECTION = {
    "payable": "outflow",
    "receivable": "inflow",
}
BANK_AMOUNT_KEY = {
    "payable": "expense_amount",
    "receivable": "income_amount",
}


@dataclass(frozen=True)
class BalanceRow:
    direction: str
    party_id: int
    party_name: str
    invoice_total: Decimal
    bank_settlement_total: Decimal
    adjustment_total: Decimal
    balance_amount: Decimal
    invoice_count: int
    bank_count: int
    adjustment_count: int


def calculate_balance_rows(store: Any, direction: str, as_of_date: date) -> list[BalanceRow]:
    """Calculate open AR/AP balances as of a date.

    The balance is based on formal counterparties only:
    invoice total - recognised settlement bank flow + signed adjustments.
    """
    if direction not in SETTLEMENT_CATEGORY:
        raise ValueError("direction must be payable or receivable")

    buckets: dict[int, dict[str, Any]] = {}

    for row in store.list_invoices(direction=direction, end_date=as_of_date):
        party_id = _party_id(row)
        if party_id is None:
            continue
        bucket = _bucket(buckets, party_id, row.get("party_name") or row.get("party_name_raw"))
        bucket["invoice_total"] += _amount(row.get("invoice_amount"))
        bucket["invoice_count"] += 1

    for row in store.list_bank_transactions(direction=BANK_DIRECTION[direction], end_date=as_of_date):
        if row.get("business_category") != SETTLEMENT_CATEGORY[direction]:
            continue
        party_id = _party_id(row)
        if party_id is None:
            continue
        bucket = _bucket(buckets, party_id, row.get("party_name") or row.get("counterparty_name_raw"))
        bucket["bank_settlement_total"] += _amount(row.get(BANK_AMOUNT_KEY[direction]))
        bucket["bank_count"] += 1

    for row in store.list_balance_adjustments(direction=direction, end_date=as_of_date):
        party_id = _party_id(row)
        if party_id is None:
            continue
        bucket = _bucket(buckets, party_id, row.get("party_name"))
        bucket["adjustment_total"] += _amount(row.get("amount"))
        bucket["adjustment_count"] += 1

    rows: list[BalanceRow] = []
    for party_id, bucket in buckets.items():
        invoice_total = bucket["invoice_total"]
        bank_total = bucket["bank_settlement_total"]
        adjustment_total = bucket["adjustment_total"]
        balance_amount = invoice_total - bank_total + adjustment_total
        if balance_amount == ZERO and not bucket["invoice_count"]:
            continue
        rows.append(
            BalanceRow(
                direction=direction,
                party_id=party_id,
                party_name=bucket["party_name"],
                invoice_total=invoice_total,
                bank_settlement_total=bank_total,
                adjustment_total=adjustment_total,
                balance_amount=balance_amount,
                invoice_count=bucket["invoice_count"],
                bank_count=bucket["bank_count"],
                adjustment_count=bucket["adjustment_count"],
            )
        )

    return sorted(rows, key=lambda row: (row.balance_amount, row.party_name), reverse=True)


def _bucket(buckets: dict[int, dict[str, Any]], party_id: int, party_name: Any) -> dict[str, Any]:
    if party_id not in buckets:
        buckets[party_id] = {
            "party_name": str(party_name or ""),
            "invoice_total": ZERO,
            "bank_settlement_total": ZERO,
            "adjustment_total": ZERO,
            "invoice_count": 0,
            "bank_count": 0,
            "adjustment_count": 0,
        }
    elif party_name and not buckets[party_id]["party_name"]:
        buckets[party_id]["party_name"] = str(party_name)
    return buckets[party_id]


def _party_id(row: dict[str, Any]) -> int | None:
    value = row.get("party_id")
    if value is None or value == "":
        return None
    return int(value)


def _amount(value: Any) -> Decimal:
    amount = normalize_amount(value)
    return amount if amount is not None else ZERO
