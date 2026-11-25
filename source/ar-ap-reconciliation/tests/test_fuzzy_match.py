from __future__ import annotations

import unittest

from finance_platform.fuzzy_match import company_match_key, company_similarity


class FuzzyMatchTest(unittest.TestCase):
    def test_company_similarity_handles_extra_business_words(self) -> None:
        score = company_similarity("示例甲包装（上海）有限公司", "示例甲包装品贸易（上海）有限公司")

        self.assertGreaterEqual(score, 0.72)

    def test_company_similarity_keeps_unrelated_names_below_review_threshold(self) -> None:
        score = company_similarity("示例甲包装（上海）有限公司", "上海示例辛印刷有限公司")

        self.assertLess(score, 0.72)

    def test_company_match_key_removes_common_company_shell_words(self) -> None:
        self.assertEqual(company_match_key("上海测试包装（上海）有限公司"), "上海测试包装上海")


if __name__ == "__main__":
    unittest.main()
