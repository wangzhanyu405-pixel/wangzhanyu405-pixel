from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from finance_platform.config import Settings
from finance_platform.db import DatabaseUnavailable, get_connection, init_db
from finance_platform.local_store import (
    LocalStore,
    _ensure_default_payment_template,
    _normalize_default_payment_template,
)
from finance_platform.normalization import clean_name


TABLE_MAP = {
    "party_aliases": "dbo.lyc_fin_party_aliases",
    "payment_templates": "dbo.lyc_fin_payment_templates",
    "bank_transactions": "dbo.lyc_fin_bank_transactions",
    "balance_adjustments": "dbo.lyc_fin_balance_adjustments",
    "match_results": "dbo.lyc_fin_match_results",
    "import_files": "dbo.lyc_fin_import_files",
    "invoices": "dbo.lyc_fin_invoices",
    "parties": "dbo.lyc_fin_parties",
}


IDENTITY_TABLES = set(TABLE_MAP.values())


class SqlServerRow(Mapping[str, Any]):
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)


class SqlServerResult:
    def __init__(
        self,
        rows: list[SqlServerRow] | None = None,
        *,
        rowcount: int = -1,
        lastrowid: int | None = None,
    ) -> None:
        self._rows = rows or []
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self._index = 0

    def fetchone(self) -> SqlServerRow | None:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self) -> list[SqlServerRow]:
        rows = self._rows[self._index :]
        self._index = len(self._rows)
        return rows


class SqlServerConnection:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._conn = get_connection(settings)

    def __enter__(self) -> "SqlServerConnection":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
        return False

    def execute(self, sql: str, params: Any = ()) -> SqlServerResult:
        return self._execute(sql, params)

    def executemany(self, sql: str, params_seq: list[list[Any]] | list[tuple[Any, ...]]) -> SqlServerResult:
        translated, _ = translate_sql_for_sqlserver(sql, [])
        cursor = self._conn.cursor()
        if hasattr(cursor, "fast_executemany"):
            cursor.fast_executemany = True
        cursor.executemany(translated, [_normalize_params(params) for params in params_seq])
        return SqlServerResult(rowcount=cursor.rowcount if cursor.rowcount is not None else -1)

    def _execute(self, sql: str, params: Any = ()) -> SqlServerResult:
        normalized_params = _normalize_params(params)
        translated_sql, translated_params = translate_sql_for_sqlserver(sql, normalized_params)
        if _is_insert_or_ignore_alias(translated_sql):
            return self._insert_or_ignore_alias(translated_params)

        translated_sql = _inject_identity_output(translated_sql)
        cursor = self._conn.cursor()
        if translated_params:
            cursor.execute(translated_sql, *translated_params)
        else:
            cursor.execute(translated_sql)

        rows = _fetch_rows(cursor)
        lastrowid = _extract_lastrowid(rows)
        rowcount = cursor.rowcount if cursor.rowcount is not None else -1
        return SqlServerResult(rows, rowcount=rowcount, lastrowid=lastrowid)

    def _insert_or_ignore_alias(self, params: list[Any]) -> SqlServerResult:
        party_id, alias_name, source_type, created_at = params
        cursor = self._conn.cursor()
        cursor.execute(
            """
            IF NOT EXISTS (
                SELECT 1
                FROM dbo.lyc_fin_party_aliases
                WHERE party_id = ? AND alias_name = ?
            )
            BEGIN
                INSERT INTO dbo.lyc_fin_party_aliases
                    (party_id, alias_name, source_type, created_at)
                OUTPUT INSERTED.id
                VALUES (?, ?, ?, ?)
            END
            """,
            int(party_id),
            alias_name,
            int(party_id),
            alias_name,
            source_type,
            created_at,
        )
        rows = _fetch_rows(cursor)
        return SqlServerResult(rows, rowcount=cursor.rowcount if cursor.rowcount is not None else -1, lastrowid=_extract_lastrowid(rows))


