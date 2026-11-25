from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from finance_platform.config import Settings
from finance_platform.db import connection_scope, init_db
from finance_platform.local_store import LocalStore
from finance_platform.sqlserver_migration import MIGRATION_TABLES, TableMigration


def copy_sqlserver_to_sqlite(
    sqlite_path: str | Path,
    *,
    settings: Settings | None = None,
    reset_target: bool = True,
) -> dict[str, int]:
    db_path = Path(sqlite_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    local_store = LocalStore(db_path)
    local_store.init()

    ok, message = init_db(settings)
    if not ok:
        raise RuntimeError(f"SQL Server 初始化失败：{message}")

    counts: dict[str, int] = {}
    with local_store.connect() as sqlite_conn:
        with connection_scope(settings) as sqlserver_conn:
            cursor = sqlserver_conn.cursor()
            if reset_target:
                _clear_sqlite_tables(sqlite_conn)
            for table in MIGRATION_TABLES:
                rows = _read_sqlserver_table(cursor, table)
                if not rows:
                    counts[table.source_table] = 0
                    continue
                insert_sql = _build_sqlite_insert_sql(table.source_table, table.columns)
                for row in rows:
                    sqlite_conn.execute(insert_sql, [_normalize_sqlite_value(row.get(column)) for column in table.columns])
                counts[table.source_table] = len(rows)

    local_store.init()
    return counts


def _read_sqlserver_table(cursor: Any, table: TableMigration) -> list[dict[str, Any]]:
    columns = ", ".join(table.columns)
    cursor.execute(f"SELECT {columns} FROM {table.target_table} ORDER BY id")
    column_names = [column[0] for column in cursor.description]
    return [dict(zip(column_names, row)) for row in cursor.fetchall()]


def _clear_sqlite_tables(conn: sqlite3.Connection) -> None:
    for table in reversed(MIGRATION_TABLES):
        conn.execute(f"DELETE FROM {table.source_table}")


def _build_sqlite_insert_sql(table_name: str, columns: tuple[str, ...]) -> str:
    column_sql = ", ".join(columns)
    placeholder_sql = ", ".join("?" for _ in columns)
    return f"INSERT INTO {table_name} ({column_sql}) VALUES ({placeholder_sql})"


def _normalize_sqlite_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bool):
        return int(value)
    return value
