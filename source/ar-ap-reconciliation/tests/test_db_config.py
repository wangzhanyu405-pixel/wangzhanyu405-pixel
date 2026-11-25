from __future__ import annotations

import unittest

from finance_platform.config import Settings
from finance_platform.db import build_connection_string


class DbConfigTest(unittest.TestCase):
    def test_connection_string_uses_configured_sql_server_2008_driver(self) -> None:
        settings = Settings(
            storage_mode="sqlite",
            db_driver="SQL Server Native Client 10.0",
            db_server="localhost",
            db_port="1433",
            db_name="demo_finance",
            db_user="test_user",
            db_password="test_only",
            db_timeout=5,
            upload_dir="data/uploads",
            export_dir="data/exports",
            local_db_path="data/local_finance.db",
        )

        connection_string = build_connection_string(settings)

        self.assertIn("DRIVER={SQL Server Native Client 10.0};", connection_string)
        self.assertIn("SERVER=localhost,1433;", connection_string)
        self.assertIn("DATABASE=demo_finance;", connection_string)

    def test_storage_mode_accepts_sqlserver_aliases(self) -> None:
        base_settings = {
            "db_driver": "SQL Server",
            "db_server": "localhost",
            "db_port": "1433",
            "db_name": "demo_finance",
            "db_user": "test_user",
            "db_password": "test_only",
            "db_timeout": 5,
            "upload_dir": "data/uploads",
            "export_dir": "data/exports",
            "local_db_path": "data/local_finance.db",
        }

        self.assertTrue(Settings(storage_mode="sqlserver", **base_settings).uses_sqlserver_storage)
        self.assertTrue(Settings(storage_mode="mssql", **base_settings).uses_sqlserver_storage)
        self.assertFalse(Settings(storage_mode="sqlite", **base_settings).uses_sqlserver_storage)


if __name__ == "__main__":
    unittest.main()
