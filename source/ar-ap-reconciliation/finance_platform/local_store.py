from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from finance_platform.bank_classification import classify_counterparty_nature
from finance_platform.date_rules import calculate_terms
from finance_platform.fuzzy_match import company_similarity
from finance_platform.normalization import clean_name, normalize_amount, normalize_date, period_month


ZERO = Decimal("0.00")
SETTLED_STATUSES = {"matched"}
BANK_CATEGORY_LABELS = {
    "supplier_payment": "供应商付款",
    "customer_receipt": "客户回款",
    "tax_payment": "税费",
    "payroll": "工资社保",
    "bank_fee": "手续费/利息",
    "internal_transfer": "内部转账",
    "refund": "退款/冲正",
    "company_candidate": "未建档公司候选",
    "personal_flow": "个人流水",
    "non_reconciliation": "非对账流水",
    "unidentified_outflow": "未识别支出",
    "unidentified_inflow": "未识别收入",
    "unknown": "未分类",
}
DEFAULT_ABC_PAYMENT_COLUMNS = [
    "编号",
    "收款方账号",
    "收款方户名",
    "是否农行账户",
    "开户银行（行别）",
    "开户行大额行号",
    "开户行支行名称",
    "金额",
    "用途（附言）",
    "企业自制凭证号",
]
DEFAULT_ABC_PAYMENT_PURPOSE = "货款"


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        should_suppress = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return bool(should_suppress)


