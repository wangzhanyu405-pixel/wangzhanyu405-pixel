from __future__ import annotations

import unittest

from finance_platform.bank_classification import (
    bank_review_action,
    classify_counterparty_nature,
    is_reconciliation_candidate,
)


class BankClassificationTest(unittest.TestCase):
    def test_person_name_is_personal_flow_but_can_be_promoted(self) -> None:
        nature = classify_counterparty_nature("李甲", summary="转存")

        self.assertEqual(nature.kind, "personal")
        self.assertFalse(nature.reconciliation_candidate)
        self.assertEqual(bank_review_action(nature), "可转为客户/供应商或标记非对账")

    def test_company_name_without_master_data_is_company_candidate(self) -> None:
        nature = classify_counterparty_nature("上海例子印刷包装有限公司", summary="转取")

        self.assertEqual(nature.kind, "company")
        self.assertTrue(nature.reconciliation_candidate)
        self.assertEqual(bank_review_action(nature), "提醒新建往来单位或加入别名")

    def test_tax_and_fee_names_are_non_reconciliation(self) -> None:
        tax = classify_counterparty_nature("待报解预算收入-国库信息处理系统扣税", summary="公共缴费")
        fee = classify_counterparty_nature("中国农业银行股份有限公司上海测试支行", summary="费用外收")

        self.assertEqual(tax.kind, "non_reconciliation")
        self.assertEqual(fee.kind, "non_reconciliation")
        self.assertFalse(is_reconciliation_candidate(tax))
        self.assertFalse(is_reconciliation_candidate(fee))


if __name__ == "__main__":
    unittest.main()
