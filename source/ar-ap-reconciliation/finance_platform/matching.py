from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import combinations

from finance_platform.normalization import clean_name, period_month


MAX_SUBSET_INVOICES = 18
MAX_SUBSET_SIZE = 8


@dataclass(frozen=True)
class InvoiceRecord:
    id: int
    party_id: int | None
    party_name: str
    amount: Decimal
    invoice_date: date
    due_date: date
    grace_deadline: date


@dataclass(frozen=True)
class BankRecord:
    id: int
    party_id: int | None
    counterparty_name: str
    amount: Decimal
    transaction_date: date
    direction: str = "outflow"


@dataclass(frozen=True)
class MatchResult:
    match_type: str
    status: str
    party_id: int | None
    party_name: str
    invoice_ids: list[int]
    bank_transaction_ids: list[int]
    invoice_total: Decimal
    bank_total: Decimal
    difference_amount: Decimal
    confidence: Decimal
    remark: str = ""
    settlement_method: str = ""
    settlement_date: str = ""


def match_payables(
    invoices: list[InvoiceRecord],
    bank_transactions: list[BankRecord],
    probable_tolerance: Decimal = Decimal("1.00"),
) -> list[MatchResult]:
    return _match_invoices_to_banks(invoices, bank_transactions, ("outflow", "unknown"), probable_tolerance)


def match_receivables(
    invoices: list[InvoiceRecord],
    bank_transactions: list[BankRecord],
    probable_tolerance: Decimal = Decimal("1.00"),
) -> list[MatchResult]:
    return _match_invoices_to_banks(invoices, bank_transactions, ("inflow", "unknown"), probable_tolerance)


def _match_invoices_to_banks(
    invoices: list[InvoiceRecord],
    bank_transactions: list[BankRecord],
    accepted_bank_directions: tuple[str, ...],
    probable_tolerance: Decimal,
) -> list[MatchResult]:
    results: list[MatchResult] = []
    unmatched_invoices = {invoice.id: invoice for invoice in invoices}
    unmatched_banks = {
        bank.id: bank
        for bank in bank_transactions
        if bank.direction in accepted_bank_directions
    }

    _match_red_offsets(unmatched_invoices, results)
    _match_exact(unmatched_invoices, unmatched_banks, results)
    _match_monthly_summary(unmatched_invoices, unmatched_banks, results)
    _match_invoice_subsets(unmatched_invoices, unmatched_banks, results)
    _match_probable(unmatched_invoices, unmatched_banks, results, probable_tolerance)
    _match_early_payment_probable(unmatched_invoices, unmatched_banks, results)
    _append_unmatched(unmatched_invoices, unmatched_banks, results)
    return results


def _match_red_offsets(
    unmatched_invoices: dict[int, InvoiceRecord],
    results: list[MatchResult],
) -> None:
    groups: dict[object, list[InvoiceRecord]] = defaultdict(list)
    for invoice in unmatched_invoices.values():
        groups[_party_key(invoice.party_id, invoice.party_name)].append(invoice)

    for grouped in groups.values():
        positives = sorted(
            [invoice for invoice in grouped if invoice.amount > Decimal("0.00")],
            key=lambda invoice: (invoice.invoice_date, invoice.id),
        )
        negatives = sorted(
            [invoice for invoice in grouped if invoice.amount < Decimal("0.00")],
            key=lambda invoice: (invoice.invoice_date, invoice.id),
        )
        used_ids: set[int] = set()
        for negative in negatives:
            if negative.id in used_ids:
                continue
            positive = next(
                (
                    candidate
                    for candidate in positives
                    if candidate.id not in used_ids and candidate.amount == -negative.amount
                ),
                None,
            )
            if positive is None:
                continue
            invoice_total = positive.amount + negative.amount
            results.append(
                MatchResult(
                    match_type="red_offset",
                    status="matched",
                    party_id=positive.party_id,
                    party_name=positive.party_name,
                    invoice_ids=[positive.id, negative.id],
                    bank_transaction_ids=[],
                    invoice_total=invoice_total,
                    bank_total=Decimal("0.00"),
                    difference_amount=Decimal("0.00"),
                    confidence=Decimal("1.00"),
                    remark="同一往来单位、正数发票与红字发票金额相反，先按发票内部冲销处理",
                )
            )
            used_ids.update({positive.id, negative.id})

        for invoice_id in used_ids:
            unmatched_invoices.pop(invoice_id, None)


