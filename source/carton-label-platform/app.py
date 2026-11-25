# -*- coding: utf-8 -*-
"""
app.py —— 箱贴打印与追溯系统 v3

v3 核心变化(都集中在 Tab3 打印预览):
    1. 累计箱号:同 工艺单+料号 跨次打印自动累加箱号
    2. 超量保护:已打印 + 本次 > 订货 时阻塞打印
    3. 尾数三级:满箱 / 尾箱 / 尾包 自动分流
    4. 单张实时预览:改字段或参数实时刷新预览图
    5. 重打模式:不占新箱号
    6. 移除"每箱数量""总箱数"的字段池入口,改由装箱参数自动算
"""

import streamlit as st
import pandas as pd
import io
import os
import tempfile
import datetime
import copy
import base64
import json
import html
import importlib
import uuid
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from PIL import ImageDraw
import streamlit.components.v1 as components

from db_manager import (
    fetch_production_data,
    fetch_shipment_by_process_no,
    authenticate_user,
    init_shadow_db,
    insert_to_shadow_db,
    get_pending_records,
    confirm_shadow_record,
    confirm_shadow_records_batch,
    delete_shadow_record,
    delete_shadow_records_batch,
    log_print_action,
    get_print_history,
    fetch_system_print_history,
    get_print_templates,
    save_print_template,
    calculate_box_distribution,
)
try:
    from db_manager import fetch_trace_print_logs
except ImportError:
    fetch_trace_print_logs = None
try:
    from db_manager import fetch_trace_records
except ImportError:
    fetch_trace_records = None
DB_MANAGER_FILE = "未知"
try:
    DB_MANAGER_FILE = getattr(importlib.import_module("db_manager"), "__file__", "未知")
except Exception:
    pass
DB_MANAGER_ACCOUNT_IMPORT_ERROR = None
try:
    from db_manager import list_system_users, save_system_user, delete_system_user, PERMISSION_MODULES
except ImportError as exc:
    DB_MANAGER_ACCOUNT_IMPORT_ERROR = str(exc)
    list_system_users = None
    save_system_user = None
    delete_system_user = None
    PERMISSION_MODULES = {
        "label_print": "标签打印",
        "scan_in": "扫码入库",
        "scan_out": "扫码出库",
        "trace_query": "溯源查询",
        "template": "打印模板",
        "accounts": "账号管理",
    }
try:
    from db_manager import record_scan_in, fetch_scan_in_logs, parse_qr_content
except ImportError:
    record_scan_in = None
    fetch_scan_in_logs = None
    parse_qr_content = None
try:
    from db_manager import record_scan_out, fetch_scan_out_logs
except ImportError:
    record_scan_out = None
    fetch_scan_out_logs = None
from label_engine import (
    generate_single_label,
    build_single_label_zpl,
    make_default_fields_from_data,
)
from template_engine import (
    list_builtin_templates,
    get_default_template,
    render_label_image,
    render_tray_label_image,
    render_tray_label_zpl,
    complete_tray_editor_rows,
    normalize_tray_label_rows,
    ensure_canvas_template,
    build_render_context,
    build_qr_content_from_context,
    get_template_signature,
)



init_shadow_db()


PRINT_JOB_CACHE = {}
PRINT_LOGGED_TOKENS = set()
PRINT_JOB_LOCK = threading.Lock()
PRINT_CALLBACK_PORT = 8601
PRINT_CALLBACK_PORT_MAX = 8610


TEMPLATE_FIELD_OPTIONS = {
    "SAP号": "sap_no",
    "产品名称": "product_name",
    "料号": "item_no",
    "订单号": "order_no",
    "订单数量": "order_qty",
    "包装方式": "pack_method",
    "供应商": "supplier",
    "日期": "print_date",
    "日期YYYYMMDD": "print_date_compact",
    "打印时间": "print_time_compact",
    "发货时间": "shipment_time_compact",
    "打印人": "operator_name",
    "每箱数量": "box_qty",
    "每箱数量纯数字": "box_qty_raw",
    "当前箱序号": "box_index",
    "当前箱序号4位": "box_index_4",
    "第N箱/总箱数": "box_index_total",
    "总箱数": "total_boxes",
    "标准装量": "std_qty",
}
TEMPLATE_FIELD_LABELS = {value: label for label, value in TEMPLATE_FIELD_OPTIONS.items()}


def fields_to_template(selected_labels, separator="#", shipment_prefix=False):
    parts = []
    for label in selected_labels:
        key = TEMPLATE_FIELD_OPTIONS.get(label, label)
        token = "{" + key + "}"
        if shipment_prefix and key == "shipment_time_compact":
            token = "SHIP" + token
        parts.append(token)
    return separator.join(parts)


def clear_print_work_state():
    for key in [
        'ready_to_print', 'batch_print_records', 'print_fields',
        'generated_images', 'generated_qrs', 'generated_meta',
        'generated_zpls', 'print_time_used', 'print_context',
    ]:
        st.session_state.pop(key, None)


def make_fields_for_record(record):
    sap_no = str(record.get('sap_no', '') or '').strip()
    if sap_no in ('0', '0.0', '0.00'):
        sap_no = ''
    product_data = {
        'SAP号': sap_no,
        '料号': record.get('item_no', ''),
        '品名': record.get('product_name', ''),
        '订货数量': record.get('order_qty', 0),
        '最终成品数': record.get('done_qty', 0),
        '模数': record.get('modulus', 0),
        '包装方式': record.get('pack_method', '') or record.get('包装方式', ''),
    }
    order_header = {
        '定单号码': record.get('order_no', ''),
        '客户名称': record.get('customer', ''),
    }
    return make_default_fields_from_data(product_data, order_header, sap_no=sap_no)


def build_box_distribution_for_range(start_box, end_box, total_boxes, std_qty, order_qty):
    """按箱号范围生成箱贴分布。用于正常补打某段,也用于重打某段。"""
    start_box = int(start_box)
    end_box = int(end_box)
    total_boxes = int(total_boxes or 0)
    std_qty = int(std_qty or 0)
    order_qty = int(order_qty or 0)
    rows = []
    if start_box <= 0 or end_box < start_box or std_qty <= 0:
        return rows

    for box_no in range(start_box, end_box + 1):
        qty = std_qty
        box_type = "full"
        if order_qty > 0 and total_boxes > 0 and box_no == total_boxes:
            tail_qty = order_qty - std_qty * (total_boxes - 1)
            if 0 < tail_qty < std_qty:
                qty = tail_qty
                box_type = "tail"
        rows.append({"box_index": box_no, "qty": int(qty), "box_type": box_type})
    return rows


def append_special_label(generated_images, generated_zpls, generated_qrs, generated_meta,
                         fields, total_boxes, qty, std_qty, pack_info,
                         print_time, shipment_time_for_qr, template, base_meta,
                         box_type, skip_log=True):
    """追加一张特殊箱贴。特殊贴不占订单箱号,默认不参与订单累计。"""
    qty = int(qty or 0)
    special_fields = copy.deepcopy(fields)
    generated_images.append(generate_single_label(
        fields=copy.deepcopy(special_fields),
        current_box_index=0,
        total_boxes=total_boxes,
        current_box_qty=qty,
        std_qty=std_qty,
        pack_info=pack_info,
        box_type=box_type,
        print_time=print_time,
        shipment_time=shipment_time_for_qr,
        template=template,
    ))
    generated_zpls.append(build_single_label_zpl(
        fields=copy.deepcopy(special_fields),
        current_box_index=0,
        total_boxes=total_boxes,
        current_box_qty=qty,
        std_qty=std_qty,
        pack_info=pack_info,
        box_type=box_type,
        print_time=print_time,
        shipment_time=shipment_time_for_qr,
        template=template,
    ))
    special_ctx = build_render_context(
        special_fields, 0, total_boxes, qty, std_qty,
        pack_info=pack_info,
        box_type=box_type,
        print_time=print_time,
        shipment_time=shipment_time_for_qr,
        template=template,
    )
    qr_content = build_qr_content_from_context(template, special_ctx)
    generated_qrs.append(qr_content)
    meta = copy.deepcopy(base_meta)
    meta.update({
        'box_index': 0,
        'qty': qty,
        'std_qty': std_qty,
        'box_type': box_type,
        'total_boxes': total_boxes,
        'shipment_time': shipment_time_for_qr,
        'skip_log': bool(skip_log),
    })
    generated_meta.append(meta)


def append_special_labels(generated_images, generated_zpls, generated_qrs, generated_meta,
                          fields, total_boxes, std_qty, pack_info,
                          print_time, shipment_time_for_qr, template, base_meta,
                          special_counts):
    for box_type, count in special_counts.items():
        for _ in range(max(0, int(count or 0))):
            append_special_label(
                generated_images, generated_zpls, generated_qrs, generated_meta,
                fields, total_boxes, 0, std_qty, pack_info,
                print_time, shipment_time_for_qr, template, base_meta,
                box_type=box_type,
                skip_log=True,
            )


def append_tray_labels(generated_images, generated_zpls, generated_qrs, generated_meta,
                       tray_rows, label_date, base_meta):
    """追加拖贴。拖贴记录到打印流水,但由 db_manager 保证不计入箱贴累计。"""
    printable_rows = normalize_tray_label_rows(tray_rows, label_date)
    for row in printable_rows:
        generated_images.append(render_tray_label_image(
            item_no=row["item_no"],
            label_date=label_date,
            tray_no=row["tray_no"],
            qty=row["qty"],
        ))
        generated_zpls.append(render_tray_label_zpl(
            item_no=row["item_no"],
            label_date=label_date,
            tray_no=row["tray_no"],
            qty=row["qty"],
        ))
        generated_qrs.append(row["qr_content"])
        meta = copy.deepcopy(base_meta)
        meta.update({
            'item_no': row["item_no"],
            'box_index': row["tray_no"],
            'qty': row["qty"],
            'std_qty': None,
            'box_type': 'tray',
            'total_boxes': 0,
            'shipment_time': label_date.strftime("%Y%m%d") if hasattr(label_date, "strftime") else str(label_date),
            'tray_copy_index': row["copy_index"],
            'tray_copies': row["copies"],
            'skip_log': False,
        })
        generated_meta.append(meta)
    return printable_rows


def fields_with_print_date(fields, print_time):
    """生成箱贴前把标签上的日期同步为本次打印/重打选择的日期。"""
    updated = copy.deepcopy(fields)
    date_text = (print_time or datetime.datetime.now()).strftime("%Y.%m.%d")
    found = False
    for field in updated:
        if field.get("key") == "date":
            field["value"] = date_text
            found = True
            break
    if not found:
        updated.append({
            "key": "date",
            "label": "日期",
            "value": date_text,
            "visible": True,
            "editable": True,
        })
    return updated


def write_generated_print_logs(generated_qrs, generated_meta, print_ctx, printer_name, current_user):
    """把已打印箱贴写入 SQL Server 累计表。备品跳过,重打按不计累计写入。"""
    action = 'tray' if print_ctx.get('tray_mode') else 'reprint' if print_ctx.get('reprint_mode') else 'new'
    count = 0
    for qr_c, meta in zip(generated_qrs, generated_meta):
        if meta.get('skip_log'):
            continue
        log_print_action(
            process_no=meta.get('process_no', ''),
            order_no=meta.get('order_no', ''),
            item_no=meta.get('item_no', ''),
            product_name=meta.get('product_name', ''),
            box_index=meta.get('box_index', 0),
            total_boxes=meta.get('total_boxes', 0),
            qty_in_box=meta.get('qty', 0),
            std_qty=meta.get('std_qty'),
            printer_name=printer_name,
            qr_content=qr_c,
            shipment_time=meta.get('shipment_time', ''),
            operator=current_user.get('username', 'system'),
            action_type=action,
            box_type=meta.get('box_type', 'full'),
        )
        count += 1
    return count


def ensure_operator_field(fields, current_user, visible=False):
    operator_value = current_user.get('display_name') or current_user.get('username') or ''
    for field in fields:
        if field.get('key') == 'operator_name':
            field['value'] = operator_value
            field['visible'] = bool(visible)
            return fields
    fields.append({
        'key': 'operator_name',
        'label': '打印人',
        'value': operator_value,
        'visible': bool(visible),
        'editable': True,
    })
    return fields


def prepare_template_for_operator(template, show_operator):
    tpl = copy.deepcopy(template)
    if not show_operator:
        rows = tpl.get('rows', [])
        heights = tpl.get('layout', {}).get('row_heights', [])
        keep = [(idx, row) for idx, row in enumerate(rows) if row.get('field') != 'operator_name']
        if len(keep) != len(rows):
            tpl['rows'] = [row for _, row in keep]
            if heights:
                tpl.setdefault('layout', {})['row_heights'] = [
                    heights[idx] for idx, _ in keep if idx < len(heights)
                ]
        return tpl
    rows = tpl.setdefault('rows', [])
    if any(row.get('field') == 'operator_name' for row in rows):
        return tpl
    fonts = tpl.setdefault('fonts', {})
    rows.append({
        'label': '打印人',
        'field': 'operator_name',
        'font': 'normal',
        'label_font_size': int(fonts.get('label', 22)),
        'font_size': int(fonts.get('normal', 22)),
        'label_align': 'center',
        'label_valign': 'middle',
        'value_align': 'center',
        'value_valign': 'middle',
        'qr_side': False,
    })
    layout = tpl.setdefault('layout', {})
    heights = layout.setdefault('row_heights', [])
    heights.append(42)
    paper = tpl.setdefault('paper', {})
    dots_per_mm = int(paper.get('dots_per_mm', 8))
    available = int(paper.get('height_mm', 60) * dots_per_mm) - int(layout.get('margin', 14)) * 2
    total = sum(int(h) for h in heights)
    if total > available and total > 0:
        scale = available / total
        layout['row_heights'] = [max(18, int(int(h) * scale)) for h in heights]
    return tpl