class SqlServerStore(LocalStore):
    """SQL Server backed store that keeps the LocalStore business API."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings.local_db_path)
        self.settings = settings

    def connect(self) -> SqlServerConnection:
        return SqlServerConnection(self.settings)

    def init(self) -> None:
        ok, message = init_db(self.settings)
        if not ok:
            raise DatabaseUnavailable(message)
        with self.connect() as conn:
            _ensure_default_payment_template(conn)  # type: ignore[arg-type]
            _normalize_default_payment_template(conn)  # type: ignore[arg-type]

    def _add_alias_with_conn(self, conn: SqlServerConnection, party_id: int, alias_name: str, source_type: str) -> None:  # type: ignore[override]
        alias_name = clean_name(alias_name)
        if not alias_name:
            return
        conn.execute(
            """
            IF NOT EXISTS (
                SELECT 1
                FROM party_aliases
                WHERE party_id = ? AND alias_name = ?
            )
            BEGIN
                INSERT INTO party_aliases (party_id, alias_name, source_type, created_at)
                VALUES (?, ?, ?, ?)
            END
            """,
            (int(party_id), alias_name, int(party_id), alias_name, source_type, datetime.now().isoformat(timespec="seconds")),
        )


def translate_sql_for_sqlserver(sql: str, params: list[Any] | tuple[Any, ...] | None = None) -> tuple[str, list[Any]]:
    translated = _replace_tables(sql)
    translated = _translate_text_coalesce_for_decimal_columns(translated)
    translated, translated_params = _translate_limit(translated, list(params or []))
    translated = _translate_insert_or_ignore(translated)
    return translated, translated_params


def _translate_text_coalesce_for_decimal_columns(sql: str) -> str:
    translated = sql
    for column in ("income_amount", "expense_amount", "invoice_amount", "amount_without_tax", "tax_amount", "invoice_total", "bank_total", "difference_amount", "confidence"):
        translated = re.sub(
            rf"COALESCE\(\s*{column}\s*,\s*''\s*\)",
            f"COALESCE(CONVERT(NVARCHAR(50), {column}), '')",
            translated,
            flags=re.IGNORECASE,
        )
    return translated


def _replace_tables(sql: str) -> str:
    translated = sql
    for source, target in sorted(TABLE_MAP.items(), key=lambda item: len(item[0]), reverse=True):
        translated = re.sub(rf"\b{re.escape(source)}\b", target, translated, flags=re.IGNORECASE)
    return translated


def _translate_limit(sql: str, params: list[Any]) -> tuple[str, list[Any]]:
    param_match = re.search(r"\s+LIMIT\s+\?\s*$", sql, flags=re.IGNORECASE)
    if param_match:
        if not params:
            raise ValueError("SQL Server LIMIT 转换缺少参数")
        limit = int(params.pop())
        return _inject_top(sql[: param_match.start()], limit), params

    literal_match = re.search(r"\s+LIMIT\s+(\d+)\s*$", sql, flags=re.IGNORECASE)
    if literal_match:
        limit = int(literal_match.group(1))
        return _inject_top(sql[: literal_match.start()], limit), params

    return sql, params


def _inject_top(sql: str, limit: int) -> str:
    safe_limit = max(1, int(limit))
    return re.sub(r"^\s*SELECT\s+", lambda match: f"{match.group(0)}TOP {safe_limit} ", sql, count=1, flags=re.IGNORECASE)


def _translate_insert_or_ignore(sql: str) -> str:
    return re.sub(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", "INSERT OR IGNORE INTO ", sql, count=1, flags=re.IGNORECASE)


def _is_insert_or_ignore_alias(sql: str) -> bool:
    return bool(re.match(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+dbo\.lyc_fin_party_aliases\b", sql, flags=re.IGNORECASE))


def _inject_identity_output(sql: str) -> str:
    match = re.match(
        r"^(\s*INSERT\s+INTO\s+([^\s(]+)\s*\([^)]*\)\s*)VALUES\b",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return sql
    table_name = match.group(2)
    if table_name.lower() not in {table.lower() for table in IDENTITY_TABLES}:
        return sql
    if re.search(r"\bOUTPUT\s+INSERTED\.id\b", sql, flags=re.IGNORECASE):
        return sql
    return f"{match.group(1)}OUTPUT INSERTED.id VALUES{sql[match.end():]}"


def _normalize_params(params: Any) -> list[Any]:
    if params is None:
        return []
    if isinstance(params, list):
        return [_normalize_param(value) for value in params]
    if isinstance(params, tuple):
        return [_normalize_param(value) for value in params]
    return [_normalize_param(params)]


def _normalize_param(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    return value


def _fetch_rows(cursor: Any) -> list[SqlServerRow]:
    if not cursor.description:
        return []
    columns = [column[0] for column in cursor.description]
    return [
        SqlServerRow({column: _normalize_output_value(value) for column, value in zip(columns, row)})
        for row in cursor.fetchall()
    ]


def _normalize_output_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, bool):
        return int(value)
    return value


def _extract_lastrowid(rows: list[SqlServerRow]) -> int | None:
    if not rows:
        return None
    first = rows[0]
    if "id" in first:
        return int(first["id"])
    values = list(first.values())
    if not values:
        return None
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return None
