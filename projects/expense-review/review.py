"""使用虚构的财务明细演示逐笔核对。仅依赖 Python 标准库。"""
import argparse
import csv
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path

BASE = Path(__file__).resolve().parent
FIELDS = ["申请号", "员工代号", "费用项目", "币种", "金额", "审核结果", "候选凭证", "复核说明"]
MATCHED, AMBIGUOUS, MISSING = "匹配成功", "重复待核", "缺少凭证"


def load_rows(path, required, id_field):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not set(required).issubset(reader.fieldnames or []):
            raise ValueError(f"{path.name} 缺少必填列")
        records = []
        seen = set()
        for number, raw in enumerate(reader, 2):
            row = {f: (raw.get(f) or "").strip() for f in required}
            if not all(row.values()) or row[id_field] in seen:
                raise ValueError(f"{path.name} 第 {number} 行字段为空或编号重复")
            try:
                amount = Decimal(row["金额"])
                if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal(".01")):
                    raise ValueError
            except (ValueError, InvalidOperation):
                raise ValueError(f"{path.name} 第 {number} 行金额须为正数，最多两位小数") from None
            row["金额"] = f"{amount:.2f}"
            row["币种"] = row["币种"].upper()
            seen.add(row[id_field])
            records.append(row)
        return records


def match_key(row):
    return row["员工代号"], row["币种"], row["金额"]


def reconcile(claims, receipts):
    counts = Counter(match_key(row) for row in claims)
    candidates = defaultdict(list)
    for receipt in receipts:
        candidates[match_key(receipt)].append(receipt["凭证号"])
    output = []
    for claim in claims:
        key = match_key(claim)
        ids = candidates[key]
        if not ids:
            status, reason = MISSING, "查无同员工、同币种、同金额凭证"
        elif counts[key] != 1 or len(ids) != 1:
            status, reason = AMBIGUOUS, f"{counts[key]} 笔同额申请对应 {len(ids)} 张候选凭证"
        else:
            status, reason = MATCHED, "申请与凭证一一对应"
        output.append({**claim, "审核结果": status, "复核说明": reason, "候选凭证": "、".join(ids)})
    return output


def write_html(path, rows, receipt_count):
    statuses = Counter(r["审核结果"] for r in rows)
    style = """
    *{box-sizing:border-box}html{background:#f3f5f4;color:#1c302d;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif}
    body{margin:0}main{margin:38px auto;max-width:1112px;background:white;border:1px solid #d5dfdc;padding:32px}
    h1{font-size:34px;margin:0 0 8px;letter-spacing:-1px}p{color:#50655e;margin:0;font-size:15px}
    .counts{display:flex;margin:27px 0 23px;padding:17px 0;border-block:1px solid #d3dfda}
    .metric{flex:1;padding:0 18px;border-right:1px solid #d3dfda}.metric:first-child{padding-left:0}.metric:last-child{border:0}
    .metric b{font-size:30px;margin-right:9px}.metric span{font-size:15px}table{width:100%;border-collapse:collapse;font-size:14px}
    th{text-align:left;background:#eaf0ed;color:#374d45}td,th{padding:13px 8px;border-bottom:1px solid #e2e7e4}
    td{font-variant-numeric:tabular-nums}th:nth-child(4),.money{text-align:right;white-space:nowrap}.tag{font-weight:600;white-space:nowrap}
    .ok{color:#17634d}.check{color:#925719}.missing{color:#aa3e38}footer{margin-top:20px;font-size:13px;color:#586961}
    @media(max-width:830px){main{margin:0;padding:18px}.scroll{overflow-x:auto}table{min-width:860px}.metric{padding:0 6px}}
    """
    summary = "".join(f'<div class="metric"><b>{n}</b><span>{label}</span></div>'
                      for label,n in [("申请总数",len(rows)),("匹配成功",statuses[MATCHED]),
                                      ("重复待核",statuses[AMBIGUOUS]),("缺少凭证",statuses[MISSING])])
    headers = ["申请号", "员工代号", "费用项目", "申请金额", "审核结果", "候选凭证", "复核说明"]
    table = []
    classes = {MATCHED: "ok", AMBIGUOUS: "check", MISSING: "missing"}
    for row in rows:
        cells = [escape(row[k]) for k in ("申请号", "员工代号", "费用项目")]
        cells += [escape(row["币种"]+" "+row["金额"]),
                  f'<span class="tag {classes[row["审核结果"]]}">{row["审核结果"]}</span>',
                  escape(row["候选凭证"] or "—"), escape(row["复核说明"])]
        table.append("<tr>"+"".join(f'<td class="{"money" if i==3 else ""}">{v}</td>' for i,v in enumerate(cells))+"</tr>")
    path.write_text(f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>报销核对结果</title>
    <style>{style}</style><main><h1>报销核对结果</h1><p>演示数据 · {len(rows)} 笔申请与 {receipt_count} 张凭证</p>
    <section class="counts">{summary}</section><div class="scroll"><table>
    <thead><tr>{''.join('<th>'+h+'</th>' for h in headers)}</tr></thead><tbody>{''.join(table)}</tbody>
    </table></div><footer>匹配依据为员工代号、币种和金额。重复组保留全部候选凭证，供人工确认归属。</footer><p class="source-link"><a href="https://github.com/wangzhanyu405-pixel/wangzhanyu405-pixel/tree/main/source/expense-review-archive">查看报销审核与归档完整源码 ↗</a></p></main></html>""",encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="用 CSV 核对报销申请与凭证")
    parser.add_argument("--claims", type=Path, default=BASE/"claims.csv")
    parser.add_argument("--receipts", type=Path, default=BASE/"receipts.csv")
    parser.add_argument("--out", type=Path, default=BASE/"output")
    args = parser.parse_args()
    claims = load_rows(args.claims, FIELDS[:5], "申请号")
    receipts = load_rows(args.receipts, ["凭证号", "员工代号", "币种", "金额"], "凭证号")
    results = reconcile(claims, receipts)
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out/"review.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, FIELDS)
        writer.writeheader()
        writer.writerows(results)
    write_html(args.out/"review.html", results, len(receipts))
    print(f"已核对 {len(results)} 笔报销申请。打开 {args.out/'review.html'} 查看结果。")


if __name__ == "__main__":
    main()
