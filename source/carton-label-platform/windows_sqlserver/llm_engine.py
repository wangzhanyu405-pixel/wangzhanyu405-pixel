# -*- coding: utf-8 -*-
"""
llm_engine.py v2 —— 审计引擎(对齐 db_manager v2 多料号结构)

暴露接口:
    generate_audit_report(summary) → Markdown 字符串

设计:
    1. 规则审计(不依赖网络,永远可用)
    2. AI JSON 结构化审计(格式稳定)
    3. Markdown 完全由 Python 渲染,AI 只输出字段值

输入 summary 结构:
    summary['order_header']         客户/备注/交期
    summary['products']             [{料号,品名,品类,订货数量,模数,最后工序名称,最终成品数,后道工序:[...]}]
    summary['shared_front_processes'] 前道共用工序
    summary['warnings']             全局警告
"""

import os
import json
import requests

API_KEY = os.environ.get("DASHSCOPE_API_KEY")
API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


# ============================================================
# 一、规则审计
# ============================================================
def rule_based_audit(summary):
    """多料号规则审计。"""
    alerts = []

    for p in summary.get('products', []):
        item = p['料号']
        name = p['品名'][:20] if p['品名'] else ''
        order_qty = p.get('订货数量', 0)
        final_qty = p.get('最终成品数', 0)
        final_name = p.get('最后工序名称', '')

        if final_name == '(无数据)':
            alerts.append(f"🔴 料号 {item}【{name}】无任何成品数据,请核对工序进度")
            continue
        if '估算' in final_name:
            alerts.append(f"🟡 料号 {item}【{name}】为前道估算值,未找到后道专属报工")

        if order_qty > 0 and final_qty > 0:
            ratio = final_qty / order_qty
            if ratio > 1.15:
                alerts.append(
                    f"🔴 料号 {item} 成品 {final_qty} 超订货 {order_qty} 达 {(ratio-1)*100:.1f}%"
                )
            elif ratio < 0.85:
                alerts.append(
                    f"🟡 料号 {item} 成品 {final_qty} 低于订货 {order_qty} 达 {(1-ratio)*100:.1f}%"
                )

    for w in summary.get('warnings', []):
        alerts.append(f"⚠️ {w}")

    if not alerts:
        alerts.append("✅ 规则审计通过,无异常项")
    return alerts


# ============================================================
# 二、AI JSON 审计
# ============================================================
AI_SYSTEM_PROMPT = """你是印刷包装厂的生产数据审计员。严格按以下 JSON 格式输出(只输出 JSON,不要任何其他文字,不要 Markdown 代码块):

{
  "delivery_requirement": "从备注中提炼一句话客户交货要求(无备注则写'无特殊说明')",
  "per_product_notes": [
    {"item_no": "料号", "observation": "对该款产品的一句话观察点"}
  ],
  "risk_level": "low|medium|high",
  "risk_reason": "若风险非 low,写明理由;若为 low 则写'无明显风险'"
}

规则:
- 不要做任何加减乘除,所有数字已由系统算好,你只负责解读和审视
- per_product_notes 每款产品写一条即可
- JSON 字段名必须完全一致,值用中文
- 整个回复必须是单一个合法 JSON 对象
"""


def ai_audit(summary, timeout=25):
    """调 AI,返回 (result_dict, error_str)。失败时 result_dict 为 None。"""
    if not API_KEY:
        return None, "⚙️ 未配置 API_KEY,已跳过 AI 审计"

    minimal_data = {
        "订单头信息": {
            "工艺单号": summary['process_no'],
            "客户": summary['order_header'].get('客户名称', ''),
            "定单号": summary['order_header'].get('定单号码', ''),
            "预定交期": summary['order_header'].get('预定交期', ''),
            "工艺单备注": summary['order_header'].get('工艺单备注', ''),
            "后道备注": summary['order_header'].get('后道备注', ''),
        },
        "各款产品状态": [
            {
                "料号": p['料号'],
                "品名": p['品名'],
                "品类": p['品类'],
                "订货数量": p['订货数量'],
                "模数": p['模数'],
                "箱规": p.get('箱规', 0),
                "最后工序": p['最后工序名称'],
                "最终成品数": p['最终成品数'],
            } for p in summary['products']
        ],
        "前道共用工序": [
            {
                "工序": f['工序名称'],
                "计划": f"{f['计划投产']}{f['单位']}",
                "实报": f"{f['实际报工(原单位)']}{f['单位']}",
            } for f in summary['shared_front_processes']
        ],
    }

    payload = {
        "model": "qwen-plus",
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(minimal_data, ensure_ascii=False, indent=2)},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    try:
        session = requests.Session()
        session.trust_env = False
        resp = session.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        content = resp.json()['choices'][0]['message']['content'].strip()
        # 兜底:去掉可能的 ```json ... ``` 代码块包装
        if content.startswith("```"):
            content = content.strip("`")
            if content.lower().startswith("json"):
                content = content[4:].strip()
        data = json.loads(content)
        return data, None
    except json.JSONDecodeError as e:
        return None, f"⚠️ AI 输出不是合法 JSON({e}),已自动降级"
    except Exception as e:
        return None, f"⚠️ AI 连接失败: {str(e)}"


