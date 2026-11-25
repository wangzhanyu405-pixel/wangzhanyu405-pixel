from __future__ import annotations

import hashlib
import shutil
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO

from finance_platform.config import Settings, get_settings
from finance_platform.matching import BankRecord, InvoiceRecord, MatchResult, match_payables
from finance_platform.models import BANK_OUTFLOW
from finance_platform.parsers import (
    DEFAULT_COMPANY_NAME,
    ParseResult,
    ParsedBankTransaction,
    ParsedInvoice,
    infer_invoice_file_role,
    parse_bank_statement,
    parse_party_directory,
    parse_payable_invoices,
    parse_receivable_invoices,
)
from finance_platform import repositories


@dataclass(frozen=True)
class ImportSummary:
    file_role: str
    original_filename: str
    stored_path: str
    file_hash: str
    row_count: int
    parsed_count: int
    error_count: int
    warnings: list[str]
    preview_rows: list[dict[str, Any]]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_upload(file_obj: BinaryIO, filename: str, period_month: str | None, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    target_dir = settings.upload_dir / (period_month or "unknown")
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name
    target = target_dir / safe_name
    with target.open("wb") as output:
        shutil.copyfileobj(file_obj, output)
    return target


def parse_file_by_role(path: Path, file_role: str) -> ParseResult:
    if file_role == "party_directory":
        return parse_party_directory(path)
    if file_role == "payable_invoice":
        return parse_payable_invoices(path)
    if file_role == "receivable_invoice":
        return parse_receivable_invoices(path)
    if file_role == "bank_statement":
        return parse_bank_statement(path)
    raise ValueError(f"未知文件类型: {file_role}")


def resolve_upload_file_role(path: Path, requested_role: str) -> str:
    if requested_role in {"invoice_auto", "payable_invoice", "receivable_invoice"}:
        fallback = requested_role if requested_role in {"payable_invoice", "receivable_invoice"} else None
        return infer_invoice_file_role(path, fallback_role=fallback)
    return requested_role


def repair_local_invoice_import_directions(store: Any, *, company_name: str = DEFAULT_COMPANY_NAME) -> dict[str, int]:
    repaired_files = 0
    repaired_rows = 0
    skipped_files = 0
    for import_row in store.list_import_files(file_roles=("payable_invoice", "receivable_invoice")):
        stored_path = Path(import_row.get("stored_path") or "")
        if not stored_path.exists():
            skipped_files += 1
            continue
        current_role = str(import_row.get("file_role") or "")
        inferred_role = infer_invoice_file_role(
            stored_path,
            fallback_role=current_role if current_role in {"payable_invoice", "receivable_invoice"} else None,
            company_name=company_name,
        )
        if inferred_role == current_role:
            continue
        result = parse_file_by_role(stored_path, inferred_role)
        source_file_id = int(import_row["id"])
        store.delete_invoices_for_import(source_file_id)
        store.update_import_parse_counts(
            source_file_id,
            period_month=import_row.get("period_month"),
            stored_path=str(stored_path),
            row_count=result.row_count,
            parsed_count=result.parsed_count,
            error_count=result.error_count,
            file_role=inferred_role,
        )
        from finance_platform.local_store import insert_parsed_invoices

        saved = insert_parsed_invoices(store, result.rows, source_file_id)
        store.update_import_saved_count(source_file_id, saved)
        repaired_files += 1
        repaired_rows += saved
    return {"repaired_files": repaired_files, "repaired_rows": repaired_rows, "skipped_files": skipped_files}


def build_import_summary(path: Path, file_role: str) -> ImportSummary:
    result = parse_file_by_role(path, file_role)
    return ImportSummary(
        file_role=file_role,
        original_filename=path.name,
        stored_path=str(path),
        file_hash=sha256_file(path),
        row_count=result.row_count,
        parsed_count=result.parsed_count,
        error_count=result.error_count,
        warnings=result.warnings,
        preview_rows=[asdict(row) for row in result.rows[:20]],
    )


def persist_party_rows(result: ParseResult) -> int:
    count = 0
    for row in result.rows:
        party_id = repositories.upsert_party(
            party_type=row.party_type,
            name=row.name,
            short_name=row.short_name,
            default_term_months=getattr(row, "default_term_days", row.default_term_months),
            grace_months=0,
        )
        repositories.add_party_alias(party_id, row.name, source_type="invoice")
        count += 1
    return count


def persist_invoice_rows(result: ParseResult, batch_id: int | None = None, source_file_id: int | None = None) -> int:
    rows = []
    for parsed in result.rows:
        if not isinstance(parsed, ParsedInvoice):
            continue
        row = asdict(parsed)
        row["batch_id"] = batch_id
        row["source_file_id"] = source_file_id
        row["party_name_raw"] = parsed.party_name_raw
        row["invoice_amount"] = parsed.invoice_amount
        rows.append(row)
    return repositories.insert_invoices(rows)


def persist_bank_rows(result: ParseResult, batch_id: int | None = None, source_file_id: int | None = None) -> int:
    rows = []
    for parsed in result.rows:
        if not isinstance(parsed, ParsedBankTransaction):
            continue
        row = asdict(parsed)
        row["batch_id"] = batch_id
        row["source_file_id"] = source_file_id
        rows.append(row)
    return repositories.insert_bank_transactions(rows)


def records_from_parsed_payables(rows: list[ParsedInvoice]) -> list[InvoiceRecord]:
    records: list[InvoiceRecord] = []
    for idx, row in enumerate(rows, start=1):
        records.append(
            InvoiceRecord(
                id=idx,
                party_id=None,
                party_name=row.party_name_raw,
                amount=row.invoice_amount,
                invoice_date=row.invoice_date,
                due_date=row.due_date,
                grace_deadline=row.grace_deadline,
            )
        )
    return records


def records_from_parsed_bank(rows: list[ParsedBankTransaction]) -> list[BankRecord]:
    records: list[BankRecord] = []
    for idx, row in enumerate(rows, start=1):
        if row.direction != BANK_OUTFLOW:
            continue
        amount = row.expense_amount or Decimal("0.00")
        records.append(
            BankRecord(
                id=idx,
                party_id=None,
                counterparty_name=row.counterparty_name_raw,
                amount=amount,
                transaction_date=row.transaction_date,
                direction=row.direction,
            )
        )
    return records


def run_payable_matching_preview(
    invoices: list[ParsedInvoice],
    bank_transactions: list[ParsedBankTransaction],
    as_of_date: date | None = None,
) -> list[MatchResult]:
    _ = as_of_date
    return match_payables(records_from_parsed_payables(invoices), records_from_parsed_bank(bank_transactions))


def dataclass_rows(rows: list[Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        output.append(asdict(row) if hasattr(row, "__dataclass_fields__") else dict(row))
    return output
