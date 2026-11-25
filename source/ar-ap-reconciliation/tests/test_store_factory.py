from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from finance_platform.config import Settings


def _settings(storage_mode: str) -> Settings:
    return Settings(
        storage_mode=storage_mode,
        db_driver="SQL Server",
        db_server="localhost",
        db_port="1433",
        db_name="demo_finance",
        db_user="test_user",
        db_password="test_only",
        db_timeout=5,
        upload_dir=Path("data/uploads"),
        export_dir=Path("data/exports"),
        local_db_path=Path("data/local_finance.db"),
    )


class StoreFactoryTest(unittest.TestCase):
    def test_sqlserver_mode_uses_direct_sqlserver_store(self) -> None:
        from finance_platform.sqlserver_store import SqlServerStore
        from finance_platform.store import create_store

        with (
            patch.object(SqlServerStore, "init", autospec=True) as init,
            patch.object(SqlServerStore, "rebuild_party_links", autospec=True, return_value={}) as rebuild,
        ):
            store = create_store(_settings("sqlserver"))

        self.assertIsInstance(store, SqlServerStore)
        self.assertFalse(hasattr(store, "refresh_from_sqlserver"))
        self.assertFalse(hasattr(store, "sync_to_sqlserver"))
        init.assert_called_once_with(store)
        rebuild.assert_called_once_with(store)

    def test_sqlserver_translation_targets_finance_tables_directly(self) -> None:
        from finance_platform.sqlserver_store import translate_sql_for_sqlserver

        sql, params = translate_sql_for_sqlserver(
            "SELECT * FROM parties WHERE is_active = ? ORDER BY id DESC LIMIT ?",
            [1, 5],
        )

        self.assertIn("FROM dbo.lyc_fin_parties", sql)
        self.assertIn("SELECT TOP 5", sql)
        self.assertNotIn("LIMIT", sql.upper())
        self.assertEqual(params, [1])

    def test_sqlserver_translation_converts_amount_coalesce_to_text(self) -> None:
        from finance_platform.sqlserver_store import translate_sql_for_sqlserver

        sql, params = translate_sql_for_sqlserver(
            """
            SELECT id
            FROM bank_transactions
            WHERE COALESCE(income_amount, '') = ?
              AND COALESCE(expense_amount, '') = ?
            LIMIT 1
            """,
            ["", "42000.00"],
        )

        self.assertIn("CONVERT(NVARCHAR(50), income_amount)", sql)
        self.assertIn("CONVERT(NVARCHAR(50), expense_amount)", sql)
        self.assertNotIn("COALESCE(income_amount, '')", sql)
        self.assertNotIn("COALESCE(expense_amount, '')", sql)
        self.assertEqual(params, ["", "42000.00"])


if __name__ == "__main__":
    unittest.main()
