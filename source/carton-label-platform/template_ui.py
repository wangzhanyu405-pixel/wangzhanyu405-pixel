# -*- coding: utf-8 -*-
"""Structured print-template editor for Streamlit.

This page intentionally avoids free-form dragging as the default workflow.
The factory label is a table, so editing rows, fields, font sizes and QR
settings is faster and less fragile than moving dozens of absolute elements.
"""

import copy
import base64
import datetime
import io
import time
import uuid

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from db_manager import delete_print_template, get_print_templates, save_print_template
from label_engine import make_default_fields_from_data
from print_manager import get_default_printer, list_printers, print_zpl
from template_engine import (
    CUSTOMER_QR_FIELDS,
    CUSTOMER_QR_TEMPLATE,
    OLD_DEFAULT_QR_TEMPLATES,
    get_default_template,
    get_template_signature,
    inspect_grid_qr_placement,
    make_template_copy,
    render_label_image,
    render_label_zpl,
    validate_grid_layout,
)


FIELD_OPTIONS = {
    "SAP号": "sap_no",
    "产品名称": "product_name",
    "料号": "item_no",
    "订单号": "order_no",
    "订单数量": "order_qty",
    "包装方式": "pack_method",
    "每箱数量": "box_qty",
    "每箱数量纯数字": "box_qty_raw",
    "总箱数": "box_index_total",
    "当前箱序号": "box_index",
    "当前箱序号4位": "box_index_4",
    "日期": "print_date",
    "日期YYYYMMDD": "print_date_compact",
    "打印时间": "print_time_compact",
    "发货时间": "shipment_time_compact",
    "打印人": "operator_name",
    "供应商": "supplier",
    "固定文字": "__fixed__",
}
FONT_FAMILY_OPTIONS = ["微软雅黑", "宋体", "黑体", "楷体", "仿宋", "方正博雅宋_GBK", "Arial"]
FIELD_LABELS = {value: label for label, value in FIELD_OPTIONS.items()}
ALIGN_OPTIONS = ["左", "中", "右"]
VALIGN_OPTIONS = ["上", "中", "下"]
ALIGN_MAP = {"左": "left", "中": "center", "右": "right"}
VALIGN_MAP = {"上": "top", "中": "middle", "下": "bottom"}
ALIGN_REVERSE = {value: key for key, value in ALIGN_MAP.items()}
VALIGN_REVERSE = {value: key for key, value in VALIGN_MAP.items()}


def _sample_fields():
    return make_default_fields_from_data(
        {
            "料号": "DEMO-A01",
            "品名": "示例包装纸盒",
            "订货数量": 10000,
            "包装方式": "250份/包",
        },
        {"定单号码": "PO-DEMO-001"},
    )


def _load_templates():
    try:
        templates = get_print_templates(active_only=True)
    except Exception as exc:
        st.warning(f"读取数据库模板失败，暂时使用内置模板：{exc}")
        templates = [get_default_template()]
    return templates or [get_default_template()]


def _normalize_template(template):
    tpl = copy.deepcopy(template or get_default_template())
    tpl["layout_mode"] = "grid"
    tpl.pop("elements", None)
    tpl.setdefault("id", "standard_80x60")
    tpl.setdefault("name", "80x60标准箱贴")
    tpl.setdefault("template_type", "label")
    tpl.setdefault("output_mode", "zpl_bitmap")
    tpl.setdefault("font_family", "微软雅黑")

    paper = tpl.setdefault("paper", {})
    paper.setdefault("width_mm", 80)
    paper.setdefault("height_mm", 60)
    paper.setdefault("dpi", 203)
    paper["dots_per_mm"] = 8 if int(paper.get("dpi", 203)) == 203 else 12
    paper.setdefault("orientation", "landscape")

    layout = tpl.setdefault("layout", {})
    layout.setdefault("margin", 14)
    layout.setdefault("line_width", 3)
    layout.setdefault("label_col_width", 96)
    layout.setdefault("qr_x", 370)
    layout["qr_draw_boundary"] = True

    fonts = tpl.setdefault("fonts", {})
    fonts.setdefault("item_no", 34)
    fonts.setdefault("qr_text", 18)

    rows = tpl.setdefault("rows", [])
    for row in rows:
        row.setdefault("_row_id", uuid.uuid4().hex)
        row.setdefault("label", FIELD_LABELS.get(row.get("field"), "字段"))
        row.setdefault("field", "product_name")
        row.setdefault("fixed_text", "")
        row.setdefault("font", "normal")
        row.setdefault("label_font_size", fonts.get("label", 25))
        row.setdefault("font_size", fonts.get(row.get("font", "normal"), fonts.get("normal", 28)))
        row.setdefault("label_align", "center")
        row.setdefault("label_valign", "middle")
        row.setdefault("value_align", "center")
        row.setdefault("value_valign", "middle")
        row.setdefault("qr_side", False)
        row.setdefault("auto_fit", False)
    if not rows:
        tpl["rows"] = copy.deepcopy(get_default_template()["rows"])
        return _normalize_template(tpl)

    row_heights = [int(v) for v in layout.get("row_heights", [])]
    if len(row_heights) < len(rows):
        row_heights += [54] * (len(rows) - len(row_heights))
    layout["row_heights"] = row_heights[: len(rows)]
    _fit_row_heights(tpl)

    qr = tpl.setdefault("qr", {})
    qr.setdefault("enabled", True)
    qr.setdefault("show_text", True)
    qr.setdefault("render_mode", "zpl_native")
    qr.setdefault("top_row", min(3, max(0, len(rows) - 1)))
    qr.setdefault("bottom_row", min(7, len(rows)))
    qr.setdefault("padding_x", 46)
    qr.setdefault("padding_y", 32)
    qr.setdefault("offset_x", 0)
    qr.setdefault("offset_y", 30)
    qr.setdefault("text_offset_x", 0)
    qr.setdefault("text_offset_y", 0)
    qr.setdefault("size", 0)
    qr.setdefault("text_bottom", 22)
    qr.setdefault("zpl_x", 0)
    qr.setdefault("zpl_y", 0)
    qr.setdefault("zpl_offset_x", 0)
    qr.setdefault("zpl_offset_y", 0)
    qr.setdefault("zpl_scale", 4)
    qr.setdefault("zpl_text_x", 0)
    qr.setdefault("zpl_text_y", 0)
    qr.setdefault("zpl_text_offset_x", 0)
    qr.setdefault("zpl_text_offset_y", 0)
    qr.setdefault("separator", "#")
    qr.setdefault("shipment_prefix", "SHIP")
    if qr.get("content_template") in OLD_DEFAULT_QR_TEMPLATES:
        qr["template_mode"] = "customer"
        qr["content_template"] = CUSTOMER_QR_TEMPLATE
        qr["text_template"] = CUSTOMER_QR_TEMPLATE
        qr["fields"] = CUSTOMER_QR_FIELDS[:]
        qr["text_fields"] = CUSTOMER_QR_FIELDS[:]
    qr.setdefault("template_mode", "customer")
    qr.setdefault(
        "fields",
        CUSTOMER_QR_FIELDS[:],
    )
    qr.setdefault(
        "text_fields",
        CUSTOMER_QR_FIELDS[:],
    )
    qr.setdefault("content_template", CUSTOMER_QR_TEMPLATE)
    qr.setdefault("text_template", CUSTOMER_QR_TEMPLATE)
    qr["top_row"] = max(0, min(int(qr.get("top_row", 3)), len(rows) - 1))
    qr["bottom_row"] = max(qr["top_row"] + 1, min(int(qr.get("bottom_row", len(rows))), len(rows)))
    _sync_qr_templates(tpl)

    special_marks = tpl.setdefault("special_marks", {})
    tail_mark = special_marks.setdefault("tail", {})
    tail_mark.setdefault("text", "尾箱")
    tail_mark.setdefault("font_scale", 1.35)
    tail_mark.setdefault("bold", True)
    spare_mark = special_marks.setdefault("spare", {})
    spare_mark.setdefault("text", "备品")
    spare_mark.setdefault("font_scale", 1.35)
    spare_mark.setdefault("bold", True)

    zpl = tpl.setdefault("zpl", {})
    zpl["rotate_for_media"] = bool(zpl.get("rotate_for_media", False)) and bool(zpl.get("force_rotate", False))
    zpl.setdefault("darkness", 18)
    zpl.setdefault("speed", 3)
    zpl.setdefault("clarity_mode", "standard")
    return tpl


