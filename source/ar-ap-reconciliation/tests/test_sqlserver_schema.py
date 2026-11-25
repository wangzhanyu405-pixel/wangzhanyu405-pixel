from __future__ import annotations

import unittest

from pathlib import Path

from finance_platform.schema import SCHEMA_STATEMENTS


class SqlServerSchemaTest(unittest.TestCase):
    def test_schema_contains_current_local_store_tables(self) -> None:
        schema_sql = "\n".join(SCHEMA_STATEMENTS)

        for table_name in [
            "dbo.lyc_fin_parties",
            "dbo.lyc_fin_party_aliases",
            "dbo.lyc_fin_import_files",
            "dbo.lyc_fin_invoices",
            "dbo.lyc_fin_bank_transactions",
            "dbo.lyc_fin_match_results",
            "dbo.lyc_fin_payment_templates",
            "dbo.lyc_fin_balance_adjustments",
        ]:
            self.assertIn(table_name, schema_sql)

    def test_schema_contains_current_business_columns_and_idempotent_migrations(self) -> None:
        schema_sql = "\n".join(SCHEMA_STATEMENTS)

        for column_name in [
            "bank_account",
            "bank_name",
            "saved_count",
            "invoice_status_raw",
            "is_positive",
            "original_invoice_no",
            "red_confirmation_no",
            "invoice_type",
            "amount_without_tax",
            "tax_amount",
            "settlement_method",
            "settlement_date",
            "column_config",
            "adjustment_type",
            "adjustment_date",
            "source_type",
        ]:
            self.assertIn(column_name, schema_sql)

        self.assertIn("COL_LENGTH(N'dbo.lyc_fin_parties', N'bank_account')", schema_sql)
        self.assertIn("COL_LENGTH(N'dbo.lyc_fin_invoices', N'invoice_status_raw')", schema_sql)
        self.assertIn("COL_LENGTH(N'dbo.lyc_fin_match_results', N'settlement_method')", schema_sql)

    def test_schema_and_sql_helpers_are_sql_server_2008_safe(self) -> None:
        schema_sql = "\n".join(SCHEMA_STATEMENTS).upper()
        repository_sql = Path("finance_platform/repositories.py").read_text(encoding="utf-8").upper()

        self.assertNotIn("DATETIME2", schema_sql)
        self.assertNotIn("SYSDATETIME", schema_sql)
        self.assertNotIn("SYSDATETIME", repository_sql)
        self.assertIn("DATETIME NOT NULL DEFAULT GETDATE()", schema_sql)


if __name__ == "__main__":
    unittest.main()
