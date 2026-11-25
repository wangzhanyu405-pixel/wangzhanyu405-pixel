from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from finance_platform.local_store import LocalStore, insert_parsed_invoices
from finance_platform.services import parse_file_by_role, repair_local_invoice_import_directions


class InvoiceImportDirectionTest(unittest.TestCase):
    def test_repair_reparses_issued_invoice_import_as_receivable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            workbook = tmp_path / "26年开具发票.xlsx"
            pd.DataFrame(
                [
                    {
                        "数电发票号码": "OUT-1",
                        "销方名称": "示例核算主体",
                        "购买方名称": "上海测试客户有限公司",
                        "开票日期": date(2026, 6, 1),
                        "价税合计": 1200,
                    }
                ]
            ).to_excel(workbook, sheet_name="发票基础信息", index=False)

            store = LocalStore(tmp_path / "finance.db")
            store.init()
            source_file_id = store.record_import_file(
                period_month="2026-06",
                file_role="payable_invoice",
                original_filename=workbook.name,
                stored_path=str(workbook),
                file_hash="hash-issued",
                row_count=1,
                parsed_count=1,
                error_count=0,
            )
            wrong_result = parse_file_by_role(workbook, "payable_invoice")
            insert_parsed_invoices(store, wrong_result.rows, source_file_id)

            self.assertEqual(len(store.list_invoices("receivable")), 0)
            self.assertEqual(store.list_invoices("payable")[0]["party_name_raw"], "示例核算主体")

            report = repair_local_invoice_import_directions(store)

            receivables = store.list_invoices("receivable")
            payables = store.list_invoices("payable")
            imports = store.recent_imports(10)
            self.assertEqual(report["repaired_files"], 1)
            self.assertEqual(len(payables), 0)
            self.assertEqual(len(receivables), 1)
            self.assertEqual(receivables[0]["party_name_raw"], "上海测试客户有限公司")
            self.assertEqual(imports[0]["file_role"], "receivable_invoice")
            self.assertEqual(imports[0]["saved_count"], 1)


if __name__ == "__main__":
    unittest.main()