def _sync_qr_templates(template):
    qr = template.setdefault("qr", {})
    mode = qr.get("template_mode", "customer")
    if mode == "customer":
        qr["fields"] = CUSTOMER_QR_FIELDS[:]
        qr["text_fields"] = CUSTOMER_QR_FIELDS[:]
        qr["content_template"] = CUSTOMER_QR_TEMPLATE
        qr["text_template"] = CUSTOMER_QR_TEMPLATE
        return
    if mode == "custom":
        qr.setdefault("content_template", CUSTOMER_QR_TEMPLATE)
        qr.setdefault("text_template", qr.get("content_template", CUSTOMER_QR_TEMPLATE))
        return
    sep = qr.get("separator", "#") or "#"
    qr["content_template"] = sep.join(
        f"{qr.get('shipment_prefix', 'SHIP')}{{{field}}}" if field == "shipment_time_compact" else f"{{{field}}}"
        for field in qr.get("fields", [])
    )
    qr["text_template"] = sep.join(f"{{{field}}}" for field in qr.get("text_fields", []))


def _template_state_key(template):
    return f"{template.get('_db_id', 'builtin')}::{template.get('name', '')}"


def _tpl_widget_key(name):
    base = str(st.session_state.get("grid_tpl_key", "current"))
    safe = "".join(ch if ch.isalnum() else "_" for ch in base)
    return f"{safe}_{name}"


def _template_widget_prefix(state_key=None):
    base = str(state_key or st.session_state.get("grid_tpl_key", "current"))
    safe = "".join(ch if ch.isalnum() else "_" for ch in base)
    return f"{safe}_"


def _init_state(template):
    state_key = _template_state_key(template)
    if st.session_state.get("grid_tpl_key") != state_key:
        st.session_state.grid_tpl_key = state_key
        st.session_state.grid_tpl_config = _normalize_template(template)


def _clear_row_editor_state():
    for key in list(st.session_state.keys()):
        if str(key).startswith("tpl_row_") or str(key).startswith("tpl_new_row_"):
            st.session_state.pop(key, None)


def _clear_template_widget_state(state_key=None):
    prefix = _template_widget_prefix(state_key)
    for key in list(st.session_state.keys()):
        if str(key).startswith(prefix):
            st.session_state.pop(key, None)


def _clear_template_preview_state():
    for key in list(st.session_state.keys()):
        if str(key).startswith("grid_tpl_preview::"):
            st.session_state.pop(key, None)


def _clear_generated_print_state():
    for key in ["generated_images", "generated_qrs", "generated_meta", "generated_zpls", "print_context"]:
        st.session_state.pop(key, None)


def _make_default_row(template, label="新字段"):
    fonts = template.setdefault("fonts", {})
    return {
        "_row_id": uuid.uuid4().hex,
        "label": label,
        "field": "__fixed__",
        "fixed_text": "",
        "font": "normal",
        "label_font_size": fonts.get("label", 25),
        "font_size": fonts.get("normal", 28),
        "label_align": "center",
        "label_valign": "middle",
        "value_align": "center",
        "value_valign": "middle",
        "qr_side": False,
        "auto_fit": False,
    }


def _move_row(template, from_idx, to_idx):
    rows = template.setdefault("rows", [])
    heights = template.setdefault("layout", {}).setdefault("row_heights", [])
    if not rows:
        return template
    from_idx = max(0, min(int(from_idx), len(rows) - 1))
    to_idx = max(0, min(int(to_idx), len(rows) - 1))
    if from_idx == to_idx:
        return template
    row = rows.pop(from_idx)
    rows.insert(to_idx, row)
    if from_idx < len(heights):
        height = heights.pop(from_idx)
        heights.insert(to_idx, height)
    _fit_row_heights(template)
    return template


