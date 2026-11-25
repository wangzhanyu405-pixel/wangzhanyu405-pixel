# -*- coding: utf-8 -*-
"""Template-driven print rendering.

The first template matches the current 80x60mm Zebra label.  The public
functions are intentionally small so app.py can keep using the existing print
flow while the layout stops being hard-coded in the UI layer.
"""

import copy
import datetime
import hashlib
import json
import os

import qrcode
from PIL import Image, ImageDraw, ImageFont
from PIL import ImageFilter


FONT_CANDIDATES = [
    os.path.join("assets", "msyh.ttc"),
    os.path.join("assests", "msyh.ttc"),
    os.path.join("assests", "STHUPO.TTF"),
]

FONT_FAMILIES = {
    "微软雅黑": [
        os.path.join("C:\\", "Windows", "Fonts", "msyh.ttc"),
        os.path.join("assets", "msyh.ttc"),
        os.path.join("assests", "msyh.ttc"),
    ],
    "宋体": [os.path.join("C:\\", "Windows", "Fonts", "simsun.ttc")],
    "黑体": [os.path.join("C:\\", "Windows", "Fonts", "simhei.ttf")],
    "楷体": [os.path.join("C:\\", "Windows", "Fonts", "simkai.ttf")],
    "仿宋": [os.path.join("C:\\", "Windows", "Fonts", "simfang.ttf")],
    "方正博雅宋_GBK": [
        os.path.join("C:\\", "Windows", "Fonts", "FZBYSK.TTF"),
        os.path.join("C:\\", "Windows", "Fonts", "FZBYSJW.TTF"),
        os.path.join("C:\\", "Windows", "Fonts", "FZBoYaSong_GBK.TTF"),
        os.path.join("C:\\", "Windows", "Fonts", "FZSTK.TTF"),
        os.path.join("C:\\", "Windows", "Fonts", "STFANGSO.TTF"),
    ],
    "Arial": [os.path.join("C:\\", "Windows", "Fonts", "arial.ttf")],
}

CUSTOMER_QR_TEMPLATE = "{item_no}#{print_date_compact}{box_index_4}#{box_qty_raw}"
CUSTOMER_QR_FIELDS = ["item_no", "print_date_compact", "box_index_4", "box_qty_raw"]
OLD_DEFAULT_QR_TEMPLATES = {
    "{item_no}#{print_date_compact}#{box_index}#{box_qty_raw}#{print_time_compact}#SHIP{shipment_time_compact}",
    "{item_no}#{print_date_compact}#{box_index_4}#{box_qty_raw}",
}


DEFAULT_LABEL_TEMPLATE = {
    "id": "standard_80x60",
    "name": "80x60标准箱贴",
    "template_type": "label",
    "layout_mode": "grid",
    "output_mode": "zpl_bitmap",
    "font_family": "微软雅黑",
    "paper": {
        "width_mm": 80,
        "height_mm": 60,
        "dpi": 203,
        "dots_per_mm": 8,
        "orientation": "landscape",
    },
    "layout": {
        "margin": 14,
        "line_width": 3,
        "label_col_width": 96,
        "qr_x": 370,
        "qr_draw_boundary": True,
        "row_heights": [74, 56, 54, 58, 58, 58, 58, 52],
    },
    "fonts": {
        "label": 25,
        "sap_no": 28,
        "product_name": 21,
        "normal": 28,
        "item_no": 34,
        "qr_text": 18,
    },
    "rows": [
        {"label": "品名", "field": "product_name", "font": "product_name", "full_width": True, "label_align": "center", "value_align": "left", "value_valign": "middle"},
        {"label": "料号", "field": "item_no", "font": "item_no", "full_width": True, "label_align": "center", "value_align": "center", "value_valign": "middle"},
        {"label": "订单号", "field": "order_no", "font": "normal", "full_width": True, "label_align": "center", "value_align": "center", "value_valign": "middle"},
        {"label": "订单数量", "field": "order_qty", "font": "normal", "qr_side": True, "label_align": "center", "value_align": "center", "value_valign": "middle"},
        {"label": "每箱数量", "field": "box_qty", "font": "normal", "qr_side": True, "label_align": "center", "value_align": "center", "value_valign": "middle"},
        {"label": "总箱数", "field": "box_index_total", "font": "normal", "qr_side": True, "label_align": "center", "value_align": "center", "value_valign": "middle"},
        {"label": "日期", "field": "print_date", "font": "normal", "qr_side": True, "label_align": "center", "value_align": "center", "value_valign": "middle"},
        {"label": "供应商", "field": "supplier", "font": "normal", "full_width": True, "label_align": "center", "value_align": "center", "value_valign": "middle"},
    ],
    "qr": {
        "enabled": True,
        "show_text": True,
        "render_mode": "zpl_native",
        "top_row": 3,
        "bottom_row": 7,
        "padding_x": 52,
        "padding_y": 38,
        "offset_x": 0,
        "offset_y": 30,
        "text_offset_x": 0,
        "text_offset_y": 0,
        "size": 0,
        "text_bottom": 25,
        "zpl_x": 0,
        "zpl_y": 0,
        "zpl_offset_x": 0,
        "zpl_offset_y": 0,
        "zpl_scale": 4,
        "zpl_text_x": 0,
        "zpl_text_y": 0,
        "zpl_text_offset_x": 0,
        "zpl_text_offset_y": 0,
        "template_mode": "customer",
        "fields": CUSTOMER_QR_FIELDS[:],
        "content_template": CUSTOMER_QR_TEMPLATE,
        "text_template": CUSTOMER_QR_TEMPLATE,
        "text_fields": CUSTOMER_QR_FIELDS[:],
        "separator": "#",
        "shipment_prefix": "SHIP",
    },
    "zpl": {
        "rotate_for_media": False,
        "darkness": 18,
        "speed": 3,
        "clarity_mode": "standard",
    },
    "special_marks": {
        "tail": {"text": "尾箱", "font_scale": 1.35, "bold": True},
        "spare": {"text": "备品", "font_scale": 1.35, "bold": True},
        "stock": {"text": "库存", "font_scale": 1.35, "bold": True},
    },
}