class LocalStore:
    def __init__(self, db_path: str | Path = "data/local_finance.db") -> None:
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS parties (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    party_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    short_name TEXT,
                    default_term_months INTEGER NOT NULL DEFAULT 30,
                    grace_months INTEGER NOT NULL DEFAULT 0,
                    bank_account TEXT,
                    bank_name TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    remark TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT,
                    UNIQUE (party_type, name)
                );

                CREATE TABLE IF NOT EXISTS party_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    party_id INTEGER NOT NULL,
                    alias_name TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'manual',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE (party_id, alias_name)
                );

                CREATE TABLE IF NOT EXISTS import_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_month TEXT,
                    file_role TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    stored_path TEXT,
                    file_hash TEXT,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    parsed_count INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    saved_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS invoices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    direction TEXT NOT NULL,
                    party_id INTEGER,
                    party_name_raw TEXT NOT NULL,
                    invoice_no TEXT,
                    invoice_date TEXT NOT NULL,
                    invoice_amount TEXT NOT NULL,
                    term_months INTEGER NOT NULL DEFAULT 30,
                    grace_months INTEGER NOT NULL DEFAULT 0,
                    due_date TEXT NOT NULL,
                    grace_deadline TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unmatched',
                    invoice_status_raw TEXT,
                    is_positive INTEGER,
                    original_invoice_no TEXT,
                    red_confirmation_no TEXT,
                    invoice_type TEXT,
                    amount_without_tax TEXT,
                    tax_amount TEXT,
                    remark TEXT,
                    source_file_id INTEGER,
                    source_row_no INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bank_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_date TEXT NOT NULL,
                    income_amount TEXT,
                    expense_amount TEXT,
                    counterparty_account TEXT,
                    counterparty_name_raw TEXT,
                    counterparty_bank TEXT,
                    subject TEXT,
                    summary TEXT,
                    party_id INTEGER,
                    direction TEXT NOT NULL,
                    match_status TEXT NOT NULL DEFAULT 'unmatched',
                    source_file_id INTEGER,
                    source_row_no INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS match_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_no TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    party_id INTEGER,
                    party_name TEXT,
                    match_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    invoice_ids TEXT,
                    bank_transaction_ids TEXT,
                    invoice_total TEXT NOT NULL,
                    bank_total TEXT NOT NULL,
                    difference_amount TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    remark TEXT,
                    settlement_method TEXT,
                    settlement_date TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS payment_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bank_code TEXT NOT NULL,
                    bank_name TEXT NOT NULL,
                    template_name TEXT NOT NULL,
                    sheet_name TEXT NOT NULL,
                    default_purpose TEXT,
                    column_config TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT,
                    UNIQUE (bank_code, template_name)
                );

                CREATE TABLE IF NOT EXISTS balance_adjustments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    direction TEXT NOT NULL,
                    party_id INTEGER NOT NULL,
                    adjustment_type TEXT NOT NULL,
                    adjustment_date TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'manual',
                    remark TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT
                );
                """
            )
            _ensure_column(conn, "parties", "bank_account", "TEXT")
            _ensure_column(conn, "parties", "bank_name", "TEXT")
            _ensure_column(conn, "import_files", "saved_count", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "invoices", "invoice_status_raw", "TEXT")
            _ensure_column(conn, "invoices", "is_positive", "INTEGER")
            _ensure_column(conn, "invoices", "original_invoice_no", "TEXT")
            _ensure_column(conn, "invoices", "red_confirmation_no", "TEXT")
            _ensure_column(conn, "invoices", "invoice_type", "TEXT")
            _ensure_column(conn, "invoices", "amount_without_tax", "TEXT")
            _ensure_column(conn, "invoices", "tax_amount", "TEXT")
            _ensure_column(conn, "invoices", "remark", "TEXT")
            _ensure_column(conn, "match_results", "settlement_method", "TEXT")
            _ensure_column(conn, "match_results", "settlement_date", "TEXT")
            _refresh_import_saved_counts_with_conn(conn)
            _ensure_default_payment_template(conn)
            _normalize_default_payment_template(conn)

    def clear_all(self) -> None:
        with self.connect() as conn:
            for table in ["balance_adjustments", "match_results", "bank_transactions", "invoices", "import_files", "party_aliases", "parties"]:
                conn.execute(f"DELETE FROM {table}")

    def upsert_party(
        self,
        party_type: str,
        name: str,
        short_name: str | None = None,
        default_term_months: int | None = None,
        grace_months: int | None = None,
        default_term_days: int | None = None,
        bank_account: str | None = None,
        bank_name: str | None = None,
        remark: str | None = None,
    ) -> int:
        name = clean_name(name)
        term_days = _coerce_term_days(default_term_days, default_term_months)
        bank_account = clean_name(bank_account) or None
        bank_name = clean_name(bank_name) or None
        now = _now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM parties WHERE party_type = ? AND name = ?",
                (party_type, name),
            ).fetchone()
            if row:
                party_id = int(row["id"])
                conn.execute(
                    """
                    UPDATE parties
                    SET short_name = ?, default_term_months = ?, grace_months = ?,
                        bank_account = COALESCE(?, bank_account),
                        bank_name = COALESCE(?, bank_name),
                        remark = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        short_name,
                        term_days,
                        0,
                        bank_account,
                        bank_name,
                        remark,
                        now,
                        party_id,
                    ),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO parties
                        (party_type, name, short_name, default_term_months, grace_months,
                         bank_account, bank_name, remark, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        party_type,
                        name,
                        short_name,
                        term_days,
                        0,
                        bank_account,
                        bank_name,
                        remark,
                        now,
                    ),
                )
                party_id = int(cursor.lastrowid)
            self._add_alias_with_conn(conn, party_id, name, "name")
            return party_id

    def add_alias(self, party_id: int, alias_name: str, source_type: str = "manual") -> None:
        with self.connect() as conn:
            self._add_alias_with_conn(conn, party_id, alias_name, source_type)

    def update_party_fields(
        self,
        party_id: int,
        *,
        default_term_months: int | None = None,
        default_term_days: int | None = None,
        grace_months: int | None = None,
        bank_account: str | None = None,
        bank_name: str | None = None,
        short_name: str | None = None,
        remark: str | None = None,
        alias_name: str | None = None,
    ) -> None:
        assignments = []
        params: list[Any] = []
        if default_term_days is not None or default_term_months is not None:
            assignments.append("default_term_months = ?")
            params.append(_coerce_term_days(default_term_days, default_term_months))
        if grace_months is not None:
            assignments.append("grace_months = ?")
            params.append(0)
        if bank_account is not None:
            assignments.append("bank_account = ?")
            params.append(clean_name(bank_account) or None)
        if bank_name is not None:
            assignments.append("bank_name = ?")
            params.append(clean_name(bank_name) or None)
        if short_name is not None:
            assignments.append("short_name = ?")
            params.append(clean_name(short_name) or None)
        if remark is not None:
            assignments.append("remark = ?")
            params.append(clean_name(remark) or None)
        assignments.append("updated_at = ?")
        params.append(_now())
        params.append(int(party_id))

        with self.connect() as conn:
            conn.execute(
                f"""
                UPDATE parties
                SET {", ".join(assignments)}
                WHERE id = ?
                """,
                params,
            )
            if alias_name:
                self._add_alias_with_conn(conn, int(party_id), alias_name, "manual")

    def update_party_identity(self, party_id: int, *, party_type: str, name: str) -> None:
        name = clean_name(name)
        if party_type not in {"supplier", "customer"}:
            raise ValueError("往来单位类型只能是供应商或客户")
        if not name:
            raise ValueError("往来单位名称不能为空")
        with self.connect() as conn:
            current = conn.execute(
                "SELECT id FROM parties WHERE id = ? AND is_active = 1",
                (int(party_id),),
            ).fetchone()
            if not current:
                raise ValueError("往来单位不存在或已删除")
            duplicate = conn.execute(
                """
                SELECT id FROM parties
                WHERE party_type = ? AND name = ? AND id <> ? AND is_active = 1
                """,
                (party_type, name, int(party_id)),
            ).fetchone()
            if duplicate:
                raise ValueError("同类型下已存在相同名称的往来单位")
            conn.execute(
                """
                UPDATE parties
                SET party_type = ?, name = ?, updated_at = ?
                WHERE id = ?
                """,
                (party_type, name, _now(), int(party_id)),
            )
            self._add_alias_with_conn(conn, int(party_id), name, "name")

    def archive_party(self, party_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE parties
                SET is_active = 0, updated_at = ?
                WHERE id = ?
                """,
                (_now(), int(party_id)),
            )
            conn.execute("UPDATE party_aliases SET is_active = 0 WHERE party_id = ?", (int(party_id),))
            conn.execute("UPDATE invoices SET party_id = NULL WHERE party_id = ?", (int(party_id),))
            conn.execute("UPDATE bank_transactions SET party_id = NULL WHERE party_id = ?", (int(party_id),))

    def archive_all_parties(self) -> int:
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM parties WHERE is_active = 1").fetchall()
            party_ids = [int(row["id"]) for row in rows]
            if not party_ids:
                return 0
            now = _now()
            conn.execute("UPDATE parties SET is_active = 0, updated_at = ? WHERE is_active = 1", (now,))
            conn.execute("UPDATE party_aliases SET is_active = 0")
            conn.execute("UPDATE invoices SET party_id = NULL WHERE party_id IS NOT NULL")
            conn.execute("UPDATE bank_transactions SET party_id = NULL WHERE party_id IS NOT NULL")
            return len(party_ids)

    def _add_alias_with_conn(self, conn: sqlite3.Connection, party_id: int, alias_name: str, source_type: str) -> None:
        alias_name = clean_name(alias_name)
        if not alias_name:
            return
        conn.execute(
            """
            INSERT OR IGNORE INTO party_aliases (party_id, alias_name, source_type, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (party_id, alias_name, source_type, _now()),
        )

    def resolve_party_id(self, name: str, preferred_party_type: str | None = None) -> int | None:
        name = clean_name(name)
        if not name:
            return None
        with self.connect() as conn:
            row = _resolve_party_id_with_conn(conn, name, preferred_party_type=preferred_party_type)
            return row

    def _fallback_resolve_party_id(self, name: str) -> int | None:
        name = clean_name(name)
        if not name:
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM parties WHERE name = ? AND is_active = 1", (name,)).fetchone()
            if row:
                return int(row["id"])
            alias = conn.execute(
                """
                SELECT party_id FROM party_aliases
                WHERE alias_name = ? AND is_active = 1
                """,
                (name,),
            ).fetchone()
            return int(alias["party_id"]) if alias else None

    def get_party(self, party_id: int | None) -> dict[str, Any] | None:
        if party_id is None:
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
            return _party_dict(row) if row else None

    def get_invoice(self, invoice_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT i.*, p.name AS party_name
                FROM invoices i
                LEFT JOIN parties p ON p.id = i.party_id
                WHERE i.id = ?
                """,
                (int(invoice_id),),
            ).fetchone()
            return _invoice_dict(row) if row else None

    def list_parties(self, party_type: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM parties WHERE is_active = 1"
        params: list[Any] = []
        if party_type:
            sql += " AND party_type = ?"
            params.append(party_type)
        sql += " ORDER BY party_type, name"
        with self.connect() as conn:
            return [_party_dict(row) for row in conn.execute(sql, params).fetchall()]

    def list_aliases(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT a.*, p.party_type, p.name AS party_name
                    FROM party_aliases a
                    INNER JOIN parties p ON p.id = a.party_id
                    WHERE a.is_active = 1 AND p.is_active = 1
                    ORDER BY p.party_type, p.name, a.alias_name
                    """
                ).fetchall()
            ]

    def list_payment_templates(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = [
                _payment_template_row(row)
                for row in conn.execute(
                    """
                    SELECT * FROM payment_templates
                    WHERE is_active = 1
                    ORDER BY bank_code, template_name
                    """
                ).fetchall()
            ]
        return rows

    def get_payment_template(self, bank_code: str = "ABC") -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM payment_templates
                WHERE bank_code = ? AND is_active = 1
                ORDER BY id
                LIMIT 1
                """,
                (clean_name(bank_code) or "ABC",),
            ).fetchone()
            if row is None:
                raise ValueError("找不到付款模板配置")
            return _payment_template_row(row)

    def save_payment_template(
        self,
        template_id: int,
        *,
        template_name: str,
        sheet_name: str,
        default_purpose: str,
        columns: list[str],
    ) -> None:
        cleaned_columns = [_template_text(column) for column in columns if _template_text(column)]
        if not cleaned_columns:
            raise ValueError("付款模板至少需要保留一列")
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE payment_templates
                SET template_name = ?, sheet_name = ?, default_purpose = ?,
                    column_config = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    clean_name(template_name) or "农业银行批量付款模板",
                    clean_name(sheet_name) or "页一【批量转账文件】",
                    clean_name(default_purpose),
                    json.dumps(cleaned_columns, ensure_ascii=False),
                    _now(),
                    int(template_id),
                ),
            )

    def upsert_payment_template(
        self,
        *,
        bank_code: str,
        bank_name: str,
        template_name: str,
        sheet_name: str,
        default_purpose: str,
        columns: list[str],
    ) -> int:
        cleaned_columns = [_template_text(column) for column in columns if _template_text(column)]
        if not cleaned_columns:
            raise ValueError("付款模板至少需要保留一列")
        code = clean_name(bank_code) or "ABC"
        name = clean_name(template_name) or "农业银行批量付款模板"
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM payment_templates
                WHERE bank_code = ? AND template_name = ?
                LIMIT 1
                """,
                (code, name),
            ).fetchone()
            if row:
                template_id = int(row["id"])
                conn.execute(
                    """
                    UPDATE payment_templates
                    SET bank_name = ?, sheet_name = ?, default_purpose = ?,
                        column_config = ?, is_active = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        clean_name(bank_name) or "中国农业银行",
                        clean_name(sheet_name) or "页一【批量转账文件】",
                        clean_name(default_purpose),
                        json.dumps(cleaned_columns, ensure_ascii=False),
                        _now(),
                        template_id,
                    ),
                )
                return template_id
            cursor = conn.execute(
                """
                INSERT INTO payment_templates
                    (bank_code, bank_name, template_name, sheet_name, default_purpose,
                     column_config, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    clean_name(bank_name) or "中国农业银行",
                    name,
                    clean_name(sheet_name) or "页一【批量转账文件】",
                    clean_name(default_purpose),
                    json.dumps(cleaned_columns, ensure_ascii=False),
                    _now(),
                ),
            )
            return int(cursor.lastrowid)

    def delete_payment_template(self, template_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE payment_templates
                SET is_active = 0, updated_at = ?
                WHERE id = ?
                """,
                (_now(), int(template_id)),
            )

    def bank_transaction_duplicate_exists(
        self,
        *,
        transaction_date: date,
        income_amount: Decimal | None,
        expense_amount: Decimal | None,
        counterparty_account: str | None,
        counterparty_name_raw: str,
        counterparty_bank: str | None,
        subject: str | None,
        summary: str | None,
        direction: str,
        source_file_id: int | None,
    ) -> bool:
        if source_file_id is None:
            return False
        with self.connect() as conn:
            return _bank_transaction_duplicate_exists_with_conn(
                conn,
                transaction_date=transaction_date,
                income_amount=income_amount,
                expense_amount=expense_amount,
                counterparty_account=counterparty_account,
                counterparty_name_raw=counterparty_name_raw,
                counterparty_bank=counterparty_bank,
                subject=subject,
                summary=summary,
                direction=direction,
                source_file_id=source_file_id,
            )

    def deduplicate_cross_file_bank_transactions(self) -> int:
        removed = 0
        with self.connect() as conn:
            groups = conn.execute(
                """
                SELECT transaction_date,
                       COALESCE(income_amount, '') AS income_amount,
                       COALESCE(expense_amount, '') AS expense_amount,
                       COALESCE(counterparty_account, '') AS counterparty_account,
                       COALESCE(counterparty_name_raw, '') AS counterparty_name_raw,
                       COALESCE(counterparty_bank, '') AS counterparty_bank,
                       COALESCE(subject, '') AS subject,
                       COALESCE(summary, '') AS summary,
                       COALESCE(direction, '') AS direction,
                       MIN(source_file_id) AS keep_source_file_id
                FROM bank_transactions
                WHERE source_file_id IS NOT NULL
                GROUP BY transaction_date, COALESCE(income_amount, ''), COALESCE(expense_amount, ''),
                         COALESCE(counterparty_account, ''), COALESCE(counterparty_name_raw, ''),
                         COALESCE(counterparty_bank, ''), COALESCE(subject, ''), COALESCE(summary, ''),
                         COALESCE(direction, '')
                HAVING COUNT(DISTINCT source_file_id) > 1
                """
            ).fetchall()
            for group in groups:
                cursor = conn.execute(
                    """
                    DELETE FROM bank_transactions
                    WHERE transaction_date = ?
                      AND COALESCE(income_amount, '') = ?
                      AND COALESCE(expense_amount, '') = ?
                      AND COALESCE(counterparty_account, '') = ?
                      AND COALESCE(counterparty_name_raw, '') = ?
                      AND COALESCE(counterparty_bank, '') = ?
                      AND COALESCE(subject, '') = ?
                      AND COALESCE(summary, '') = ?
                      AND COALESCE(direction, '') = ?
                      AND source_file_id <> ?
                    """,
                    (
                        group["transaction_date"],
                        group["income_amount"],
                        group["expense_amount"],
                        group["counterparty_account"],
                        group["counterparty_name_raw"],
                        group["counterparty_bank"],
                        group["subject"],
                        group["summary"],
                        group["direction"],
                        int(group["keep_source_file_id"]),
                    ),
                )
                removed += max(cursor.rowcount, 0)
            if removed:
                _refresh_import_saved_counts_with_conn(conn)
        return removed

    def fuzzy_party_candidates(
        self,
        name: str,
        *,
        preferred_party_type: str | None = None,
        min_score: float = 0.80,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        name = clean_name(name)
        if not name:
            return []
        with self.connect() as conn:
            return _fuzzy_party_candidates_with_conn(
                conn,
                name,
                preferred_party_type=preferred_party_type,
                min_score=min_score,
                limit=limit,
            )

    def unidentified_bank_fuzzy_matches(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        *,
        min_score: float = 0.80,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for row in self.list_bank_transactions(start_date=start_date, end_date=end_date):
            if row["business_category"] not in {"company_candidate", "unidentified_outflow", "unidentified_inflow"}:
                continue
            candidates = self.fuzzy_party_candidates(
                row.get("counterparty_name_raw") or "",
                preferred_party_type=_preferred_party_type(row.get("direction")),
                min_score=min_score,
                limit=3,
            )
            if not candidates:
                continue
            best = candidates[0]
            output.append(
                {
                    "bank_transaction_id": int(row["id"]),
                    "transaction_date": row["transaction_date"],
                    "direction": row["direction"],
                    "direction_label": "收入" if row["direction"] == "inflow" else "支出",
                    "counterparty_name_raw": row.get("counterparty_name_raw") or "",
                    "amount": _bank_row_amount(row),
                    "business_category": row["business_category"],
                    "business_category_label": row["business_category_label"],
                    "suggested_party_id": best["party_id"],
                    "suggested_party_name": best["party_name"],
                    "suggested_party_type": best["party_type"],
                    "suggested_party_type_label": _party_type_label(best["party_type"]),
                    "matched_name": best["matched_name"],
                    "score": best["score"],
                    "candidate_count": len(candidates),
                }
            )
            if len(output) >= limit:
                break
        return output

    def confirm_bank_fuzzy_match(self, bank_transaction_id: int, party_id: int) -> None:
        with self.connect() as conn:
            bank = conn.execute(
                """
                SELECT id, direction, counterparty_name_raw
                FROM bank_transactions
                WHERE id = ?
                """,
                (int(bank_transaction_id),),
            ).fetchone()
            if bank is None:
                raise ValueError("找不到这条银行流水")
            party = conn.execute(
                """
                SELECT id, party_type
                FROM parties
                WHERE id = ? AND is_active = 1
                """,
                (int(party_id),),
            ).fetchone()
            if party is None:
                raise ValueError("找不到这个往来单位")
            expected_type = _preferred_party_type(bank["direction"])
            if expected_type and party["party_type"] != expected_type:
                raise ValueError(f"这条{_bank_direction_label(bank['direction'])}流水只能确认到{_party_type_label(expected_type)}")
            alias_name = clean_name(bank["counterparty_name_raw"])
            existing = conn.execute(
                """
                SELECT a.party_id
                FROM party_aliases a
                INNER JOIN parties p ON p.id = a.party_id
                WHERE a.alias_name = ? AND a.is_active = 1
                  AND p.party_type = ? AND p.is_active = 1
                  AND a.party_id <> ?
                LIMIT 1
                """,
                (alias_name, party["party_type"], int(party_id)),
            ).fetchone()
            if existing:
                raise ValueError("这个银行户名已经映射到同类型的其他往来单位")
            self._add_alias_with_conn(conn, int(party_id), alias_name, "fuzzy_confirmed")
            conn.execute(
                "UPDATE bank_transactions SET party_id = ? WHERE id = ?",
                (int(party_id), int(bank_transaction_id)),
            )

    def promote_bank_counterparty(
        self,
        bank_transaction_id: int,
        party_type: str,
        *,
        term_months: int | None = None,
        grace_months: int | None = None,
        term_days: int | None = None,
    ) -> int:
        if party_type not in {"supplier", "customer"}:
            raise ValueError("往来单位类型只能是 supplier 或 customer")
        with self.connect() as conn:
            bank = conn.execute(
                """
                SELECT id, counterparty_name_raw, counterparty_account, counterparty_bank
                FROM bank_transactions
                WHERE id = ?
                """,
                (int(bank_transaction_id),),
            ).fetchone()
            if bank is None:
                raise ValueError("找不到这条银行流水")
            party_name = clean_name(bank["counterparty_name_raw"])
            if not party_name:
                raise ValueError("这条流水没有对方户名，不能直接转为往来单位")
            existing = _resolve_party_id_with_conn(conn, party_name, preferred_party_type=party_type)
            if existing is not None:
                party_id = int(existing)
            else:
                now = _now()
                effective_term_days = _coerce_term_days(term_days, term_months)
                cursor = conn.execute(
                    """
                    INSERT INTO parties
                        (party_type, name, default_term_months, grace_months,
                         bank_account, bank_name, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        party_type,
                        party_name,
                        effective_term_days,
                        0,
                        clean_name(bank["counterparty_account"]) or None,
                        clean_name(bank["counterparty_bank"]) or None,
                        now,
                    ),
                )
                party_id = int(cursor.lastrowid)
            self._add_alias_with_conn(conn, party_id, party_name, "bank_review")
            conn.execute(
                "UPDATE bank_transactions SET party_id = ? WHERE id = ?",
                (party_id, int(bank_transaction_id)),
            )
            return party_id

    def record_import_file(
        self,
        period_month: str | None,
        file_role: str,
        original_filename: str,
        stored_path: str | None,
        file_hash: str | None,
        row_count: int,
        parsed_count: int,
        error_count: int,
        saved_count: int = 0,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO import_files
                    (period_month, file_role, original_filename, stored_path, file_hash,
                     row_count, parsed_count, error_count, saved_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    period_month,
                    file_role,
                    original_filename,
                    stored_path,
                    file_hash,
                    int(row_count),
                    int(parsed_count),
                    int(error_count),
                    int(saved_count),
                    _now(),
                ),
            )
            return int(cursor.lastrowid)

    def update_import_saved_count(self, source_file_id: int, saved_count: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE import_files SET saved_count = ? WHERE id = ?",
                (int(saved_count), int(source_file_id)),
            )

    def update_import_parse_counts(
        self,
        source_file_id: int,
        *,
        period_month: str | None,
        stored_path: str | None,
        row_count: int,
        parsed_count: int,
        error_count: int,
        file_role: str | None = None,
    ) -> None:
        assignments = [
            "period_month = ?",
            "stored_path = ?",
            "row_count = ?",
            "parsed_count = ?",
            "error_count = ?",
        ]
        params: list[Any] = [
            period_month,
            stored_path,
            int(row_count),
            int(parsed_count),
            int(error_count),
        ]
        if file_role:
            assignments.insert(0, "file_role = ?")
            params.insert(0, clean_name(file_role))
        params.append(int(source_file_id))
        with self.connect() as conn:
            conn.execute(
                f"""
                UPDATE import_files
                SET {", ".join(assignments)}
                WHERE id = ?
                """,
                params,
            )

    def count_import_detail_rows(self, file_role: str, source_file_id: int) -> int:
        with self.connect() as conn:
            return _count_import_details_with_conn(conn, file_role, int(source_file_id))

    def list_import_files(self, file_roles: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM import_files"
        params: list[Any] = []
        if file_roles:
            placeholders = ", ".join("?" for _ in file_roles)
            sql += f" WHERE file_role IN ({placeholders})"
            params.extend(file_roles)
        sql += " ORDER BY id"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def delete_invoices_for_import(self, source_file_id: int) -> int:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM invoices WHERE source_file_id = ?", (int(source_file_id),))
            removed = max(cursor.rowcount, 0)
            _refresh_import_saved_counts_with_conn(conn)
            return removed

    def refresh_import_saved_counts(self) -> None:
        with self.connect() as conn:
            _refresh_import_saved_counts_with_conn(conn)

    def delete_bank_transactions_in_date_range(self, start_date: date, end_date: date) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM bank_transactions
                WHERE transaction_date >= ? AND transaction_date <= ?
                """,
                (_date_to_text(start_date), _date_to_text(end_date)),
            )
            removed = max(cursor.rowcount, 0)
            _refresh_import_saved_counts_with_conn(conn)
            return removed

    def find_import_by_hash(self, file_hash: str | None, file_role: str | None = None) -> dict[str, Any] | None:
        if not file_hash:
            return None
        sql = "SELECT * FROM import_files WHERE file_hash = ?"
        params: list[Any] = [file_hash]
        if file_role:
            sql += " AND file_role = ?"
            params.append(file_role)
        sql += " ORDER BY created_at DESC, id DESC LIMIT 1"
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def insert_invoice(
        self,
        direction: str,
        party_id: int | None,
        party_name_raw: str,
        invoice_no: str | None,
        invoice_date: date,
        invoice_amount: Decimal,
        term_months: int | None = None,
        grace_months: int | None = None,
        term_days: int | None = None,
        source_file_id: int | None = None,
        source_row_no: int | None = None,
        status: str = "unmatched",
        invoice_status_raw: str | None = None,
        is_positive: bool | None = None,
        original_invoice_no: str | None = None,
        red_confirmation_no: str | None = None,
        invoice_type: str | None = None,
        amount_without_tax: Decimal | None = None,
        tax_amount: Decimal | None = None,
        remark: str | None = None,
    ) -> int:
        party = self.get_party(party_id)
        effective_term = int(party["default_term_days"]) if party else _coerce_term_days(term_days, term_months)
        terms = calculate_terms(invoice_date, term_days=effective_term)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO invoices
                    (direction, party_id, party_name_raw, invoice_no, invoice_date,
                     invoice_amount, term_months, grace_months, due_date, grace_deadline,
                     status, invoice_status_raw, is_positive, original_invoice_no,
                     red_confirmation_no, invoice_type, amount_without_tax, tax_amount,
                     remark, source_file_id, source_row_no, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    direction,
                    party_id,
                    clean_name(party_name_raw),
                    invoice_no,
                    _date_to_text(invoice_date),
                    _decimal_to_text(invoice_amount),
                    effective_term,
                    0,
                    _date_to_text(terms.due_date),
                    _date_to_text(terms.grace_deadline),
                    status,
                    clean_name(invoice_status_raw) or None,
                    None if is_positive is None else int(bool(is_positive)),
                    clean_name(original_invoice_no) or None,
                    clean_name(red_confirmation_no) or None,
                    clean_name(invoice_type) or None,
                    _decimal_to_text(amount_without_tax),
                    _decimal_to_text(tax_amount),
                    clean_name(remark) or None,
                    source_file_id,
                    source_row_no,
                    _now(),
                ),
            )
            return int(cursor.lastrowid)

    def insert_bank_transaction(
        self,
        transaction_date: date,
        income_amount: Decimal | None,
        expense_amount: Decimal | None,
        counterparty_name_raw: str,
        direction: str,
        party_id: int | None = None,
        counterparty_account: str | None = None,
        counterparty_bank: str | None = None,
        subject: str | None = None,
        summary: str | None = None,
        source_file_id: int | None = None,
        source_row_no: int | None = None,
    ) -> int:
        if party_id is None:
            party_id = self.resolve_party_id(counterparty_name_raw, preferred_party_type=_preferred_party_type(direction))
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO bank_transactions
                    (transaction_date, income_amount, expense_amount, counterparty_account,
                     counterparty_name_raw, counterparty_bank, subject, summary, party_id,
                     direction, source_file_id, source_row_no, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _date_to_text(transaction_date),
                    _decimal_to_text(income_amount),
                    _decimal_to_text(expense_amount),
                    counterparty_account,
                    clean_name(counterparty_name_raw),
                    counterparty_bank,
                    subject,
                    summary,
                    party_id,
                    direction,
                    source_file_id,
                    source_row_no,
                    _now(),
                ),
            )
            return int(cursor.lastrowid)

    def insert_balance_adjustment(
        self,
        direction: str,
        party_id: int,
        adjustment_type: str,
        adjustment_date: date,
        amount: Decimal,
        remark: str | None = None,
        source_type: str = "manual",
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO balance_adjustments
                    (direction, party_id, adjustment_type, adjustment_date, amount,
                     source_type, remark, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_name(direction),
                    int(party_id),
                    clean_name(adjustment_type) or "manual",
                    _date_to_text(adjustment_date),
                    _decimal_to_text(amount),
                    clean_name(source_type) or "manual",
                    clean_name(remark) or None,
                    _now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_balance_adjustments(
        self,
        direction: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        party_id: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT a.*, p.name AS party_name
            FROM balance_adjustments a
            INNER JOIN parties p ON p.id = a.party_id
            WHERE p.is_active = 1
        """
        params: list[Any] = []
        if direction:
            sql += " AND a.direction = ?"
            params.append(clean_name(direction))
        if start_date:
            sql += " AND a.adjustment_date >= ?"
            params.append(_date_to_text(start_date))
        if end_date:
            sql += " AND a.adjustment_date <= ?"
            params.append(_date_to_text(end_date))
        if party_id is not None:
            sql += " AND a.party_id = ?"
            params.append(int(party_id))
        sql += " ORDER BY a.adjustment_date DESC, a.id DESC"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def rebuild_party_links(self) -> dict[str, int]:
        stats = {
            "payable_invoices": 0,
            "receivable_invoices": 0,
            "bank_transactions": 0,
        }
        with self.connect() as conn:
            party_rows = conn.execute(
                """
                SELECT id, name, bank_name
                FROM parties
                WHERE is_active = 1
                """
            ).fetchall()
            for row in party_rows:
                self._add_alias_with_conn(conn, int(row["id"]), row["name"], "name")
                if row["bank_name"]:
                    self._add_alias_with_conn(conn, int(row["id"]), row["bank_name"], "bank")

            invoice_rows = conn.execute(
                """
                SELECT i.id, i.direction, i.party_name_raw, i.term_months, i.grace_months,
                       i.party_id, p.party_type
                FROM invoices i
                LEFT JOIN parties p ON p.id = i.party_id
                """
            ).fetchall()
            for row in invoice_rows:
                name = clean_name(row["party_name_raw"])
                if not name:
                    continue
                expected_type = "supplier" if row["direction"] == "payable" else "customer"
                if row["party_id"] is not None and row["party_type"] == expected_type:
                    continue
                party_id = _resolve_party_id_with_conn(conn, name, preferred_party_type=expected_type)
                if party_id is not None and int(row["party_id"] or 0) != int(party_id):
                    if row["direction"] == "payable":
                        stats["payable_invoices"] += 1
                    elif row["direction"] == "receivable":
                        stats["receivable_invoices"] += 1
                conn.execute("UPDATE invoices SET party_id = ? WHERE id = ?", (party_id, int(row["id"])))

            bank_rows = conn.execute(
                """
                SELECT b.id, b.counterparty_name_raw, b.direction, b.party_id
                FROM bank_transactions b
                """
            ).fetchall()
            for row in bank_rows:
                party_id = _resolve_party_id_with_conn(
                    conn,
                    row["counterparty_name_raw"],
                    preferred_party_type=_preferred_party_type(row["direction"]),
                )
                if party_id is not None and int(row["party_id"] or 0) != int(party_id):
                    stats["bank_transactions"] += 1
                    conn.execute("UPDATE bank_transactions SET party_id = ? WHERE id = ?", (party_id, int(row["id"])))
        return stats

    def promote_bank_counterparty_group(
        self,
        bank_transaction_id: int,
        party_type: str,
        *,
        term_months: int | None = None,
        grace_months: int | None = None,
        term_days: int | None = None,
    ) -> dict[str, Any]:
        if party_type not in {"supplier", "customer"}:
            raise ValueError("往来单位类型只能是 supplier 或 customer")
        with self.connect() as conn:
            bank = conn.execute(
                """
                SELECT id, direction, counterparty_name_raw, counterparty_account, counterparty_bank
                FROM bank_transactions
                WHERE id = ?
                """,
                (int(bank_transaction_id),),
            ).fetchone()
            if bank is None:
                raise ValueError("找不到这条银行流水")
            party_name = clean_name(bank["counterparty_name_raw"])
            if not party_name:
                raise ValueError("这条流水没有对方户名，不能直接转为往来单位")

            existing = _resolve_party_id_with_conn(conn, party_name, preferred_party_type=party_type)
            if existing is not None:
                party_id = int(existing)
            else:
                now = _now()
                effective_term_days = _coerce_term_days(term_days, term_months)
                cursor = conn.execute(
                    """
                    INSERT INTO parties
                        (party_type, name, default_term_months, grace_months,
                         bank_account, bank_name, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        party_type,
                        party_name,
                        effective_term_days,
                        0,
                        clean_name(bank["counterparty_account"]) or None,
                        clean_name(bank["counterparty_bank"]) or None,
                        now,
                    ),
                )
                party_id = int(cursor.lastrowid)

            self._add_alias_with_conn(conn, party_id, party_name, "bank_review")
            if bank["counterparty_bank"]:
                self._add_alias_with_conn(conn, party_id, bank["counterparty_bank"], "bank")
            cursor = conn.execute(
                """
                UPDATE bank_transactions
                SET party_id = ?
                WHERE direction = ?
                  AND counterparty_name_raw = ?
                """,
                (party_id, bank["direction"], party_name),
            )
            return {
                "party_id": party_id,
                "party_name": party_name,
                "linked_bank_transactions": int(cursor.rowcount if cursor.rowcount is not None else 0),
            }

    def dashboard_summary(self, start_date: date, end_date: date) -> dict[str, Decimal | int]:
        invoices = self.list_invoices(start_date=start_date, end_date=end_date)
        banks = self.list_bank_transactions(start_date=start_date, end_date=end_date)
        payable = [row for row in invoices if row["direction"] == "payable"]
        receivable = [row for row in invoices if row["direction"] == "receivable"]
        outflows = [row for row in banks if row["direction"] == "outflow"]
        inflows = [row for row in banks if row["direction"] == "inflow"]
        overdue = [
            row for row in invoices
            if row["due_date"] < _date_to_text(date.today()) and not _is_settled(row)
        ]
        return {
            "payable_amount": _sum_decimal(row["invoice_amount"] for row in payable),
            "receivable_amount": _sum_decimal(row["invoice_amount"] for row in receivable),
            "paid_amount": _sum_decimal(row["expense_amount"] for row in outflows),
            "received_amount": _sum_decimal(row["income_amount"] for row in inflows),
            "payable_count": len(payable),
            "receivable_count": len(receivable),
            "bank_outflow_count": len(outflows),
            "bank_inflow_count": len(inflows),
            "overdue_count": len(overdue),
            "exception_count": len([row for row in invoices if not _is_settled(row)]),
        }

    def monthly_series(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        invoices = self.list_invoices(start_date=start_date, end_date=end_date)
        banks = self.list_bank_transactions(start_date=start_date, end_date=end_date)
        months = sorted({
            period_month(_text_to_date(row["invoice_date"])) for row in invoices
        } | {
            period_month(_text_to_date(row["transaction_date"])) for row in banks
        })
        output = []
        for month in months:
            month_payable = _sum_decimal(row["invoice_amount"] for row in invoices if row["direction"] == "payable" and row["invoice_date"].startswith(month))
            month_receivable = _sum_decimal(row["invoice_amount"] for row in invoices if row["direction"] == "receivable" and row["invoice_date"].startswith(month))
            month_paid = _sum_decimal(row["expense_amount"] for row in banks if row["direction"] == "outflow" and row["transaction_date"].startswith(month))
            month_received = _sum_decimal(row["income_amount"] for row in banks if row["direction"] == "inflow" and row["transaction_date"].startswith(month))
            output.append({
                "月份": month,
                "应付": month_payable,
                "已付": month_paid,
                "应收": month_receivable,
                "已收": month_received,
            })
        return output

    def list_invoices(
        self,
        direction: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT i.*, p.name AS party_name
            FROM invoices i
            LEFT JOIN parties p ON p.id = i.party_id
            WHERE 1 = 1
        """
        params: list[Any] = []
        if direction:
            sql += " AND i.direction = ?"
            params.append(direction)
        if start_date:
            sql += " AND i.invoice_date >= ?"
            params.append(_date_to_text(start_date))
        if end_date:
            sql += " AND i.invoice_date <= ?"
            params.append(_date_to_text(end_date))
        sql += " ORDER BY i.invoice_date DESC, i.id DESC"
        with self.connect() as conn:
            return [_invoice_dict(row) for row in conn.execute(sql, params).fetchall()]

    def list_bank_transactions(
        self,
        direction: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT b.*, p.name AS party_name, p.party_type AS party_type
            FROM bank_transactions b
            LEFT JOIN parties p ON p.id = b.party_id
            WHERE 1 = 1
        """
        params: list[Any] = []
        if direction:
            sql += " AND b.direction = ?"
            params.append(direction)
        if start_date:
            sql += " AND b.transaction_date >= ?"
            params.append(_date_to_text(start_date))
        if end_date:
            sql += " AND b.transaction_date <= ?"
            params.append(_date_to_text(end_date))
        sql += " ORDER BY b.transaction_date DESC, b.id DESC"
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        for row in rows:
            category = classify_bank_transaction(row)
            row["business_category"] = category
            row["business_category_label"] = BANK_CATEGORY_LABELS.get(category, category)
        return rows

    def recent_imports(self, limit: int = 8) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM import_files
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]

    def party_totals(self, direction: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        if direction in {"payable", "receivable"}:
            invoices = self.list_invoices(direction, start_date, end_date)
            grouped: dict[str, Decimal] = {}
            for row in invoices:
                if row.get("party_id") is None:
                    continue
                key = row["party_name"] or row["party_name_raw"]
                grouped[key] = grouped.get(key, ZERO) + _decimal(row["invoice_amount"])
            return [{"往来单位": key, "金额": amount} for key, amount in sorted(grouped.items(), key=lambda item: item[1], reverse=True)]

        bank_direction = "outflow" if direction == "outflow" else "inflow"
        amount_key = "expense_amount" if bank_direction == "outflow" else "income_amount"
        expected_category = "supplier_payment" if bank_direction == "outflow" else "customer_receipt"
        banks = self.list_bank_transactions(bank_direction, start_date, end_date)
        grouped: dict[str, Decimal] = {}
        for row in banks:
            if row.get("business_category") != expected_category:
                continue
            key = row["party_name"] or row["counterparty_name_raw"] or "未识别"
            grouped[key] = grouped.get(key, ZERO) + _decimal(row[amount_key])
        return [{"往来单位": key, "金额": amount} for key, amount in sorted(grouped.items(), key=lambda item: item[1], reverse=True)]

    def settlement_summary(
        self,
        start_date: date,
        end_date: date,
        *,
        as_of: date | None = None,
    ) -> dict[str, Decimal | int]:
        as_of = as_of or date.today()
        payable_due = self._due_invoices("payable", start_date, end_date)
        receivable_due = self._due_invoices("receivable", start_date, end_date)
        payable_overdue = self._overdue_invoices("payable", as_of)
        receivable_overdue = self._overdue_invoices("receivable", as_of)
        bank_totals = self.bank_category_totals(start_date, end_date)
        return {
            "payable_due_amount": _sum_decimal(row["invoice_amount"] for row in payable_due),
            "payable_settled_amount": _sum_decimal(row["invoice_amount"] for row in payable_due if _is_settled(row)),
            "payable_pending_amount": _sum_decimal(row["invoice_amount"] for row in payable_due if not _is_settled(row)),
            "payable_overdue_amount": _sum_decimal(row["invoice_amount"] for row in payable_overdue),
            "payable_due_count": len(payable_due),
            "payable_pending_count": len([row for row in payable_due if not _is_settled(row)]),
            "payable_overdue_count": len(payable_overdue),
            "receivable_due_amount": _sum_decimal(row["invoice_amount"] for row in receivable_due),
            "receivable_settled_amount": _sum_decimal(row["invoice_amount"] for row in receivable_due if _is_settled(row)),
            "receivable_pending_amount": _sum_decimal(row["invoice_amount"] for row in receivable_due if not _is_settled(row)),
            "receivable_overdue_amount": _sum_decimal(row["invoice_amount"] for row in receivable_overdue),
            "receivable_due_count": len(receivable_due),
            "receivable_pending_count": len([row for row in receivable_due if not _is_settled(row)]),
            "receivable_overdue_count": len(receivable_overdue),
            "supplier_payment_amount": bank_totals.get("supplier_payment", ZERO),
            "customer_receipt_amount": bank_totals.get("customer_receipt", ZERO),
        }

    def bank_category_totals(self, start_date: date, end_date: date) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for row in self.list_bank_transactions(start_date=start_date, end_date=end_date):
            category = row["business_category"]
            amount_key = "expense_amount" if row["direction"] == "outflow" else "income_amount"
            totals[category] = totals.get(category, ZERO) + _decimal(row[amount_key])
        return totals

    def bank_category_rows(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        totals = self.bank_category_totals(start_date, end_date)
        return [
            {"分类": BANK_CATEGORY_LABELS.get(category, category), "金额": amount}
            for category, amount in sorted(totals.items(), key=lambda item: item[1], reverse=True)
        ]

    def party_due_totals(
        self,
        direction: str,
        start_date: date,
        end_date: date,
        *,
        overdue_only: bool = False,
        as_of: date | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._overdue_invoices(direction, as_of or date.today()) if overdue_only else self._due_invoices(direction, start_date, end_date)
        grouped: dict[str, Decimal] = {}
        for row in rows:
            if _is_settled(row):
                continue
            key = row["party_name"] or row["party_name_raw"] or "未识别"
            grouped[key] = grouped.get(key, ZERO) + _decimal(row["invoice_amount"])
        return [{"往来单位": key, "金额": amount} for key, amount in sorted(grouped.items(), key=lambda item: item[1], reverse=True)]

    def _due_invoices(self, direction: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        return [
            row for row in self.list_invoices(direction)
            if row.get("party_id") is not None and start_date <= _text_to_date(row["due_date"]) <= end_date
        ]

    def _overdue_invoices(self, direction: str, as_of: date) -> list[dict[str, Any]]:
        return [
            row for row in self.list_invoices(direction)
            if row.get("party_id") is not None and _text_to_date(row["due_date"]) < as_of and not _is_settled(row)
        ]

    def save_match_results(self, direction: str, matches: list[Any], run_no: str | None = None) -> str:
        run_no = run_no or _new_run_no(direction)
        now = _now()
        with self.connect() as conn:
            _save_match_results_with_conn(conn, direction, matches, run_no, now)
        return run_no

    def save_manual_settlement(
        self,
        direction: str,
        invoice_id: int,
        settlement_method: str,
        settlement_date: date,
        amount: Decimal,
        remark: str | None = None,
    ) -> str:
        invoice = self.get_invoice(invoice_id)
        if invoice is None:
            raise ValueError(f"未找到发票: {invoice_id}")
        if invoice["direction"] != direction:
            raise ValueError("发票方向和处理方向不一致")
        invoice_amount = _decimal(invoice["invoice_amount"])
        paid_amount = _decimal(amount)
        difference = max(invoice_amount - paid_amount, ZERO)
        status = "matched" if difference <= ZERO else "probable"
        method = clean_name(settlement_method) or "other"
        remark_text = clean_name(remark) or _manual_settlement_remark(direction, method)
        run_no = _new_manual_run_no(direction)
        match = {
            "match_type": f"manual_{method}",
            "status": status,
            "party_id": invoice.get("party_id"),
            "party_name": invoice.get("party_name") or invoice.get("party_name_raw"),
            "invoice_ids": [int(invoice_id)],
            "bank_transaction_ids": [],
            "invoice_total": invoice_amount,
            "bank_total": paid_amount,
            "difference_amount": difference,
            "confidence": Decimal("1.00"),
            "remark": remark_text,
            "settlement_method": method,
            "settlement_date": _date_to_text(settlement_date),
        }
        now = _now()
        with self.connect() as conn:
            _save_match_results_with_conn(conn, direction, [match], run_no, now)
            party_id = invoice.get("party_id")
            if party_id is not None and paid_amount != ZERO:
                conn.execute(
                    """
                    INSERT INTO balance_adjustments
                        (direction, party_id, adjustment_type, adjustment_date, amount,
                         source_type, remark, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        direction,
                        int(party_id),
                        method,
                        _date_to_text(settlement_date),
                        _decimal_to_text(-paid_amount),
                        "manual_settlement",
                        f"{remark_text}；发票ID {int(invoice_id)}；匹配批次 {run_no}",
                        now,
                    ),
                )
        return run_no

    def list_match_results(self, direction: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM match_results WHERE 1 = 1"
        params: list[Any] = []
        if direction:
            sql += " AND direction = ?"
            params.append(direction)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _save_match_results_with_conn(conn: Any, direction: str, matches: list[Any], run_no: str, now: str) -> None:
    for match in matches:
        invoice_ids = _match_attr(match, "invoice_ids", [])
        bank_ids = _match_attr(match, "bank_transaction_ids", [])
        status = str(_match_attr(match, "status", "unmatched"))
        conn.execute(
            """
            INSERT INTO match_results
                (run_no, direction, party_id, party_name, match_type, status,
                 invoice_ids, bank_transaction_ids, invoice_total, bank_total,
                 difference_amount, confidence, remark, settlement_method,
                 settlement_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_no,
                direction,
                _match_attr(match, "party_id", None),
                _match_attr(match, "party_name", None),
                str(_match_attr(match, "match_type", "")),
                status,
                json.dumps(invoice_ids, ensure_ascii=False),
                json.dumps(bank_ids, ensure_ascii=False),
                _decimal_to_text(_match_attr(match, "invoice_total", Decimal("0.00"))),
                _decimal_to_text(_match_attr(match, "bank_total", Decimal("0.00"))),
                _decimal_to_text(_match_attr(match, "difference_amount", Decimal("0.00"))),
                _decimal_to_text(_match_attr(match, "confidence", Decimal("0.00"))),
                _match_attr(match, "remark", None),
                clean_name(_match_attr(match, "settlement_method", None)) or None,
                clean_name(_match_attr(match, "settlement_date", None)) or None,
                now,
            ),
        )
        _update_statuses_for_match(conn, invoice_ids, bank_ids, status)


def insert_parsed_parties(store: LocalStore, rows: list[Any]) -> int:
    count = 0
    for row in rows:
        store.upsert_party(
            row.party_type,
            row.name,
            short_name=row.short_name,
            default_term_days=getattr(row, "default_term_days", row.default_term_months),
            bank_account=getattr(row, "bank_account", None),
            bank_name=getattr(row, "bank_name", None),
        )
        count += 1
    return count


def insert_parsed_invoices(store: LocalStore, rows: list[Any], source_file_id: int | None = None) -> int:
    now = _now()
    count = 0
    with store.connect() as conn:
        for row in rows:
            party_type = "supplier" if row.direction == "payable" else "customer"
            party_id = _resolve_party_id_with_conn(conn, row.party_name_raw, preferred_party_type=party_type)
            party = conn.execute("SELECT default_term_months FROM parties WHERE id = ?", (party_id,)).fetchone() if party_id else None
            effective_term = int(party["default_term_months"]) if party else _coerce_term_days(getattr(row, "term_days", None), row.term_months)
            terms = calculate_terms(row.invoice_date, term_days=effective_term)
            conn.execute(
                """
                INSERT INTO invoices
                    (direction, party_id, party_name_raw, invoice_no, invoice_date,
                     invoice_amount, term_months, grace_months, due_date, grace_deadline,
                     status, invoice_status_raw, is_positive, original_invoice_no,
                     red_confirmation_no, invoice_type, amount_without_tax, tax_amount,
                     remark, source_file_id, source_row_no, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.direction,
                    party_id,
                    clean_name(row.party_name_raw),
                    row.invoice_no,
                    _date_to_text(row.invoice_date),
                    _decimal_to_text(row.invoice_amount),
                    effective_term,
                    0,
                    _date_to_text(terms.due_date),
                    _date_to_text(terms.grace_deadline),
                    "unmatched",
                    clean_name(getattr(row, "invoice_status_raw", None)) or None,
                    None if getattr(row, "is_positive", None) is None else int(bool(getattr(row, "is_positive", None))),
                    clean_name(getattr(row, "original_invoice_no", None)) or None,
                    clean_name(getattr(row, "red_confirmation_no", None)) or None,
                    clean_name(getattr(row, "invoice_type", None)) or None,
                    _decimal_to_text(getattr(row, "amount_without_tax", None)),
                    _decimal_to_text(getattr(row, "tax_amount", None)),
                    clean_name(getattr(row, "remark", None)) or None,
                    source_file_id,
                    row.source_row_no,
                    now,
                ),
            )
            count += 1
    return count


def insert_parsed_bank_transactions(store: LocalStore, rows: list[Any], source_file_id: int | None = None) -> int:
    now = _now()
    count = 0
    with store.connect() as conn:
        for row in rows:
            if source_file_id is not None and _bank_transaction_duplicate_exists_with_conn(
                conn,
                transaction_date=row.transaction_date,
                income_amount=row.income_amount,
                expense_amount=row.expense_amount,
                counterparty_account=row.counterparty_account,
                counterparty_name_raw=row.counterparty_name_raw,
                counterparty_bank=row.counterparty_bank,
                subject=row.subject,
                summary=row.summary,
                direction=row.direction,
                source_file_id=source_file_id,
            ):
                continue
            party_id = _resolve_party_id_with_conn(
                conn,
                row.counterparty_name_raw,
                preferred_party_type=_preferred_party_type(row.direction),
            )
            conn.execute(
                """
                INSERT INTO bank_transactions
                    (transaction_date, income_amount, expense_amount, counterparty_account,
                     counterparty_name_raw, counterparty_bank, subject, summary, party_id,
                     direction, source_file_id, source_row_no, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _date_to_text(row.transaction_date),
                    _decimal_to_text(row.income_amount),
                    _decimal_to_text(row.expense_amount),
                    row.counterparty_account,
                    clean_name(row.counterparty_name_raw),
                    row.counterparty_bank,
                    row.subject,
                    row.summary,
                    party_id,
                    row.direction,
                    source_file_id,
                    row.source_row_no,
                    now,
                ),
            )
            count += 1
    return count


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_run_no(direction: str) -> str:
    return f"{direction}-{datetime.now():%Y%m%d%H%M%S}-{uuid4().hex[:6]}"


def _new_manual_run_no(direction: str) -> str:
    return f"{direction}-manual-{datetime.now():%Y%m%d%H%M%S}-{uuid4().hex[:6]}"


def _manual_settlement_remark(direction: str, method: str) -> str:
    action = "付款" if direction == "payable" else "收款"
    labels = {
        "abc_bank": "农业银行",
        "acceptance_bill": "承兑汇票",
        "cash": "现金",
        "other": "其他",
    }
    return f"人工确认{labels.get(method, '其他')}{action}"


def _ensure_default_payment_template(conn: sqlite3.Connection) -> None:
    active = conn.execute(
        """
        SELECT id FROM payment_templates
        WHERE bank_code = ? AND is_active = 1
        LIMIT 1
        """,
        ("ABC",),
    ).fetchone()
    if active:
        return
    existing_default = conn.execute(
        """
        SELECT id FROM payment_templates
        WHERE bank_code = ? AND template_name = ?
        LIMIT 1
        """,
        ("ABC", "农业银行批量付款模板"),
    ).fetchone()
    if existing_default:
        return
    conn.execute(
        """
        INSERT INTO payment_templates
            (bank_code, bank_name, template_name, sheet_name, default_purpose,
             column_config, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ABC",
            "中国农业银行",
            "农业银行批量付款模板",
            "页一【批量转账文件】",
            DEFAULT_ABC_PAYMENT_PURPOSE,
            json.dumps(DEFAULT_ABC_PAYMENT_COLUMNS, ensure_ascii=False),
            _now(),
        ),
    )


def _normalize_default_payment_template(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE payment_templates
        SET default_purpose = ?
        WHERE bank_code = ? AND default_purpose = ?
        """,
        (DEFAULT_ABC_PAYMENT_PURPOSE, "ABC", "丰登路后道厂-货款"),
    )


def _payment_template_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        columns = json.loads(data.get("column_config") or "[]")
    except json.JSONDecodeError:
        columns = []
    data["columns"] = [_template_text(column) for column in columns if _template_text(column)]
    return data


def _template_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\u3000", " ").strip().split())


def _bank_transaction_duplicate_exists_with_conn(
    conn: sqlite3.Connection,
    *,
    transaction_date: date,
    income_amount: Decimal | None,
    expense_amount: Decimal | None,
    counterparty_account: str | None,
    counterparty_name_raw: str,
    counterparty_bank: str | None,
    subject: str | None,
    summary: str | None,
    direction: str,
    source_file_id: int,
) -> bool:
    row = conn.execute(
        """
        SELECT id
        FROM bank_transactions
        WHERE transaction_date = ?
          AND COALESCE(income_amount, '') = ?
          AND COALESCE(expense_amount, '') = ?
          AND COALESCE(counterparty_account, '') = ?
          AND COALESCE(counterparty_name_raw, '') = ?
          AND COALESCE(counterparty_bank, '') = ?
          AND COALESCE(subject, '') = ?
          AND COALESCE(summary, '') = ?
          AND COALESCE(direction, '') = ?
          AND source_file_id IS NOT NULL
          AND source_file_id <> ?
        LIMIT 1
        """,
        (
            _date_to_text(transaction_date),
            _decimal_to_text(income_amount) or "",
            _decimal_to_text(expense_amount) or "",
            clean_name(counterparty_account),
            clean_name(counterparty_name_raw),
            clean_name(counterparty_bank),
            clean_name(subject),
            clean_name(summary),
            clean_name(direction),
            int(source_file_id),
        ),
    ).fetchone()
    return row is not None


def _resolve_party_id_with_conn(
    conn: sqlite3.Connection,
    name: str,
    *,
    preferred_party_type: str | None = None,
) -> int | None:
    name = clean_name(name)
    if not name:
        return None
    if preferred_party_type:
        row = conn.execute(
            """
            SELECT id FROM parties
            WHERE name = ? AND party_type = ? AND is_active = 1
            """,
            (name, preferred_party_type),
        ).fetchone()
        if row:
            return int(row["id"])
        alias = conn.execute(
            """
            SELECT a.party_id
            FROM party_aliases a
            INNER JOIN parties p ON p.id = a.party_id
            WHERE a.alias_name = ? AND a.is_active = 1
              AND p.party_type = ? AND p.is_active = 1
            """,
            (name, preferred_party_type),
        ).fetchone()
        if alias:
            return int(alias["party_id"])
        return None

    row = conn.execute("SELECT id FROM parties WHERE name = ? AND is_active = 1", (name,)).fetchone()
    if row:
        return int(row["id"])
    alias = conn.execute(
        """
        SELECT party_id FROM party_aliases
        WHERE alias_name = ? AND is_active = 1
        """,
        (name,),
    ).fetchone()
    return int(alias["party_id"]) if alias else None


def _fuzzy_party_candidates_with_conn(
    conn: sqlite3.Connection,
    name: str,
    *,
    preferred_party_type: str | None,
    min_score: float,
    limit: int,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    party_filter = ""
    if preferred_party_type:
        party_filter = " AND p.party_type = ?"
        params.append(preferred_party_type)

    party_rows = conn.execute(
        f"""
        SELECT p.id AS party_id, p.party_type, p.name AS party_name,
               p.name AS matched_name
        FROM parties p
        WHERE p.is_active = 1{party_filter}
        """,
        params,
    ).fetchall()
    alias_rows = conn.execute(
        f"""
        SELECT p.id AS party_id, p.party_type, p.name AS party_name,
               a.alias_name AS matched_name
        FROM party_aliases a
        INNER JOIN parties p ON p.id = a.party_id
        WHERE a.is_active = 1 AND p.is_active = 1{party_filter}
        """,
        params,
    ).fetchall()

    best_by_party: dict[int, dict[str, Any]] = {}
    for row in [*party_rows, *alias_rows]:
        score = company_similarity(name, row["matched_name"])
        if score < min_score:
            continue
        party_id = int(row["party_id"])
        existing = best_by_party.get(party_id)
        if existing is None or score > float(existing["score"]):
            best_by_party[party_id] = {
                "party_id": party_id,
                "party_type": row["party_type"],
                "party_name": row["party_name"],
                "matched_name": row["matched_name"],
                "score": score,
            }

    return sorted(
        best_by_party.values(),
        key=lambda item: (-float(item["score"]), item["party_type"], item["party_name"]),
    )[: max(1, int(limit))]


def _upsert_party_with_conn(
    conn: sqlite3.Connection,
    party_type: str,
    name: str,
    *,
    default_term_months: int = 30,
    grace_months: int = 0,
) -> int:
    name = clean_name(name)
    existing = conn.execute(
        "SELECT id FROM parties WHERE party_type = ? AND name = ?",
        (party_type, name),
    ).fetchone()
    if existing:
        party_id = int(existing["id"])
        conn.execute(
            """
            UPDATE parties
            SET default_term_months = ?, grace_months = ?, updated_at = ?
            WHERE id = ?
            """,
            (int(default_term_months), 0, _now(), party_id),
        )
    else:
        cursor = conn.execute(
            """
            INSERT INTO parties
                (party_type, name, default_term_months, grace_months, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (party_type, name, int(default_term_months), 0, _now()),
        )
        party_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT OR IGNORE INTO party_aliases (party_id, alias_name, source_type, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (party_id, name, "name", _now()),
    )
    return party_id


def _match_attr(match: Any, name: str, default: Any = None) -> Any:
    if hasattr(match, name):
        return getattr(match, name)
    if isinstance(match, dict):
        return match.get(name, default)
    return default


def _preferred_party_type(direction: str | None) -> str | None:
    direction = clean_name(direction)
    if direction == "inflow":
        return "customer"
    if direction == "outflow":
        return "supplier"
    return None


def _party_type_label(party_type: str | None) -> str:
    if party_type == "supplier":
        return "供应商"
    if party_type == "customer":
        return "客户"
    return "往来单位"


def _bank_direction_label(direction: str | None) -> str:
    if direction == "inflow":
        return "收入"
    if direction == "outflow":
        return "支出"
    return "银行"


def _update_statuses_for_match(
    conn: sqlite3.Connection,
    invoice_ids: list[int],
    bank_ids: list[int],
    match_status: str,
) -> None:
    invoice_status, bank_status = _storage_statuses(match_status)
    if invoice_status is not None and invoice_ids:
        _update_many(conn, "invoices", "status", invoice_status, invoice_ids)
    if bank_status is not None and bank_ids:
        _update_many(conn, "bank_transactions", "match_status", bank_status, bank_ids)


def _storage_statuses(match_status: str) -> tuple[str | None, str | None]:
    if match_status == "matched":
        return "matched", "matched"
    if match_status == "probable":
        return "partial", "partial"
    # Unmatched rows are useful history, but they must not reopen rows that were
    # already confirmed by an earlier run with a different bank date window.
    return None, None


def _update_many(conn: sqlite3.Connection, table: str, column: str, value: str, ids: list[int]) -> None:
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE {table} SET {column} = ? WHERE id IN ({placeholders})",
        [value, *[int(id_) for id_ in ids]],
    )


def classify_bank_transaction(row: dict[str, Any]) -> str:
    direction = clean_name(row.get("direction"))
    party_type = clean_name(row.get("party_type"))
    purpose_text = clean_name(" ".join(str(row.get(key) or "") for key in ["subject", "summary"]))
    text = clean_name(" ".join(str(row.get(key) or "") for key in ["counterparty_name_raw", "counterparty_bank", "subject", "summary"]))
    if _contains_any(text, ["待报解预算收入", "国库", "扣税", "税务", "税款", "增值税", "所得税"]):
        return "tax_payment"
    if _contains_any(text, ["工资", "社保", "公积金", "薪资"]):
        return "payroll"
    if _contains_any(text, ["手续费", "利息", "账户管理费", "服务费", "费用外收"]):
        return "bank_fee"
    if _contains_any(purpose_text, ["股权投资", "投资款", "借款", "押金", "保证金", "投标保证金"]):
        return "non_reconciliation"
    if _contains_any(text, ["内部转账", "备用金", "转户", "往来款转入"]):
        return "internal_transfer"
    if _contains_any(text, ["退款", "退回", "冲正", "退票"]):
        return "refund"
    if direction == "outflow" and party_type == "supplier":
        return "supplier_payment"
    if direction == "inflow" and party_type == "customer":
        return "customer_receipt"
    nature = classify_counterparty_nature(
        row.get("counterparty_name_raw"),
        summary=row.get("summary"),
        bank=row.get("counterparty_bank"),
    )
    if nature.kind == "non_reconciliation":
        return "non_reconciliation"
    if nature.kind == "personal":
        return "personal_flow"
    if nature.kind == "company":
        return "company_candidate"
    if direction == "outflow":
        return "unidentified_outflow"
    if direction == "inflow":
        return "unidentified_inflow"
    return "unknown"


def _contains_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _party_dict(row: sqlite3.Row) -> dict[str, Any]:
    output = dict(row)
    output["default_term_days"] = int(output.get("default_term_months") or 0)
    output["grace_months"] = 0
    return output


def _invoice_dict(row: sqlite3.Row) -> dict[str, Any]:
    output = dict(row)
    output["term_days"] = int(output.get("term_months") or 0)
    output["grace_months"] = 0
    return output


def _coerce_term_days(term_days: int | None = None, term_months: int | None = None, default: int = 30) -> int:
    if term_days is not None:
        return max(0, int(term_days))
    if term_months is not None:
        return max(0, int(term_months) * 30)
    return max(0, int(default))


def _is_settled(row: dict[str, Any]) -> bool:
    return clean_name(row.get("status")) in SETTLED_STATUSES


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def _refresh_import_saved_counts_with_conn(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, file_role, parsed_count FROM import_files").fetchall()
    for row in rows:
        file_role = clean_name(row["file_role"])
        if file_role == "party_directory":
            saved_count = int(row["parsed_count"] or 0)
        else:
            saved_count = _count_import_details_with_conn(conn, file_role, int(row["id"]))
        conn.execute(
            "UPDATE import_files SET saved_count = ? WHERE id = ?",
            (int(saved_count), int(row["id"])),
        )


def _count_import_details_with_conn(conn: sqlite3.Connection, file_role: str, source_file_id: int) -> int:
    file_role = clean_name(file_role)
    if file_role == "bank_statement":
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM bank_transactions WHERE source_file_id = ?",
            (int(source_file_id),),
        ).fetchone()
        return int(row["count"] or 0)
    if file_role in {"payable_invoice", "receivable_invoice"}:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM invoices WHERE source_file_id = ?",
            (int(source_file_id),),
        ).fetchone()
        return int(row["count"] or 0)
    row = conn.execute("SELECT saved_count FROM import_files WHERE id = ?", (int(source_file_id),)).fetchone()
    return int(row["saved_count"] or 0) if row else 0


def _date_to_text(value: date) -> str:
    return value.isoformat()


def _text_to_date(value: str) -> date:
    parsed = normalize_date(value)
    if parsed is None:
        raise ValueError(f"无效日期: {value}")
    return parsed


def _decimal_to_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    amount = normalize_amount(value)
    return f"{amount:.2f}" if amount is not None else None


def _decimal(value: Any) -> Decimal:
    amount = normalize_amount(value)
    return amount if amount is not None else ZERO


def _bank_row_amount(row: dict[str, Any]) -> Decimal:
    if row.get("direction") == "outflow":
        return _decimal(row.get("expense_amount"))
    return _decimal(row.get("income_amount"))


def _sum_decimal(values) -> Decimal:
    total = ZERO
    for value in values:
        total += _decimal(value)
    return total