def _insert_row(template, insert_idx):
    rows = template.setdefault("rows", [])
    heights = template.setdefault("layout", {}).setdefault("row_heights", [])
    insert_idx = max(0, min(int(insert_idx), len(rows)))
    rows.insert(insert_idx, _make_default_row(template))
    heights.insert(insert_idx, 54)
    _shift_qr_range_on_insert(template, insert_idx)
    _fit_row_heights(template)
    return template


def _delete_row(template, delete_idx):
    rows = template.setdefault("rows", [])
    heights = template.setdefault("layout", {}).setdefault("row_heights", [])
    if len(rows) <= 1:
        return template
    delete_idx = max(0, min(int(delete_idx), len(rows) - 1))
    rows.pop(delete_idx)
    if delete_idx < len(heights):
        heights.pop(delete_idx)
    _shift_qr_range_on_delete(template, delete_idx)
    _fit_row_heights(template)
    return template


def _shift_qr_range_on_insert(template, insert_idx):
    qr = template.setdefault("qr", {})
    top = int(qr.get("top_row", 0))
    bottom = int(qr.get("bottom_row", len(template.get("rows", []))))
    if insert_idx <= top:
        qr["top_row"] = top + 1
        qr["bottom_row"] = bottom + 1
    elif insert_idx < bottom:
        qr["bottom_row"] = bottom + 1


def _shift_qr_range_on_delete(template, delete_idx):
    rows_count = len(template.get("rows", []))
    if rows_count <= 0:
        return
    qr = template.setdefault("qr", {})
    top = int(qr.get("top_row", 0))
    bottom = int(qr.get("bottom_row", rows_count))
    if delete_idx < top:
        top -= 1
        bottom -= 1
    elif delete_idx < bottom:
        bottom -= 1
    top = max(0, min(top, rows_count - 1))
    bottom = max(top + 1, min(bottom, rows_count))
    qr["top_row"] = top
    qr["bottom_row"] = bottom


def _apply_row_position(row_id):
    template = st.session_state.get("grid_tpl_config")
    if not template:
        return
    rows = template.setdefault("rows", [])
    current_idx = next((idx for idx, row in enumerate(rows) if row.get("_row_id") == row_id), None)
    if current_idx is None:
        return
    target_key = f"tpl_row_pos_{row_id}"
    target_pos = int(st.session_state.get(target_key, current_idx + 1) or current_idx + 1)
    if target_pos != current_idx + 1:
        _move_row(template, current_idx, target_pos - 1)
        st.session_state.grid_tpl_config = template
        _clear_row_editor_state()


def _rows_to_table(template):
    heights = template["layout"].get("row_heights", [])
    rows = []
    for idx, row in enumerate(template.get("rows", []), start=1):
        rows.append(
            {
                "序号": idx,
                "显示名": row.get("label", ""),
                "绑定字段": FIELD_LABELS.get(row.get("field"), "固定文字"),
                "固定文字": row.get("fixed_text", ""),
                "行高": int(heights[idx - 1]) if idx - 1 < len(heights) else 54,
                "左列字号": int(row.get("label_font_size", template["fonts"].get("label", 25))),
                "内容字号": int(row.get("font_size", template["fonts"].get("normal", 28))),
                "内容对齐": ALIGN_REVERSE.get(row.get("value_align", "center"), "中"),
                "垂直对齐": VALIGN_REVERSE.get(row.get("value_valign", "middle"), "中"),
                "二维码旁": bool(row.get("qr_side", False)),
                "删除": False,
            }
        )
    return pd.DataFrame(rows)


def _table_to_rows(template, edited_df):
    rows = []
    heights = []
    if edited_df is None or edited_df.empty:
        edited_df = _rows_to_table(_normalize_template(get_default_template()))
    df = edited_df.copy().fillna("")
    if "序号" in df.columns:
        df["序号"] = pd.to_numeric(df["序号"], errors="coerce").fillna(9999)
        df = df.sort_values("序号", kind="stable")
    for _, rec in df.iterrows():
        if bool(rec.get("删除", False)):
            continue
        field_label = str(rec.get("绑定字段") or "固定文字")
        field_key = FIELD_OPTIONS.get(field_label, "__fixed__")
        label = str(rec.get("显示名") or field_label)
        height = _clamp_int(rec.get("行高"), 24, 120, 54)
        row = {
            "label": label,
            "field": field_key,
            "fixed_text": str(rec.get("固定文字") or ""),
            "font": "normal",
            "label_font_size": _clamp_int(rec.get("左列字号"), 10, 48, template["fonts"].get("label", 25)),
            "font_size": _clamp_int(rec.get("内容字号"), 10, 64, template["fonts"].get("normal", 28)),
            "label_align": "center",
            "label_valign": "middle",
            "value_align": ALIGN_MAP.get(str(rec.get("内容对齐") or "中"), "center"),
            "value_valign": VALIGN_MAP.get(str(rec.get("垂直对齐") or "中"), "middle"),
            "qr_side": bool(rec.get("二维码旁", False)),
        }
        rows.append(row)
        heights.append(height)
    if not rows:
        rows = [
            {
                "label": "新字段",
                "field": "__fixed__",
                "fixed_text": "",
                "font": "normal",
                "label_font_size": template["fonts"].get("label", 25),
                "font_size": template["fonts"].get("normal", 28),
                "label_align": "center",
                "label_valign": "middle",
                "value_align": "center",
                "value_valign": "middle",
                "qr_side": False,
            }
        ]
        heights = [54]
    template["rows"] = rows
    template["layout"]["row_heights"] = heights
    _fit_row_heights(template)
    qr = template.setdefault("qr", {})
    if rows:
        qr["top_row"] = max(0, min(int(qr.get("top_row", 0)), len(rows) - 1))
        qr["bottom_row"] = max(qr["top_row"] + 1, min(int(qr.get("bottom_row", len(rows))), len(rows)))
    return template