BOX_80X60_TEMPLATE = copy.deepcopy(DEFAULT_LABEL_TEMPLATE)
BOX_80X60_TEMPLATE.update({
    "id": "box_80x60_package",
    "name": "80x60盒贴-包装方式",
    "template_type": "label",
    "output_mode": "zpl_bitmap",
})
BOX_80X60_TEMPLATE["layout"].update({
    "margin": 22,
    "line_width": 3,
    "label_col_width": 82,
    "qr_x": 410,
    "qr_draw_boundary": True,
    "row_heights": [82, 72, 72, 72, 72, 66],
})
BOX_80X60_TEMPLATE["fonts"].update({
    "label": 24,
    "product_name": 24,
    "normal": 28,
    "item_no": 32,
    "qr_text": 15,
})
BOX_80X60_TEMPLATE["rows"] = [
    {"label": "品名", "field": "product_name", "font": "product_name", "full_width": True, "label_align": "center", "value_align": "center", "value_valign": "middle", "auto_fit": True},
    {"label": "料号", "field": "item_no", "font": "item_no", "full_width": True, "label_align": "center", "value_align": "center", "value_valign": "middle", "bold": True},
    {"label": "订单号", "field": "order_no", "font": "normal", "full_width": True, "label_align": "center", "value_align": "center", "value_valign": "middle", "bold": True},
    {"label": "数量", "field": "pack_method", "font": "normal", "qr_side": True, "label_align": "center", "value_align": "center", "value_valign": "middle", "bold": True, "auto_fit": True},
    {"label": "日期", "field": "print_date", "font": "normal", "qr_side": True, "label_align": "center", "value_align": "center", "value_valign": "middle"},
    {"label": "供应商", "field": "supplier", "font": "normal", "full_width": True, "label_align": "center", "value_align": "center", "value_valign": "middle", "auto_fit": True},
]
BOX_80X60_TEMPLATE["qr"].update({
    "enabled": True,
    "show_text": True,
    "render_mode": "zpl_native",
    "top_row": 3,
    "bottom_row": 5,
    "padding_x": 24,
    "padding_y": 18,
    "offset_x": 0,
    "offset_y": 0,
    "text_offset_x": 0,
    "text_offset_y": 0,
    "text_bottom": 16,
    "zpl_scale": 4,
})


CANVAS_FIELD_LABELS = {
    "sap_no": "SAP号",
    "product_name": "产品名称",
    "item_no": "料号",
    "order_no": "订单号",
    "order_qty": "订单数量",
    "pack_method": "包装方式",
    "supplier": "供应商",
    "print_date": "日期",
    "print_date_compact": "日期YYYYMMDD",
    "print_time_compact": "打印时间",
    "shipment_time_compact": "发货时间",
    "box_qty": "每箱数量",
    "box_qty_raw": "每箱数量纯数字",
    "box_index": "当前箱序号",
    "box_index_4": "当前箱序号4位",
    "box_index_total": "第N箱/总箱数",
    "total_boxes": "总箱数",
    "std_qty": "标准装量",
}


def list_builtin_templates():
    return [copy.deepcopy(DEFAULT_LABEL_TEMPLATE), copy.deepcopy(BOX_80X60_TEMPLATE)]


def get_default_template():
    return copy.deepcopy(DEFAULT_LABEL_TEMPLATE)


