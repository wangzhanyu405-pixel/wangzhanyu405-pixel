from __future__ import annotations

import re
from dataclasses import dataclass

from finance_platform.normalization import clean_name


COMPANY_KEYWORDS = [
    "公司",
    "有限",
    "集团",
    "厂",
    "商行",
    "经营部",
    "中心",
    "学校",
    "学院",
    "医院",
    "事务所",
    "合作社",
    "科技",
    "贸易",
    "实业",
    "物流",
    "设备",
    "印刷",
    "包装",
    "纸业",
    "银行",
]
NON_RECONCILIATION_KEYWORDS = [
    "待报解预算收入",
    "国库",
    "扣税",
    "税务",
    "税款",
    "增值税",
    "所得税",
    "工资",
    "薪资",
    "社保",
    "公积金",
    "手续费",
    "利息",
    "账户管理费",
    "服务费",
    "费用外收",
    "内部转账",
    "备用金",
    "转户",
    "往来款转入",
]


@dataclass(frozen=True)
class CounterpartyNature:
    kind: str
    label: str
    reconciliation_candidate: bool
    reason: str


def classify_counterparty_nature(name: str | None, *, summary: str | None = None, bank: str | None = None) -> CounterpartyNature:
    """Classify a bank counterparty before it is allowed into reconciliation.

    The classification is intentionally conservative: exact master-data matches
    still happen in LocalStore first. This helper only handles rows that did not
    resolve to an existing supplier/customer.
    """
    name_text = clean_name(name)
    text = clean_name(" ".join(part for part in [name, summary, bank] if part))
    if not name_text:
        return CounterpartyNature("unknown", "未知户名", False, "银行流水没有对方户名")
    if _contains_any(text, NON_RECONCILIATION_KEYWORDS):
        return CounterpartyNature("non_reconciliation", "非对账流水", False, "税费、手续费、工资社保或内部转账类流水")
    if _contains_any(name_text, COMPANY_KEYWORDS):
        return CounterpartyNature("company", "公司候选", True, "对方户名包含公司或机构特征")
    if _looks_like_person_name(name_text):
        return CounterpartyNature("personal", "个人流水", False, "对方户名像个人姓名，默认不进入客户/供应商对账")
    return CounterpartyNature("unknown", "未识别户名", False, "没有命中公司、个人或非对账规则")


def is_reconciliation_candidate(nature: CounterpartyNature) -> bool:
    return bool(nature.reconciliation_candidate)


def bank_review_action(nature: CounterpartyNature) -> str:
    if nature.kind == "company":
        return "提醒新建往来单位或加入别名"
    if nature.kind == "personal":
        return "可转为客户/供应商或标记非对账"
    if nature.kind == "non_reconciliation":
        return "归入非对账流水"
    return "人工判断是否建档或忽略"


def _contains_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _looks_like_person_name(value: str) -> bool:
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", value):
        return False
    return not _contains_any(value, COMPANY_KEYWORDS + NON_RECONCILIATION_KEYWORDS)