def _fit_row_heights(template):
    rows = template.get("rows", [])
    if not rows:
        return template
    layout = template.setdefault("layout", {})
    heights = [max(18, int(v)) for v in layout.get("row_heights", [])[: len(rows)]]
    if len(heights) < len(rows):
        heights += [54] * (len(rows) - len(heights))
    layout["row_heights"] = heights
    return template


def _clamp_int(value, min_value, max_value, default):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = int(default)
    return max(int(min_value), min(int(max_value), number))


def _field_multiselect(label, values, key):
    options = [name for name, value in FIELD_OPTIONS.items() if value != "__fixed__"]
    reverse = FIELD_LABELS
    default = [reverse.get(value, value) for value in values if value in reverse]
    picked = st.multiselect(label, options=options, default=default, key=key)
    return [FIELD_OPTIONS[name] for name in picked if name in FIELD_OPTIONS]


def _render_preview(template, box_type="full"):
    qty = 1500 if box_type == "tail" else 0 if box_type == "spare" else 2000
    return render_label_image(
        template,
        _sample_fields(),
        current_box_index=0 if box_type == "spare" else 1,
        total_boxes=3,
        current_box_qty=qty,
        std_qty=2000,
        box_type=box_type,
        print_time=datetime.datetime(2026, 5, 18, 12, 0, 0),
        shipment_time="20260518120000",
    )


def _image_bytes(img):
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _browser_print_test_panel(zpl_text, panel_key):
    st.download_button("导出测试 ZPL", zpl_text.encode(), "template-test.zpl", key=panel_key)

def _save_current_template(template, current_user, set_default=False):
    template = _normalize_template(template)
    save_print_template(
        template,
        operator=current_user.get("username", "system") if current_user else "system",
        set_default=set_default,
    )


def _success_pause(message):
    st.success(message)
    time.sleep(2)


