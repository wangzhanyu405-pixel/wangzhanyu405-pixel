from __future__ import annotations

import unittest
from decimal import Decimal

from finance_platform.sqlserver_migration import (
    MIGRATION_TABLES,
    TableMigration,
    build_insert_sql,
    insert_rows_to_target,
    normalize_row_values,
)


class SqlServerMigrationTest(unittest.TestCase):
    def test_migration_table_plan_covers_current_local_store_tables(self) -> None:
        source_tables = [table.source_table for table in MIGRATION_TABLES]
        target_tables = [table.target_table for table in MIGRATION_TABLES]

        self.assertEqual(
            source_tables,
            [
                "parties",
                "party_aliases",
                "import_files",
                "invoices",
                "bank_transactions",
                "match_results",
                "balance_adjustments",
                "payment_templates",
            ],
        )
        self.assertIn("dbo.lyc_fin_match_results", target_tables)
        self.assertIn("dbo.lyc_fin_balance_adjustments", target_tables)
        self.assertIn("dbo.lyc_fin_payment_templates", target_tables)

    def test_build_insert_sql_preserves_identity_ids(self) -> None:
        sql = build_insert_sql("dbo.lyc_fin_parties", ["id", "party_type", "name"])

        self.assertEqual(
            sql,
            "INSERT INTO dbo.lyc_fin_parties (id, party_type, name) VALUES (?, ?, ?)",
        )

    def test_normalize_row_values_converts_money_and_boolean_fields(self) -> None:
        row = {
            "invoice_amount": "1200.50",
            "amount": "88.80",
            "amount_without_tax": "",
            "tax_amount": "13.20",
            "is_positive": 1,
            "name": "上海测试供应商",
        }

        values = normalize_row_values(row, ["invoice_amount", "amount", "amount_without_tax", "tax_amount", "is_positive", "name"])

        self.assertEqual(values[0], Decimal("1200.50"))
        self.assertEqual(values[1], Decimal("88.80"))
        self.assertIsNone(values[2])
        self.assertEqual(values[3], Decimal("13.20"))
        self.assertTrue(values[4])
        self.assertEqual(values[5], "上海测试供应商")

    def test_insert_rows_to_target_uses_batch_executemany(self) -> None:
        class FakeCursor:
            def __init__(self) -> None:
                self.fast_executemany = False
                self.executed: list[str] = []
                self.batch_calls: list[tuple[str, list[list[object]]]] = []

            def execute(self, sql: str) -> None:
                self.executed.append(sql)

            def executemany(self, sql: str, values: list[list[object]]) -> None:
                self.batch_calls.append((sql, values))

        cursor = FakeCursor()
        table = TableMigration(
            source_table="parties",
            target_table="dbo.lyc_fin_parties",
            columns=("id", "name", "is_active"),
        )
        rows = [
            {"id": 1, "name": "上海测试供应商", "is_active": 1},
            {"id": 2, "name": "上海测试客户", "is_active": 0},
        ]

        count = insert_rows_to_target(cursor, table, rows)

        self.assertEqual(count, 2)
        self.assertTrue(cursor.fast_executemany)
        self.assertEqual(cursor.executed[0], "SET IDENTITY_INSERT dbo.lyc_fin_parties ON")
        self.assertEqual(cursor.executed[-1], "SET IDENTITY_INSERT dbo.lyc_fin_parties OFF")
        self.assertEqual(len(cursor.batch_calls), 1)
        self.assertEqual(cursor.batch_calls[0][1][0], [1, "上海测试供应商", True])


if __name__ == "__main__":
    unittest.main()