def _match_exact(
    unmatched_invoices: dict[int, InvoiceRecord],
    unmatched_banks: dict[int, BankRecord],
    results: list[MatchResult],
) -> None:
    for invoice in list(unmatched_invoices.values()):
        for bank in list(unmatched_banks.values()):
            if not _same_party(invoice, bank):
                continue
            if invoice.amount != bank.amount:
                continue
            if not _payment_on_or_after_invoice(invoice, bank):
                continue
            is_late = _is_late_payment(invoice, bank)
            results.append(
                MatchResult(
                    match_type="exact_late" if is_late else "exact",
                    status="matched",
                    party_id=invoice.party_id,
                    party_name=invoice.party_name,
                    invoice_ids=[invoice.id],
                    bank_transaction_ids=[bank.id],
                    invoice_total=invoice.amount,
                    bank_total=bank.amount,
                    difference_amount=Decimal("0.00"),
                    confidence=Decimal("0.90") if is_late else Decimal("1.00"),
                    remark=(
                        "同一往来单位、金额一致，付款晚于到期日，按逾期付款匹配"
                        if is_late
                        else "同一往来单位、金额一致、付款日期在允许窗口内"
                    ),
                )
            )
            unmatched_invoices.pop(invoice.id, None)
            unmatched_banks.pop(bank.id, None)
            break


def _match_monthly_summary(
    unmatched_invoices: dict[int, InvoiceRecord],
    unmatched_banks: dict[int, BankRecord],
    results: list[MatchResult],
) -> None:
    invoice_groups: dict[tuple[object, str], list[InvoiceRecord]] = defaultdict(list)
    bank_groups: dict[tuple[object, str], list[BankRecord]] = defaultdict(list)

    for invoice in unmatched_invoices.values():
        invoice_groups[(_party_key(invoice.party_id, invoice.party_name), period_month(invoice.grace_deadline))].append(invoice)
    for bank in unmatched_banks.values():
        bank_groups[(_party_key(bank.party_id, bank.counterparty_name), period_month(bank.transaction_date))].append(bank)

    for key, grouped_invoices in list(invoice_groups.items()):
        grouped_banks = bank_groups.get(key) or []
        if not grouped_invoices or not grouped_banks:
            continue
        invoice_total = sum((invoice.amount for invoice in grouped_invoices), Decimal("0.00"))
        bank_total = sum((bank.amount for bank in grouped_banks), Decimal("0.00"))
        if invoice_total != bank_total:
            continue
        if not _all_banks_in_group_window(grouped_invoices, grouped_banks):
            continue

        party_id = grouped_invoices[0].party_id
        party_name = grouped_invoices[0].party_name
        invoice_ids = [invoice.id for invoice in grouped_invoices]
        bank_ids = [bank.id for bank in grouped_banks]
        results.append(
            MatchResult(
                match_type="monthly_summary",
                status="matched",
                party_id=party_id,
                party_name=party_name,
                invoice_ids=invoice_ids,
                bank_transaction_ids=bank_ids,
                invoice_total=invoice_total,
                bank_total=bank_total,
                difference_amount=Decimal("0.00"),
                confidence=Decimal("0.95"),
                remark="同一往来单位、到期月份和付款月汇总金额一致",
            )
        )
        for invoice_id in invoice_ids:
            unmatched_invoices.pop(invoice_id, None)
        for bank_id in bank_ids:
            unmatched_banks.pop(bank_id, None)


def _match_invoice_subsets(
    unmatched_invoices: dict[int, InvoiceRecord],
    unmatched_banks: dict[int, BankRecord],
    results: list[MatchResult],
) -> None:
    for bank in list(unmatched_banks.values()):
        eligible_invoices = [
            invoice
            for invoice in unmatched_invoices.values()
            if _same_party(invoice, bank)
            and invoice.amount != Decimal("0.00")
            and _payment_on_or_after_invoice(invoice, bank)
        ]
        subset = _unique_invoice_subset_for_amount(eligible_invoices, bank.amount)
        if not subset:
            continue
        invoice_total = sum((invoice.amount for invoice in subset), Decimal("0.00"))
        is_late = any(_is_late_payment(invoice, bank) for invoice in subset)
        results.append(
            MatchResult(
                match_type="invoice_subset_late" if is_late else "invoice_subset",
                status="matched",
                party_id=subset[0].party_id,
                party_name=subset[0].party_name,
                invoice_ids=[invoice.id for invoice in subset],
                bank_transaction_ids=[bank.id],
                invoice_total=invoice_total,
                bank_total=bank.amount,
                difference_amount=Decimal("0.00"),
                confidence=Decimal("0.85") if is_late else Decimal("0.90"),
                remark=(
                    "同一往来单位、一笔银行流水等于多张发票唯一组合，且付款晚于部分发票到期日"
                    if is_late
                    else "同一往来单位、一笔银行流水等于多张发票唯一组合"
                ),
            )
        )
        for invoice in subset:
            unmatched_invoices.pop(invoice.id, None)
        unmatched_banks.pop(bank.id, None)


def _unique_invoice_subset_for_amount(invoices: list[InvoiceRecord], target: Decimal) -> list[InvoiceRecord] | None:
    if target <= Decimal("0.00") or len(invoices) < 2 or len(invoices) > MAX_SUBSET_INVOICES:
        return None
    candidates = sorted(invoices, key=lambda invoice: (invoice.invoice_date, invoice.id))
    found: list[tuple[InvoiceRecord, ...]] = []
    max_size = min(MAX_SUBSET_SIZE, len(candidates))
    for size in range(2, max_size + 1):
        for subset in combinations(candidates, size):
            total = sum((invoice.amount for invoice in subset), Decimal("0.00"))
            if total != target:
                continue
            found.append(subset)
            if len(found) > 1:
                return None
    if len(found) != 1:
        return None
    return list(found[0])


