from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from finance_platform.config import Settings
from finance_platform.db import connection_scope, init_db


@dataclass(frozen=True)
class TableMigration:
    source_table: str
    target_table: str
    columns: tuple[str, ...]


MIGRATION_TABLES = [
    TableMigration(
        "parties",
        "dbo.lyc_fin_parties",
        (
            "id",
            "party_type",
            "name",
            "short_name",
            "default_term_months",
            "grace_months",
            "bank_account",
            "bank_name",
            "is_active",
            "remark",
            "created_at",
            "updated_at",
        ),
    ),
    TableMigration(
        "party_aliases",
        "dbo.lyc_fin_party_aliases",
        ("id", "party_id", "alias_name", "source_type", "is_active", "created_at"),
    ),
    TableMigration(
        "import_files",
        "dbo.lyc_fin_import_files",
        (
            "id",
            "period_month",
            "file_role",
            "original_filename",
            "stored_path",
            "file_hash",
            "row_count",
            "parsed_count",
            "error_count",
            "saved_count",
            "created_at",
        ),
    ),
    TableMigration(
        "invoices",
        "dbo.lyc_fin_invoices",
        (
            "id",
            "direction",
            "party_id",
            "party_name_raw",
            "invoice_no",
            "invoice_date",
            "invoice_amount",
            "term_months",
            "grace_months",
            "due_date",
            "grace_deadline",
            "status",
            "invoice_status_raw",
            "is_positive",
            "original_invoice_no",
            "red_confirmation_no",
            "invoice_type",
            "amount_without_tax",
            "tax_amount",
            "remark",
            "source_file_id",
            "source_row_no",
            "created_at",
        ),
    ),
    TableMigration(
        "bank_transactions",
        "dbo.lyc_fin_bank_transactions",
        (
            "id",
            "transaction_date",
            "income_amount",
            "expense_amount",
            "counterparty_account",
            "counterparty_name_raw",
            "counterparty_bank",
            "subject",
            "summary",
            "party_id",
            "direction",
            "match_status",
            "source_file_id",
            "source_row_no",
            "created_at",
        ),
    ),
    TableMigration(
        "match_results",
        "dbo.lyc_fin_match_results",
        (
            "id",
            "run_no",
            "direction",
            "party_id",
            "party_name",
            "match_type",
            "status",
            "invoice_ids",
            "bank_transaction_ids",
            "invoice_total",
            "bank_total",
            "difference_amount",
            "confidence",
            "remark",
            "settlement_method",
            "settlement_date",
            "created_at",
        ),
    ),
    TableMigration(
        "balance_adjustments",
        "dbo.lyc_fin_balance_adjustments",
        (
            "id",
            "direction",
            "party_id",
            "adjustment_type",
            "adjustment_date",
            "amount",
            "source_type",
            "remark",
            "created_at",
            "updated_at",
        ),
    ),
    TableMigration(
        "payment_templates",
        "dbo.lyc_fin_payment_templates",
        (
            "id",
            "bank_code",
            "bank_name",
            "template_name",
            "sheet_name",
            "default_purpose",
            "column_config",
            "is_active",
            "created_at",
            "updated_at",
        ),
    ),
]

MONEY_COLUMNS = {
    "invoice_amount",
    "amount_without_tax",
    "tax_amount",
    "income_amount",
    "expense_amount",
    "invoice_total",
    "bank_total",
    "difference_amount",
    "confidence",
    "amount",
}

BOOLEAN_COLUMNS = {"is_active", "is_positive"}


def build_insert_sql(target_table: str, columns: list[str] | tuple[str, ...]) -> str:
    column_sql = ", ".join(columns)
    placeholder_sql = ", ".join("?" for _ in columns)
    return f"INSERT INTO {target_table} ({column_sql}) VALUES ({placeholder_sql})"


def normalize_row_values(row: dict[str, Any], columns: list[str] | tuple[str, ...]) -> list[Any]:
    return [_normalize_value(column, row.get(column)) for column in columns]


def copy_sqlite_to_sqlserver(
    sqlite_path: str | Path,
    *,
    settings: Settings | None = None,
    reset_target: bool = False,
) -> dict[str, int]:
    db_path = Path(sqlite_path)
    if not db_path.exists():
        raise FileNotFoundError(f"找不到本地 SQLite 数据库：{db_path}")

    ok, message = init_db(settings)
    if not ok:
        raise RuntimeError(f"SQL Server 初始化失败：{message}")

    counts: dict[str, int] = {}
    with sqlite3.connect(db_path) as sqlite_conn:
        sqlite_conn.row_factory = sqlite3.Row
        with connection_scope(settings) as sqlserver_conn:
            cursor = sqlserver_conn.cursor()
            if reset_target:
                _clear_target_tables(cursor)
            for table in MIGRATION_TABLES:
                rows = _read_sqlite_table(sqlite_conn, table)
                if not rows:
                    counts[table.source_table] = 0
                    continue
                counts[table.source_table] = insert_rows_to_target(cursor, table, [dict(row) for row in rows])
    return counts


def _read_sqlite_table(conn: sqlite3.Connection, table: TableMigration) -> list[sqlite3.Row]:
    columns = ", ".join(table.columns)
    return conn.execute(f"SELECT {columns} FROM {table.source_table} ORDER BY id").fetchall()


def _clear_target_tables(cursor: Any) -> None:
    for table in reversed(MIGRATION_TABLES):
        cursor.execute(f"DELETE FROM {table.target_table}")


def insert_rows_to_target(cursor: Any, table: TableMigration, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    insert_sql = build_insert_sql(table.target_table, table.columns)
    values = [normalize_row_values(row, table.columns) for row in rows]
    if hasattr(cursor, "fast_executemany"):
        cursor.fast_executemany = True
    cursor.execute(f"SET IDENTITY_INSERT {table.target_table} ON")
    try:
        cursor.executemany(insert_sql, values)
    finally:
        cursor.execute(f"SET IDENTITY_INSERT {table.target_table} OFF")
    return len(values)


def _normalize_value(column: str, value: Any) -> Any:
    if value == "":
        return None
    if value is None:
        return None
    if column in MONEY_COLUMNS:
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    if column in BOOLEAN_COLUMNS:
        return bool(value)
    return value
