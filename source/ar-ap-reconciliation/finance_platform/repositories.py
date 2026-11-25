from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from finance_platform.db import connection_scope


def _row_to_dict(cursor, row) -> dict[str, Any]:
    columns = [column[0] for column in cursor.description]
    return dict(zip(columns, row))


def _as_mapping(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    return dict(value)


def create_import_batch(period_month: str | None, batch_type: str, uploaded_by: str = "system") -> int:
    batch_no = f"{batch_type}-{period_month or 'all'}-{datetime.now():%Y%m%d%H%M%S%f}"
    with connection_scope() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO dbo.lyc_fin_import_batches (batch_no, period_month, batch_type, uploaded_by)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?)
            """,
            batch_no,
            period_month,
            batch_type,
            uploaded_by,
        )
        return int(cursor.fetchone()[0])


def record_import_file(file_info: dict[str, Any]) -> int:
    with connection_scope() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO dbo.lyc_fin_import_files
                (batch_id, file_role, original_filename, stored_path, file_hash,
                 row_count, parsed_count, error_count, status)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            file_info.get("batch_id"),
            file_info["file_role"],
            file_info["original_filename"],
            file_info.get("stored_path"),
            file_info.get("file_hash"),
            int(file_info.get("row_count") or 0),
            int(file_info.get("parsed_count") or 0),
            int(file_info.get("error_count") or 0),
            file_info.get("status", "created"),
        )
        return int(cursor.fetchone()[0])


def upsert_party(
    party_type: str,
    name: str,
    short_name: str | None = None,
    default_term_months: int = 30,
    grace_months: int = 0,
    remark: str | None = None,
) -> int:
    with connection_scope() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM dbo.lyc_fin_parties WHERE party_type = ? AND name = ?",
            party_type,
            name,
        )
        row = cursor.fetchone()
        if row:
            party_id = int(row[0])
            cursor.execute(
                """
                UPDATE dbo.lyc_fin_parties
                SET short_name = ?, default_term_months = ?, grace_months = ?,
                    remark = ?, updated_at = GETDATE()
                WHERE id = ?
                """,
                short_name,
                int(default_term_months),
                0,
                remark,
                party_id,
            )
            return party_id

        cursor.execute(
            """
            INSERT INTO dbo.lyc_fin_parties
                (party_type, name, short_name, default_term_months, grace_months, remark)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            party_type,
            name,
            short_name,
            int(default_term_months),
            0,
            remark,
        )
        return int(cursor.fetchone()[0])


def add_party_alias(party_id: int, alias_name: str, source_type: str = "manual") -> None:
    with connection_scope() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            IF NOT EXISTS (
                SELECT 1 FROM dbo.lyc_fin_party_aliases
                WHERE party_id = ? AND alias_name = ?
            )
            INSERT INTO dbo.lyc_fin_party_aliases (party_id, alias_name, source_type)
            VALUES (?, ?, ?)
            """,
            party_id,
            alias_name,
            party_id,
            alias_name,
            source_type,
        )


def list_parties(party_type: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM dbo.lyc_fin_parties WHERE is_active = 1"
    params: list[Any] = []
    if party_type:
        sql += " AND party_type = ?"
        params.append(party_type)
    sql += " ORDER BY party_type, name"
    with connection_scope() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, *params)
        return [_row_to_dict(cursor, row) for row in cursor.fetchall()]


def list_aliases() -> list[dict[str, Any]]:
    with connection_scope() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT a.*, p.party_type, p.name AS party_name
            FROM dbo.lyc_fin_party_aliases a
            INNER JOIN dbo.lyc_fin_parties p ON p.id = a.party_id
            WHERE a.is_active = 1 AND p.is_active = 1
            """
        )
        return [_row_to_dict(cursor, row) for row in cursor.fetchall()]


