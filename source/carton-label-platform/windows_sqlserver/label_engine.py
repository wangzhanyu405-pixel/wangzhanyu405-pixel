# -*- coding: utf-8 -*-
"""
label_engine.py —— 箱贴渲染引擎 v6 (手绘图 1:1 定制复刻版)
完全按照手绘图布局：
- 品名、料号、订单号：全宽
- 订单数量、每箱数量、总箱数：左半部分
- 二维码：只占这三行的右半部分
- 日期：全宽
- 供应商：底部全宽
"""

import qrcode
import os
import datetime
import copy
from PIL import Image, ImageDraw, ImageFont
from template_engine import (
    get_default_template,
    render_label_image,
    render_label_zpl,
    build_qr_content_from_context,
    build_render_context,
)

FONT_CANDIDATES = [
    os.path.join("assets", "msyh.ttc"),
    os.path.join("assests", "msyh.ttc"),
    os.path.join("assests", "STHUPO.TTF"),
]

# ============================================================
# 物理尺寸(斑马 ZD888 203 DPI)
# ============================================================
PRINTER_DPI = 203
DOTS_PER_MM = 8
LABEL_WIDTH_MM = 80
LABEL_HEIGHT_MM = 60
LABEL_WIDTH = LABEL_WIDTH_MM * DOTS_PER_MM
LABEL_HEIGHT = LABEL_HEIGHT_MM * DOTS_PER_MM
ZPL_MEDIA_WIDTH = LABEL_HEIGHT
ZPL_MEDIA_LENGTH = LABEL_WIDTH

def get_font(size):
    for font_path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(font_path, size)
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
    return qr.make_image(fill_color="black", back_color="white").convert('RGB')

def _resize_qr_sharp(qr_img, target_size):
    return qr_img.resize((target_size, target_size), Image.Resampling.NEAREST)


def _blank_zero_code(value):
    s = str(value or '').strip()
    return '' if s in ('0', '0.0', '0.00') else s

# ============================================================
# 字段池
# ============================================================
DEFAULT_FIELDS = [
    {'key': 'sap_no',       'label': 'SAP号',    'value': '', 'visible': False, 'editable': True},
    {'key': 'product_name', 'label': '品名',     'value': '', 'visible': True,  'editable': True},
    {'key': 'item_no',      'label': '料号',     'value': '', 'visible': True,  'editable': True},
    {'key': 'order_no',     'label': '定单号',   'value': '', 'visible': True,  'editable': True},
    {'key': 'order_qty',    'label': '订单数量', 'value': '', 'visible': True,  'editable': True},
    {'key': 'pack_method',  'label': '包装方式', 'value': '', 'visible': True,  'editable': True},
    {'key': 'date',         'label': '日期',     'value': '', 'visible': True,  'editable': True},
    {'key': 'supplier',     'label': '供应商',   'value': '演示包装有限公司', 'visible': True, 'editable': True},
    {'key': 'operator_name','label': '打印人',   'value': '', 'visible': False, 'editable': True},
]

def build_qr_content(item_no, box_index, total_boxes, qty_in_box,
                     print_time=None, shipment_time=None):
    fields = [
        {'key': 'item_no', 'value': item_no, 'visible': True},
        {'key': 'date', 'value': (print_time or datetime.datetime.now()).strftime("%Y.%m.%d"), 'visible': True},
    ]
    template = get_default_template()
    ctx = build_render_context(
        fields, box_index, total_boxes, qty_in_box, qty_in_box,
        print_time=print_time, shipment_time=shipment_time,
    )
    return build_qr_content_from_context(template, ctx)


def _zpl_escape(value):
    return str(value or '').replace('\\', '/').replace('^', ' ').replace('~', ' ')


def _zpl_text(x, y, width, height, text, font_h=28, font_w=None, align='C', bold=False):
    font_w = font_w or font_h
    value = _zpl_escape(text)
    parts = [
        f"^FO{x},{y}",
        f"^A0N,{font_h},{font_w}",
        f"^FB{width},1,0,{align},0",
        f"^FD{value}^FS",
    ]
    if bold:
        parts.insert(1, "^FR")
    return ''.join(parts)


def _zpl_box(x, y, w, h, thickness=2):
    return f"^FO{x},{y}^GB{w},{h},{thickness}^FS"


def _zpl_line(x, y, w, h, thickness=2):
    return f"^FO{x},{y}^GB{w},{h},{thickness}^FS"


def image_to_zpl_graphic(img, rotate_for_media=False):
    """把 PIL 图像转成 Zebra ^GFA 点阵。避免打印机缺中文字体。"""
    if rotate_for_media:
        img = img.rotate(90, expand=True)
    mono = img.convert("L").point(lambda p: 0 if p < 180 else 255, "1")
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
    data = ''.join(rows)
    total_bytes = bytes_per_row * height
    return (
        "^XA"
        "^CI28"
        f"^PW{width}"
        f"^LL{height}"
        "^LH0,0"
        "^MD18"
        "^PR3"
        f"^FO0,0^GFA,{total_bytes},{total_bytes},{bytes_per_row},{data}^FS"
        "^XZ"
    )


def build_single_label_zpl(fields, current_box_index, total_boxes, current_box_qty,
                           std_qty, pack_info=None, box_type='full',
                           print_time=None, shipment_time=None, template=None):
    """生成 80x60mm 箱贴 ZPL。用点阵图保证中文和模板跨电脑一致。"""
    return render_label_zpl(
        template or get_default_template(),
        fields=fields,
        current_box_index=current_box_index,
        total_boxes=total_boxes,
        current_box_qty=current_box_qty,
        std_qty=std_qty,
        pack_info=pack_info,
        box_type=box_type,
        print_time=print_time,
        shipment_time=shipment_time,
    )

def generate_single_label(fields, current_box_index, total_boxes, current_box_qty,
                          std_qty, pack_info=None, box_type='full',
                          print_time=None, shipment_time=None, template=None):
    return render_label_image(
        template or get_default_template(),
        fields=fields,
        current_box_index=current_box_index,
        total_boxes=total_boxes,
        current_box_qty=current_box_qty,
        std_qty=std_qty,
        pack_info=pack_info,
        box_type=box_type,
        print_time=print_time,
        shipment_time=shipment_time,
    )

def make_default_fields_from_data(product_data, order_header, sap_no=''):
    fields = copy.deepcopy(DEFAULT_FIELDS)
    today_str = datetime.datetime.now().strftime("%Y.%m.%d")
    order_qty = int(product_data.get('订货数量', 0) or 0)

    value_map = {
        'sap_no':       _blank_zero_code(sap_no),
        'product_name': str(product_data.get('品名', '')),
        'item_no':      str(product_data.get('料号', '')),
        'order_no':     str(order_header.get('定单号码', '')),
        'order_qty':    f"{order_qty} pcs" if order_qty else '',
        'pack_method':  str(product_data.get('包装方式', '') or ''),
        'date':         today_str,
        'supplier':     '演示包装有限公司',
        'operator_name': '',
    }

    for f in fields:
        if f['key'] in value_map: f['value'] = value_map[f['key']]
        if f['key'] == 'sap_no' and _blank_zero_code(sap_no): f['visible'] = True

    return fields
