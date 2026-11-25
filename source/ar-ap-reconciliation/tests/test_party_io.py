from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from finance_platform.party_io import (
    PARTY_IMPORT_COLUMNS,
    build_party_template_rows,
    parse_party_import_workbook,
)


class PartyImportExportTest(unittest.TestCase):
    def test_parse_party_import_recognizes_supplier_customer_terms_and_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "往来单位维护.xlsx"
            pd.DataFrame(
                [
                    {
                        "类型": "供应商",
                        "往来单位": "上海测试纸业有限公司",
                        "账期": "月结30天",
                        "宽限月": 1,
                        "收款账号": "6222001",
                        "开户行": "中国农业银行上海支行",
                        "别名": "测试纸业",
                    },
                    {
                        "类型": "客户",
                        "往来单位": "上海测试客户有限公司",
                        "账期": "2个月",
                        "宽限月": 1,
                    },
                ]
            ).to_excel(path, index=False)

            result = parse_party_import_workbook(path)

        self.assertEqual(result.parsed_count, 2)
        self.assertEqual(result.rows[0].party_type, "supplier")
        self.assertEqual(result.rows[0].default_term_days, 30)
        self.assertEqual(result.rows[0].bank_account, "6222001")
        self.assertEqual(result.rows[0].alias_name, "测试纸业")
        self.assertEqual(result.rows[1].party_type, "customer")
        self.assertEqual(result.rows[1].default_term_days, 60)

    def test_parse_party_import_infers_type_from_sheet_name_without_type_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "供应商和客户名单明细.xlsx"
            with pd.ExcelWriter(path) as writer:
                pd.DataFrame(
                    [
                        {
                            "收款方户名": "上海测试供应商有限公司",
                            "账期": "90天",
                            "收款方账号": "6222001",
                            "开户行支行名称": "中国农业银行上海支行",
                        }
                    ]
                ).to_excel(writer, sheet_name="应付款", index=False)
                pd.DataFrame(
                    [
                        {
                            "付款单位": "上海测试客户有限公司",
                            "账期": "120天",
                            "付款方账号": "00000000012",
                            "开户行支行名称": "上海银行股份有限公司测试支行",
                        }
                    ]
                ).to_excel(writer, sheet_name="应收款", index=False)

            result = parse_party_import_workbook(path)

        self.assertEqual(result.parsed_count, 2)
        self.assertEqual([row.party_type for row in result.rows], ["supplier", "customer"])
        self.assertEqual([row.name for row in result.rows], ["上海测试供应商有限公司", "上海测试客户有限公司"])
        self.assertEqual([row.bank_account for row in result.rows], ["6222001", "00000000012"])
        self.assertEqual([row.default_term_days for row in result.rows], [90, 120])

    def test_party_template_rows_keep_expected_columns(self) -> None:
        rows = build_party_template_rows()

        self.assertEqual(list(rows[0].keys()), PARTY_IMPORT_COLUMNS)
        self.assertIn("账期天数", PARTY_IMPORT_COLUMNS)
        self.assertNotIn("宽限月数", PARTY_IMPORT_COLUMNS)


if __name__ == "__main__":
    unittest.main()