def _match_probable(
    unmatched_invoices: dict[int, InvoiceRecord],
    unmatched_banks: dict[int, BankRecord],
    results: list[MatchResult],
    probable_tolerance: Decimal,
) -> None:
    for invoice in list(unmatched_invoices.values()):
        for bank in list(unmatched_banks.values()):
            if not _same_party(invoice, bank):
                continue
            if not _within_invoice_window(invoice, bank):
                continue
            difference = abs(invoice.amount - bank.amount)
            if difference > probable_tolerance:
                continue
            results.append(
                MatchResult(
                    match_type="probable",
                    status="probable",
                    party_id=invoice.party_id,
                    party_name=invoice.party_name,
                    invoice_ids=[invoice.id],
                    bank_transaction_ids=[bank.id],
                    invoice_total=invoice.amount,
                    bank_total=bank.amount,
                    difference_amount=difference,
                    confidence=Decimal("0.75"),
                    remark="同一往来单位、日期合理，金额存在小额差异，需人工确认",
                )
            )
            unmatched_invoices.pop(invoice.id, None)
            unmatched_banks.pop(bank.id, None)
            break


def _match_early_payment_probable(
    unmatched_invoices: dict[int, InvoiceRecord],
    unmatched_banks: dict[int, BankRecord],
    results: list[MatchResult],
) -> None:
    for invoice in list(unmatched_invoices.values()):
        if invoice.amount <= Decimal("0.00"):
            continue
        for bank in list(unmatched_banks.values()):
            if not _same_party(invoice, bank):
                continue
            if invoice.amount != bank.amount:
                continue
            if bank.transaction_date >= invoice.invoice_date:
                continue
            results.append(
                MatchResult(
                    match_type="early_payment",
                    status="probable",
                    party_id=invoice.party_id,
                    party_name=invoice.party_name,
                    invoice_ids=[invoice.id],
                    bank_transaction_ids=[bank.id],
                    invoice_total=invoice.amount,
                    bank_total=bank.amount,
                    difference_amount=Decimal("0.00"),
                    confidence=Decimal("0.80"),
                    remark="同一往来单位、金额一致，但银行日期早于发票日期，可能是预收或预付，需人工确认",
                )
            )
            unmatched_invoices.pop(invoice.id, None)
            unmatched_banks.pop(bank.id, None)
            break


def _append_unmatched(
    unmatched_invoices: dict[int, InvoiceRecord],
    unmatched_banks: dict[int, BankRecord],
    results: list[MatchResult],
) -> None:
    for invoice in unmatched_invoices.values():
        results.append(
            MatchResult(
                match_type="unmatched",
                status="unmatched_invoice",
                party_id=invoice.party_id,
                party_name=invoice.party_name,
                invoice_ids=[invoice.id],
                bank_transaction_ids=[],
                invoice_total=invoice.amount,
                bank_total=Decimal("0.00"),
                difference_amount=invoice.amount,
                confidence=Decimal("0.00"),
                remark="发票未匹配到付款流水",
            )
        )
    for bank in unmatched_banks.values():
        results.append(
            MatchResult(
                match_type="unmatched",
                status="unmatched_bank",
                party_id=bank.party_id,
                party_name=bank.counterparty_name,
                invoice_ids=[],
                bank_transaction_ids=[bank.id],
                invoice_total=Decimal("0.00"),
                bank_total=bank.amount,
                difference_amount=bank.amount,
                confidence=Decimal("0.00"),
                remark="银行付款未匹配到发票",
            )
        )


def _same_party(invoice: InvoiceRecord, bank: BankRecord) -> bool:
    return _party_key(invoice.party_id, invoice.party_name) == _party_key(bank.party_id, bank.counterparty_name)


def _party_key(party_id: int | None, name: str) -> object:
    if party_id is not None:
        return ("id", party_id)
    return ("name", clean_name(name))


def _within_invoice_window(invoice: InvoiceRecord, bank: BankRecord) -> bool:
    return invoice.invoice_date <= bank.transaction_date <= invoice.grace_deadline


def _payment_on_or_after_invoice(invoice: InvoiceRecord, bank: BankRecord) -> bool:
    return invoice.invoice_date <= bank.transaction_date


def _is_late_payment(invoice: InvoiceRecord, bank: BankRecord) -> bool:
    return bank.transaction_date > invoice.grace_deadline


def _all_banks_in_group_window(invoices: list[InvoiceRecord], banks: list[BankRecord]) -> bool:
    earliest_invoice = min(invoice.invoice_date for invoice in invoices)
    latest_deadline = max(invoice.grace_deadline for invoice in invoices)
    return all(earliest_invoice <= bank.transaction_date <= latest_deadline for bank in banks)