def render_basic_summary(summary):
    header = summary.get('order_header', {})
    st.markdown("### 搜索结果")
    st.caption("这里只展示本次检索真正命中的产品，确认后会同步进入下方待打印数据。")
    rows = []
    for p in summary.get('products', []):
        rows.append({
            '工艺单号': summary.get('process_no', ''),
            '定单号码': p.get('定单号码') or header.get('定单号码', ''),
            '料号': p.get('料号', ''),
            '品名': p.get('品名', ''),
            '订货数量': p.get('订货数量', 0),
            '最终成品数': p.get('最终成品数', 0),
            '箱规': p.get('箱规', 0),
            '入库时间': p.get('最近入库时间', ''),
            '出货时间': p.get('最近出货时间', ''),
            '最后工序': p.get('最后工序名称', ''),
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("本次搜索没有可展示的产品明细。")


def render_pending_quick_panel(current_user, key_prefix="quick"):
    pending_df = get_pending_records()
    keyword = st.session_state.get(f"{key_prefix}_pending_filter_keyword", "")

    if pending_df.empty:
        st.info("当前没有待打印数据。")
        return

    filtered_df = pending_df.copy()
    if keyword.strip():
        pattern = keyword.strip().lower()
        mask = pd.Series(False, index=filtered_df.index)
        for col in ["process_no", "order_no", "item_no", "sap_no", "product_name", "customer"]:
            if col in filtered_df.columns:
                mask = mask | filtered_df[col].astype(str).str.lower().str.contains(pattern, na=False)
        filtered_df = filtered_df[mask]

    if filtered_df.empty:
        st.warning("没有匹配的待打印记录。")
        return

    display_df = filtered_df.copy().reset_index(drop=True)
    display_df.insert(0, "选择", False)
    display_df.insert(1, "序号", range(1, len(display_df) + 1))
    rename_map = {
        "id": "真实ID",
        "process_no": "工艺单号",
        "order_no": "订单号",
        "item_no": "料号",
        "sap_no": "SAP号",
        "product_name": "产品名称",
        "order_qty": "订货数量",
        "done_qty": "完工数量",
        "box_spec": "箱规",
        "stock_in_time": "入库时间",
        "stock_out_time": "出货时间",
        "last_process": "最后工序",
        "customer": "客户",
        "created_at": "创建时间",
        "source_synced_at": "ERP同步时间",
    }
    display_cols = [
        c for c in [
            "选择", "序号", "id", "process_no", "order_no", "item_no",
            "sap_no", "product_name", "order_qty", "done_qty", "box_spec",
            "stock_in_time", "stock_out_time", "last_process",
            "customer", "source_synced_at", "created_at",
        ] if c in display_df.columns
    ]
    table_df = display_df[display_cols].rename(columns=rename_map)
    edited_df = st.data_editor(
        table_df,
        use_container_width=True,
        hide_index=True,
        disabled=[c for c in table_df.columns if c != "选择"],
        column_config={
            "选择": st.column_config.CheckboxColumn("选择"),
            "真实ID": st.column_config.NumberColumn("真实ID"),
        },
        key=f"{key_prefix}_pending_editor",
    )
    selected_ids = edited_df.loc[edited_df["选择"], "真实ID"].astype(int).tolist()
    selected_records = filtered_df[filtered_df["id"].isin(selected_ids)].to_dict("records")

    a1, a2, a3, a4 = st.columns([1.3, 1.2, 1.2, 4])
    with a1:
        if st.button("确认并在本页打印", type="primary", disabled=not selected_records, key=f"{key_prefix}_confirm_print"):
            confirm_shadow_records_batch(selected_ids, operator=current_user.get("username", "system"))
            clear_print_work_state()
            st.session_state["ready_to_print"] = selected_records[0]
            if len(selected_records) > 1:
                st.session_state["batch_print_records"] = selected_records
            st.success("已确认，下面直接生成箱贴并打印。")
            st.rerun()
    with a2:
        if st.button("刷新", key=f"{key_prefix}_refresh", use_container_width=True):
            st.rerun()
    with a3:
        confirm_delete = st.checkbox("确认删除", key=f"{key_prefix}_confirm_delete")
    with a4:
        if st.button("删除选中", disabled=(not selected_records) or (not confirm_delete), key=f"{key_prefix}_delete"):
            n = delete_shadow_records_batch(selected_ids, operator=current_user.get("username", "system"))
            st.success(f"已删除 {n} 条待打印数据")
            st.rerun()
        st.caption(f"当前显示 {len(filtered_df)} 条，已选择 {len(selected_records)} 条。")

def asset_data_uri(path, mime="image/png"):
    try:
        with open(path, "rb") as f:
            return f"data:{mime};base64,{base64.b64encode(f.read()).decode('ascii')}"
    except Exception:
        return ""


st.set_page_config(page_title="箱贴打印与追溯系统", layout="wide")
COMPANY_LOGO_URI = ""
st.markdown(
    """
    <style>
    html, body, [class*="css"] { font-size: 17px; }
    .block-container { padding-top: .75rem; padding-bottom: 1.2rem; max-width: 100%; }
    .stTextInput input, .stNumberInput input, .stSelectbox div[data-baseweb="select"] {
        min-height: 46px;
        font-size: 17px;
    }
    .stButton button, .stDownloadButton button {
        min-height: 46px;
        font-size: 17px;
        border-radius: 8px;
    }
    [data-testid="stForm"] {
        max-width: 480px;
        margin: 24px auto 0 auto;
        border-radius: 12px;
    }
    [data-testid="stForm"] input { min-height: 48px; }
    div[data-testid="stDataFrame"] { font-size: 16px; }
    h1, h2, h3 { letter-spacing: 0; }
    div[data-testid="stMarkdownContainer"] p { margin-bottom: .25rem; }
    hr { margin: .85rem 0; }
    div[data-testid="stVerticalBlock"] > div:has(> .section-divider) { margin-top: .4rem; }
    .lyc-topbar {
        display:flex; align-items:center; gap:12px; margin: 0 0 12px 0;
        font-weight:700; color:#111827;
    }
    .lyc-logo { width:38px; height:38px; object-fit:contain; }
    .lyc-brand { font-size: 24px; }
    .lyc-badge { font-size:14px; color:#6b7280; border:1px solid #e5e7eb; border-radius:999px; padding:3px 10px; }
    .lyc-home-shell {
        min-height: calc(100vh - 32px);
        margin: -10px -24px -20px -24px;
        padding: 22px clamp(22px, 7vw, 96px) 56px;
        background: radial-gradient(circle at 55% 20%, #241654 0, #151236 42%, #0c1024 100%);
        color: #f8fafc;
    }
    .lyc-home-top {
        display:flex; justify-content:space-between; align-items:center;
        padding-bottom:18px; border-bottom:1px solid rgba(255,255,255,.1);
    }
    .lyc-home-brand { display:flex; align-items:center; gap:12px; font-size:22px; font-weight:800; }
    .lyc-home-logo { width:150px; height:54px; object-fit:contain; background:#fff; border-radius:10px; padding:6px; }
    .lyc-home-user { color:#cbd5e1; font-size:14px; }
    .lyc-home-title { text-align:center; margin:42px 0 30px 0; }
    .lyc-home-title h1 { font-size:34px; margin:0; color:#fff; }
    .lyc-home-title p { margin-top:8px; color:#a5b4fc; }
    .lyc-card-grid { display:grid; grid-template-columns:repeat(2,minmax(260px,1fr)); gap:22px; max-width:980px; margin:0 auto; }
    .lyc-home-card {
        display:block;
        text-decoration:none !important;
        border:1px solid rgba(255,255,255,.13);
        background:rgba(255,255,255,.075);
        border-radius:14px; padding:26px; min-height:172px;
        box-shadow:0 18px 40px rgba(0,0,0,.2);
        transition: transform .12s ease, background .12s ease, border-color .12s ease;
    }
    .lyc-home-card:hover {
        transform: translateY(-2px);
        background:rgba(255,255,255,.105);
        border-color:rgba(255,255,255,.28);
    }
    .lyc-home-icon {
        width:54px; height:54px; border-radius:12px; display:flex; align-items:center; justify-content:center;
        font-size:26px; margin-bottom:18px;
    }
    .lyc-home-card h3 { color:#fff; font-size:22px; margin:0 0 8px 0; }
    .lyc-home-card p { color:#aeb8d6; font-size:15px; min-height:26px; margin:0 0 18px 0; }
    .lyc-home-card .enter { color:#dbeafe; font-size:15px; }
    .lyc-home-actions { display:flex; gap:10px; flex-wrap:wrap; }
    .lyc-home-actions span {
        display:inline-flex; align-items:center; min-height:30px; padding:4px 10px;
        border:1px solid rgba(219,234,254,.34); border-radius:999px; color:#dbeafe; font-size:14px;
    }
    .lyc-hero {
        border:1px solid #e5e7eb; border-radius:8px; padding:14px 16px; margin:8px 0 16px 0;
        background:#fff;
    }
    .lyc-section-title { font-size:28px; font-weight:750; margin:16px 0 6px 0; }
    .lyc-muted { color:#6b7280; font-size:16px; margin-bottom:10px; }
    .lyc-print-title { font-size:28px; font-weight:750; margin:10px 0 10px 0; }
    .lyc-status-row {
        display:grid; grid-template-columns: repeat(3, minmax(0, 1fr));
        gap:10px; margin: 4px 0 8px 0;
    }
    .lyc-status-card {
        border:1px solid #e5e7eb; border-radius:8px; padding:8px 10px; background:#fff;
    }
    .lyc-status-label { color:#6b7280; font-size:12px; margin-bottom:2px; }
    .lyc-status-value { color:#111827; font-size:20px; font-weight:650; line-height:1.2; }
    @media (max-width: 760px) {
        .lyc-card-grid { grid-template-columns: 1fr; }
        .lyc-home-shell { margin-left:-16px; margin-right:-16px; padding:18px; }
    }
    </style>
    <div class="lyc-topbar">
      __LOGO_HTML__
      <div class="lyc-brand">箱贴打印与追溯系统</div>
      <div class="lyc-badge">标签打印</div>
      <div class="lyc-badge">Zebra ZD888</div>
    </div>
    """.replace("__LOGO_HTML__", f'<img class="lyc-logo" src="{COMPANY_LOGO_URI}">' if COMPANY_LOGO_URI else ""),
    unsafe_allow_html=True,
)


def infer_search_type(keyword):
    text = str(keyword or "").strip()
    if text.isdigit():
        return "工艺单号"
    upper_text = text.upper()
    if upper_text.startswith("CG") or upper_text.startswith("PO"):
        return "订单号"
    return "料号"


def require_login():
    if 'user' in st.session_state:
        return st.session_state['user']

    st.markdown(
        f"""
        <div style="max-width:520px;margin:28px auto 6px auto;text-align:center;">
            {'<img src="' + COMPANY_LOGO_URI + '" style="width:190px;max-height:88px;object-fit:contain;margin-bottom:12px;">' if COMPANY_LOGO_URI else ''}
            <h1 style="font-size:30px;margin:0;color:#111827;">箱贴生产追溯系统</h1>
            <p style="color:#6b7280;margin-top:8px;">登录后选择要进入的功能板块</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.form("login_form"):
        username = st.text_input("账号")
        password = st.text_input("密码", type="password")
        submitted = st.form_submit_button("登录", type="primary", use_container_width=True)
    if submitted:
        try:
            user = authenticate_user(username, password)
        except Exception as e:
            st.error(f"登录服务连接失败:{e}")
            st.stop()
        if user:
            st.session_state['user'] = user
            st.rerun()
        else:
            st.error("账号或密码不正确，或账号已停用")
    st.stop()


def go_module(module):
    st.session_state["active_module"] = module
    st.query_params["module"] = module
    st.rerun()


def user_can_access(current_user, module_code):
    if module_code == "home":
        return True
    if current_user.get("role_code") == "admin":
        return True
    permissions = current_user.get("permissions")
    if permissions is None:
        permissions = ["label_print", "scan_in", "scan_out", "trace_query"]
    return module_code in set(permissions)


def require_module_access(current_user, module_code):
    if user_can_access(current_user, module_code):
        return
    st.error("当前账号没有进入这个板块的权限，请联系管理员开通。")
    if st.button("返回功能选择", key=f"deny_back_{module_code}"):
        go_module("home")
    st.stop()


def render_home_page(current_user, is_admin):
    user_label = f"{current_user.get('display_name')} ({current_user.get('role_code')})"
    st.markdown(
        f"""
        <style>
          .stApp {{
            background: radial-gradient(circle at 55% 20%, #241654 0, #151236 42%, #0c1024 100%);
          }}
          .block-container {{ padding-top: 24px; max-width: 1180px; }}
          .lyc-home-top {{
            display:flex; justify-content:space-between; align-items:center;
            padding-bottom:18px; border-bottom:1px solid rgba(255,255,255,.1);
          }}
          .lyc-home-logo {{ width:260px; height:86px; object-fit:contain; background:#fff; border-radius:12px; padding:8px; }}
          .lyc-home-user {{ color:#cbd5e1; font-size:17px; font-weight:650; }}
          .lyc-home-title {{ text-align:center; margin:56px 0 34px 0; }}
          .lyc-home-title h1 {{ font-size:42px; margin:0; color:#fff; }}
          .lyc-home-title p {{ margin-top:10px; color:#a5b4fc; font-size:20px; }}
          div[data-testid="stVerticalBlockBorderWrapper"] {{
            background: rgba(255,255,255,.08);
            border: 1px solid rgba(255,255,255,.16);
            border-radius: 16px;
            box-shadow: 0 18px 40px rgba(0,0,0,.2);
          }}
          .lyc-home-card {{
            min-height: 170px;
            padding: 10px 8px 4px;
          }}
          .lyc-home-icon {{
            width:58px; height:58px; border-radius:13px;
            display:flex; align-items:center; justify-content:center;
            font-size:28px; margin-bottom:20px;
          }}
          .lyc-home-card h3 {{ color:#fff; font-size:24px; margin:0 0 10px 0; }}
          .lyc-home-card p {{ color:#aeb8d6; font-size:17px; min-height:32px; margin:0 0 20px 0; }}
          .lyc-home-actions span {{
            display:inline-flex; align-items:center; min-height:32px; padding:4px 12px;
            border:1px solid rgba(219,234,254,.34); border-radius:999px;
            color:#dbeafe; font-size:15px; font-family:inherit;
          }}
          .stButton button {{
            background: transparent;
            border: 1px solid rgba(219,234,254,.34);
            color: #dbeafe;
            border-radius: 999px;
            min-height: 34px;
            padding: 4px 14px;
            font-size: 15px;
          }}
          .stButton button:hover {{
            background: rgba(219,234,254,.12);
            border-color: rgba(219,234,254,.54);
            color: #fff;
          }}
        </style>
        <div class="lyc-home-top">
          <div>{'<img class="lyc-home-logo" src="' + COMPANY_LOGO_URI + '">' if COMPANY_LOGO_URI else ''}</div>
          <div class="lyc-home-user">{user_label}</div>
        </div>
        <div class="lyc-home-title">
          <h1>功能选择</h1>
          <p>选择要进行的操作</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if user_can_access(current_user, "accounts"):
        admin_left, admin_right = st.columns([5, 1])
        with admin_right:
            if st.button("账号管理", key="home_go_accounts", use_container_width=True):
                go_module("accounts")

    row1_left, row1_right = st.columns(2, gap="large")
    row2_left, row2_right = st.columns(2, gap="large")

    if user_can_access(current_user, "label_print") or user_can_access(current_user, "template"):
        with row1_left:
            with st.container(border=True):
                st.markdown(
                    """
                    <div class="lyc-home-card">
                      <div class="lyc-home-icon" style="background:#7c3aed;">&#128424;</div>
                      <h3>标签打印</h3>
                      <p>搜索产品、打印标签、模板设计</p>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                b1, b2 = st.columns([1, 1])
                with b1:
                    if user_can_access(current_user, "label_print") and st.button("进入标签打印", key="home_go_label"):
                        go_module("label_print")
                with b2:
                    if user_can_access(current_user, "template") and st.button("打印模板", key="home_go_template"):
                        go_module("template")

    if user_can_access(current_user, "scan_in"):
        with row1_right:
            with st.container(border=True):
                st.markdown(
                    """
                    <div class="lyc-home-card">
                      <div class="lyc-home-icon" style="background:#0f766e;">&#128230;</div>
                      <h3>扫码入库</h3>
                      <p>扫描二维码、产品入库登记</p>
                      
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                if st.button("进入扫码入库", key="home_go_scan_in"):
                    go_module("scan_in")

    if user_can_access(current_user, "scan_out"):
        with row2_left:
            with st.container(border=True):
                st.markdown(
                    """
                    <div class="lyc-home-card">
                      <div class="lyc-home-icon" style="background:#b45309;">&#128228;</div>
                      <h3>扫码出库</h3>
                      <p>扫描二维码、产品出库登记</p>
                      
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                if st.button("进入扫码出库", key="home_go_scan_out"):
                    go_module("scan_out")

    if user_can_access(current_user, "trace_query"):
        with row2_right:
            with st.container(border=True):
                st.markdown(
                    """
                    <div class="lyc-home-card">
                      <div class="lyc-home-icon" style="background:#0284c7;">&#128269;</div>
                      <h3>溯源查询</h3>
                      <p>扫描二维码、查看完整流转记录</p>
                      
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                if st.button("进入溯源查询", key="home_go_trace"):
                    go_module("trace_query")

def render_coming_soon(title):
    if st.button("← 返回功能选择"):
        go_module("home")
    st.info(f"{title} 功能敬请期待。")


def render_scan_in(current_user, is_admin):
    if st.button("← 返回功能选择", key="scan_in_back_home"):
        go_module("home")
    st.markdown('<div class="lyc-section-title">扫码入库</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="lyc-muted">使用扫码枪或复制二维码内容，系统会匹配已打印箱贴并写入入库记录。</div>',
        unsafe_allow_html=True,
    )

    st.caption("体验箱码（工艺单 900002，订单 PO-DEMO-002），可复制到下方扫码框。")
    st.code(SAMPLE_QR, language=None)
    with open(os.path.join(os.path.dirname(__file__), "samples", "扫码体验箱贴_900002.pdf"), "rb") as sample:
        st.download_button("下载此体验箱贴", sample.read(), file_name="扫码体验箱贴_900002.pdf", mime="application/pdf")
    if record_scan_in is None or fetch_scan_in_logs is None or parse_qr_content is None:
        st.error("当前 db_manager.py 缺少扫码入库函数，请同步最新代码。")
        return

    with st.container(border=True):
        st.markdown("### 入库扫码")
        with st.form("scan_in_form", clear_on_submit=True):
            qr_content = st.text_input(
                "二维码内容",
                placeholder="扫码枪扫描箱贴二维码，或粘贴二维码内容后回车",
                key="scan_in_qr_input",
            )
            remark = st.text_input(
                "备注",
                placeholder="可选，例如：仓库入库、异常说明",
                key="scan_in_remark",
            )
            submitted = st.form_submit_button("确认入库", type="primary", use_container_width=True)

        if submitted:
            try:
                result = record_scan_in(
                    qr_content,
                    operator=current_user.get("username", "system"),
                    source="scan_in_page",
                    remark=remark,
                )
            except Exception as exc:
                st.error(f"入库失败：{exc}")
                result = None

            if result:
                if result.get("ok"):
                    st.success(result.get("message", "入库成功"))
                else:
                    st.error(result.get("message", "入库失败"))

                print_log = result.get("print_log") or {}
                parsed = result.get("parsed") or parse_qr_content(qr_content)
                if print_log:
                    info_cols = st.columns(4)
                    info_cols[0].metric("工艺单号", print_log.get("process_no") or "-")
                    info_cols[1].metric("料号", print_log.get("item_no") or parsed.get("item_no") or "-")
                    info_cols[2].metric("箱号", print_log.get("box_index") or parsed.get("box_index") or "-")
                    info_cols[3].metric("本箱数量", print_log.get("qty_in_box") or parsed.get("qty_in_box") or "-")
                    detail_df = pd.DataFrame([{
                        "订单号": print_log.get("order_no", ""),
                        "产品名称": print_log.get("product_name", ""),
                        "总箱数": print_log.get("total_boxes", ""),
                        "标准装量": print_log.get("std_qty", ""),
                        "打印时间": print_log.get("printed_at", ""),
                        "打印人": print_log.get("operator_name", ""),
                        "二维码内容": print_log.get("qr_content", qr_content),
                    }])
                    st.dataframe(detail_df, use_container_width=True, hide_index=True)
                else:
                    st.caption(
                        f"解析结果：料号 {parsed.get('item_no') or '-'}，"
                        f"箱号 {parsed.get('box_index') or '-'}，"
                        f"数量 {parsed.get('qty_in_box') or '-'}"
                    )

    st.markdown("### 最近入库记录")
    with st.container(border=True):
        c1, c2, c3 = st.columns([5, 1.2, 1.2])
        with c1:
            keyword = st.text_input(
                "查询入库记录",
                placeholder="搜索：工艺单号 / 订单号 / 料号 / 产品名称 / 二维码内容 / 入库人...",
                key="scan_in_log_keyword",
                label_visibility="collapsed",
            )
        with c2:
            limit = st.number_input("条数", min_value=20, max_value=1000, value=200, step=20, key="scan_in_log_limit")
        with c3:
            include_void = st.checkbox("含作废", value=False, key="scan_in_include_void")

        try:
            logs_df = fetch_scan_in_logs(keyword=keyword, limit=int(limit), include_void=include_void)
        except Exception as exc:
            st.error(f"读取入库记录失败：{exc}")
            logs_df = pd.DataFrame()

        if logs_df.empty:
            st.info("暂无入库记录。")
        else:
            show_df = logs_df.copy()
            column_map = {
                "id": "入库ID",
                "print_log_id": "打印记录ID",
                "process_no": "工艺单号",
                "order_no": "订单号",
                "item_no": "料号",
                "product_name": "产品名称",
                "box_index": "箱号",
                "total_boxes": "总箱数",
                "qty_in_box": "本箱数量",
                "std_qty": "标准装量",
                "scanned_by_name": "入库人",
                "scanned_at": "入库时间",
                "source": "来源",
                "status": "状态",
                "remark": "备注",
                "qr_content": "二维码内容",
            }
            ordered_cols = [c for c in column_map if c in show_df.columns]
            st.caption(f"共查询到 {len(show_df)} 条入库记录。")
            st.dataframe(
                show_df[ordered_cols].rename(columns=column_map),
                use_container_width=True,
                hide_index=True,
            )


def render_scan_out(current_user, is_admin):
    if st.button("← 返回功能选择", key="scan_out_back_home"):
        go_module("home")
    st.markdown('<div class="lyc-section-title">扫码出库</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="lyc-muted">使用扫码枪或复制二维码内容，系统会匹配已打印箱贴并写入出库记录。</div>',
        unsafe_allow_html=True,
    )

    st.caption("体验箱码（工艺单 900002，订单 PO-DEMO-002），可复制到下方扫码框。")
    st.code(SAMPLE_QR, language=None)
    with open(os.path.join(os.path.dirname(__file__), "samples", "扫码体验箱贴_900002.pdf"), "rb") as sample:
        st.download_button("下载此体验箱贴", sample.read(), file_name="扫码体验箱贴_900002.pdf", mime="application/pdf")
    if record_scan_out is None or fetch_scan_out_logs is None or parse_qr_content is None:
        st.error("当前 db_manager.py 缺少扫码出库函数，请同步最新代码。")
        return

    with st.container(border=True):
        st.markdown("### 出库扫码")
        with st.form("scan_out_form", clear_on_submit=True):
            qr_content = st.text_input(
                "二维码内容",
                placeholder="扫码枪扫描箱贴二维码，或粘贴二维码内容后回车",
                key="scan_out_qr_input",
            )
            remark = st.text_input(
                "备注",
                placeholder="可选，例如：出货单号、客户、异常说明",
                key="scan_out_remark",
            )
            submitted = st.form_submit_button("确认出库", type="primary", use_container_width=True)

        if submitted:
            try:
                result = record_scan_out(
                    qr_content,
                    operator=current_user.get("username", "system"),
                    source="scan_out_page",
                    remark=remark,
                    allow_without_scan_in=True,
                )
            except Exception as exc:
                st.error(f"出库失败：{exc}")
                result = None

            if result:
                if result.get("ok"):
                    if result.get("warning") == "missing_scan_in":
                        st.warning(result.get("message", "出库成功，但未找到入库记录"))
                    else:
                        st.success(result.get("message", "出库成功"))
                else:
                    st.error(result.get("message", "出库失败"))

                print_log = result.get("print_log") or {}
                scan_in = result.get("scan_in") or {}
                parsed = result.get("parsed") or parse_qr_content(qr_content)
                if print_log:
                    info_cols = st.columns(4)
                    info_cols[0].metric("工艺单号", print_log.get("process_no") or "-")
                    info_cols[1].metric("料号", print_log.get("item_no") or parsed.get("item_no") or "-")
                    info_cols[2].metric("箱号", print_log.get("box_index") or parsed.get("box_index") or "-")
                    info_cols[3].metric("本箱数量", print_log.get("qty_in_box") or parsed.get("qty_in_box") or "-")
                    detail_df = pd.DataFrame([{
                        "订单号": print_log.get("order_no", ""),
                        "产品名称": print_log.get("product_name", ""),
                        "总箱数": print_log.get("total_boxes", ""),
                        "标准装量": print_log.get("std_qty", ""),
                        "打印时间": print_log.get("printed_at", ""),
                        "打印人": print_log.get("operator_name", ""),
                        "入库时间": scan_in.get("scanned_at", "") if scan_in else "",
                        "入库人": scan_in.get("scanned_by_name", "") if scan_in else "",
                        "二维码内容": print_log.get("qr_content", qr_content),
                    }])
                    st.dataframe(detail_df, use_container_width=True, hide_index=True)
                else:
                    st.caption(
                        f"解析结果：料号 {parsed.get('item_no') or '-'}，"
                        f"箱号 {parsed.get('box_index') or '-'}，"
                        f"数量 {parsed.get('qty_in_box') or '-'}"
                    )

    st.markdown("### 最近出库记录")
    with st.container(border=True):
        c1, c2, c3 = st.columns([5, 1.2, 1.2])
        with c1:
            keyword = st.text_input(
                "查询出库记录",
                placeholder="搜索：工艺单号 / 订单号 / 料号 / 产品名称 / 二维码内容 / 出库人...",
                key="scan_out_log_keyword",
                label_visibility="collapsed",
            )
        with c2:
            limit = st.number_input("条数", min_value=20, max_value=1000, value=200, step=20, key="scan_out_log_limit")
        with c3:
            include_void = st.checkbox("含作废", value=False, key="scan_out_include_void")

        try:
            logs_df = fetch_scan_out_logs(keyword=keyword, limit=int(limit), include_void=include_void)
        except Exception as exc:
            st.error(f"读取出库记录失败：{exc}")
            logs_df = pd.DataFrame()

        if logs_df.empty:
            st.info("暂无出库记录。")
        else:
            show_df = logs_df.copy()
            column_map = {
                "id": "出库ID",
                "print_log_id": "打印记录ID",
                "scan_in_id": "入库ID",
                "process_no": "工艺单号",
                "order_no": "订单号",
                "item_no": "料号",
                "product_name": "产品名称",
                "box_index": "箱号",
                "total_boxes": "总箱数",
                "qty_in_box": "本箱数量",
                "std_qty": "标准装量",
                "scanned_by_name": "出库人",
                "scanned_at": "出库时间",
                "source": "来源",
                "status": "状态",
                "remark": "备注",
                "qr_content": "二维码内容",
            }
            ordered_cols = [c for c in column_map if c in show_df.columns]
            st.caption(f"共查询到 {len(show_df)} 条出库记录。")
            st.dataframe(
                show_df[ordered_cols].rename(columns=column_map),
                use_container_width=True,
                hide_index=True,
            )


def render_trace_query(current_user, is_admin):
    if st.button("← 返回功能选择", key="trace_back_home"):
        go_module("home")
    st.markdown('<div class="lyc-section-title">溯源查询</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="lyc-muted">按二维码、工艺单、订单、料号查询打印、入库、出库的完整流转记录。</div>',
        unsafe_allow_html=True,
    )

    st.caption("可用订单 PO-DEMO-002 或箱码 "+ SAMPLE_QR +" 查询体验箱的流转记录。")
    trace_reader = fetch_trace_records or fetch_trace_print_logs
    if trace_reader is None:
        st.error("当前 db_manager.py 缺少溯源查询函数，请同步最新代码。")
        return

    with st.container(border=True):
        c1, c2, c3 = st.columns([5, 1.2, 1.2])
        with c1:
            keyword = st.text_input(
                "查询条件",
                placeholder="搜索：工艺单号 / 订单号 / 料号 / 产品名称 / 二维码内容 / 操作人...",
                key="trace_keyword",
                label_visibility="collapsed",
            )
        with c2:
            limit = st.number_input("显示条数", min_value=50, max_value=5000, value=1000, step=50)
        with c3:
            include_deleted = st.checkbox("含已删除", value=False)

    try:
        df = trace_reader(keyword=keyword, limit=int(limit), include_deleted=include_deleted)
    except Exception as e:
        st.error(f"读取溯源记录失败：{e}")
        return

    if df.empty:
        st.info("没有匹配的流转记录。")
    else:
        show_df = df.copy()
        show_df["box_type"] = show_df["box_type"].map({
            "full": "满箱",
            "tail": "尾箱",
            "pack": "尾包",
            "spare": "备品",
            "stock": "库存",
        }).fillna(show_df["box_type"])
        show_df["action_type"] = show_df["action_type"].map({
            "new": "新打",
            "reprint": "重打",
        }).fillna(show_df["action_type"])
        if "is_counted" in show_df.columns:
            show_df["is_counted"] = show_df["is_counted"].map({True: "计入", False: "不计入", 1: "计入", 0: "不计入"})
        if "is_deleted" in show_df.columns:
            show_df["is_deleted"] = show_df["is_deleted"].map({True: "已删除", False: "正常", 1: "已删除", 0: "正常"})
        if "trace_status" not in show_df.columns:
            show_df["trace_status"] = show_df["is_deleted"].map({"已删除": "打印已删除"}).fillna("仅打印流水")
        status_counts = show_df["trace_status"].value_counts().to_dict()
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("查询结果", len(show_df))
        s2.metric("已出库", int(status_counts.get("已出库", 0)))
        s3.metric("已入库未出库", int(status_counts.get("已入库未出库", 0)))
        s4.metric("已打印未入库", int(status_counts.get("已打印未入库", 0)))
        column_map = {
            "trace_status": "当前状态",
            "print_log_id": "打印记录ID",
            "id": "打印记录ID",
            "process_no": "工艺单号",
            "order_no": "订单号",
            "item_no": "料号",
            "product_name": "产品名称",
            "box_index": "箱号",
            "total_boxes": "总箱数",
            "qty_in_box": "本箱数量",
            "std_qty": "标准装量",
            "box_type": "箱贴类型",
            "action_type": "打印类型",
            "is_counted": "是否计入累计",
            "is_deleted": "记录状态",
            "printer_name": "打印机",
            "printed_by_name": "打印人",
            "operator_name": "打印人",
            "printed_at": "打印时间",
            "scan_in_id": "入库ID",
            "scan_in_by_name": "入库人",
            "scan_in_at": "入库时间",
            "scan_in_remark": "入库备注",
            "scan_out_id": "出库ID",
            "scan_out_by_name": "出库人",
            "scan_out_at": "出库时间",
            "scan_out_remark": "出库备注",
            "shipment_time": "二维码发货时间",
            "qr_content": "二维码内容",
            "deleted_by_name": "删除人",
            "deleted_at": "删除时间",
            "delete_reason": "删除原因",
        }
        ordered_cols = [c for c in column_map if c in show_df.columns]
        st.caption(f"共查询到 {len(show_df)} 条流转记录。")
        st.dataframe(
            show_df[ordered_cols].rename(columns=column_map),
            use_container_width=True,
            hide_index=True,
        )


def show_db_manager_account_error():
    st.error(
        "账号管理函数没有成功导入。"
        "这通常表示当前运行的服务加载了旧版 db_manager.py，或改完代码后服务还没重启。"
    )
    st.code(
        "\n".join([
            f"当前加载的 db_manager.py: {DB_MANAGER_FILE}",
            f"导入错误: {DB_MANAGER_ACCOUNT_IMPORT_ERROR or '未记录'}",
            "需要函数: list_system_users, save_system_user, delete_system_user, PERMISSION_MODULES",
        ]),
        language="text",
    )


def render_account_manager(current_user):
    st.subheader("👤 操作员账号管理")
    st.caption("账号保存在 SQL Server，所有电脑连接同一数据库后共用。密码不会明文保存。")

    if list_system_users is None or save_system_user is None:
        show_db_manager_account_error()
        st.stop()

    try:
        users_df = list_system_users()
    except Exception as exc:
        st.error(f"读取账号失败：{exc}")
        users_df = pd.DataFrame()

    if not users_df.empty:
        show_df = users_df.copy()
        rename_map = {
            'id': 'ID',
            'username': '账号',
            'display_name': '姓名',
            'role_code': '权限',
            'is_active': '启用',
            'created_at': '创建时间',
            'last_login_at': '最后登录',
        }
        show_df = show_df.rename(columns=rename_map)
        st.dataframe(show_df[list(rename_map.values())], use_container_width=True, hide_index=True)

    st.markdown("### 新增账号")
    add_c1, add_c2, add_c3, add_c4 = st.columns([1.2, 1.2, 1.2, 0.9])
    with add_c1:
        new_username = st.text_input("新账号", key="user_add_username")
    with add_c2:
        new_display_name = st.text_input("姓名", key="user_add_display")
    with add_c3:
        new_password = st.text_input("初始密码", type="password", key="user_add_password")
    with add_c4:
        new_role = st.selectbox("权限", ["operator", "admin"], format_func=lambda x: "操作员" if x == "operator" else "管理员", key="user_add_role")
    if st.button("创建账号", type="primary"):
        try:
            save_system_user(
                username=new_username,
                display_name=new_display_name,
                password=new_password,
                role_code=new_role,
                is_active=True,
                operator=current_user.get('username', 'system'),
            )
            st.success("账号已创建")
            st.rerun()
        except Exception as exc:
            st.error(f"创建失败：{exc}")

    st.markdown("### 编辑账号")
    if users_df.empty:
        st.info("暂无账号数据")
        return

    user_options = [
        f"{row.username} - {row.display_name} ({row.role_code})"
        for row in users_df.itertuples(index=False)
    ]
    selected_user_label = st.selectbox("选择账号", user_options)
    selected_idx = user_options.index(selected_user_label)
    selected_user = users_df.iloc[selected_idx]

    edit_c1, edit_c2, edit_c3, edit_c4 = st.columns([1.2, 1.2, 1.2, 0.8])
    with edit_c1:
        edit_username = st.text_input("账号", value=str(selected_user['username']), disabled=True)
    with edit_c2:
        edit_display_name = st.text_input("姓名", value=str(selected_user['display_name']), key=f"user_edit_display_{selected_user['id']}")
    with edit_c3:
        edit_password = st.text_input("新密码（不填则不改）", type="password", key=f"user_edit_password_{selected_user['id']}")
    with edit_c4:
        role_values = ["operator", "admin"]
        current_role = str(selected_user['role_code'])
        edit_role = st.selectbox(
            "权限",
            role_values,
            index=role_values.index(current_role) if current_role in role_values else 0,
            format_func=lambda x: "操作员" if x == "operator" else "管理员",
            key=f"user_edit_role_{selected_user['id']}",
        )

    is_self = str(selected_user['username']) == str(current_user.get('username'))
    active_value = bool(selected_user['is_active'])
    edit_active = st.checkbox(
        "启用该账号",
        value=active_value,
        disabled=is_self,
        key=f"user_edit_active_{selected_user['id']}",
    )
    if is_self:
        edit_active = True
        edit_role = "admin"
        st.caption("当前登录的管理员账号不能停用，也不能改成操作员。")

    if st.button("保存修改", type="primary"):
        try:
            save_system_user(
                username=edit_username,
                display_name=edit_display_name,
                password=edit_password if edit_password.strip() else None,
                role_code=edit_role,
                is_active=edit_active,
                operator=current_user.get('username', 'system'),
            )
            if is_self:
                st.session_state['user']['display_name'] = edit_display_name
            st.success("账号已更新")
            st.rerun()
        except Exception as exc:
            st.error(f"保存失败：{exc}")


def render_account_manager(current_user):
    st.subheader("👤 操作员账号管理")
    st.caption("账号保存在 SQL Server，所有电脑连接同一数据库后共用。密码不会明文保存。")

    if list_system_users is None or save_system_user is None or delete_system_user is None:
        show_db_manager_account_error()
        st.stop()

    try:
        users_df = list_system_users()
    except Exception as exc:
        st.error(f"读取账号失败：{exc}")
        users_df = pd.DataFrame()

    if not users_df.empty:
        show_df = users_df.copy()
        rename_map = {
            'id': 'ID',
            'username': '账号',
            'role_code': '权限',
            'permissions': '可进入板块',
            'is_active': '启用',
            'created_at': '创建时间',
            'last_login_at': '最后登录',
        }
        show_df = show_df.rename(columns=rename_map)
        st.dataframe(show_df[list(rename_map.values())], use_container_width=True, hide_index=True)

    st.markdown("### 新增账号")
    add_c1, add_c2, add_c3 = st.columns([1.4, 1.2, 0.9])
    with add_c1:
        new_username = st.text_input("账号（直接填人员名字）", key="user_add_username")
    with add_c2:
        new_password = st.text_input("初始密码", type="password", key="user_add_password")
    with add_c3:
        new_role = st.selectbox(
            "权限",
            ["operator", "admin"],
            format_func=lambda x: "操作员" if x == "operator" else "管理员",
            key="user_add_role",
        )
    default_new_permissions = list(PERMISSION_MODULES.keys()) if new_role == "admin" else ["label_print", "scan_in", "scan_out", "trace_query"]
    new_permissions = st.multiselect(
        "允许进入的板块",
        options=list(PERMISSION_MODULES.keys()),
        default=default_new_permissions,
        format_func=lambda code: PERMISSION_MODULES.get(code, code),
        disabled=new_role == "admin",
        key="user_add_permissions",
    )
    if st.button("创建账号", type="primary"):
        try:
            save_system_user(
                username=new_username,
                display_name=new_username,
                password=new_password,
                role_code=new_role,
                is_active=True,
                operator=current_user.get('username', 'system'),
                permissions=list(PERMISSION_MODULES.keys()) if new_role == "admin" else new_permissions,
            )
            st.success("账号已创建")
            st.rerun()
        except Exception as exc:
            st.error(f"创建失败：{exc}")

    st.markdown("### 编辑 / 删除账号")
    if users_df.empty:
        st.info("暂无账号数据")
        return

    user_options = [
        f"{row.username} ({row.role_code})"
        for row in users_df.itertuples(index=False)
    ]
    selected_user_label = st.selectbox("选择账号", user_options)
    selected_idx = user_options.index(selected_user_label)
    selected_user = users_df.iloc[selected_idx]

    edit_c1, edit_c2, edit_c3 = st.columns([1.4, 1.2, 0.9])
    with edit_c1:
        edit_username = st.text_input("账号", value=str(selected_user['username']), disabled=True)
    with edit_c2:
        edit_password = st.text_input("新密码（不填则不改）", type="password", key=f"user_edit_password_{selected_user['id']}")
    with edit_c3:
        role_values = ["operator", "admin"]
        current_role = str(selected_user['role_code'])
        edit_role = st.selectbox(
            "权限",
            role_values,
            index=role_values.index(current_role) if current_role in role_values else 0,
            format_func=lambda x: "操作员" if x == "operator" else "管理员",
            key=f"user_edit_role_{selected_user['id']}",
        )
    selected_permissions_text = str(selected_user.get('permissions', '') or '')
    selected_permissions = [
        code for code in selected_permissions_text.split(',')
        if code in PERMISSION_MODULES
    ]
    if not selected_permissions:
        selected_permissions = list(PERMISSION_MODULES.keys()) if edit_role == "admin" else ["label_print", "scan_in", "scan_out", "trace_query"]
    edit_permissions = st.multiselect(
        "允许进入的板块",
        options=list(PERMISSION_MODULES.keys()),
        default=list(PERMISSION_MODULES.keys()) if edit_role == "admin" else selected_permissions,
        format_func=lambda code: PERMISSION_MODULES.get(code, code),
        disabled=edit_role == "admin",
        key=f"user_edit_permissions_{selected_user['id']}",
    )

    is_self = str(selected_user['username']) == str(current_user.get('username'))
    is_default_admin = str(selected_user['username']) == 'admin'
    active_value = bool(selected_user['is_active'])
    edit_active = st.checkbox(
        "启用该账号",
        value=active_value,
        disabled=is_self,
        key=f"user_edit_active_{selected_user['id']}",
    )
    if is_self:
        edit_active = True
        edit_role = "admin"
        st.caption("当前登录账号不能停用，也不能改成操作员。")

    save_col, delete_col = st.columns([1, 1])
    with save_col:
        save_clicked = st.button("保存修改", type="primary", use_container_width=True)
    with delete_col:
        delete_confirm = st.checkbox(
            "确认删除该账号",
            disabled=is_self or is_default_admin,
            key=f"user_delete_confirm_{selected_user['id']}",
        )
        delete_clicked = st.button(
            "删除账号",
            disabled=is_self or is_default_admin or not delete_confirm,
            use_container_width=True,
        )

    if save_clicked:
        try:
            save_system_user(
                username=edit_username,
                display_name=edit_username,
                password=edit_password if edit_password.strip() else None,
                role_code=edit_role,
                is_active=edit_active,
                operator=current_user.get('username', 'system'),
                permissions=list(PERMISSION_MODULES.keys()) if edit_role == "admin" else edit_permissions,
            )
            if is_self:
                st.session_state['user']['display_name'] = edit_username
            st.success("账号已更新")
            st.rerun()
        except Exception as exc:
            st.error(f"保存失败：{exc}")

    if delete_clicked:
        try:
            delete_system_user(edit_username, operator=current_user.get('username', 'system'))
            st.success("账号已删除")
            st.rerun()
        except Exception as exc:
            st.error(f"删除失败：{exc}")


from db_manager import DEMO_USER, demo_rows, SAMPLE_QR, print_batch

current_user = DEMO_USER
if "demo_initialized" not in st.session_state:
    st.session_state["demo_initialized"] = True
    st.session_state["ready_to_print"] = demo_rows()[0]

is_admin = current_user.get('role_code') == 'admin'

if "active_module" not in st.session_state:
    st.session_state["active_module"] = "home"

query_module = st.query_params.get("module")
if query_module in {"home", "label_print", "template", "accounts", "scan_in", "scan_out", "trace_query"}:
    st.session_state["active_module"] = query_module

active_module = st.session_state.get("active_module", "home")
if active_module == "home":
    render_home_page(current_user, is_admin)
    st.stop()

require_module_access(current_user, active_module)

nav_1, nav_2, nav_3, nav_4 = st.columns([1, 1, 1, 5])
with nav_1:
    if st.button("← 功能选择", key="nav_home", use_container_width=True):
        go_module("home")
if active_module in {"label_print", "template"} and (
    user_can_access(current_user, "label_print") or user_can_access(current_user, "template")
):
    with nav_2:
        if user_can_access(current_user, "label_print") and st.button("标签打印", key="nav_label_print", use_container_width=True):
            go_module("label_print")
    with nav_3:
        if user_can_access(current_user, "template") and st.button("打印模板", key="nav_template", use_container_width=True):
            go_module("template")
elif user_can_access(current_user, "accounts"):
    with nav_2:
        if st.button("账号管理", key="nav_accounts", use_container_width=True):
            go_module("accounts")
with nav_4:
    st.caption(f"当前用户：{current_user.get('display_name')} ({current_user.get('role_code')})")
    if st.button("重置演示", key="demo_reset"):
        st.session_state.clear()
        st.query_params.clear()
        st.rerun()
if active_module == "scan_in":
    render_scan_in(current_user, is_admin)
    st.stop()
if active_module == "scan_out":
    render_scan_out(current_user, is_admin)
    st.stop()
if active_module == "trace_query":
    render_trace_query(current_user, is_admin)
    st.stop()
if active_module == "template":
    from template_ui import render_template_tab
    render_template_tab(current_user)
    st.stop()
if active_module == "accounts":
    render_account_manager(current_user)
    st.stop()

tab1 = st.container()
tab4 = None
tab5 = None
tab6 = None

# ============================================================
# Tab 1: 数据抓取与审计
# ============================================================
with tab1:
    st.markdown('<div class="lyc-section-title">标签打印</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="lyc-muted">搜索 ERP 数据并进入待打印表。选中记录后直接在本页生成箱贴。</div>',
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        search_col, btn_col, reset_col = st.columns([6, 0.8, 0.8])
        with search_col:
            search_keyword = st.text_input(
                "搜索",
                placeholder="搜索：料号 / 产品名称 / 定单号码 / 工艺单号...",
                key="home_pending_keyword",
                label_visibility="collapsed",
                on_change=lambda: st.session_state.update({"home_search_requested": True}),
            )
        with btn_col:
            search_clicked = st.button("搜索", type="primary", use_container_width=True)
        with reset_col:
            reset_clicked = st.button("重置", use_container_width=True, on_click=lambda: st.session_state.update({"home_pending_keyword": ""}))
        if reset_clicked:
            st.session_state["home_pending_filter_keyword"] = ""
            st.session_state.pop('last_summary', None)
            st.session_state.pop('last_ai_report', None)
            st.rerun()

    search_requested = bool(search_clicked or st.session_state.pop("home_search_requested", False))
    if search_requested:
        if not search_keyword:
            st.warning("输入不能为空!")
        else:
            search_type = infer_search_type(search_keyword)
            with st.spinner("正在精确查询 gyd / gydcp / gydqt / LWQT 四级表..."):
                result = fetch_production_data(search_keyword, search_type)

            if result["status"] == "error":
                st.warning(f"{result['msg']}。如果这是产品名称，会继续在下方待打印表内过滤。")
                st.session_state["home_pending_filter_keyword"] = search_keyword
            else:
                if result["status"] == "warning":
                    st.warning(result["msg"])
                else:
                    st.success(result["msg"])

                summary = result["summary"]
                clear_print_work_state()
                for p in summary['products']:
                    insert_to_shadow_db(p, summary['order_header'], summary['process_no'])
                st.session_state["home_pending_filter_keyword"] = str(summary.get('process_no') or search_keyword)
                st.info(f"✅ 已同步 {len(summary['products'])} 款产品到待打印表")

                st.session_state['last_summary'] = summary
                st.session_state.pop('last_ai_report', None)

                if False and is_admin:
                    with st.expander("📂 查看各工序原始报工明细"):
                        st.markdown("**前道共用工序:**")
                        for f in summary['shared_front_processes']:
                            if f['报工明细']:
                                st.markdown(f"- **{f['工序名称']}**")
                                st.dataframe(
                                    pd.DataFrame(f['报工明细']),
                                    use_container_width=True, hide_index=True,
                                )
                        st.markdown("**后道分款工序:**")
                        for p in summary['products']:
                            for q in p['后道工序']:
                                if q['报工明细']:
                                    st.markdown(f"- **{p['料号']} / {q['工序名称']}**")
                                    st.dataframe(
                                        pd.DataFrame(q['报工明细']),
                                        use_container_width=True, hide_index=True,
                                    )

    if is_admin and st.session_state.get('last_summary'):
        with st.expander("🤖 AI 审计报告(管理员)", expanded=False):
            if st.button("生成 AI 审计报告"):
                try:
                    with st.spinner("正在生成审计报告..."):
                        st.session_state['last_ai_report'] = generate_audit_report(
                            st.session_state['last_summary']
                        )
                except Exception as e:
                    st.session_state['last_ai_report'] = f"❌ AI 审计生成失败: {e}"
            if st.session_state.get('last_ai_report'):
                st.markdown(st.session_state['last_ai_report'])

    st.markdown('<div class="lyc-section-title">待打印数据</div>', unsafe_allow_html=True)
    render_pending_quick_panel(current_user, key_prefix="home")


# ============================================================
# Tab 2: 人工审核(已下线，功能合并到 Tab1)
# ============================================================
if False:
    st.subheader("待确认的标签数据(影子库)")

    pending_df = get_pending_records()

    if pending_df.empty:
        st.info("当前影子库中没有待审核的数据")
    else:
        display_df = pending_df.copy().reset_index(drop=True)
        display_df.insert(0, '选择', False)
        display_df.insert(1, '序号', range(1, len(display_df) + 1))
        rename_map = {
            'id': '真实ID',
            'process_no': '工艺单号',
            'order_no': '定单号码',
            'item_no': '料号',
            'product_name': '品名',
            'order_qty': '订货数量',
            'done_qty': '最终成品数',
            'last_process': '最后工序',
            'customer': '客户',
            'category': '品类',
            'modulus': '模数',
            'box_spec': '箱规',
            'stock_in_time': '入库时间',
            'stock_out_time': '出货时间',
            'created_at': '创建时间',
        }
        display_cols = [
            c for c in [
                '选择', '序号', 'id', 'process_no', 'order_no', 'item_no',
                'product_name', 'order_qty', 'done_qty', 'box_spec',
                'stock_in_time', 'stock_out_time', 'last_process',
                'customer', 'created_at',
            ] if c in display_df.columns
        ]
        edited_df = st.data_editor(
            display_df[display_cols].rename(columns=rename_map),
            use_container_width=True,
            hide_index=True,
            disabled=[c for c in display_df[display_cols].rename(columns=rename_map).columns if c != '选择'],
            column_config={
                '选择': st.column_config.CheckboxColumn('选择'),
                '真实ID': st.column_config.NumberColumn('真实ID'),
            },
            key='pending_editor',
        )
        selected_ids = edited_df.loc[edited_df['选择'], '真实ID'].astype(int).tolist()
        selected_records = pending_df[pending_df['id'].isin(selected_ids)].to_dict('records')
        st.write("---")

        if not selected_records:
            st.info("请先在表格中勾选要审核、打印或删除的记录。")
        else:
            st.caption(f"已选择 {len(selected_records)} 条记录")

            with st.expander("🧾 查看选中料号的系统打印历史 / ERP出货记录"):
                preview_record = selected_records[0]
                if len(selected_records) > 1:
                    labels = [
                        f"{r.get('process_no', '')} / {r.get('item_no', '')} / {r.get('product_name', '')}"
                        for r in selected_records
                    ]
                    chosen_label = st.selectbox("选择要查看的料号", labels)
                    preview_record = selected_records[labels.index(chosen_label)]
                process_no = preview_record.get('process_no', '')
                item_no = preview_record.get('item_no', '')
                if process_no:
                    st.markdown("#### 🖨️ 系统打印历史")
                    try:
                        print_history_df = fetch_system_print_history(process_no, item_no)
                        if print_history_df.empty:
                            st.info("暂无本系统打印记录")
                        else:
                            show_df = print_history_df.copy()
                            show_df['类型'] = show_df['box_type'].map({
                                'full': '满箱', 'tail': '尾箱', 'pack': '尾包', 'stock': '库存'
                            }).fillna(show_df['box_type'])
                            show_df['操作'] = show_df['action_type'].map({
                                'new': '新打', 'reprint': '重打'
                            }).fillna(show_df['action_type'])
                            show_df = show_df[[
                                'process_no', 'item_no', 'box_index', 'total_boxes',
                                'qty_in_box', '类型', '操作', 'operator_name',
                                'printer_name', 'printed_at'
                            ]]
                            show_df.columns = [
                                '工艺单号', '料号', '箱号', '总箱数', '本箱数量',
                                '类型', '操作', '打印人', '打印机', '打印时间'
                            ]
                            st.dataframe(show_df, use_container_width=True, hide_index=True)
                    except Exception as e:
                        st.warning(f"查询系统打印历史失败: {e}")

                    st.markdown("#### 🚚 ERP出货记录")
                    try:
                        shipments = fetch_shipment_by_process_no(process_no, item_no)
                        if shipments.empty:
                            st.info("暂无匹配的 ERP 出货记录")
                        else:
                            st.caption(
                                f"查询条件:工艺单 {process_no}"
                                + (f" + 料号 {item_no}" if item_no else "")
                            )
                            st.dataframe(shipments, use_container_width=True, hide_index=True)
                    except Exception as e:
                        st.warning(f"查询 ERP 出货记录失败: {e}")

            col_btn1, col_btn2 = st.columns(2)
            with col_btn1:
                if st.button("✅ 批量审核并进入打印排版", type="primary"):
                    confirm_shadow_records_batch(
                        selected_ids,
                        operator=current_user.get('username', 'system'),
                    )
                    clear_print_work_state()
                    st.session_state['ready_to_print'] = selected_records[0]
                    if len(selected_records) > 1:
                        st.session_state['batch_print_records'] = selected_records
                    st.success("审核通过!请前往「打印预览」标签页")
                    st.rerun()
            with col_btn2:
                confirm_delete_pending = st.checkbox("确认删除选中的待审核记录")
                if st.button(
                    "🗑️ 批量删除选中记录",
                    disabled=not confirm_delete_pending,
                ):
                    n = delete_shadow_records_batch(
                        selected_ids,
                        operator=current_user.get('username', 'system'),
                    )
                    st.success(f"已删除 {n} 条记录")
                    st.rerun()


# ============================================================
# Tab 3: 打印预览(v3 大改，现合并到 Tab1)
# ============================================================
def render_print_panel(current_user, is_admin):
    st.markdown('<div class="lyc-print-title">📦 箱贴生成与打印</div>', unsafe_allow_html=True)

    if 'ready_to_print' not in st.session_state:
        st.warning("请先在上方待打印数据中勾选记录，再进行打印排版。")
    else:
        batch_records = st.session_state.get('batch_print_records', [])
        data = st.session_state['ready_to_print']
        if batch_records:
            st.info(f"批量打印模式:已载入 {len(batch_records)} 款产品。各料号箱号独立累计。")
            record_labels = [
                f"{r.get('process_no', '')} / {r.get('item_no', '')} / {r.get('product_name', '')}"
                for r in batch_records
            ]
            active_label = st.selectbox("当前预览产品", record_labels, key='batch_active_record')
            data = batch_records[record_labels.index(active_label)]
            st.session_state['ready_to_print'] = data

        process_no = str(data.get('process_no', ''))
        item_no = str(data.get('item_no', ''))
        order_no = str(data.get('order_no', ''))
        order_qty = int(data.get('order_qty', 0) or 0)
        done_qty = int(data.get('done_qty', 0) or 0)
        box_spec = int(data.get('box_spec', 0) or 0)
        record_key = f"{process_no}_{item_no}"

        if st.session_state.get('active_print_record_key') != record_key:
            st.session_state['active_print_record_key'] = record_key
            st.session_state['print_fields'] = make_fields_for_record(data)
            for key in ['generated_images', 'generated_qrs', 'generated_meta', 'generated_zpls', 'print_context']:
                st.session_state.pop(key, None)
        if 'print_fields' not in st.session_state:
            st.session_state['print_fields'] = make_fields_for_record(data)
        ensure_operator_field(
            st.session_state['print_fields'],
            current_user,
            visible=st.session_state.get(f'show_operator_on_label_{record_key}', False),
        )

        # ============== 历史累计信息 ==============
        history = get_print_history(process_no, item_no)
        printed_count = history['printed_box_count']
        printed_qty = history['printed_qty_total']
        max_box = history['max_box_index']

        remaining = max(0, order_qty - printed_qty)
        st.markdown(
            f"""
            <div class="lyc-status-row">
              <div class="lyc-status-card">
                <div class="lyc-status-label">订货数量</div>
                <div class="lyc-status-value">{order_qty:,} pcs</div>
              </div>
              <div class="lyc-status-card">
                <div class="lyc-status-label">历史已打印</div>
                <div class="lyc-status-value">{printed_count} 箱 / {printed_qty:,} pcs</div>
              </div>
              <div class="lyc-status-card">
                <div class="lyc-status-label">剩余可打印</div>
                <div class="lyc-status-value">{remaining:,} pcs</div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # 工序报工数仅作"参考",淡化展示,避免误导
        if done_qty > 0:
            st.caption(
                f"💡 仅供参考:ERP 工序报工数为 {done_qty:,} pcs(此数 ≠ 实际装箱数,"
                "请以现场实际装箱情况为准)"
            )

        if history['reprint_count'] > 0:
            st.caption(f"📝 历史重打次数:{history['reprint_count']} 次(不计入累计)")
        if history.get('excluded_tail_count', 0) > 0:
            st.caption(
                f"📝 历史尾箱/尾包 {history['excluded_tail_count']} 张 / "
                f"{history.get('excluded_tail_qty', 0):,} pcs 已排除累计"
            )
        if printed_count > 0:
            st.caption(f"📝 已打箱号范围:{min(history['printed_box_list'])} ~ {max_box}")

        # ============== 打印记录管理(v3.1 新增) ==============
        if printed_count > 0 or history['reprint_count'] > 0:
            with st.expander("🗑️ 管理打印历史(用于撤销已打印记录,重新计数)"):
                from db_manager import (
                    get_print_log_details,
                    delete_print_logs_by_ids,
                    delete_all_print_logs,
                )
                logs_df = get_print_log_details(process_no, item_no)
                if logs_df.empty:
                    st.info("暂无打印记录")
                else:
                    st.caption(
                        "⚠️ 删除打印记录会让累计回退,起始箱号会自动重新计算。"
                        "**操作不可恢复**,请谨慎确认。"
                    )

                    # 让用户勾选要删的记录
                    display_df = logs_df.copy()
                    display_df['类型'] = display_df['box_type'].map({
                        'full': '满箱', 'tail': '尾箱', 'pack': '尾包', 'stock': '库存'
                    }).fillna(display_df['box_type'])
                    display_df['操作类型'] = display_df['action_type'].map({
                        'new': '✓ 新打',
                        'reprint': '🔁 重打(不计累计)',
                    }).fillna(display_df['action_type'])
                    display_df = display_df[[
                        'id', 'box_index', 'qty_in_box', '类型', '操作类型',
                        'operator_name', 'ts', 'printer_name'
                    ]]
                    display_df.columns = [
                        'ID', '箱号', '本箱数量', '类型', '操作类型',
                        '打印人', '时间', '打印机'
                    ]
                    st.dataframe(display_df, use_container_width=True, hide_index=True)

                    # 选择要删的 ID
                    selected_ids = st.multiselect(
                        "选择要删除的记录 ID",
                        options=logs_df['id'].tolist(),
                        key='delete_log_ids',
                    )

                    col_d1, col_d2, col_d3 = st.columns(3)
                    with col_d1:
                        confirm_delete = st.checkbox(
                            "我已确认要删除这些记录",
                            key='confirm_delete_some',
                        )
                        if st.button(
                            "🗑️ 删除选中",
                            disabled=(not selected_ids or not confirm_delete),
                        ):
                            n = delete_print_logs_by_ids(
                                selected_ids,
                                operator=current_user.get('username', 'system'),
                            )
                            st.success(f"✅ 已删除 {n} 条记录,即将刷新...")
                            st.rerun()

                    with col_d2:
                        confirm_clear = st.checkbox(
                            "我已确认要清空全部",
                            key='confirm_clear_all',
                        )
                        if st.button(
                            "💥 清空全部记录",
                            disabled=(not confirm_clear),
                            type="secondary",
                        ):
                            n = delete_all_print_logs(
                                process_no,
                                item_no,
                                operator=current_user.get('username', 'system'),
                            )
                            st.success(f"✅ 已清空 {n} 条记录,即将刷新...")
                            st.rerun()

                    with col_d3:
                        st.caption(
                            "💡 提示:\n"
                            "- 删除后,起始箱号会重新计算\n"
                            "- 如果删除全部,起始箱号回到 1"
                        )
        # ============== 装箱参数 ==============
        st.markdown("---")
        st.markdown("### ⚙️ 第 1 步:装箱参数")

        try:
            templates = get_print_templates()
        except Exception as e:
            st.warning(f"读取数据库模板失败,已使用内置模板: {e}")
            templates = list_builtin_templates()
        template_names = [t['name'] for t in templates]
        selected_template_name = st.selectbox(
            "打印模板",
            options=template_names,
            index=0,
            help="当前先使用内置模板。后续会开放模板参数编辑和保存。",
        )
        active_template = copy.deepcopy(templates[template_names.index(selected_template_name)])
        active_template.setdefault('zpl', {})
        active_template['zpl'].setdefault('rotate_for_media', False)
        active_template['zpl'].setdefault('force_rotate', bool(active_template['zpl'].get('rotate_for_media', False)))
        active_template['zpl'].setdefault('clarity_mode', 'standard')
        paper = active_template.get('paper', {})
        st.caption(
            f"当前模板:{selected_template_name} | "
            f"{paper.get('width_mm')}×{paper.get('height_mm')}mm | "
            f"{paper.get('dpi')}dpi | {active_template.get('output_mode')}"
        )
        uses_pack_method = any(
            row.get("field") == "pack_method"
            for row in active_template.get("rows", [])
        )
        if uses_pack_method:
            pack_method_key = f'pack_method_{record_key}'
            if pack_method_key not in st.session_state:
                st.session_state[pack_method_key] = str(
                    data.get('pack_method', '') or data.get('包装方式', '') or ''
                )
            pack_method_value = st.text_input(
                "包装方式 / 数量",
                key=pack_method_key,
                placeholder="例如:250份/包。ERP 未识别时在这里手动填写。",
            )
            data['pack_method'] = pack_method_value
            for field in st.session_state.get('print_fields', []):
                if field.get('key') == 'pack_method':
                    field['value'] = pack_method_value

        col_q1, col_q2 = st.columns(2)
        total_qty_key = f'total_qty_{record_key}'
        std_qty_key = f'std_qty_input_{record_key}'
        std_qty_source_key = f'std_qty_source_{record_key}'
        start_box_key = f'start_box_{record_key}'
        total_boxes_key = f'total_boxes_{record_key}'
        if total_qty_key not in st.session_state:
            st.session_state[total_qty_key] = max(1, int(remaining or 0))
        expected_std_qty_source = f"erp:{box_spec}" if box_spec > 0 else "missing"
        if st.session_state.get(std_qty_source_key) != expected_std_qty_source:
            if box_spec > 0:
                st.session_state[std_qty_key] = box_spec
            elif st.session_state.get(std_qty_key) in (None, 1000):
                st.session_state[std_qty_key] = None
            st.session_state[std_qty_source_key] = expected_std_qty_source
        if std_qty_key not in st.session_state:
            st.session_state[std_qty_key] = box_spec if box_spec > 0 else None
        if start_box_key not in st.session_state:
            st.session_state[start_box_key] = max_box + 1 if max_box > 0 else 1

        with col_q1:
            total_qty = st.number_input(
                "本次实际打包数量 (pcs) ⚠️ 必填",
                min_value=1,
                key=total_qty_key,
                help="请按现场实际装箱情况填写。此数不会自动从 ERP 抓取。",
            )
        with col_q2:
            std_qty_input = st.number_input(
                "每箱标准装量 (pcs)",
                min_value=1,
                value=None,
                placeholder="未抓到箱规，请填写",
                key=std_qty_key,
            )
            if box_spec > 0:
                st.caption(f"已从 ERP 箱规自动带入:{box_spec} pcs")
            else:
                st.error("未从 ERP 抓到每箱数量。请先填写每箱标准装量，否则不能生成或打印箱贴。")

        # ----- 偏差软警告:输入值 vs ERP 工序报工差异>30% 时提示 -----
        if done_qty > 0 and total_qty > 1:
            diff_ratio = abs(total_qty - done_qty) / done_qty
            if diff_ratio > 0.3:
                st.warning(
                    f"⚠️ 偏差提示:你输入的 **{total_qty:,}** pcs 与 ERP 工序报工 "
                    f"**{done_qty:,}** pcs 相差 {diff_ratio*100:.0f}%。"
                    "如果是少打了 0,请核对修正。"
                )

        pack_info = None
        std_qty = int(std_qty_input or 0)
        counted_total_qty = int(total_qty or 0)
        if counted_total_qty <= 0:
            counted_total_qty = 1

        # ----- 装箱分布计算 -----
        boxes_dist = calculate_box_distribution(counted_total_qty, std_qty, pack_info)
        n_boxes_this_batch = len(boxes_dist)

        # 总箱数(理论上的,基于订货量)
        if std_qty > 0:
            theoretical_total_boxes = -(-order_qty // std_qty)  # 向上取整
        else:
            theoretical_total_boxes = 0

        # 起始箱号(默认 = max_box + 1)
        col_b1, col_b2 = st.columns(2)
        with col_b1:
            start_box = st.number_input(
                "本次起始箱号",
                min_value=1,
                key=start_box_key,
                help=f"系统建议:{max_box + 1 if max_box > 0 else 1}(根据已打印记录)",
            )
        with col_b2:
            recommended_total_boxes = max(1, theoretical_total_boxes, start_box + n_boxes_this_batch - 1)
            total_boxes_sig_key = f'total_boxes_sig_{record_key}'
            total_boxes_sig = (
                selected_template_name,
                int(total_qty), int(std_qty), int(start_box),
                int(order_qty), int(n_boxes_this_batch), int(max_box)
            )
            if st.session_state.get(total_boxes_sig_key) != total_boxes_sig:
                st.session_state[total_boxes_key] = recommended_total_boxes
                st.session_state[total_boxes_sig_key] = total_boxes_sig
            total_boxes = st.number_input(
                "总箱数(箱贴上 X / 总箱数)",
                min_value=1,
                key=total_boxes_key,
                help=f"系统建议:{theoretical_total_boxes}(订货 ÷ 每箱)",
            )

        with st.expander("特殊箱贴数量", expanded=False):
            st.caption("0 表示不打印。特殊箱贴不占订单箱号，也不计入累计。")
            special_col1, special_col2, special_col3 = st.columns(3)
            with special_col1:
                blank_label_count = st.number_input("空标签", 0, 20, 1, key=f"special_blank_count_{record_key}")
            with special_col2:
                spare_label_count = st.number_input("备品", 0, 20, 1, key=f"special_spare_count_{record_key}")
            with special_col3:
                stock_label_count = st.number_input("库存", 0, 20, 1, key=f"special_stock_count_{record_key}")
        special_label_counts = {
            "blank": int(blank_label_count or 0),
            "spare": int(spare_label_count or 0),
            "stock": int(stock_label_count or 0),
        }

        # ----- 打印范围 -----
        range_block_print = False
        range_enabled = st.checkbox(
            "指定打印范围（按箱号，例如 201 到 230）",
            value=False,
            key=f"print_range_enabled_{record_key}",
            help="不开启时按本次打包数量自动生成。开启后只生成指定箱号范围；如果不勾重打，也会计入累计打印。",
        )
        if range_enabled:
            range_default_start = int(start_box)
            range_default_end = max(range_default_start, range_default_start + int(n_boxes_this_batch) - 1)
            range_start_key = f"print_range_start_{record_key}"
            range_end_key = f"print_range_end_{record_key}"
            if range_start_key not in st.session_state:
                st.session_state[range_start_key] = range_default_start
            if range_end_key not in st.session_state:
                st.session_state[range_end_key] = range_default_end
            col_r1, col_r2 = st.columns(2)
            with col_r1:
                range_start = st.number_input("打印范围起始箱号", min_value=1, key=range_start_key)
            with col_r2:
                range_end = st.number_input("打印范围结束箱号", min_value=1, key=range_end_key)

            if int(range_end) < int(range_start):
                st.error("打印范围结束箱号不能小于起始箱号。")
                range_block_print = True
                boxes_dist = []
            else:
                boxes_dist = build_box_distribution_for_range(
                    int(range_start), int(range_end), int(total_boxes), int(std_qty), int(order_qty)
                )
                start_box = int(range_start)
                n_boxes_this_batch = len(boxes_dist)
                total_qty = sum(int(b.get("qty", 0) or 0) for b in boxes_dist)
                st.caption(
                    f"本次将生成 {n_boxes_this_batch} 张箱贴，箱号 {int(range_start)} ~ {int(range_end)}，"
                    f"合计 {total_qty:,} pcs。"
                )

        # ----- 重打模式 -----
        with st.container(border=True):
            reprint_mode = st.checkbox(
                "🔁 重打模式(不计入累计,用于撕坏的箱贴重新打)",
                value=False, key='reprint_mode',
            )
            if reprint_mode:
                st.caption(
                    "重打模式下,系统不会把这次打印记入累计统计,"
                    "起始箱号请手动输入需要重打的箱号"
                )
                label_dt_key = f"reprint_label_dt_{record_key}"
                if label_dt_key not in st.session_state:
                    st.session_state[label_dt_key] = datetime.datetime.now()
                col_t1, col_t2 = st.columns(2)
                with col_t1:
                    reprint_date = st.date_input(
                        "重打标签日期",
                        value=st.session_state[label_dt_key].date(),
                        key=f"reprint_label_date_{record_key}",
                    )
                with col_t2:
                    reprint_time = st.time_input(
                        "重打标签时间",
                        value=st.session_state[label_dt_key].time().replace(microsecond=0),
                        key=f"reprint_label_time_{record_key}",
                    )
                st.session_state[label_dt_key] = datetime.datetime.combine(reprint_date, reprint_time)
                st.caption("重打时,箱贴日期和二维码时间会使用这里选择的时间；实际操作时间仍由系统记录。")
            else:
                st.session_state[f"reprint_label_dt_{record_key}"] = datetime.datetime.now()

        tray_label_date = (
            st.session_state.get(f"reprint_label_dt_{record_key}", datetime.datetime.now()).date()
            if reprint_mode else datetime.date.today()
        )
        tray_rows_key = f"tray_rows_{record_key}"
        tray_include_key = f"tray_include_in_current_{record_key}"
        if tray_rows_key not in st.session_state:
            st.session_state[tray_rows_key] = [{
                "料号": str(item_no or ""),
                "拖号": 1,
                "数量": 5000,
                "打印份数": 1,
            }]
        tray_rows = st.session_state[tray_rows_key]
        tray_preview_rows = []
        with st.expander("拖贴打印", expanded=False):
            st.caption("拖贴贴在板/拖上,不占箱号、不计入箱贴累计。每一行都可以单独改料号、拖号、数量和打印份数。")
            st.caption(f"二维码格式: 料号#日期YYYYMMDD+拖号4位#数量；本次日期: {tray_label_date.strftime('%Y-%m-%d')}")
            tray_editor_version_key = f"tray_editor_version_{record_key}"
            if tray_editor_version_key not in st.session_state:
                st.session_state[tray_editor_version_key] = 0
            tray_source_df = pd.DataFrame(st.session_state[tray_rows_key])
            for col_name in ["料号", "拖号", "数量", "打印份数"]:
                if col_name not in tray_source_df.columns:
                    tray_source_df[col_name] = [] if tray_source_df.empty else ""
            tray_source_df = tray_source_df[["料号", "拖号", "数量", "打印份数"]]
            tray_df = st.data_editor(
                tray_source_df,
                hide_index=True,
                num_rows="fixed",
                use_container_width=True,
                key=f"tray_editor_{record_key}_{st.session_state[tray_editor_version_key]}",
                column_config={
                    "料号": st.column_config.TextColumn("料号", required=True),
                    "拖号": st.column_config.NumberColumn("拖号", min_value=1, step=1, required=True),
                    "数量": st.column_config.NumberColumn("数量", min_value=1, step=1, required=True),
                    "打印份数": st.column_config.NumberColumn("打印份数", min_value=1, max_value=20, step=1, required=True),
                },
            )
            tray_rows = tray_df.to_dict("records") if hasattr(tray_df, "to_dict") else []
            completed_tray_rows = complete_tray_editor_rows(tray_rows, default_item_no=item_no, default_qty=5000)
            if completed_tray_rows != tray_rows:
                st.session_state[tray_rows_key] = completed_tray_rows
                st.session_state[tray_editor_version_key] += 1
                st.rerun()
            st.session_state[tray_rows_key] = completed_tray_rows
            tray_rows = completed_tray_rows
            tray_btn_col1, tray_btn_col2 = st.columns([1, 1])
            with tray_btn_col1:
                if st.button("新增一拖", key=f"add_tray_row_{record_key}"):
                    base_rows = complete_tray_editor_rows(tray_rows, default_item_no=item_no, default_qty=5000)
                    last_row = base_rows[-1] if base_rows else {
                        "料号": str(item_no or ""),
                        "拖号": 0,
                        "数量": 5000,
                        "打印份数": 1,
                    }
                    base_rows.append({
                        "料号": str(last_row.get("料号") or item_no or ""),
                        "拖号": int(last_row.get("拖号") or 0) + 1,
                        "数量": int(last_row.get("数量") or 5000),
                        "打印份数": int(last_row.get("打印份数") or 1),
                    })
                    st.session_state[tray_rows_key] = base_rows
                    st.session_state[tray_editor_version_key] += 1
                    st.rerun()
            with tray_btn_col2:
                if st.button("删除最后一拖", key=f"remove_tray_row_{record_key}", disabled=len(tray_rows) <= 1):
                    st.session_state[tray_rows_key] = tray_rows[:-1]
                    st.session_state[tray_editor_version_key] += 1
                    st.rerun()
            tray_preview_rows = normalize_tray_label_rows(tray_rows, tray_label_date)
            if tray_preview_rows:
                st.caption("将生成: " + "；".join(
                    f"{row['qr_content']} × 第{row['copy_index']}/{row['copies']}份"
                    for row in tray_preview_rows[:6]
                ) + (" ..." if len(tray_preview_rows) > 6 else ""))
            else:
                st.caption("请至少填写一行有效拖贴数据。")
            tray_gen_clicked = st.button(
                "生成拖贴",
                type="secondary",
                disabled=not bool(tray_preview_rows),
                key=f"generate_tray_labels_{record_key}",
            )
            if tray_gen_clicked:
                st.session_state[tray_include_key] = True
            if st.session_state.get(tray_include_key):
                st.success(
                    f"✅ 已加入本次生成: {len(tray_preview_rows)} 张拖贴。"
                    "请点击下方“生成当前产品箱贴”，会一起预览、一起打印。"
                )
                if st.button("取消本次拖贴", key=f"cancel_tray_labels_{record_key}"):
                    st.session_state[tray_include_key] = False
                    st.rerun()

        st.caption("特殊箱贴按上方数量生成；箱号和数量留空，供现场手写。")

        ship_key = f"shipment_time_{record_key}"
        if ship_key not in st.session_state:
            st.session_state[ship_key] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        label_print_time = st.session_state.get(f"reprint_label_dt_{record_key}", datetime.datetime.now()) if reprint_mode else datetime.datetime.now()
        if reprint_mode:
            st.session_state[ship_key] = label_print_time.strftime('%Y-%m-%d %H:%M:%S')
        shipment_time_for_qr = st.session_state[ship_key].replace('-', '').replace(':', '').replace(' ', '')

        # ----- 起始箱号校验 -----
        warnings_msgs = []
        if not reprint_mode:
            planned_end_box = start_box + max(n_boxes_this_batch - 1, 0)
            if max_box > 0 and start_box <= max_box:
                warnings_msgs.append(
                    f"⚠️ 起始箱号 {start_box} 与已打印箱号(最大 {max_box})有重叠"
                )
            if max_box > 0 and start_box > max_box + 1:
                gap_start = max_box + 1
                gap_end = start_box - 1
                warnings_msgs.append(
                    f"⚠️ 起始箱号 {start_box} 跳过了 {gap_start}~{gap_end} 号"
                )
            if range_enabled and max_box > 0 and planned_end_box <= max_box:
                warnings_msgs.append(
                    f"⚠️ 当前范围 {start_box}~{planned_end_box} 都在已打印范围内。"
                    "如是补打且要计入累计请继续；如只是重打，请勾选重打模式。"
                )

        for w in warnings_msgs:
            st.warning(w)

        # ----- 装箱结果校验 -----
        if not boxes_dist:
            st.warning("装箱数量为 0,请检查参数")

        # ----- 超量保护(C 方案) -----
        block_print = bool(range_block_print)
        if std_qty <= 0:
            st.error("每箱标准装量为空，已禁止生成和打印箱贴。请先填写每箱数量。")
            block_print = True
        if not reprint_mode:
            counted_print_qty = sum(int(b.get("qty", 0) or 0) for b in boxes_dist)
            projected = printed_qty + counted_print_qty
            if projected > order_qty and order_qty > 0:
                st.error(
                    f"🛑 **超量保护**:已打印 {printed_qty:,} + 本次 {counted_print_qty:,} = "
                    f"**{projected:,}** > 订货 {order_qty:,}\n\n"
                    f"超出 {projected - order_qty:,} pcs。请核对生产数据,"
                    f"或减少本次打印数量。"
                )
                block_print = True
            if batch_records:
                batch_over = []
                for record in batch_records:
                    rec_process_no = str(record.get('process_no', ''))
                    rec_item_no = str(record.get('item_no', ''))
                    rec_order_qty = int(record.get('order_qty', 0) or 0)
                    rec_history = get_print_history(rec_process_no, rec_item_no)
                    rec_projected = int(rec_history.get('printed_qty_total', 0) or 0) + int(counted_print_qty)
                    if rec_order_qty > 0 and rec_projected > rec_order_qty:
                        batch_over.append(
                            f"{rec_item_no}: {rec_projected:,} > {rec_order_qty:,}"
                        )
                if batch_over:
                    st.error("🛑 批量超量保护:" + "；".join(batch_over))
                    block_print = True

        fields = st.session_state['print_fields']
        operator_key = f'show_operator_on_label_{record_key}'
        show_operator_on_label = bool(st.session_state.get(operator_key, False))
        current_generated_is_tray = bool(st.session_state.get('print_context', {}).get('tray_mode'))
        operator_sig_key = f'operator_sig_{record_key}'
        if st.session_state.get(operator_sig_key) != show_operator_on_label:
            st.session_state[operator_sig_key] = show_operator_on_label
            if not current_generated_is_tray:
                for key in ['generated_images', 'generated_qrs', 'generated_meta', 'generated_zpls', 'print_context']:
                    st.session_state.pop(key, None)
        ensure_operator_field(fields, current_user, visible=show_operator_on_label)
        active_template_for_print = prepare_template_for_operator(active_template, show_operator_on_label)
        template_sig_key = f'active_template_sig_{record_key}'
        template_sig = get_template_signature(active_template_for_print)
        if st.session_state.get(template_sig_key) != template_sig:
            st.session_state[template_sig_key] = template_sig
            if not current_generated_is_tray:
                for key in ['generated_images', 'generated_qrs', 'generated_meta', 'generated_zpls', 'print_context']:
                    st.session_state.pop(key, None)

        print_option_sig_key = f'print_option_sig_{record_key}'
        print_option_sig = (
            bool(reprint_mode),
            label_print_time.strftime('%Y-%m-%d %H:%M:%S') if reprint_mode else None,
            tuple(sorted(special_label_counts.items())),
            int(counted_total_qty or 0),
            int(std_qty or 0),
            int(start_box or 0),
            int(total_boxes or 0),
            tuple((int(b.get('box_index', 0) or 0), int(b.get('qty', 0) or 0), str(b.get('box_type', ''))) for b in boxes_dist),
        )
        if st.session_state.get(print_option_sig_key) != print_option_sig:
            st.session_state[print_option_sig_key] = print_option_sig
            if not current_generated_is_tray:
                for key in ['generated_images', 'generated_qrs', 'generated_meta', 'generated_zpls', 'print_context']:
                    st.session_state.pop(key, None)

        # ============== 生成全部箱贴 ==============
        st.markdown("---")
        st.markdown("### 🚀 第 2 步:生成并打印")

        col_gen1, col_gen2 = st.columns(2)
        with col_gen1:
            gen_clicked = st.button(
                "🎨 生成当前产品箱贴",
                type="primary",
                disabled=block_print or n_boxes_this_batch == 0,
            )
        with col_gen2:
            batch_gen_clicked = st.button(
                "🎨 批量生成全部产品箱贴",
                disabled=(not batch_records) or block_print or n_boxes_this_batch == 0,
            )

        if gen_clicked:
            with st.spinner(f"正在渲染 {n_boxes_this_batch} 张箱贴..."):
                generated_images = []
                generated_qrs = []
                generated_meta = []
                generated_zpls = []
                print_time = label_print_time
                render_fields = fields_with_print_date(fields, print_time)

                for i, box_dist in enumerate(boxes_dist):
                    global_box_idx = int(box_dist.get('box_index', start_box + i))
                    box_qty = box_dist['qty']
                    box_type = box_dist['box_type']

                    label_img = generate_single_label(
                        fields=copy.deepcopy(render_fields),
                        current_box_index=global_box_idx,
                        total_boxes=total_boxes,
                        current_box_qty=box_qty,
                        std_qty=std_qty,
                        pack_info=pack_info,
                        box_type=box_type,
                        print_time=print_time,
                        shipment_time=shipment_time_for_qr,
                        template=active_template_for_print,
                    )
                    generated_images.append(label_img)
                    generated_zpls.append(build_single_label_zpl(
                        fields=copy.deepcopy(render_fields),
                        current_box_index=global_box_idx,
                        total_boxes=total_boxes,
                        current_box_qty=box_qty,
                        std_qty=std_qty,
                        pack_info=pack_info,
                        box_type=box_type,
                        print_time=print_time,
                        shipment_time=shipment_time_for_qr,
                        template=active_template_for_print,
                    ))

                    qr_ctx = build_render_context(
                        render_fields, global_box_idx, total_boxes, box_qty, std_qty,
                        pack_info=pack_info,
                        box_type=box_type,
                        print_time=print_time,
                        shipment_time=shipment_time_for_qr,
                        template=active_template_for_print,
                    )
                    qr_content = build_qr_content_from_context(active_template_for_print, qr_ctx)
                    generated_qrs.append(qr_content)
                    generated_meta.append({
                        'process_no': process_no,
                        'order_no': order_no,
                        'item_no': item_no,
                        'product_name': data.get('product_name', ''),
                        'box_index': global_box_idx,
                        'qty': box_qty,
                        'std_qty': std_qty,
                        'box_type': box_type,
                        'total_boxes': total_boxes,
                        'shipment_time': shipment_time_for_qr,
                    })

                append_special_labels(
                    generated_images, generated_zpls, generated_qrs, generated_meta,
                    render_fields, total_boxes, std_qty, pack_info,
                    print_time, shipment_time_for_qr, active_template_for_print,
                    {
                        'process_no': process_no,
                        'order_no': order_no,
                        'item_no': item_no,
                        'product_name': data.get('product_name', ''),
                    },
                    special_label_counts,
                )
                tray_rows_to_append = st.session_state.get(tray_rows_key, tray_rows)
                if st.session_state.get(tray_include_key):
                    appended_tray_rows = append_tray_labels(
                        generated_images,
                        generated_zpls,
                        generated_qrs,
                        generated_meta,
                        tray_rows_to_append,
                        tray_label_date,
                        {
                            'process_no': process_no,
                            'order_no': order_no,
                            'item_no': item_no,
                            'product_name': data.get('product_name', ''),
                        },
                    )
                    if appended_tray_rows:
                        st.session_state[tray_include_key] = False

                st.session_state['generated_images'] = generated_images
                st.session_state['generated_qrs'] = generated_qrs
                st.session_state['generated_meta'] = generated_meta
                st.session_state['generated_zpls'] = generated_zpls
                st.session_state['print_time_used'] = print_time
                st.session_state['print_context'] = {
                    'process_no': process_no,
                    'item_no': item_no,
                    'total_boxes': total_boxes,
                    'reprint_mode': reprint_mode,
                    'record_key': record_key,
                    'template_name': selected_template_name,
                    'template_signature': template_sig,
                }

            st.success(f"✅ 已生成 {len(generated_images)} 张箱贴")

        if batch_gen_clicked:
            generated_images = []
            generated_qrs = []
            generated_meta = []
            generated_zpls = []
            print_time = label_print_time
            with st.spinner(f"正在批量渲染 {len(batch_records)} 款产品箱贴..."):
                for record in batch_records:
                    rec_process_no = str(record.get('process_no', ''))
                    rec_item_no = str(record.get('item_no', ''))
                    rec_order_qty = int(record.get('order_qty', 0) or 0)
                    rec_history = get_print_history(rec_process_no, rec_item_no)
                    rec_start_box = 1 if reprint_mode else int(rec_history.get('max_box_index', 0) or 0) + 1
                    rec_total_boxes = -(-rec_order_qty // std_qty) if std_qty > 0 and rec_order_qty > 0 else n_boxes_this_batch
                    rec_total_boxes = max(rec_total_boxes, rec_start_box + n_boxes_this_batch - 1)
                    rec_fields = make_fields_for_record(record)
                    ensure_operator_field(rec_fields, current_user, visible=show_operator_on_label)
                    rec_fields = fields_with_print_date(rec_fields, print_time)
                    rec_template_for_print = prepare_template_for_operator(active_template, show_operator_on_label)

                    for i, box_dist in enumerate(boxes_dist):
                        global_box_idx = int(box_dist.get('box_index', rec_start_box + i))
                        box_qty = box_dist['qty']
                        box_type = box_dist['box_type']
                        label_img = generate_single_label(
                            fields=copy.deepcopy(rec_fields),
                            current_box_index=global_box_idx,
                            total_boxes=rec_total_boxes,
                            current_box_qty=box_qty,
                            std_qty=std_qty,
                            pack_info=pack_info,
                            box_type=box_type,
                            print_time=print_time,
                            shipment_time=shipment_time_for_qr,
                            template=rec_template_for_print,
                        )
                        generated_images.append(label_img)
                        generated_zpls.append(build_single_label_zpl(
                            fields=copy.deepcopy(rec_fields),
                            current_box_index=global_box_idx,
                            total_boxes=rec_total_boxes,
                            current_box_qty=box_qty,
                            std_qty=std_qty,
                            pack_info=pack_info,
                            box_type=box_type,
                            print_time=print_time,
                            shipment_time=shipment_time_for_qr,
                            template=rec_template_for_print,
                        ))
                        qr_ctx = build_render_context(
                            rec_fields, global_box_idx, rec_total_boxes, box_qty, std_qty,
                            pack_info=pack_info,
                            box_type=box_type,
                            print_time=print_time,
                            shipment_time=shipment_time_for_qr,
                            template=rec_template_for_print,
                        )
                        qr_content = build_qr_content_from_context(rec_template_for_print, qr_ctx)
                        generated_qrs.append(qr_content)
                        generated_meta.append({
                            'process_no': rec_process_no,
                            'order_no': record.get('order_no', ''),
                            'item_no': rec_item_no,
                            'product_name': record.get('product_name', ''),
                            'box_index': global_box_idx,
                            'qty': box_qty,
                            'std_qty': std_qty,
                            'box_type': box_type,
                            'total_boxes': rec_total_boxes,
                            'shipment_time': shipment_time_for_qr,
                        })
                    append_special_labels(
                        generated_images, generated_zpls, generated_qrs, generated_meta,
                        rec_fields, rec_total_boxes, std_qty, pack_info,
                        print_time, shipment_time_for_qr, rec_template_for_print,
                        {
                            'process_no': rec_process_no,
                            'order_no': record.get('order_no', ''),
                            'item_no': rec_item_no,
                            'product_name': record.get('product_name', ''),
                        },
                        special_label_counts,
                    )

            st.session_state['generated_images'] = generated_images
            st.session_state['generated_qrs'] = generated_qrs
            st.session_state['generated_meta'] = generated_meta
            st.session_state['generated_zpls'] = generated_zpls
            st.session_state['print_time_used'] = print_time
            st.session_state['print_context'] = {
                'process_no': 'BATCH',
                'item_no': '',
                'total_boxes': 0,
                'reprint_mode': reprint_mode,
                'record_key': 'BATCH',
                'template_name': selected_template_name,
                'template_signature': template_sig,
            }
            st.success(f"✅ 已批量生成 {len(generated_images)} 张箱贴")

        # ============== 预览 + 打印 ==============
        if 'generated_images' in st.session_state:
            generated_images = st.session_state['generated_images']
            generated_qrs = st.session_state['generated_qrs']
            generated_meta = st.session_state['generated_meta']
            generated_zpls = st.session_state.get('generated_zpls', [])
            print_ctx = st.session_state.get('print_context', {})
            generated_template_sig = print_ctx.get('template_signature')
            output_is_tray = bool(print_ctx.get('tray_mode'))
            stale_generated_output = bool(generated_zpls and not output_is_tray and generated_template_sig != template_sig)

            preview_title = "拖贴预览" if output_is_tray else "箱贴预览"
            with st.expander(f"📸 全部 {len(generated_images)} 张{preview_title}(点击展开)"):
                if stale_generated_output:
                    st.error(
                        "当前预览/ZPL 是旧模板生成的。请重新生成箱贴后再打印。"
                        f"旧签名: {generated_template_sig or '无'}，当前签名: {template_sig}"
                    )
                cols = st.columns(2)
                for i, (img, meta) in enumerate(zip(generated_images, generated_meta)):
                    with cols[i % 2]:
                        gidx = meta['box_index']
                        gqty = meta['qty']
                        gtype = meta['box_type']
                        gtotal = meta['total_boxes']
                        type_label = {'full': '满箱', 'tail': '尾箱', 'pack': '尾包', 'blank': '空标签', 'spare': '备品', 'stock': '库存', 'tray': '拖贴'}.get(gtype, '?')
                        if gtype == 'spare':
                            cap = f"{meta['item_no']} 备品箱贴（箱号/数量空白）"
                        elif gtype == 'stock':
                            cap = f"{meta['item_no']} 库存箱贴（数量:{int(meta.get('qty', 0) or 0):,} pcs）"
                        elif gtype == 'blank':
                            cap = f"{meta['item_no']} 空标签（每箱数量/总箱数空白）"
                        elif gtype == 'tray':
                            cap = (
                                f"{meta['item_no']} 拖贴 {int(gidx):04d} "
                                f"({gqty} pcs, 第{int(meta.get('tray_copy_index', 1) or 1)}/"
                                f"{int(meta.get('tray_copies', 1) or 1)}份)"
                            )
                        else:
                            cap = (
                                f"{meta['item_no']} 第 {gidx}/{gtotal} 箱 "
                                f"({type_label}, {gqty} pcs)"
                            )
                        st.image(img, caption=cap, width=400)

            # 生成 PDF(203 DPI 匹配斑马 ZD888)
            pdf_buffer = io.BytesIO()
            generated_images[0].save(
                pdf_buffer,
                format="PDF",
                save_all=True,
                append_images=generated_images[1:] if len(generated_images) > 1 else [],
                resolution=203.0,  # 匹配斑马 ZD888 的 203 DPI
            )
            pdf_bytes = pdf_buffer.getvalue()

            st.markdown("#### 本地打印登记与导出")
            st.caption("下载文件后可登记本批箱贴，再复制箱码到扫码入库、扫码出库或溯源查询。")
            eligible = [qr for qr, meta in zip(generated_qrs, generated_meta) if qr and not meta.get("skip_log")]
            for qr in eligible:
                st.code(qr, language=None)
            if st.button("登记本批箱贴到本地打印流水", disabled=not eligible or stale_generated_output):
                token = print_ctx.setdefault("demo_job_token", uuid.uuid4().hex)
                with print_batch(token) as fresh:
                    if fresh:
                        count = write_generated_print_logs(generated_qrs, generated_meta, print_ctx, "本地文件导出", current_user)
                st.success(f"已登记 {count} 张箱贴。" if fresh else "此批箱贴已登记。")
            with st.container():
                col_b, col_c = st.columns(2)
                with col_b:
                    st.download_button(
                        label="导出 PDF",
                        data=pdf_bytes,
                        file_name=f"装箱贴_{order_no}_{process_no}_{item_no}.pdf",
                        mime="application/pdf",
                    )
                with col_c:
                    st.download_button(
                        label="导出 ZPL",
                        data=''.join(generated_zpls).encode('utf-8'),
                        file_name=f"装箱贴_{order_no}_{process_no}_{item_no}.zpl",
                        mime="text/plain",
                        disabled=not generated_zpls,
                    )


with tab1:
    if 'ready_to_print' in st.session_state:
        st.markdown("---")
        render_print_panel(current_user, is_admin)

# ============================================================
# Tab 4: ERP出货追溯(v4 新增)
# ============================================================
if False:
    st.subheader("🚚 ERP 出库记录追溯")
    st.caption(
        "这里查询的是 ERP `bcpchd / bcpchdmx` 出货单数据,不是本系统打印历史。"
        "如果 ERP 尚未录入出货单,这里会查不到；打印历史请在 Tab2 或 Tab3 查看。"
    )

    col_t4_1, col_t4_2 = st.columns([1, 3])
    with col_t4_1:
        t4_search_type = st.selectbox(
            "检索依据",
            ["工艺单号", "工艺单号+料号"],
            key='t4_search_type',
        )
    with col_t4_2:
        if t4_search_type == "工艺单号":
            t4_keyword = st.text_input(
                "工艺单号",
                key='t4_kw_process',
                placeholder="例如:900001",
            )
            t4_item = ''
        else:
            col_a, col_b = st.columns(2)
            with col_a:
                t4_keyword = st.text_input(
                    "工艺单号",
                    key='t4_kw_combo_p',
                    placeholder="例如:900001",
                )
            with col_b:
                t4_item = st.text_input(
                    "料号(精确或部分)",
                    key='t4_kw_combo_i',
                    placeholder="例如:DEMO-A01",
                )

    if st.button("🔍 查询 ERP 出货记录", type="primary", key='t4_query_btn'):
        if not t4_keyword:
            st.warning("请输入工艺单号")
        else:
            with st.spinner("正在查询 ERP 出货数据..."):
                try:
                    shipments = fetch_shipment_by_process_no(
                        t4_keyword,
                        t4_item if t4_item else None,
                    )
                except Exception as e:
                    st.error(f"查询失败:{e}")
                    shipments = pd.DataFrame()

            if shipments.empty:
                st.info(
                    f"未找到工艺单号 {t4_keyword}"
                    + (f" + 料号 {t4_item}" if t4_item else "")
                    + " 的任何 ERP 出货记录。\n\n"
                    "可能原因:\n"
                    "1. 该工艺单还没出过货\n"
                    "2. 工艺单号或料号不正确\n"
                    "3. 仓库还没在 ERP 录入"
                )
            else:
                # 汇总信息
                total_shipments = len(shipments)
                total_qty = shipments['出货数量'].sum() if '出货数量' in shipments.columns else 0
                unique_orders = shipments['出货单号'].nunique() if '出货单号' in shipments.columns else 0

                st.success(
                    f"✅ 找到 **{unique_orders}** 张出货单,共 **{total_shipments}** 条明细,"
                    f"累计出货 **{int(total_qty):,} pcs**"
                )

                # 按料号汇总(如果有多款)
                if '半成品名称' in shipments.columns:
                    by_item = shipments.groupby(['半成品编号', '半成品名称'], dropna=False).agg(
                        出货次数=('出货单号', 'nunique'),
                        累计数量=('出货数量', 'sum'),
                    ).reset_index()
                    if len(by_item) > 1:
                        st.markdown("#### 📊 按料号汇总")
                        st.dataframe(by_item, use_container_width=True, hide_index=True)

                # 详细记录
                st.markdown("#### 📋 出货明细(按时间倒序)")
                st.dataframe(
                    shipments,
                    use_container_width=True,
                    hide_index=True,
                )

                # 提供 CSV 下载
                csv_bytes = shipments.to_csv(index=False).encode('utf-8-sig')
                st.download_button(
                    "📥 导出为 CSV",
                    data=csv_bytes,
                    file_name=f"出货记录_{t4_keyword}_{t4_item or 'all'}.csv",
                    mime="text/csv",
                )


# ============================================================
# Tab 5: 模板管理(管理员)
# ============================================================
if is_admin and tab5 is not None:
    with tab5:
        from template_ui import render_template_tab
        render_template_tab(current_user)


if is_admin and tab6 is not None:
    with tab6:
        st.subheader("👤 操作员账号管理")
        st.caption("账号保存在 SQL Server，所有电脑连接同一数据库后共用。密码不会明文保存。")

        if list_system_users is None or save_system_user is None:
            show_db_manager_account_error()
            st.stop()

        try:
            users_df = list_system_users()
        except Exception as exc:
            st.error(f"读取账号失败：{exc}")
            users_df = pd.DataFrame()

        if not users_df.empty:
            show_df = users_df.copy()
            rename_map = {
                'id': 'ID',
                'username': '账号',
                'display_name': '姓名',
                'role_code': '权限',
                'is_active': '启用',
                'created_at': '创建时间',
                'last_login_at': '最后登录',
            }
            show_df = show_df.rename(columns=rename_map)
            st.dataframe(show_df[list(rename_map.values())], use_container_width=True, hide_index=True)

        st.markdown("### 新增账号")
        add_c1, add_c2, add_c3, add_c4 = st.columns([1.2, 1.2, 1.2, 0.9])
        with add_c1:
            new_username = st.text_input("新账号", key="user_add_username")
        with add_c2:
            new_display_name = st.text_input("姓名", key="user_add_display")
        with add_c3:
            new_password = st.text_input("初始密码", type="password", key="user_add_password")
        with add_c4:
            new_role = st.selectbox("权限", ["operator", "admin"], format_func=lambda x: "操作员" if x == "operator" else "管理员", key="user_add_role")
        if st.button("创建账号", type="primary"):
            try:
                save_system_user(
                    username=new_username,
                    display_name=new_display_name,
                    password=new_password,
                    role_code=new_role,
                    is_active=True,
                    operator=current_user.get('username', 'system'),
                )
                st.success("账号已创建")
                st.rerun()
            except Exception as exc:
                st.error(f"创建失败：{exc}")

        st.markdown("### 编辑账号")
        if users_df.empty:
            st.info("暂无账号数据")
        else:
            user_options = [
                f"{row.username} - {row.display_name} ({row.role_code})"
                for row in users_df.itertuples(index=False)
            ]
            selected_user_label = st.selectbox("选择账号", user_options)
            selected_idx = user_options.index(selected_user_label)
            selected_user = users_df.iloc[selected_idx]

            edit_c1, edit_c2, edit_c3, edit_c4 = st.columns([1.2, 1.2, 1.2, 0.8])
            with edit_c1:
                edit_username = st.text_input("账号", value=str(selected_user['username']), disabled=True)
            with edit_c2:
                edit_display_name = st.text_input("姓名", value=str(selected_user['display_name']), key=f"user_edit_display_{selected_user['id']}")
            with edit_c3:
                edit_password = st.text_input("新密码（不填则不改）", type="password", key=f"user_edit_password_{selected_user['id']}")
            with edit_c4:
                role_values = ["operator", "admin"]
                current_role = str(selected_user['role_code'])
                edit_role = st.selectbox(
                    "权限",
                    role_values,
                    index=role_values.index(current_role) if current_role in role_values else 0,
                    format_func=lambda x: "操作员" if x == "operator" else "管理员",
                    key=f"user_edit_role_{selected_user['id']}",
                )

            is_self = str(selected_user['username']) == str(current_user.get('username'))
            active_value = bool(selected_user['is_active'])
            edit_active = st.checkbox(
                "启用该账号",
                value=active_value,
                disabled=is_self,
                key=f"user_edit_active_{selected_user['id']}",
            )
            if is_self:
                edit_active = True
                edit_role = "admin"
                st.caption("当前登录的管理员账号不能停用，也不能改成操作员。")

            if st.button("保存修改", type="primary"):
                try:
                    save_system_user(
                        username=edit_username,
                        display_name=edit_display_name,
                        password=edit_password if edit_password.strip() else None,
                        role_code=edit_role,
                        is_active=edit_active,
                        operator=current_user.get('username', 'system'),
                    )
                    if is_self:
                        st.session_state['user']['display_name'] = edit_display_name
                    st.success("账号已更新")
                    st.rerun()
                except Exception as exc:
                    st.error(f"保存失败：{exc}")
