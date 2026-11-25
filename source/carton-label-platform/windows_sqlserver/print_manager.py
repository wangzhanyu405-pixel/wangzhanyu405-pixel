# -*- coding: utf-8 -*-
"""
print_manager.py —— 服务端打印机管理(强制 1:1 原尺寸打印版)
"""

import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

SUMATRA_PATHS =[
    r"C:\SumatraPDF\SumatraPDF.exe",
    r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
    r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe",
    os.path.join("assets", "SumatraPDF.exe"),
    os.path.join("assests", "SumatraPDF-3.6-64.exe"),
    os.path.join(os.path.dirname(__file__), "SumatraPDF.exe"),
    os.path.join(os.path.dirname(__file__), "assests", "SumatraPDF-3.6-64.exe"),
]

def find_sumatra():
    for path in SUMATRA_PATHS:
        if os.path.exists(path):
            return path
    return None

def list_printers():
    if sys.platform != "win32": return[]
    try:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        return [p[2] for p in win32print.EnumPrinters(flags)]
    except Exception:
        return[]

def get_default_printer():
    if sys.platform != "win32": return None
    try:
        import win32print
        return win32print.GetDefaultPrinter()
    except Exception:
        return None


def _calibration_font(size):
    candidates = [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\msyh.ttc",
        os.path.join(os.path.dirname(__file__), "assests", "STHUPO.TTF"),
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _bitmap_to_zpl(img, darkness=18, speed=3):
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


def build_zebra_calibration_zpl(case_code):
    """Build one bitmap calibration label.

    Direction cases:
    A: no rotation, B: counter-clockwise 90, C: clockwise 90, D: 180.

    Scale cases:
    S1: 80x60 full-size border, S2: 60x80 full-size border,
    S3: 80x60 with 10mm inset, S4: 80x60 90% content.
    """
    case_code = str(case_code or "A").upper()
    rotations = {"A": 0, "B": 90, "C": -90, "D": 180, "S1": 0, "S2": 0, "S3": 0, "S4": 0}
    rotation = rotations.get(case_code, 0)
    canvas_sizes = {"S2": (480, 640)}
    canvas_w, canvas_h = canvas_sizes.get(case_code, (640, 480))

    img = Image.new("RGB", (canvas_w, canvas_h), "white")
    draw = ImageDraw.Draw(img)
    thick = 6
    inset = 8
    if case_code == "S3":
        inset = 80
    elif case_code == "S4":
        inset = 32
    left, top, right, bottom = inset, inset, canvas_w - inset, canvas_h - inset
    draw.rectangle([left, top, right, bottom], outline="black", width=thick)
    draw.line([(left, top + 72), (right, top + 72)], fill="black", width=3)
    draw.line([(left, bottom - 72), (right, bottom - 72)], fill="black", width=3)
    draw.line([(left + 72, top), (left + 72, bottom)], fill="black", width=3)
    draw.line([(right - 72, top), (right - 72, bottom)], fill="black", width=3)

    big_font = _calibration_font(150)
    mid_font = _calibration_font(36)
    small_font = _calibration_font(24)
    title = f"CASE {case_code}"
    desc = {
        "A": "NO ROTATE  PW640 LL480",
        "B": "ROTATE 90  PW480 LL640",
        "C": "ROTATE -90 PW480 LL640",
        "D": "ROTATE 180 PW640 LL480",
        "S1": "SCALE 100% 80x60 PW640 LL480",
        "S2": "SCALE 100% 60x80 PW480 LL640",
        "S3": "INSET 10mm ON 80x60",
        "S4": "CONTENT 90% ON 80x60",
    }.get(case_code, "")

    def centered_text(box, text, font):
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x1, y1, x2, y2 = box
        draw.text((x1 + (x2 - x1 - tw) / 2, y1 + (y2 - y1 - th) / 2), text, font=font, fill="black")

    centered_text((left + 80, top + 90, right - 80, bottom - 120), case_code, big_font)
    centered_text((left + 80, top + 12, right - 80, top + 64), title, mid_font)
    centered_text((left + 80, bottom - 64, right - 80, bottom - 8), desc, small_font)
    draw.text((left + 10, top + 14), "LEFT", font=small_font, fill="black")
    draw.text((max(left + 10, right - 82), top + 14), "RIGHT", font=small_font, fill="black")
    draw.text((left + 10, max(top + 10, bottom - 48)), "BOTTOM", font=small_font, fill="black")
    draw.text((max(left + 10, right - 95), max(top + 10, bottom - 48)), "BOTTOM", font=small_font, fill="black")

    if rotation:
        img = img.rotate(rotation, expand=True)
    return _bitmap_to_zpl(img)

def print_pdf_file(pdf_path, printer_name=None, copies=1, use_system_default=False):
    """
    用 SumatraPDF 静默打印 PDF。

    打印链路问题:
    - PDF 文件本身是 104×50.8mm(我们生成的)
    - 标签纸物理尺寸也是 104×50.8mm
    - 但 SumatraPDF 各种参数对小标签机的处理方式不同:
        * noscale: 按"标准纸"裁剪,内容被裁掉
        * fit:    按"可打印区"等比缩放,在某些驱动下变成 50%
        * shrink: 仅缩小不放大,适用本场景

    备用方案 (use_system_default=True):
        走 Windows ShellExecute "printto" 路径,
        这条路径等价于"右键 PDF → 打印到 XX 打印机",
        会用系统默认 PDF 阅读器(Edge/Adobe)的渲染逻辑,
        效果与 PDF 阅读器手动打印一致。
    """
    if not os.path.exists(pdf_path):
        return False, "PDF 文件不存在"
    printer_name = printer_name or get_default_printer()
    if not printer_name:
        return False, "未指定打印机"

    # 备用方案:走系统默认 PDF 程序
    if use_system_default and sys.platform == "win32":
        try:
            import win32api
            for _ in range(max(1, int(copies))):
                win32api.ShellExecute(
                    0, "printto",
                    pdf_path, f'"{printer_name}"',
                    ".", 0
                )
            return True, (
                f"✅ 已通过系统默认 PDF 程序发送至【{printer_name}】"
                "(请确保系统默认 PDF 程序是 Edge / Adobe / Foxit 等支持打印的)"
            )
        except Exception as e:
            return False, f"系统打印调用失败: {e}"

    sumatra = find_sumatra()
    if sumatra:
        try:
            for _ in range(max(1, int(copies))):
                cmd = [
                    sumatra,
                    "-print-to", printer_name,
                    "-silent",
                    # shrink:不会强制裁剪,也不会无谓缩小;PDF 跟纸一样大时近似 1:1
                    "-print-settings", "shrink",
                    pdf_path,
                ]
                result = subprocess.run(cmd, timeout=60, capture_output=True)
                if result.returncode != 0:
                    err = result.stderr.decode('utf-8', errors='ignore') if result.stderr else ''
                    return False, f"Sumatra 错误: {err}"
            return True, f"✅ 已成功发送至打印机【{printer_name}】"
        except Exception as e:
            return False, f"调用异常: {e}"

    return False, "未找到 SumatraPDF,请装到 C:\\SumatraPDF\\SumatraPDF.exe"


def print_zpl(zpl_text, printer_name=None, copies=1):
    """把 ZPL 原生命令以 RAW 方式发送到 Zebra/ZDesigner ZPL 打印队列。"""
    if sys.platform != "win32":
        return False, "ZPL 直打目前仅支持 Windows"
    printer_name = printer_name or get_default_printer()
    if not printer_name:
        return False, "未指定打印机"
    try:
        import win32print
        handle = win32print.OpenPrinter(printer_name)
        try:
            for _ in range(max(1, int(copies))):
                job = win32print.StartDocPrinter(
                    handle,
                    1,
                    ("LYC Zebra Label", None, "RAW"),
                )
                try:
                    win32print.StartPagePrinter(handle)
                    win32print.WritePrinter(handle, zpl_text.encode("utf-8"))
                    win32print.EndPagePrinter(handle)
                finally:
                    win32print.EndDocPrinter(handle)
        finally:
            win32print.ClosePrinter(handle)
        return True, f"✅ 已通过 ZPL 直发送至打印机【{printer_name}】"
    except Exception as e:
        return False, f"ZPL 直打失败: {e}"