def _canonicalize_template(value):
    if isinstance(value, dict):
        return {
            key: _canonicalize_template(item)
            for key, item in sorted(value.items())
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [_canonicalize_template(item) for item in value]
    return value


def _strip_runtime_metadata(value):
    if isinstance(value, dict):
        return {
            key: _strip_runtime_metadata(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [_strip_runtime_metadata(item) for item in value]
    return value


def get_template_signature(template):
    canonical = _canonicalize_template(template or {})
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:12]


def make_template_copy(template, new_name):
    copied = _strip_runtime_metadata(copy.deepcopy(template or get_default_template()))
    copied["name"] = str(new_name or "").strip()
    copied["_is_default"] = False
    return copied


def _grid_geometry(template):
    paper = template.get("paper", {})
    layout = template.get("layout", {})
    rows = template.get("rows", [])
    dots_per_mm = int(paper.get("dots_per_mm", 8) or 8)
    width = int(paper.get("width_mm", 80) * dots_per_mm)
    height = int(paper.get("height_mm", 60) * dots_per_mm)
    margin = int(layout.get("margin", 14))
    row_heights = [max(0, int(v)) for v in layout.get("row_heights", [])[: len(rows)]]
    if len(row_heights) < len(rows):
        row_heights += [54] * (len(rows) - len(row_heights))
    y_positions = [margin]
    for row_h in row_heights:
        y_positions.append(y_positions[-1] + row_h)
    return {
        "width": width,
        "height": height,
        "margin": margin,
        "right": width - margin,
        "bottom": height - margin,
        "row_heights": row_heights,
        "y_positions": y_positions,
        "row_height_available": max(0, height - margin * 2),
        "row_height_used": sum(row_heights),
    }


def validate_grid_layout(template):
    geometry = _grid_geometry(template)
    issues = []
    if geometry["row_height_used"] > geometry["row_height_available"]:
        issues.append({
            "code": "row_height_overflow",
            "severity": "error",
            "message": (
                f"行高合计 {geometry['row_height_used']} dot，"
                f"超过可用高度 {geometry['row_height_available']} dot。"
            ),
        })
    if not template.get("rows"):
        issues.append({
            "code": "no_rows",
            "severity": "error",
            "message": "模板至少需要保留一行。",
        })
    return {
        "row_height_used": geometry["row_height_used"],
        "row_height_available": geometry["row_height_available"],
        "issues": issues,
        "ok": not any(issue["severity"] == "error" for issue in issues),
    }


def inspect_grid_qr_placement(template, ctx):
    geometry = _grid_geometry(template)
    layout = template.get("layout", {})
    qr = template.get("qr", {})
    y_positions = geometry["y_positions"]
    margin = geometry["margin"]
    right = geometry["right"]
    height = geometry["height"]
    area_left = margin + int(layout.get("qr_x", 370))
    qr_top = max(0, min(int(qr.get("top_row", 3)), len(y_positions) - 1))
    qr_bottom = max(qr_top + 1, min(int(qr.get("bottom_row", 7)), len(y_positions) - 1))
    area_top = y_positions[qr_top]
    area_bottom = y_positions[qr_bottom]
    qr_text = build_qr_plain_text(template, ctx or {}) if qr.get("show_text", True) else ""
    text_h = int(template.get("fonts", {}).get("qr_text", 18)) + 4 if qr_text else 0
    padding_x = max(0, int(qr.get("padding_x", 52) or 0))
    padding_y = max(0, int(qr.get("padding_y", 38) or 0))
    qr_area_w = max(40, right - area_left)
    qr_area_h = max(40, area_bottom - area_top - text_h)
    available = max(40, min(qr_area_w - padding_x, qr_area_h - padding_y))
    requested_size = int(qr.get("size", 0) or 0)
    qr_size = max(40, min(requested_size or available, available))
    base_x = area_left + (qr_area_w - qr_size) // 2
    base_y = area_top
    requested_x = base_x + int(qr.get("offset_x", 0) or 0)
    requested_y = base_y + int(qr.get("offset_y", 30) or 0)
    min_x = area_left + 2
    max_x = right - qr_size - 2
    min_y = margin + 2
    max_y = height - margin - text_h - qr_size - 2
    x = max(min_x, min(requested_x, max_x))
    y_pos = max(min_y, min(requested_y, max_y))
    text_y = area_bottom - int(qr.get("text_bottom", 25)) - text_h
    issues = []
    if x != requested_x:
        issues.append({
            "code": "qr_x_clamped",
            "severity": "warning",
            "message": "二维码已到左右边界，继续调整左右微调不会继续移动。",
        })
    if y_pos != requested_y:
        issues.append({
            "code": "qr_y_clamped",
            "severity": "warning",
            "message": "二维码已到上下边界，继续调整上下微调不会继续移动。",
        })
    if available <= 40:
        issues.append({
            "code": "qr_area_too_small",
            "severity": "warning",
            "message": "二维码区域偏小，建议增加二维码区域或减少留白。",
        })
    return {
        "x": x,
        "y": y_pos,
        "requested_x": requested_x,
        "requested_y": requested_y,
        "area_left": area_left,
        "area_top": area_top,
        "area_right": right,
        "area_bottom": area_bottom,
        "text_y": max(area_top, text_y),
        "qr_size": qr_size,
        "available": available,
        "clamped_x": x != requested_x,
        "clamped_y": y_pos != requested_y,
        "issues": issues,
    }


def get_font(size, family=None):
    candidates = []
    if family:
        candidates.extend(FONT_FAMILIES.get(str(family), []))
    candidates.extend(FONT_CANDIDATES)
    for font_path in candidates:
        try:
            return ImageFont.truetype(font_path, int(size))
        except IOError:
            continue
    return ImageFont.load_default()


def get_bold_font(size, family=None):
    candidates = []
    if family:
        candidates.extend(FONT_FAMILIES.get(str(family), []))
    candidates.extend([
        os.path.join("C:\\", "Windows", "Fonts", "msyhbd.ttc"),
        os.path.join("C:\\", "Windows", "Fonts", "simhei.ttf"),
        os.path.join("C:\\", "Windows", "Fonts", "arialbd.ttf"),
    ])
    candidates.extend(FONT_CANDIDATES)
    for font_path in candidates:
        try:
            return ImageFont.truetype(font_path, int(size))
        except IOError:
            continue
    return ImageFont.load_default()


def generate_qr_code(qr_string):
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        box_size=4,
        border=1,
    )
    qr.add_data(qr_string)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def resize_qr_sharp(qr_img, target_size):
    return qr_img.resize((int(target_size), int(target_size)), Image.Resampling.NEAREST)


def _coerce_label_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return datetime.date.today()


def build_tray_qr_content(item_no, label_date, tray_no, qty):
    """Build tray-label QR/plain text: item#YYYYMMDDNNNN#qty."""
    item = str(item_no or "").strip()
    date_text = _coerce_label_date(label_date).strftime("%Y%m%d")
    tray_text = f"{max(0, int(tray_no or 0)):04d}"
    qty_text = str(max(0, int(qty or 0)))
    return f"{item}#{date_text}{tray_text}#{qty_text}"


def _is_blank_editor_value(value):
    if value is None:
        return True
    try:
        if value != value:
            return True
    except Exception:
        pass
    text = str(value).strip()
    return text == "" or text.lower() in ("none", "nan", "null", "<na>")


def _tray_text(value):
    return "" if _is_blank_editor_value(value) else str(value).strip()


def _tray_int(value, default):
    if _is_blank_editor_value(value):
        return int(default)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def complete_tray_editor_rows(rows, default_item_no="", default_qty=5000):
    """Fill Streamlit data-editor blank rows by copying previous values and incrementing tray no."""
    completed = []
    last_item = str(default_item_no or "").strip()
    last_tray_no = 0
    last_qty = max(1, _tray_int(default_qty, 5000))
    last_copies = 1
    for row in rows or []:
        has_any_value = any(
            not _is_blank_editor_value(row.get(col))
            for col in ("料号", "拖号", "数量", "打印份数", "item_no", "tray_no", "qty", "copies")
        )
        if not has_any_value and completed:
            item_no = last_item
            tray_no = last_tray_no + 1
            qty = last_qty
            copies = last_copies
        else:
            item_no = _tray_text(row.get("料号") if "料号" in row else row.get("item_no")) or last_item
            tray_no_default = last_tray_no + 1 if completed else 1
            tray_no = max(1, _tray_int(row.get("拖号") if "拖号" in row else row.get("tray_no"), tray_no_default))
            qty = max(1, _tray_int(row.get("数量") if "数量" in row else row.get("qty"), last_qty))
            copies = max(1, _tray_int(row.get("打印份数") if "打印份数" in row else row.get("copies"), last_copies))
        if not item_no:
            continue
        normalized_row = {
            "料号": item_no,
            "拖号": tray_no,
            "数量": qty,
            "打印份数": copies,
        }
        completed.append(normalized_row)
        last_item = item_no
        last_tray_no = tray_no
        last_qty = qty
        last_copies = copies
    if not completed and last_item:
        completed.append({
            "料号": last_item,
            "拖号": 1,
            "数量": last_qty,
            "打印份数": last_copies,
        })
    return completed


def normalize_tray_label_rows(rows, label_date):
    """Return printable tray rows, expanding each row by its copy count."""
    normalized = []
    for row in rows or []:
        item_no = _tray_text(row.get("item_no") if "item_no" in row else row.get("料号"))
        if not item_no:
            continue
        tray_no = max(1, _tray_int(row.get("tray_no") if "tray_no" in row else row.get("拖号"), 1))
        qty = max(1, _tray_int(row.get("qty") if "qty" in row else row.get("数量"), 0))
        copies = max(1, _tray_int(row.get("copies") if "copies" in row else row.get("打印份数"), 1))
        qr_content = build_tray_qr_content(item_no, label_date, tray_no, qty)
        for copy_index in range(1, copies + 1):
            normalized.append({
                "item_no": item_no,
                "tray_no": tray_no,
                "tray_no_4": f"{tray_no:04d}",
                "qty": qty,
                "copies": copies,
                "copy_index": copy_index,
                "qr_content": qr_content,
            })
    return normalized


def render_tray_label_image(item_no, label_date, tray_no, qty, width_mm=80, height_mm=60, dpi=203):
    dots_per_mm = 8 if int(dpi or 203) == 203 else max(1, round(int(dpi) / 25.4))
    width = int(width_mm * dots_per_mm)
    height = int(height_mm * dots_per_mm)
    qr_content = build_tray_qr_content(item_no, label_date, tray_no, qty)
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    line_w = 3
    margin = 14
    draw.rectangle([margin, margin, width - margin, height - margin], outline="black", width=line_w)

    text_font = get_font(18, "Arial")
    text_box = draw.textbbox((0, 0), qr_content, font=text_font)
    text_w = text_box[2] - text_box[0]
    text_h = text_box[3] - text_box[1]
    text_y = height - margin - text_h - 52

    max_qr_size = min(width - margin * 2 - 150, text_y - margin - 70)
    qr_size = max(140, min(275, int(max_qr_size)))
    qr_img = resize_qr_sharp(generate_qr_code(qr_content), qr_size)
    qr_x = (width - qr_size) // 2
    qr_y = margin + max(24, (text_y - margin - qr_size) // 2)
    img.paste(qr_img, (qr_x, qr_y))

    text_x = max(margin + 4, (width - text_w) // 2)
    draw.text((text_x, text_y), qr_content, font=text_font, fill="black")
    return img


def render_tray_label_zpl(item_no, label_date, tray_no, qty, width_mm=80, height_mm=60, dpi=203):
    dots_per_mm = 8 if int(dpi or 203) == 203 else max(1, round(int(dpi) / 25.4))
    width = int(width_mm * dots_per_mm)
    height = int(height_mm * dots_per_mm)
    margin = 14
    border_w = max(40, width - margin * 2)
    border_h = max(40, height - margin * 2)
    qr_content = _zpl_safe_data(build_tray_qr_content(item_no, label_date, tray_no, qty))

    qr_scale = 7
    qr_x = max(margin + 10, (width - 205) // 2)
    qr_y = margin + 58
    text_y = height - margin - 96
    return (
        "^XA"
        "^CI28"
        f"^PW{width}"
        f"^LL{height}"
        "^LH0,0"
        "^MD8"
        "^PR3"
        f"^FO{margin},{margin}^GB{border_w},{border_h},3^FS"
        f"^FO{qr_x},{qr_y}^BQN,2,{qr_scale}^FDQA,{qr_content}^FS"
        f"^FO{margin},{text_y}^A0N,18,16^FB{border_w},1,0,C,0^FD{qr_content}^FS"
        "^XZ"
    )


def _format_time(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, str):
        raw = value.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S", "%Y.%m.%d %H:%M:%S"):
            try:
                return datetime.datetime.strptime(raw, fmt)
            except ValueError:
                pass
    return fallback


def _field_dict(fields):
    return {f["key"]: f for f in fields if f.get("visible", True)}


def _blank_zero_code(value):
    s = str(value or '').strip()
    return '' if s in ('0', '0.0', '0.00') else s


def get_special_mark(template, box_type):
    marks = template.setdefault("special_marks", {})
    defaults = {
        "tail": {"text": "尾箱", "font_scale": 1.35, "bold": True},
        "spare": {"text": "备品", "font_scale": 1.35, "bold": True},
        "stock": {"text": "库存", "font_scale": 1.35, "bold": True},
    }
    cfg = copy.deepcopy(defaults.get(str(box_type or ""), {}))
    cfg.update(marks.get(str(box_type or ""), {}) or {})
    return cfg


def build_render_context(fields, current_box_index, total_boxes, current_box_qty,
                         std_qty, pack_info=None, box_type="full",
                         print_time=None, shipment_time=None, template=None):
    print_time = print_time or datetime.datetime.now()
    shipment_dt = _format_time(shipment_time, print_time)
    fd = _field_dict(fields)

    qty_display = f"{int(current_box_qty)} pcs"
    if pack_info and box_type != "pack":
        per_pack = int(pack_info.get("per_pack", 0) or 0)
        actual_packs = int(current_box_qty) // per_pack if per_pack else 0
        if per_pack and actual_packs:
            qty_display = f"{int(current_box_qty)}({actual_packs}×{per_pack})"
    special_template = template or DEFAULT_LABEL_TEMPLATE
    if box_type == "tail":
        tail_text = get_special_mark(special_template, "tail").get("text", "尾箱")
        if int(current_box_qty or 0) > 0:
            qty_display += f" {tail_text}"
        else:
            qty_display = tail_text
    elif box_type == "pack":
        qty_display += " 尾包"
    elif box_type == "spare":
        qty_display = get_special_mark(special_template, "spare").get("text", "备品")
    elif box_type == "stock":
        stock_mark = get_special_mark(special_template, "stock").get("text", "库存")
        if int(current_box_qty or 0) > 0:
            qty_display += f" {stock_mark}"
        else:
            qty_display = stock_mark
    elif box_type == "blank":
        qty_display = ""

    if box_type in ("spare", "stock", "blank") or (box_type == "tail" and int(current_box_qty or 0) <= 0):
        box_index = ""
        box_index_4 = ""
        box_index_total = ""
        box_qty_raw = "" if box_type in ("spare", "tail", "blank") or int(current_box_qty or 0) <= 0 else str(int(current_box_qty))
        total_boxes_text = ""
    else:
        box_index = f"{int(current_box_index):03d}"
        box_index_4 = f"{int(current_box_index):04d}"
        box_index_total = f"{int(current_box_index)} / {int(total_boxes)} 箱"
        box_qty_raw = str(int(current_box_qty))
        total_boxes_text = str(int(total_boxes))

    ctx = {
        "sap_no": _blank_zero_code(fd.get("sap_no", {}).get("value", "")),
        "product_name": str(fd.get("product_name", {}).get("value", "")),
        "item_no": str(fd.get("item_no", {}).get("value", "")),
        "order_no": str(fd.get("order_no", {}).get("value", "")),
        "order_qty": str(fd.get("order_qty", {}).get("value", "")),
        "pack_method": str(fd.get("pack_method", {}).get("value", "")),
        "supplier": str(fd.get("supplier", {}).get("value", "演示包装有限公司")),
        "operator_name": str(fd.get("operator_name", {}).get("value", "")),
        "print_date": str(fd.get("date", {}).get("value", print_time.strftime("%Y.%m.%d"))),
        "print_date_compact": print_time.strftime("%Y%m%d"),
        "print_time_compact": print_time.strftime("%Y%m%d%H%M%S"),
        "shipment_time_compact": shipment_dt.strftime("%Y%m%d%H%M%S"),
        "box_qty": qty_display,
        "box_qty_raw": box_qty_raw,
        "box_index": box_index,
        "box_index_4": box_index_4,
        "box_index_total": box_index_total,
        "total_boxes": total_boxes_text,
        "std_qty": str(int(std_qty)),
        "box_type": str(box_type or "full"),
    }
    return ctx


def _resolve_qr_template(qr, key):
    if qr.get("template_mode") == "customer":
        return CUSTOMER_QR_TEMPLATE
    value = qr.get(key)
    if value in OLD_DEFAULT_QR_TEMPLATES:
        return CUSTOMER_QR_TEMPLATE
    return value


def build_qr_content_from_context(template, ctx):
    qr = template.get("qr", {})
    content_template = _resolve_qr_template(qr, "content_template")
    if content_template:
        try:
            return content_template.format(**ctx)
        except Exception:
            pass
    sep = qr.get("separator", "#")
    parts = []
    for field in qr.get("fields", []):
        value = ctx.get(field, "")
        if field == "shipment_time_compact":
            value = f"{qr.get('shipment_prefix', 'SHIP')}{value}"
        parts.append(str(value))
    return sep.join(parts)


def build_qr_plain_text(template, ctx):
    qr = template.get("qr", {})
    text_template = _resolve_qr_template(qr, "text_template")
    if text_template:
        try:
            return text_template.format(**ctx)
        except Exception:
            pass
    sep = qr.get("separator", "#")
    return sep.join(str(ctx.get(field, "")) for field in qr.get("text_fields", []))


def resolve_row_value(row, ctx):
    if row.get("field") == "__fixed__":
        return row.get("fixed_text", "")
    return ctx.get(row.get("field", ""), "")


def resolve_row_font_size(row, font_cfg):
    if row.get("font_size") is not None:
        try:
            return int(row.get("font_size"))
        except (TypeError, ValueError):
            pass
    return int(font_cfg.get(row.get("font", "normal"), font_cfg.get("normal", 28)))


def resolve_label_font_size(row, font_cfg):
    if row.get("label_font_size") is not None:
        try:
            return int(row.get("label_font_size"))
        except (TypeError, ValueError):
            pass
    return int(font_cfg.get("label", 25))


def _text_size(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _wrap_text(draw, text, font, max_width):
    text = str(text or "")
    lines = []
    for raw_line in text.splitlines() or [""]:
        line = ""
        for ch in raw_line:
            test = line + ch
            if line and _text_size(draw, test, font)[0] > max_width:
                lines.append(line)
                line = ch
            else:
                line = test
        lines.append(line)
    return lines or [""]


def draw_box_text(draw, box, text, font_size, align="center", valign="middle",
                  padding=4, min_size=8, font_family=None, auto_fit=False):
    x1, y1, x2, y2 = [int(v) for v in box]
    max_width = max(1, x2 - x1 - padding * 2)
    max_height = max(1, y2 - y1 - padding * 2)
    text = str(text or "")
    selected = None
    start_size = int(font_size)
    if auto_fit and text.strip():
        start_size = max(start_size, min(96, max_height * 2))

    for size in range(int(start_size), int(min_size) - 1, -1):
        font = get_font(size, font_family)
        line_height = max(_text_size(draw, "国", font)[1], size) + 2
        lines = _wrap_text(draw, text, font, max_width)
        total_height = line_height * len(lines)
        widest = max((_text_size(draw, line, font)[0] for line in lines), default=0)
        if total_height <= max_height and widest <= max_width:
            selected = font, lines, line_height, total_height
            break

    if selected is None:
        font = get_font(min_size, font_family)
        line_height = max(_text_size(draw, "国", font)[1], min_size) + 2
        max_lines = max(1, max_height // line_height)
        lines = _wrap_text(draw, text, font, max_width)[:max_lines]
        selected = font, lines, line_height, line_height * len(lines)

    font, lines, line_height, total_height = selected
    if valign == "top":
        y = y1 + padding
    elif valign == "bottom":
        y = y2 - padding - total_height
    else:
        y = y1 + (y2 - y1 - total_height) / 2

    for line in lines:
        tw = _text_size(draw, line, font)[0]
        if align == "left":
            x = x1 + padding
        elif align == "right":
            x = x2 - padding - tw
        else:
            x = x1 + (x2 - x1 - tw) / 2
        draw.text((x, y), line, font=font, fill="black")
        y += line_height


def draw_marked_box_text(draw, box, text, marker, font_size, align="center", valign="middle",
                         padding=4, min_size=8, font_family=None, marker_scale=1.35):
    x1, y1, x2, y2 = [int(v) for v in box]
    suffix = str(marker or "").strip()
    main = str(text or "").replace(suffix, "").strip()
    max_width = max(1, x2 - x1 - padding * 2)
    max_height = max(1, y2 - y1 - padding * 2)
    selected = None
    start_size = int(font_size)
    for main_size in range(start_size, int(min_size) - 1, -1):
        suffix_size = max(main_size + 6, int(main_size * float(marker_scale or 1.35)))
        for suffix_try in range(suffix_size, int(min_size) - 1, -1):
            main_font = get_font(main_size, font_family)
            suffix_font = get_bold_font(suffix_try, font_family)
            main_w, main_h = _text_size(draw, main, main_font)
            suffix_w, suffix_h = _text_size(draw, suffix, suffix_font)
            gap = max(6, main_size // 3) if main else 0
            total_w = main_w + gap + suffix_w
            total_h = max(main_h, suffix_h)
            if total_w <= max_width and total_h <= max_height:
                selected = main_font, suffix_font, main_w, suffix_w, total_w, total_h, gap
                break
        if selected:
            break
    if selected is None:
        draw_box_text(draw, box, text, font_size, align, valign, padding, min_size, font_family)
        return

    main_font, suffix_font, main_w, suffix_w, total_w, total_h, gap = selected
    if align == "left":
        x = x1 + padding
    elif align == "right":
        x = x2 - padding - total_w
    else:
        x = x1 + (x2 - x1 - total_w) / 2
    if valign == "top":
        y = y1 + padding
    elif valign == "bottom":
        y = y2 - padding - total_h
    else:
        y = y1 + (y2 - y1 - total_h) / 2

    main_h = _text_size(draw, main, main_font)[1]
    suffix_h = _text_size(draw, suffix, suffix_font)[1]
    draw.text((x, y + (total_h - main_h) / 2), main, font=main_font, fill="black")
    sx = x + main_w + gap
    sy = y + (total_h - suffix_h) / 2
    draw.text((sx, sy), suffix, font=suffix_font, fill="black")
    draw.text((sx + 1, sy), suffix, font=suffix_font, fill="black")


def rows_to_canvas_elements(template):
    layout = template.get("layout", {})
    paper = template.get("paper", {})
    dots_per_mm = int(paper.get("dots_per_mm", 8))
    width = int(paper.get("width_mm", 80) * dots_per_mm)
    height = int(paper.get("height_mm", 60) * dots_per_mm)
    margin = int(layout.get("margin", 14))
    line_w = int(layout.get("line_width", 3))
    left = top = margin
    right = width - margin
    bottom = height - margin
    row_heights = [int(v) for v in layout.get("row_heights", [])]
    y = [top]
    for row_h in row_heights:
        y.append(y[-1] + row_h)

    elements = [{
        "id": "outer_border",
        "name": "外框",
        "type": "rect",
        "x": left,
        "y": top,
        "w": right - left,
        "h": bottom - top,
        "line_width": line_w,
        "z": 1,
        "visible": True,
    }]

    for idx, yy in enumerate(y[1:-1], start=1):
        if template.get("qr", {}).get("enabled", True) and int(template.get("qr", {}).get("top_row", 3)) < idx < int(template.get("qr", {}).get("bottom_row", 7)):
            line_right = left + int(layout.get("qr_x", 370))
        else:
            line_right = right
        elements.append({
            "id": f"line_h_{idx}",
            "name": f"横线{idx}",
            "type": "line",
            "orientation": "h",
            "x": left,
            "y": yy,
            "w": line_right - left,
            "h": 0,
            "line_width": line_w,
            "z": 2,
            "visible": True,
        })

    elements.append({
        "id": "line_v_label",
        "name": "字段分隔线",
        "type": "line",
        "orientation": "v",
        "x": left + int(layout.get("label_col_width", 96)),
        "y": top,
        "w": 0,
        "h": bottom - top,
        "line_width": line_w,
        "z": 2,
        "visible": True,
    })

    for idx, row in enumerate(template.get("rows", [])):
        row_y = y[idx]
        row_h = row_heights[idx] if idx < len(row_heights) else 50
        label_w = int(row.get("label_width") or layout.get("label_col_width", 96))
        value_x = left + label_w
        qr_enabled = bool(template.get("qr", {}).get("enabled", True))
        value_right = left + int(layout.get("qr_x", 370)) if qr_enabled and row.get("qr_side") else right
        if row.get("value_width"):
            value_right = min(right, value_x + int(row.get("value_width")))
        value_right = max(value_x + 4, value_right)
        elements.append({
            "id": f"label_{idx}",
            "name": row.get("label", f"字段名{idx + 1}"),
            "type": "text",
            "text": row.get("label", ""),
            "x": left,
            "y": row_y,
            "w": label_w,
            "h": row_h,
            "font_size": int(template.get("fonts", {}).get("label", 25)),
            "align": row.get("label_align", "center"),
            "valign": row.get("label_valign", "middle"),
            "z": 5,
            "visible": True,
        })
        elements.append({
            "id": f"field_{idx}",
            "name": CANVAS_FIELD_LABELS.get(row.get("field"), row.get("field", f"内容{idx + 1}")),
            "type": "field",
            "field": row.get("field", ""),
            "x": value_x,
            "y": row_y,
            "w": value_right - value_x,
            "h": row_h,
            "font_size": int(template.get("fonts", {}).get(row.get("font", "normal"), 28)),
            "align": row.get("value_align", "center"),
            "valign": row.get("value_valign", "middle"),
            "z": 5,
            "visible": True,
        })

    qr = template.get("qr", {})
    if qr.get("enabled", True) and not qr.get("_suppress_bitmap_content", False):
        qr_top = int(qr.get("top_row", 3))
        qr_bottom = int(qr.get("bottom_row", 7))
        area_left = left + int(layout.get("qr_x", 370))
        area_top = y[qr_top] if qr_top < len(y) else top
        area_bottom = y[qr_bottom] if qr_bottom < len(y) else bottom
        elements.append({
            "id": "line_v_qr",
            "name": "二维码分隔线",
            "type": "line",
            "orientation": "v",
            "x": area_left,
            "y": area_top,
            "w": 0,
            "h": area_bottom - area_top,
            "line_width": line_w,
            "z": 3,
            "visible": True,
        })
        elements.append({
            "id": "qr_code",
            "name": "二维码",
            "type": "qr",
            "x": area_left + 34,
            "y": area_top + 30,
            "w": max(40, (right - area_left) - int(qr.get("padding_x", 52))),
            "h": max(40, (area_bottom - area_top) - int(qr.get("padding_y", 38))),
            "align": "center",
            "z": 6,
            "visible": True,
        })
        if qr.get("show_text", True):
            elements.append({
                "id": "qr_plain_text",
                "name": "二维码明文",
                "type": "qr_text",
                "x": area_left + 4,
                "y": area_bottom - int(qr.get("text_bottom", 25)) - 20,
                "w": right - area_left - 8,
                "h": 24,
                "font_size": int(template.get("fonts", {}).get("qr_text", 18)),
                "align": qr.get("text_align", "center"),
                "valign": "middle",
                "z": 7,
                "visible": True,
            })
    return elements


def ensure_canvas_template(template):
    canvas = copy.deepcopy(template)
    canvas["layout_mode"] = "canvas"
    if not canvas.get("elements"):
        canvas["elements"] = rows_to_canvas_elements(canvas)
    return canvas


def render_label_image(template, fields, current_box_index, total_boxes,
                       current_box_qty, std_qty, pack_info=None, box_type="full",
                       print_time=None, shipment_time=None):
    paper = template["paper"]
    dots_per_mm = int(paper.get("dots_per_mm", 8))
    width = int(paper["width_mm"] * dots_per_mm)
    height = int(paper["height_mm"] * dots_per_mm)
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    ctx = build_render_context(
        fields, current_box_index, total_boxes, current_box_qty, std_qty,
        pack_info, box_type, print_time, shipment_time, template,
    )

    if template.get("layout_mode") == "canvas" or template.get("elements"):
        return render_canvas_label_image(template, ctx, img, draw)

    layout = template["layout"]
    font_family = template.get("font_family", "微软雅黑")
    margin = int(layout["margin"])
    line_w = int(layout["line_width"])
    left = top = margin
    right = width - margin
    bottom = height - margin
    label_w = int(layout["label_col_width"])
    default_value_x = left + label_w
    qr_x = left + int(layout["qr_x"])
    row_heights = [int(v) for v in layout["row_heights"]]
    y = [top]
    for row_h in row_heights:
        y.append(y[-1] + row_h)

    draw.rectangle([left, top, right, bottom], outline="black", width=line_w)
    qr = template.get("qr", {})
    qr_top = int(qr.get("top_row", 3))
    qr_bottom = int(qr.get("bottom_row", 7))
    for idx, yy in enumerate(y[1:-1], start=1):
        if qr.get("enabled", True) and qr_top < idx < qr_bottom:
            draw.line([(left, yy), (qr_x, yy)], fill="black", width=line_w)
        else:
            draw.line([(left, yy), (right, yy)], fill="black", width=line_w)

    font_cfg = template["fonts"]

    if qr.get("enabled", True) and layout.get("qr_draw_boundary", False):
        area_top = y[qr_top] if qr_top < len(y) else top
        area_bottom = y[qr_bottom] if qr_bottom < len(y) else bottom
        draw.line([(qr_x, area_top), (qr_x, area_bottom)], fill="black", width=line_w)

    for idx, row in enumerate(template["rows"]):
        row_y = y[idx]
        row_h = row_heights[idx]
        row_label_w = int(row.get("label_width") or label_w)
        value_x = left + row_label_w
        value_right = qr_x if qr.get("enabled", True) and row.get("qr_side") else right
        if row.get("value_width"):
            value_right = min(right, value_x + int(row.get("value_width") or 0))
        value_right = max(value_x + 4, value_right)
        draw.line([(value_x, row_y), (value_x, row_y + row_h)], fill="black", width=line_w)
        draw_box_text(
            draw,
            (left, row_y, value_x, row_y + row_h),
            row.get("label", ""),
            resolve_label_font_size(row, font_cfg),
            align=row.get("label_align", "center"),
            valign=row.get("label_valign", "middle"),
            font_family=font_family,
        )
        value_text = resolve_row_value(row, ctx)
        box_type = ctx.get("box_type")
        mark = get_special_mark(template, box_type) if row.get("field") == "box_qty" else {}
        mark_text = str(mark.get("text", "") or "")
        if row.get("field") == "box_qty" and box_type in ("tail", "spare", "stock") and mark_text and mark_text in value_text:
            draw_marked_box_text(
                draw,
                (value_x, row_y, value_right, row_y + row_h),
                value_text,
                mark_text,
                resolve_row_font_size(row, font_cfg),
                align=row.get("value_align", "center"),
                valign=row.get("value_valign", "middle"),
                font_family=font_family,
                marker_scale=float(mark.get("font_scale", 1.35) or 1.35),
            )
        else:
            draw_box_text(
                draw,
                (value_x, row_y, value_right, row_y + row_h),
                value_text,
                resolve_row_font_size(row, font_cfg),
                align=row.get("value_align", "center"),
                valign=row.get("value_valign", "middle"),
                font_family=font_family,
                auto_fit=bool(row.get("auto_fit", False)),
            )

    if qr.get("enabled", True) and not qr.get("_suppress_bitmap_content", False):
        fonts = {name: get_font(size, font_family) for name, size in font_cfg.items()}
        qr_content = build_qr_content_from_context(template, ctx)
        qr_img = generate_qr_code(qr_content)
        qr_text = build_qr_plain_text(template, ctx) if qr.get("show_text", True) else ""
        qr_text_font = fonts.get("qr_text", get_font(18, font_family))
        t_h = 0
        if qr_text:
            tbox = draw.textbbox((0, 0), qr_text, font=qr_text_font)
            t_h = tbox[3] - tbox[1]
        area_left = qr_x
        area_top = y[qr.get("top_row", 3)]
        area_right = right
        area_bottom = y[qr.get("bottom_row", 7)]
        max_qr_size = min(
            area_right - area_left - int(qr.get("padding_x", 52)),
            area_bottom - area_top - t_h - int(qr.get("padding_y", 38)),
        )
        max_qr_size = max(20, int(max_qr_size))
        requested_qr_size = int(qr.get("size", 0) or 0)
        qr_size = requested_qr_size if requested_qr_size > 0 else max_qr_size
        qr_size = max(20, min(int(qr_size), int(max_qr_size)))
        qr_px = area_left + (area_right - area_left - qr_size) // 2 + int(qr.get("offset_x", 0))
        qr_py = area_top + int(qr.get("offset_y", 30))
        qr_px = max(area_left + 2, min(qr_px, area_right - qr_size - 2))
        qr_py = max(area_top + 2, min(qr_py, area_bottom - t_h - qr_size - 4))
        img.paste(
            resize_qr_sharp(qr_img, qr_size),
            (qr_px, qr_py),
        )
        if qr_text:
            ty = area_bottom - int(qr.get("text_bottom", 25)) - t_h
            text_dx = int(qr.get("text_offset_x", 0) or 0)
            text_dy = int(qr.get("text_offset_y", 0) or 0)
            draw_box_text(
                draw,
                (area_left + 4 + text_dx, ty - 3 + text_dy, area_right - 4 + text_dx, area_bottom - 2 + text_dy),
                qr_text,
                font_cfg.get("qr_text", 18),
                align=qr.get("text_align", "center"),
                valign="top",
                min_size=6,
                font_family=font_family,
            )

    return img


def render_canvas_label_image(template, ctx, img, draw):
    qr_content = None
    qr_plain_text = None
    font_family = template.get("font_family", "微软雅黑")
    for element in sorted(template.get("elements", []), key=lambda item: int(item.get("z", 0))):
        if not element.get("visible", True):
            continue
        typ = element.get("type", "field")
        x = int(element.get("x", 0))
        y = int(element.get("y", 0))
        w = int(element.get("w", 0))
        h = int(element.get("h", 0))
        line_w = int(element.get("line_width", template.get("layout", {}).get("line_width", 2)))
        box = (x, y, x + max(1, w), y + max(1, h))
        if typ == "rect":
            draw.rectangle(box, outline="black", width=max(1, line_w))
        elif typ == "line":
            orientation = element.get("orientation")
            if orientation == "h" or (orientation is None and abs(w) >= abs(h)):
                draw.line([(x, y), (x + w, y)], fill="black", width=max(1, line_w))
            else:
                draw.line([(x, y), (x, y + h)], fill="black", width=max(1, line_w))
        elif typ == "text":
            draw_box_text(
                draw,
                box,
                element.get("text", ""),
                int(element.get("font_size", template.get("fonts", {}).get("normal", 28))),
                align=element.get("align", "center"),
                valign=element.get("valign", "middle"),
                font_family=font_family,
            )
        elif typ == "field":
            draw_box_text(
                draw,
                box,
                element.get("fixed_text", "") if element.get("field") == "__fixed__" else ctx.get(element.get("field", ""), ""),
                int(element.get("font_size", template.get("fonts", {}).get("normal", 28))),
                align=element.get("align", "center"),
                valign=element.get("valign", "middle"),
                font_family=font_family,
            )
        elif typ == "qr":
            if template.get("qr", {}).get("_suppress_bitmap_content", False):
                continue
            if qr_content is None:
                qr_content = build_qr_content_from_context(template, ctx)
            qr_img = generate_qr_code(qr_content)
            size = max(20, min(w, h))
            px = x + (w - size) // 2 if element.get("align", "center") == "center" else x
            py = y + (h - size) // 2
            img.paste(resize_qr_sharp(qr_img, size), (px, py))
        elif typ == "qr_text":
            if template.get("qr", {}).get("_suppress_bitmap_content", False):
                continue
            if qr_plain_text is None:
                qr_plain_text = build_qr_plain_text(template, ctx)
            draw_box_text(
                draw,
                box,
                qr_plain_text,
                int(element.get("font_size", template.get("fonts", {}).get("qr_text", 18))),
                align=element.get("align", "center"),
                valign=element.get("valign", "middle"),
                min_size=6,
                font_family=font_family,
            )
    return img


def _apply_clarity_mode(img, mode):
    mode = str(mode or "standard")
    gray = img.convert("L")
    if mode == "sharp_text":
        gray = gray.filter(ImageFilter.SHARPEN)
        threshold = 165
        mono = gray.point(lambda p: 0 if p < threshold else 255, "1")
    elif mode == "bold_text":
        threshold = 205
        mono = gray.point(lambda p: 0 if p < threshold else 255, "1")
        mono = mono.filter(ImageFilter.MinFilter(3))
    elif mode == "high_contrast":
        gray = gray.filter(ImageFilter.SHARPEN).filter(ImageFilter.SHARPEN)
        threshold = 190
        mono = gray.point(lambda p: 0 if p < threshold else 255, "1")
    else:
        threshold = 180
        mono = gray.point(lambda p: 0 if p < threshold else 255, "1")
    return mono


def _zpl_safe_data(value):
    text = str(value or "")
    return text.replace("^", " ").replace("~", " ").replace("\r", " ").replace("\n", " ")


def _calc_grid_qr_origin(template, ctx):
    placement = inspect_grid_qr_placement(template, ctx)
    return (
        placement["x"],
        placement["y"],
        placement["area_left"],
        placement["text_y"],
        placement["area_right"],
        placement["area_bottom"],
    )


def _native_qr_zpl(template, ctx):
    qr = template.get("qr", {})
    if not qr.get("enabled", True) or qr.get("render_mode", "zpl_native") != "zpl_native":
        return ""
    if template.get("layout_mode") == "canvas" or template.get("elements"):
        qr_elements = [el for el in template.get("elements", []) if el.get("type") == "qr" and el.get("visible", True)]
        text_elements = [el for el in template.get("elements", []) if el.get("type") == "qr_text" and el.get("visible", True)]
        if qr_elements:
            qr_el = qr_elements[0]
            x = int(qr_el.get("x", 0))
            y = int(qr_el.get("y", 0))
            area_left = x
            area_right = x + int(qr_el.get("w", 0))
            area_bottom = y + int(qr_el.get("h", 0))
        else:
            x, y, area_left, _, area_right, area_bottom = _calc_grid_qr_origin(template, ctx)
        text_el = text_elements[0] if text_elements else {}
        text_x = int(text_el.get("x", area_left))
        text_y = int(text_el.get("y", area_bottom + 4))
        text_w = int(text_el.get("w", max(80, area_right - area_left)))
        text_size = int(text_el.get("font_size", template.get("fonts", {}).get("qr_text", 18)))
    else:
        x, y, area_left, text_y, area_right, _ = _calc_grid_qr_origin(template, ctx)
        text_x = area_left + 4
        text_w = max(80, area_right - area_left - 8)
        text_size = int(template.get("fonts", {}).get("qr_text", 18))

    if qr.get("zpl_use_absolute", False):
        x = int(qr.get("zpl_x") or x)
        y = int(qr.get("zpl_y") or y)
    x = x + int(qr.get("zpl_offset_x", 0) or 0)
    y = y + int(qr.get("zpl_offset_y", 0) or 0)
    scale = max(1, min(10, int(qr.get("zpl_scale", 4) or 4)))
    qr_content = _zpl_safe_data(build_qr_content_from_context(template, ctx))
    commands = [f"^FO{x},{y}^BQN,2,{scale}^FDQA,{qr_content}^FS"]
    if qr.get("show_text", True):
        text = _zpl_safe_data(build_qr_plain_text(template, ctx))
        if text:
            if qr.get("zpl_use_absolute", False):
                text_x = int(qr.get("zpl_text_x") or text_x)
                text_y = int(qr.get("zpl_text_y") or text_y)
            text_x = text_x + int(qr.get("zpl_text_offset_x", 0) or 0)
            text_y = text_y + int(qr.get("zpl_text_offset_y", 0) or 0)
            commands.append(f"^FO{text_x},{text_y}^A0N,{text_size},{max(8, text_size - 2)}^FB{text_w},2,0,C,0^FD{text}^FS")
    return "".join(commands)


def image_to_zpl_graphic(img, rotate_for_media=False, darkness=18, speed=3, clarity_mode="standard"):
    if rotate_for_media:
        img = img.rotate(90, expand=True)
    mono = _apply_clarity_mode(img, clarity_mode)
    width, height = mono.size
    bytes_per_row = (width + 7) // 8
    rows = []
    pixels = mono.load()
    for y in range(height):
        row = bytearray()
        for byte_idx in range(bytes_per_row):
            value = 0
            for bit in range(8):
                x = byte_idx * 8 + bit
                if x < width and pixels[x, y] == 0:
                    value |= 1 << (7 - bit)
            row.append(value)
        rows.append(row.hex().upper())
    data = "".join(rows)
    total_bytes = bytes_per_row * height
    return (
        "^XA"
        "^CI28"
        f"^PW{width}"
        f"^LL{height}"
        "^LH0,0"
        f"^MD{int(darkness)}"
        f"^PR{int(speed)}"
        f"^FO0,0^GFA,{total_bytes},{total_bytes},{bytes_per_row},{data}^FS"
        "^XZ"
    )


def render_label_zpl(template, fields, current_box_index, total_boxes,
                     current_box_qty, std_qty, pack_info=None, box_type="full",
                     print_time=None, shipment_time=None):
    source_template = template or get_default_template()
    zpl_cfg = source_template.get("zpl", {})
    qr_cfg = source_template.get("qr", {})
    use_native_qr = (
        bool(qr_cfg.get("enabled", True))
        and qr_cfg.get("render_mode", "zpl_native") == "zpl_native"
        and not bool(zpl_cfg.get("rotate_for_media", False))
    )
    render_template = copy.deepcopy(source_template) if use_native_qr else source_template
    if use_native_qr:
        render_template.setdefault("qr", {})["_suppress_bitmap_content"] = True
    img = render_label_image(
        render_template, fields, current_box_index, total_boxes, current_box_qty,
        std_qty, pack_info, box_type, print_time, shipment_time,
    )
    zpl = image_to_zpl_graphic(
        img,
        rotate_for_media=zpl_cfg.get("rotate_for_media", False),
        darkness=zpl_cfg.get("darkness", 18),
        speed=zpl_cfg.get("speed", 3),
        clarity_mode=zpl_cfg.get("clarity_mode", "standard"),
    )
    if use_native_qr:
        ctx = build_render_context(
            fields, current_box_index, total_boxes, current_box_qty,
            std_qty, pack_info, box_type, print_time, shipment_time, source_template,
        )
        qr_zpl = _native_qr_zpl(source_template, ctx)
        if qr_zpl and zpl.endswith("^XZ"):
            zpl = zpl[:-3] + qr_zpl + "^XZ"
    return zpl