# ============================================================
# 三、Markdown 渲染(Python 硬控格式)
# ============================================================
CATEGORY_LABELS = {
    'card': '🎴 卡牌',
    'box': '📦 盒子',
    'manual': '📄 说明书',
    'unknown': '❓ 未识别',
}


def render_markdown_report(summary, ai_result=None, ai_error=None):
    """完整 Markdown 审计报告(格式永远一致)。"""
    lines = []
    header = summary['order_header']

    # 一、订单头
    lines.append("### 📋 订单核心信息")
    lines.append(f"- **工艺单号**: {summary['process_no']}")
    lines.append(f"- **客户**: {header.get('客户名称', '-') or '-'}")
    lines.append(f"- **客户定单号**: {header.get('定单号码', '-') or '-'}")
    lines.append(f"- **预定交期**: {header.get('预定交期', '-') or '-'}")
    lines.append(f"- **产品款数**: {len(summary['products'])} 款")
    cpck = summary.get('cpck_summary') or {}
    if cpck:
        lines.append(f"- **最近入库时间**: {cpck.get('最近入库时间') or '-'}")
        lines.append(f"- **最近出货时间**: {cpck.get('最近出货时间') or '-'}")
    if header.get('工艺单备注'):
        lines.append(f"- **工艺单备注**: {header['工艺单备注']}")
    lines.append("")

    # 二、AI 解读交货要求
    if ai_result and ai_result.get('delivery_requirement'):
        lines.append("### 🤖 AI 解读客户交货要求")
        lines.append(f"> {ai_result['delivery_requirement']}")
        lines.append("")

    # 三、前道共用工序
    if summary['shared_front_processes']:
        lines.append("### 🏭 前道共用工序(混拼阶段)")
        lines.append("| 工序 | 计划投产 | 实际报工 | 主要机长 | 完工时间 |")
        lines.append("|---|---|---|---|---|")
        for f in summary['shared_front_processes']:
            lines.append(
                f"| {f['工序名称']} | "
                f"{f['计划投产']}{f['单位']} | "
                f"{f['实际报工(原单位)']}{f['单位']} | "
                f"{', '.join(f['机长列表']) or '-'} | "
                f"{f['完工时间'] or '-'} |"
            )
        lines.append("")

    # 四、各款产品明细
    lines.append("### 🎯 各款产品最终数量(按料号)")
    lines.append("")

    ai_notes_map = {}
    if ai_result:
        for note in ai_result.get('per_product_notes', []):
            ai_notes_map[note.get('item_no', '')] = note.get('observation', '')

    for p in summary['products']:
        cat_label = CATEGORY_LABELS.get(p['品类'], p['品类'])
        lines.append(f"#### {cat_label}  `{p['料号']}`  —  {p['品名']}")
        reason_str = p.get('识别理由', '')
        last_proc_str = p['最后工序名称']
        if reason_str:
            last_proc_str = f"{p['最后工序名称']}  *({reason_str})*"
        lines.append(
            f"- **订货**: {p['订货数量']} pcs | "
            f"**模数**: {p['模数']} | "
            f"**箱规**: {p.get('箱规', 0) or '未设置'} | "
            f"**最后工序**: {last_proc_str}"
        )
        lines.append(
            f"- **最近入库时间**: {p.get('最近入库时间') or '-'} | "
            f"**最近出货时间**: {p.get('最近出货时间') or '-'}"
        )
        lines.append(f"- 🏁 **最终成品数**: **{p['最终成品数']} 个**")

        if p.get('后道工序'):
            lines.append("- 后道工序明细:")
            for q in p['后道工序']:
                mark = " 🏁" if q.get('是否最后工序') else ""
                workers = ', '.join(q['机长列表']) if q['机长列表'] else ''
                proc_class = q.get('工序分类', '')
                class_str = f" [{proc_class}]" if proc_class else ''
                line = (
                    f"  - {q['工序名称']}{class_str}{mark}: "
                    f"实报 {q['实际报工(原单位)']}{q['单位']} → "
                    f"折算 {q['折算成品(个)']} 个"
                )
                if workers:
                    line += f" | 机长 {workers}"
                lines.append(line)

        if ai_notes_map.get(p['料号']):
            lines.append(f"- 🤖 AI 观察: *{ai_notes_map[p['料号']]}*")
        lines.append("")

    # 五、规则审计
    lines.append("### 🛡️ 规则审计")
    for a in rule_based_audit(summary):
        lines.append(f"- {a}")
    lines.append("")

    # 六、AI 风险总评
    lines.append("### 📡 AI 风险总评")
    if ai_result:
        risk_icon = {'low': '🟢', 'medium': '🟡', 'high': '🔴'}.get(
            ai_result.get('risk_level', 'low'), '⚪'
        )
        risk_level = ai_result.get('risk_level', 'low').upper()
        lines.append(f"- 风险级别: {risk_icon} **{risk_level}**")
        lines.append(f"- 理由: {ai_result.get('risk_reason', '无')}")
    elif ai_error:
        lines.append(f"- {ai_error}")
    else:
        lines.append("- (未启用 AI 审计)")

    return "\n".join(lines)


def generate_audit_report(summary):
    """对外主入口,返回完整 Markdown 报告。"""
    if not summary:
        return "❌ 无可审计数据"
    ai_result, ai_error = ai_audit(summary)
    return render_markdown_report(summary, ai_result, ai_error)