def render_template_tab(current_user):
    if not current_user or "template" not in current_user.get("permissions", []):
        st.error("打印模板只能由管理员编辑。")
        st.stop()

    st.subheader("🧩 打印模板")
    st.caption("模板修改保存在当前浏览器会话中。")

    templates = _load_templates()
    names = [tpl.get("name", "未命名模板") for tpl in templates]
    if not names:
        templates = [get_default_template()]
        names = [templates[0].get("name", "80x60标准箱贴")]

    with st.container(border=True):
        select_col, name_col, default_col, action_col = st.columns([1.3, 0.9, 0.45, 0.9])
        with select_col:
            selected_name = st.selectbox("选择模板", names, key="tpl_selected_name")
        selected_template = templates[names.index(selected_name)]
        _init_state(selected_template)

        if st.session_state.get("tpl_loaded_name") != selected_name:
            st.session_state.tpl_loaded_name = selected_name
            st.session_state.grid_tpl_config = _normalize_template(selected_template)
            _clear_template_widget_state(st.session_state.get("grid_tpl_key"))
            _clear_row_editor_state()
            _clear_template_preview_state()

        template = _normalize_template(st.session_state.get("grid_tpl_config", selected_template))
        st.session_state.grid_tpl_config = template
        layout = template.setdefault("layout", {})
        fonts = template.setdefault("fonts", {})
        qr = template.setdefault("qr", {})
        zpl = template.setdefault("zpl", {})
        saved_signature = get_template_signature(_normalize_template(selected_template))
        draft_signature = get_template_signature(template)
        validation = validate_grid_layout(template)
        status_bits = [
            f"模板: {selected_name}",
            f"签名: {draft_signature}",
            "未保存修改" if draft_signature != saved_signature else "已保存版本",
            "校验通过" if validation["ok"] else "校验未通过",
        ]
        st.caption(" | ".join(status_bits))

        with name_col:
            save_name = st.text_input("模板名称", value=str(template.get("name", selected_name)), key=_tpl_widget_key("tpl_save_name"))
            template["name"] = save_name.strip() or selected_name
        with default_col:
            set_default = st.checkbox("设为默认", value=bool(template.get("_is_default", False)), key=_tpl_widget_key("tpl_set_default"))
        with action_col:
            st.write("")
            if st.button("重新载入当前模板", use_container_width=True, key="tpl_reload_current"):
                st.session_state.grid_tpl_config = _normalize_template(selected_template)
                st.session_state.tpl_loaded_name = selected_name
                _clear_template_widget_state(st.session_state.get("grid_tpl_key"))
                _clear_row_editor_state()
                _clear_template_preview_state()
                st.rerun()

        save_col, delete_col = st.columns([1.4, 1.0])
        with save_col:
            if st.button(f"确认修改模板：{template.get('name', selected_name)}", type="primary", use_container_width=True, key="tpl_save_button"):
                try:
                    template = _normalize_template(template)
                    template["name"] = save_name.strip() or selected_name
                    validation = validate_grid_layout(template)
                    if not validation["ok"]:
                        for issue in validation["issues"]:
                            st.error(issue["message"])
                        st.stop()
                    _save_current_template(template, current_user, set_default=set_default)
                    st.session_state.grid_tpl_config = template
                    _clear_template_preview_state()
                    _clear_generated_print_state()
                    _success_pause("模板已保存。")
                    st.rerun()
                except Exception as exc:
                    st.error(f"保存失败：{exc}")
        with delete_col:
            can_delete = len(templates) > 1
            confirm_delete = st.checkbox(
                f"确认删除模板：{selected_name}",
                value=False,
                disabled=not can_delete,
                key="tpl_delete_confirm",
            )
            if st.button("删除当前模板", disabled=not (can_delete and confirm_delete), use_container_width=True, key="tpl_delete_button"):
                try:
                    affected = delete_print_template(selected_name, operator=current_user.get("username", "system"))
                    if affected:
                        st.success("模板已删除。")
                    else:
                        st.warning("没有找到可删除的模板。")
                    _clear_row_editor_state()
                    _clear_template_preview_state()
                    st.session_state.pop("tpl_loaded_name", None)
                    st.rerun()
                except Exception as exc:
                    st.error(f"删除失败：{exc}")

        with st.expander("新建模板 / 复制当前模板", expanded=False):
            default_copy_name = f"{template.get('name', selected_name)} 副本"
            copy_name = st.text_input("新模板名称", value=default_copy_name, key=_tpl_widget_key("tpl_copy_name"))
            copy_set_default = st.checkbox("复制后设为默认模板", value=False, key=_tpl_widget_key("tpl_copy_set_default"))
            if st.button("复制当前模板并新建", use_container_width=True, key="tpl_copy_current_button"):
                new_name = copy_name.strip()
                if not new_name:
                    st.error("新模板名称不能为空。")
                elif new_name in names:
                    st.error("已存在同名模板，请换一个名称。")
                else:
                    try:
                        copied_template = make_template_copy(template, new_name)
                        validation = validate_grid_layout(copied_template)
                        if not validation["ok"]:
                            for issue in validation["issues"]:
                                st.error(issue["message"])
                            st.stop()
                        _save_current_template(copied_template, current_user, set_default=copy_set_default)
                        _clear_row_editor_state()
                        _clear_template_preview_state()
                        _clear_generated_print_state()
                        st.session_state.pop("tpl_loaded_name", None)
                        _success_pause(f"已新建模板：{new_name}")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"复制新建失败：{exc}")

    st.divider()
    edit_col, preview_col, setting_col = st.columns([1.25, 1.1, 1.0], gap="large")

    with edit_col:
        st.markdown("#### 字段行")
        st.caption("每块对应箱贴上的一行。可插入、删除、移动，保存后所有电脑共用。")
        rows = template.setdefault("rows", [])
        heights = layout.setdefault("row_heights", [54] * len(rows))
        if len(heights) < len(rows):
            heights.extend([54] * (len(rows) - len(heights)))
        field_labels = list(FIELD_OPTIONS.keys())
        field_values = FIELD_OPTIONS

        for idx, row in enumerate(list(rows)):
            row_id = row.setdefault("_row_id", uuid.uuid4().hex)
            with st.container(border=True):
                top_cols = st.columns([0.45, 1.2, 0.35, 0.35, 0.55, 0.55, 0.65])
                with top_cols[0]:
                    st.selectbox(
                        "序号",
                        list(range(1, len(rows) + 1)),
                        index=idx,
                        key=f"tpl_row_pos_{row_id}",
                        on_change=_apply_row_position,
                        args=(row_id,),
                    )
                with top_cols[1]:
                    row["label"] = st.text_input("显示名", value=str(row.get("label", "字段")), key=f"tpl_row_label_{row_id}", label_visibility="collapsed")
                with top_cols[2]:
                    if st.button("↑", key=f"tpl_row_up_{row_id}", disabled=idx == 0):
                        _move_row(template, idx, idx - 1)
                        st.session_state.grid_tpl_config = template
                        _clear_row_editor_state()
                        _clear_template_preview_state()
                        st.rerun()
                with top_cols[3]:
                    if st.button("↓", key=f"tpl_row_down_{row_id}", disabled=idx >= len(rows) - 1):
                        _move_row(template, idx, idx + 1)
                        st.session_state.grid_tpl_config = template
                        _clear_row_editor_state()
                        _clear_template_preview_state()
                        st.rerun()
                with top_cols[4]:
                    if st.button("上方插入", key=f"tpl_row_insert_above_{row_id}"):
                        _insert_row(template, idx)
                        st.session_state.grid_tpl_config = template
                        _clear_row_editor_state()
                        _clear_template_preview_state()
                        st.rerun()
                with top_cols[5]:
                    if st.button("下方插入", key=f"tpl_row_insert_below_{row_id}"):
                        _insert_row(template, idx + 1)
                        st.session_state.grid_tpl_config = template
                        _clear_row_editor_state()
                        _clear_template_preview_state()
                        st.rerun()
                with top_cols[6]:
                    if st.button("删除", key=f"tpl_row_delete_{row_id}", disabled=len(rows) <= 1):
                        _delete_row(template, idx)
                        st.session_state.grid_tpl_config = template
                        _clear_row_editor_state()
                        _clear_template_preview_state()
                        st.rerun()

                form_cols = st.columns([1.0, 1.0])
                current_field = row.get("field", "__fixed__")
                current_field_label = FIELD_LABELS.get(current_field, "固定文字")
                with form_cols[0]:
                    chosen_field = st.selectbox(
                        "绑定字段",
                        field_labels,
                        index=field_labels.index(current_field_label) if current_field_label in field_labels else field_labels.index("固定文字"),
                        key=f"tpl_row_field_{row_id}",
                    )
                    row["field"] = field_values.get(chosen_field, "__fixed__")
                    row["fixed_text"] = st.text_input("固定文字", value=str(row.get("fixed_text", "")), key=f"tpl_row_fixed_{row_id}")
                    row["qr_side"] = st.checkbox(
                        "这一行左侧内容避开二维码区",
                        value=bool(row.get("qr_side", False)),
                        key=f"tpl_row_qr_side_{row_id}",
                    )
                    row["auto_fit"] = st.checkbox(
                        "内容自动撑满格子",
                        value=bool(row.get("auto_fit", False)),
                        key=f"tpl_row_auto_fit_{row_id}",
                    )
                with form_cols[1]:
                    heights[idx] = st.number_input("行高", 18, 120, int(heights[idx]), step=1, key=f"tpl_row_height_{row_id}")
                    row["label_font_size"] = st.number_input("左列字号", 8, 64, int(row.get("label_font_size", fonts.get("label", 25))), step=1, key=f"tpl_row_label_font_{row_id}")
                    row["font_size"] = st.number_input("内容字号", 8, 80, int(row.get("font_size", fonts.get("normal", 28))), step=1, key=f"tpl_row_value_font_{row_id}")
                    align_label = ALIGN_REVERSE.get(row.get("value_align", "center"), "中")
                    valign_label = VALIGN_REVERSE.get(row.get("value_valign", "middle"), "中")
                    row["value_align"] = ALIGN_MAP.get(
                        st.radio("内容对齐", ALIGN_OPTIONS, index=ALIGN_OPTIONS.index(align_label) if align_label in ALIGN_OPTIONS else 1, horizontal=True, key=f"tpl_row_align_{row_id}"),
                        "center",
                    )
                    row["value_valign"] = VALIGN_MAP.get(
                        st.radio("垂直对齐", VALIGN_OPTIONS, index=VALIGN_OPTIONS.index(valign_label) if valign_label in VALIGN_OPTIONS else 1, horizontal=True, key=f"tpl_row_valign_{row_id}"),
                        "middle",
                    )

        if st.button("新增一行", use_container_width=True, key="tpl_add_row_bottom"):
            _insert_row(template, len(template.get("rows", [])))
            st.session_state.grid_tpl_config = template
            _clear_row_editor_state()
            _clear_template_preview_state()
            st.rerun()
        layout["row_heights"] = heights[: len(template.get("rows", []))]
        _fit_row_heights(template)

    with setting_col:
        st.markdown("#### 版式")
        paper = template.setdefault("paper", {})
        b1, b2 = st.columns(2)
        with b1:
            paper["width_mm"] = st.number_input("宽度(mm)", 40, 120, int(paper.get("width_mm", 80)), step=1, key=_tpl_widget_key("tpl_paper_w"))
            layout["margin"] = st.number_input("边距(px)", 0, 80, int(layout.get("margin", 14)), step=1, key=_tpl_widget_key("tpl_margin"))
            layout["label_col_width"] = st.number_input("左列宽(px)", 40, 220, int(layout.get("label_col_width", 96)), step=1, key=_tpl_widget_key("tpl_label_col_width"))
        with b2:
            paper["height_mm"] = st.number_input("高度(mm)", 30, 120, int(paper.get("height_mm", 60)), step=1, key=_tpl_widget_key("tpl_paper_h"))
            layout["line_width"] = st.number_input("线宽(px)", 1, 8, int(layout.get("line_width", 3)), step=1, key=_tpl_widget_key("tpl_line_width"))
            layout["qr_x"] = st.number_input("二维码左侧分隔线位置(px)", 120, 600, int(layout.get("qr_x", 370)), step=1, key=_tpl_widget_key("tpl_qr_x"))
        paper["dpi"] = st.selectbox("DPI", [203, 300], index=0 if int(paper.get("dpi", 203)) == 203 else 1, key=_tpl_widget_key("tpl_dpi"))
        paper["dots_per_mm"] = 8 if int(paper.get("dpi", 203)) == 203 else 12
        template["font_family"] = st.selectbox(
            "模板字体",
            FONT_FAMILY_OPTIONS,
            index=FONT_FAMILY_OPTIONS.index(template.get("font_family", "微软雅黑")) if template.get("font_family", "微软雅黑") in FONT_FAMILY_OPTIONS else 0,
            key=_tpl_widget_key("tpl_font_family"),
        )

        st.markdown("#### 二维码")
        tpl_ui_key = str(st.session_state.get("grid_tpl_key", "current"))
        qr["enabled"] = st.checkbox("显示二维码", value=bool(qr.get("enabled", True)), key=f"{tpl_ui_key}_tpl_qr_enabled")
        qr["show_text"] = st.checkbox("显示二维码明文", value=bool(qr.get("show_text", True)), key=f"{tpl_ui_key}_tpl_qr_show_text")
        render_mode_options = {
            "Zebra原生二维码（推荐，实际打印更清晰）": "zpl_native",
            "图片二维码（备用，和预览完全一致）": "bitmap",
        }
        current_render_mode = qr.get("render_mode", "zpl_native")
        current_render_label = next((label for label, value in render_mode_options.items() if value == current_render_mode), list(render_mode_options.keys())[0])
        qr["render_mode"] = render_mode_options[
            st.selectbox(
                "二维码打印方式",
                list(render_mode_options.keys()),
                index=list(render_mode_options.keys()).index(current_render_label),
                key=f"{tpl_ui_key}_tpl_qr_render_mode",
            )
        ]
        row_names = [f"{idx + 1}. {row.get('label', '')}" for idx, row in enumerate(template.get("rows", []))]
        if row_names:
            top_idx = min(int(qr.get("top_row", 3)), len(row_names) - 1)
            bottom_idx = max(top_idx + 1, min(int(qr.get("bottom_row", len(row_names))), len(row_names)))
            qr["top_row"] = st.selectbox("二维码从哪一行开始", range(len(row_names)), index=top_idx, format_func=lambda i: row_names[i], key=f"{tpl_ui_key}_tpl_qr_top_row")
            qr["bottom_row"] = st.selectbox(
                "二维码到哪一行结束",
                range(qr["top_row"] + 1, len(row_names) + 1),
                index=max(0, bottom_idx - qr["top_row"] - 1),
                format_func=lambda i: "标签底部" if i == len(row_names) else row_names[i],
                key=f"{tpl_ui_key}_tpl_qr_bottom_row",
            )
        mode_options = {
            "客户标准：料号#日期箱号#数量": "customer",
            "字段自动拼接": "fields",
            "自定义表达式": "custom",
        }
        current_mode = qr.get("template_mode", "customer")
        current_mode_label = next((label for label, value in mode_options.items() if value == current_mode), list(mode_options.keys())[0])
        qr["template_mode"] = mode_options[
            st.selectbox(
                "二维码内容格式",
                list(mode_options.keys()),
                index=list(mode_options.keys()).index(current_mode_label),
                key=f"{tpl_ui_key}_tpl_qr_template_mode",
            )
        ]
        if qr["template_mode"] == "customer":
            qr["fields"] = CUSTOMER_QR_FIELDS[:]
            qr["text_fields"] = CUSTOMER_QR_FIELDS[:]
            qr["content_template"] = CUSTOMER_QR_TEMPLATE
            qr["text_template"] = CUSTOMER_QR_TEMPLATE
            st.info("当前二维码格式：料号#日期YYYYMMDD四位箱号#当前数量，例如 DEMO-A01#202611240001#100")
        elif qr["template_mode"] == "fields":
            qr["fields"] = _field_multiselect("二维码内容字段", qr.get("fields", []), _tpl_widget_key("qr_fields_multi"))
            qr["text_fields"] = _field_multiselect("二维码明文字段", qr.get("text_fields", []), _tpl_widget_key("qr_text_fields_multi"))
            qr["separator"] = st.text_input("字段分隔符", value=str(qr.get("separator", "#")) or "#", key=f"{tpl_ui_key}_tpl_qr_separator")
        else:
            qr["content_template"] = st.text_area(
                "二维码内容表达式",
                value=str(qr.get("content_template") or CUSTOMER_QR_TEMPLATE),
                height=86,
                key=f"{tpl_ui_key}_tpl_qr_content_template",
            )
            qr["text_template"] = st.text_input(
                "二维码明文表达式",
                value=str(qr.get("text_template") or qr.get("content_template") or CUSTOMER_QR_TEMPLATE),
                key=f"{tpl_ui_key}_tpl_qr_text_template",
            )
            st.caption("常用变量：{item_no}=料号，{print_date_compact}=YYYYMMDD，{box_index_4}=四位箱号，{box_qty_raw}=当前数量。")

        st.markdown("##### 预览位置")
        st.caption("先调这里。Zebra 原生二维码实际打印会以预览位置为基准。")
        q1, q2 = st.columns(2)
        with q1:
            qr["offset_x"] = st.number_input("二维码左右微调(dot)", -120, 120, int(qr.get("offset_x", 0)), step=1, key=f"{tpl_ui_key}_tpl_qr_offset_x")
            qr["padding_x"] = st.number_input("二维码左右留白(dot)", 0, 160, int(qr.get("padding_x", 46)), step=1, key=f"{tpl_ui_key}_tpl_qr_padding_x")
            qr["text_offset_x"] = st.number_input("明文左右微调(dot)", -80, 80, int(qr.get("text_offset_x", 0)), step=1, key=f"{tpl_ui_key}_tpl_qr_text_offset_x")
        with q2:
            qr["offset_y"] = st.number_input("二维码上下微调(dot)", -120, 120, int(qr.get("offset_y", 30)), step=1, key=f"{tpl_ui_key}_tpl_qr_offset_y")
            qr["padding_y"] = st.number_input("二维码上下留白(dot)", 0, 160, int(qr.get("padding_y", 32)), step=1, key=f"{tpl_ui_key}_tpl_qr_padding_y")
            qr["text_offset_y"] = st.number_input("明文上下微调(dot)", -80, 80, int(qr.get("text_offset_y", 0)), step=1, key=f"{tpl_ui_key}_tpl_qr_text_offset_y")
        qr["size"] = st.number_input("二维码固定尺寸(dot，0为自动)", 0, 260, int(qr.get("size", 0)), step=1, key=f"{tpl_ui_key}_tpl_qr_size")
        qr["text_bottom"] = st.number_input("明文离底部距离(dot)", 0, 80, int(qr.get("text_bottom", 22)), step=1, key=f"{tpl_ui_key}_tpl_qr_text_bottom")
        fonts["qr_text"] = st.number_input("明文字号", 8, 36, int(fonts.get("qr_text", 18)), step=1, key=f"{tpl_ui_key}_tpl_qr_text_font")

        with st.expander("Zebra 原生二维码实际打印微调", expanded=False):
            st.caption("这里是相对预览位置的细调。每次 +/- 1 只移动 1 个 dot，203dpi 下约 0.125mm。")
            nq1, nq2, nq3 = st.columns(3)
            with nq1:
                qr["zpl_offset_x"] = st.number_input("ZPL二维码左右(dot)", -60, 60, int(qr.get("zpl_offset_x", 0)), step=1, key=f"{tpl_ui_key}_tpl_qr_zpl_offset_x")
            with nq2:
                qr["zpl_offset_y"] = st.number_input("ZPL二维码上下(dot)", -60, 60, int(qr.get("zpl_offset_y", 0)), step=1, key=f"{tpl_ui_key}_tpl_qr_zpl_offset_y")
            with nq3:
                qr["zpl_scale"] = st.number_input("ZPL二维码大小系数", 1, 10, int(qr.get("zpl_scale", 4)), step=1, key=f"{tpl_ui_key}_tpl_qr_zpl_scale")
            nt1, nt2 = st.columns(2)
            with nt1:
                qr["zpl_text_offset_x"] = st.number_input("ZPL明文左右(dot)", -80, 80, int(qr.get("zpl_text_offset_x", 0)), step=1, key=f"{tpl_ui_key}_tpl_qr_zpl_text_offset_x")
            with nt2:
                qr["zpl_text_offset_y"] = st.number_input("ZPL明文上下(dot)", -80, 80, int(qr.get("zpl_text_offset_y", 0)), step=1, key=f"{tpl_ui_key}_tpl_qr_zpl_text_offset_y")

        special_marks = template.setdefault("special_marks", {})
        with st.expander("尾箱 / 备品标识", expanded=False):
            tail_mark = special_marks.setdefault("tail", {})
            spare_mark = special_marks.setdefault("spare", {})
            mark_cols = st.columns(2)
            with mark_cols[0]:
                tail_mark["text"] = st.text_input("尾箱文字", value=str(tail_mark.get("text", "尾箱")), key=_tpl_widget_key("tpl_tail_mark_text"))
                tail_mark["font_scale"] = st.slider("尾箱放大倍数", 1.0, 2.0, float(tail_mark.get("font_scale", 1.35)), 0.05, key=_tpl_widget_key("tpl_tail_mark_scale"))
                tail_mark["bold"] = True
            with mark_cols[1]:
                spare_mark["text"] = st.text_input("备品文字", value=str(spare_mark.get("text", "备品")), key=_tpl_widget_key("tpl_spare_mark_text"))
                spare_mark["font_scale"] = st.slider("备品放大倍数", 1.0, 2.0, float(spare_mark.get("font_scale", 1.35)), 0.05, key=_tpl_widget_key("tpl_spare_mark_scale"))
                spare_mark["bold"] = True

        with st.expander("打印机高级参数", expanded=False):
            clarity_options = {"标准": "standard", "锐化文字": "sharp_text", "粗黑文字": "bold_text", "高对比点阵": "high_contrast"}
            current_clarity = zpl.get("clarity_mode", "standard")
            current_clarity_label = next((label for label, value in clarity_options.items() if value == current_clarity), "标准")
            zpl["clarity_mode"] = clarity_options[
                st.selectbox("清晰度模式", list(clarity_options.keys()), index=list(clarity_options.keys()).index(current_clarity_label), key=_tpl_widget_key("tpl_clarity_mode"))
            ]
            zpl["darkness"] = st.slider("ZPL浓度", 0, 30, int(zpl.get("darkness", 18)), key=_tpl_widget_key("tpl_zpl_darkness"))
            zpl["speed"] = st.slider("ZPL速度", 1, 8, int(zpl.get("speed", 3)), key=_tpl_widget_key("tpl_zpl_speed"))
            direction = st.radio(
                "打印方向",
                ["横向输出 80x60", "旋转90度输出 60x80"],
                index=1 if bool(zpl.get("rotate_for_media", False)) else 0,
                key=_tpl_widget_key("tpl_zpl_direction"),
            )
            zpl["rotate_for_media"] = direction.startswith("旋转90度")
            zpl["force_rotate"] = zpl["rotate_for_media"]

        layout["qr_draw_boundary"] = True
        _sync_qr_templates(template)
        _fit_row_heights(template)
        validation = validate_grid_layout(template)
        st.markdown("#### 生产校验")
        st.caption(f"行高: {validation['row_height_used']} / {validation['row_height_available']} dot")
        for issue in validation["issues"]:
            if issue["severity"] == "error":
                st.error(issue["message"])
            else:
                st.warning(issue["message"])
        qr_placement = inspect_grid_qr_placement(template, {})
        for issue in qr_placement["issues"]:
            st.warning(issue["message"])
        st.session_state.grid_tpl_config = template

    with preview_col:
        st.markdown("#### 打印预览")
        st.caption("参数变化后会按当前模板签名自动刷新；按钮可用于手动重画。")
        preview_type_options = {"满箱": "full", "尾箱": "tail", "备品": "spare"}
        preview_type_label = st.selectbox("预览类型", list(preview_type_options.keys()), key="tpl_preview_box_type")
        preview_box_type = preview_type_options[preview_type_label]
        signature = get_template_signature(template)
        preview_key = f"grid_tpl_preview::{st.session_state.get('grid_tpl_key', 'current')}::{preview_box_type}::{signature}"
        refresh_preview = st.button("刷新预览", type="primary", use_container_width=True, key="tpl_refresh_preview")
        if refresh_preview or preview_key not in st.session_state:
            try:
                st.session_state[preview_key] = _image_bytes(_render_preview(template, preview_box_type))
            except Exception as exc:
                st.error(f"预览失败：{exc}")
                st.session_state.pop(preview_key, None)
        if preview_key in st.session_state:
            stale_note = "未保存修改" if signature != saved_signature else "已保存版本"
            st.caption(f"当前预览签名: {signature} | {stale_note}")
            st.image(st.session_state[preview_key], caption="最终打印引擎预览", use_container_width=True)

        st.markdown("#### 实物测试打印")
        st.caption("直接发送当前模板样张到本机 Zebra。用于校对真实打印效果。")
        test_copies = st.number_input("实物测试张数", 1, 500, 1, key="tpl_browser_test_copies")
        try:
            test_zpl_one = render_label_zpl(
                template,
                _sample_fields(),
                current_box_index=0 if preview_box_type == "spare" else 1,
                total_boxes=3,
                current_box_qty=1500 if preview_box_type == "tail" else 0 if preview_box_type == "spare" else 2000,
                std_qty=2000,
                box_type=preview_box_type,
                print_time=datetime.datetime(2026, 5, 20, 10, 0, 0),
                shipment_time="20260520100000",
            )
            st.caption(f"测试 ZPL 使用模板签名: {signature}")
            _browser_print_test_panel(test_zpl_one * int(test_copies), f"{st.session_state.get('grid_tpl_key', 'current')}_{preview_box_type}_{int(test_copies)}_{signature}")
        except Exception as exc:
            st.error(f"生成本机测试 ZPL 失败：{exc}")

        with st.expander("服务器打印机备用测试", expanded=False):
            st.caption("这里读取的是服务器电脑能看到的 Windows 打印机。模板实物测试优先使用上面的本机 Zebra Browser Print。")
            printers = list_printers()
            default_printer = get_default_printer()
            if printers:
                default_index = printers.index(default_printer) if default_printer in printers else 0
                test_printer = st.selectbox("服务器测试打印机", printers, index=default_index, key="tpl_test_printer")
            else:
                test_printer = st.text_input("服务器测试打印机", value=default_printer or "", key="tpl_test_printer_text")
                st.warning("没有读取到服务器打印机列表，可以手动输入 Windows 里的 Zebra 打印机名称。")
            copies = st.number_input("服务器测试张数", 1, 500, 1, key="tpl_test_copies")
            if st.button("服务器直打当前模板测试页", type="secondary", use_container_width=True, key="tpl_server_test_print"):
                if not test_printer:
                    st.error("请先选择或输入服务器打印机。")
                else:
                    try:
                        zpl_text = render_label_zpl(
                            template,
                            _sample_fields(),
                            current_box_index=0 if preview_box_type == "spare" else 1,
                            total_boxes=3,
                            current_box_qty=1500 if preview_box_type == "tail" else 0 if preview_box_type == "spare" else 2000,
                            std_qty=2000,
                            box_type=preview_box_type,
                            print_time=datetime.datetime(2026, 5, 20, 10, 0, 0),
                            shipment_time="20260520100000",
                        )
                        ok, msg = print_zpl(zpl_text, test_printer, copies=copies)
                        if ok:
                            st.success(msg)
                        else:
                            st.error(msg)
                    except Exception as exc:
                        st.error(f"模板服务器测试打印失败：{exc}")
