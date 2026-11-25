from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from finance_platform.normalization import clean_name


COMPANY_SHELL_WORDS = [
    "股份有限公司",
    "有限责任公司",
    "集团有限公司",
    "控股有限公司",
    "有限公司",
    "分公司",
    "总公司",
]

PUNCTUATION_RE = re.compile(r"[\s\(\)\[\]【】{}<>《》\-－—_/\\.,，、·;；:：]+")


def company_match_key(name: Any) -> str:
    text = clean_name(name)
    for word in COMPANY_SHELL_WORDS:
        text = text.replace(word, "")
    return PUNCTUATION_RE.sub("", text)


def company_similarity(left: Any, right: Any) -> float:
    left_key = company_match_key(left)
    right_key = company_match_key(right)
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 1.0
    return round(SequenceMatcher(None, left_key, right_key).ratio(), 4)
