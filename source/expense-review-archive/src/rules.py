import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = Path(os.environ.get("EXPENSE_CONFIG", ROOT / "config.local.json")).resolve()


def load_rules(path=DEFAULT_CONFIG):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"请先复制 config.example.json 为 config.local.json 并填写本地规则：{path}")
    config = json.loads(path.read_text(encoding="utf-8"))
    titles = config["valid_titles"]
    aliases = config["employee_aliases"]
    if not isinstance(titles, list) or not titles or not all(isinstance(t, str) and t.strip() for t in titles):
        raise ValueError("valid_titles 需要至少一个非空的发票抬头")
    if not isinstance(aliases, list) or not all(
        isinstance(group, list) and group and all(isinstance(v, str) and v.strip() for v in group)
        for group in aliases
    ):
        raise ValueError("employee_aliases 应为别名数组，每组第一项为归档名")
    return aliases, titles