def list_import_batches(limit: int = 100) -> list[dict[str, Any]]:
    with connection_scope() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT TOP {int(limit)}
                id, batch_no, period_month, batch_type, status,
                uploaded_by, uploaded_at, message
            FROM dbo.lyc_fin_import_batches
            ORDER BY uploaded_at DESC, id DESC
            """
        )
        return [_row_to_dict(cursor, row) for row in cursor.fetchall()]


def list_import_files(limit: int = 200) -> list[dict[str, Any]]:
    with connection_scope() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT TOP {int(limit)}
                id, batch_id, file_role, original_filename, stored_path,
                file_hash, row_count, parsed_count, error_count,
                status, uploaded_at
            FROM dbo.lyc_fin_import_files
            ORDER BY uploaded_at DESC, id DESC
            """
        )
        return [_row_to_dict(cursor, row) for row in cursor.fetchall()]


def insert_invoices(rows: Iterable[Any]) -> int:
    count = 0
    with connection_scope() as conn:
        cursor = conn.cursor()
        for row_value in rows:
            row = _as_mapping(row_value)
            cursor.execute(
                """
                INSERT INTO dbo.lyc_fin_invoices
                    (batch_id, direction, party_id, party_name_raw, invoice_no,
                     invoice_date, invoice_amount, term_months, grace_months,
                     due_date, grace_deadline, status, source_file_id, source_row_no)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row.get("batch_id"),
                row.get("direction", "payable"),
                row.get("party_id"),
                row["party_name_raw"],
                row.get("invoice_no"),
                row["invoice_date"],
                _decimal_to_db(row["invoice_amount"]),
                int(row.get("term_days") or row.get("term_months") or 30),
                0,
                row.get("due_date"),
                row.get("grace_deadline"),
                row.get("status", "unmatched"),
                row.get("source_file_id"),
                row.get("source_row_no"),
            )
            count += 1
    return count


def insert_bank_transactions(rows: Iterable[Any]) -> int:
    count = 0
    with connection_scope() as conn:
        cursor = conn.cursor()
        for row_value in rows:
            row = _as_mapping(row_value)
            cursor.execute(
                """
                INSERT INTO dbo.lyc_fin_bank_transactions
                    (batch_id, transaction_date, income_amount, expense_amount,
                     counterparty_account, counterparty_name_raw, counterparty_bank,
                     subject, summary, party_id, direction, match_status,
                     source_file_id, source_row_no)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row.get("batch_id"),
                row["transaction_date"],
                _decimal_to_db(row.get("income_amount")),
                _decimal_to_db(row.get("expense_amount")),
                row.get("counterparty_account"),
                row.get("counterparty_name_raw"),
                row.get("counterparty_bank"),
                row.get("subject"),
                row.get("summary"),
                row.get("party_id"),
                row.get("direction", "unknown"),
                row.get("match_status", "unmatched"),
                row.get("source_file_id"),
                row.get("source_row_no"),
            )
            count += 1
    return count


def create_reconcile_run(direction: str, period_start: date | None, period_end: date | None) -> int:
    run_no = f"{direction}-{period_start or 'all'}-{period_end or 'all'}-{datetime.now():%Y%m%d%H%M%S%f}"
    with connection_scope() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO dbo.lyc_fin_reconcile_runs (run_no, direction, period_start, period_end)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?)
            """,
            run_no,
            direction,
            period_start,
            period_end,
        )
        return int(cursor.fetchone()[0])


def query_payment_statistics(
    direction: str,
    start_date: date,
    end_date: date,
    party_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    amount_column = "expense_amount" if direction == "outflow" else "income_amount"
    sql = f"""
        SELECT
            party_id,
            counterparty_name_raw,
            COUNT(*) AS transaction_count,
            SUM(COALESCE({amount_column}, 0)) AS total_amount
        FROM dbo.lyc_fin_bank_transactions
        WHERE direction = ?
          AND transaction_date >= ?
          AND transaction_date <= ?
    """
    params: list[Any] = [direction, start_date, end_date]
    if party_ids:
        placeholders = ",".join("?" for _ in party_ids)
        sql += f" AND party_id IN ({placeholders})"
        params.extend(party_ids)
    sql += " GROUP BY party_id, counterparty_name_raw ORDER BY total_amount DESC"

    with connection_scope() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, *params)
        return [_row_to_dict(cursor, row) for row in cursor.fetchall()]


def _decimal_to_db(value: Decimal | float | int | str | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
