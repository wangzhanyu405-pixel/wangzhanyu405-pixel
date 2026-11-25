import argparse
from rules import DEFAULT_CONFIG, load_rules
import logging
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

import fitz
import pdfplumber
from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.styles import Alignment, Font, PatternFill
from PIL import Image as PilImage

try:
    from rapidocr_onnxruntime import RapidOCR
except ImportError:
    RapidOCR = None

logging.getLogger("pdfminer").setLevel(logging.CRITICAL)
logging.getLogger("pdfplumber").setLevel(logging.CRITICAL)


DEFAULT_WORK_DIR = Path(".")
DEFAULT_OUTPUT_DIR = Path("./输出_按员工归档_所有凭证与截图")
DEFAULT_SOURCE_CONFIG = DEFAULT_CONFIG
DEFAULT_NONSTANDARD_EXCEL = Path("./非标发票汇总.xlsx")
class ArchiveInputError(Exception):
    pass


OCR_ENGINE = None


@dataclass(frozen=True)
class PdfPage:
    number: int
    text: str


def clean_amount(value):
    text = str(value).upper().replace("CNY", "").replace("USD", "").replace("EUR", "")
    text = re.sub(r"[^\d.]", "", text)
    return float(text) if text else 0.0


def expense_month_from_pdf_name(pdf_file):
    match = re.search(r"\b(0?[1-9]|1[0-2])\s+(\d{4})\b", Path(pdf_file).name)
    if not match:
        return ""
    month = int(match.group(1))
    year = match.group(2)
    return f"{year}.{month}"


def make_name_normalizer(raw_mapping):
    mapping = {}
    for aliases in raw_mapping:
        standard_name = aliases[0]
        for alias in aliases:
            clean_alias = str(alias).lower().replace(" ", "")
            mapping[clean_alias] = standard_name
            parts = str(alias).split()
            if len(parts) == 2:
                mapping[(parts[1] + parts[0]).lower()] = standard_name

    def normalize(name):
        clean_name = str(name).lower().replace(" ", "")
        return mapping.get(clean_name, str(name).strip())

    return normalize


def parse_summary_text(text, pdf_file):
    rows = []
    compact_text = re.sub(r"[ \t]+", " ", text.replace("\n", " "))
    parts = re.split(r"([0-9a-f]{8}\.\.\.[0-9a-f]{7})", compact_text, flags=re.IGNORECASE)

    for i in range(1, len(parts), 2):
        trans_id = parts[i]
        tail = parts[i + 1][:260]
        match = re.search(r"([\d,]+(?:\.\d+)?)\s+([A-Z]{3})\s+(.+?)\s+(Yes|No)\s+(.+)", tail)
        if not match:
            continue

        rows.append(
            {
                "Trans_ID": trans_id,
                "Trans_Prefix": trans_id[:8],
                "Amount": clean_amount(match.group(1)),
                "Currency": match.group(2).upper(),
                "Name": match.group(3).strip(),
                "Vendor": match.group(5).strip(),
                "PDF_File": Path(pdf_file).name,
            }
        )

    return rows


def extract_pdf_summaries(pdf_paths):
    summaries = {}
    for pdf_path in pdf_paths:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if "Transaction ID:" in text:
                    break
                for row in parse_summary_text(text, pdf_path):
                    summaries[row["Trans_Prefix"]] = row
    return summaries


def discover_pdf_paths(work_dir):
    work_dir = Path(work_dir)
    output_dir_name = DEFAULT_OUTPUT_DIR.name
    pdf_paths = []

    for pdf_path in sorted(work_dir.glob("*.pdf")):
        if output_dir_name not in pdf_path.parts:
            pdf_paths.append(pdf_path)

    return pdf_paths


def group_receipt_pages(page_texts, valid_prefixes, page_has_images=None):
    if page_has_images is None:
        page_has_images = [False] * len(page_texts)

    groups = {prefix: [] for prefix in valid_prefixes}
    current_prefix = None
    intro_only = re.compile(
        r"^Transaction ID:\s*[a-zA-Z0-9-]+(?:\s+Your receipt is attached below)?\s*$",
        re.S,
    )

    for page_index, text in enumerate(page_texts):
        match = re.search(r"Transaction ID:\s*([a-zA-Z0-9-]+)", text)
        if match:
            current_prefix = match.group(1)[:8]
            if current_prefix not in valid_prefixes:
                current_prefix = None
                continue
            if intro_only.match(text.strip()) and not page_has_images[page_index]:
                continue

        if current_prefix in valid_prefixes:
            groups[current_prefix].append(page_index)

    return {prefix: pages for prefix, pages in groups.items() if pages}


def classify_receipt_text(text, valid_titles):
    compact_text = re.sub(r"\s+", "", text)
    compact_titles = [re.sub(r"\s+", "", title) for title in valid_titles]
    return "标准发票" if any(title in compact_text for title in compact_titles) else "非标或异常"


def get_ocr_engine():
    global OCR_ENGINE
    if OCR_ENGINE is None and RapidOCR is not None:
        OCR_ENGINE = RapidOCR()
    return OCR_ENGINE


def ocr_pdf_page_text(pdf_doc, page_index, dpi=150):
    engine = get_ocr_engine()
    if engine is None:
        return ""

    pix = pdf_doc[page_index].get_pixmap(dpi=dpi)
    result, _ = engine(pix.tobytes("png"))
    if not result:
        return ""
    return " ".join(item[1] for item in result)


def should_ocr_page(extracted_text, page_has_image=False):
    stripped = extracted_text.strip()
    id_only = bool(re.match(r"^Transaction ID:\s*[a-zA-Z0-9-]+\s*$", stripped))
    return (not stripped) or ("发票" in stripped) or (page_has_image and id_only)


def classify_receipt_page(pdf_doc, page_index, extracted_text, valid_titles, page_has_image=False):
    text_type = classify_receipt_text(extracted_text, valid_titles)
    if text_type == "标准发票":
        return text_type
    if not should_ocr_page(extracted_text, page_has_image):
        return text_type
    ocr_text = ocr_pdf_page_text(pdf_doc, page_index)
    return classify_receipt_text(ocr_text, valid_titles)


def finalize_receipt_types_for_group(receipt_types):
    if "标准发票" not in receipt_types:
        return receipt_types
    return ["标准发票" if receipt_type == "标准发票" else "配套附件" for receipt_type in receipt_types]


def summarize_receipt_types(receipt_types):
    unique_types = set(receipt_types)
    if "标准发票" in unique_types and "配套附件" in unique_types:
        return "标准发票 + 配套附件"
    if "标准发票" in unique_types:
        return "标准发票"
    if "非标或异常" in unique_types:
        return "非标或异常"
    return " / ".join(sorted(unique_types)) if unique_types else ""


def safe_path_part(value):
    return re.sub(r'[\\/:*?"<>|]+', "_", str(value)).strip() or "未知"


def make_tree_writable(path):
    path = Path(path)
    if not path.exists():
        return

    for current_path in [path, *path.rglob("*")]:
        try:
            current_path.chmod(current_path.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass


def receipt_output_parts(row, normalize_name, receipt_type, sequence):
    employee_name = safe_path_part(normalize_name(row["Name"]))
    amount = row["Amount"]
    prefix = row["Trans_Prefix"]
    folder = f"【本月有报销】_{employee_name}/{receipt_type}"
    if receipt_type == "标准发票":
        filename = f"发票_{amount}_{prefix}_P{sequence}.png"
    elif receipt_type == "配套附件":
        filename = f"配套附件_{amount}_{prefix}_P{sequence}.png"
    else:
        filename = f"非标或异常_{amount}_{prefix}_P{sequence}.png"
    return folder, filename


def render_pdf_page(pdf_doc, page_index, output_png, dpi=150):
    output_png = Path(output_png)
    output_png.parent.mkdir(parents=True, exist_ok=True)
    page = pdf_doc[page_index]
    pix = page.get_pixmap(dpi=dpi)
    pix.save(output_png)


def employee_name_from_archive_folder(folder_path):
    name = Path(folder_path).name
    return name.replace("【本月有报销】_", "", 1)


def collect_nonstandard_images_by_employee(output_dir):
    output_dir = Path(output_dir)
    image_exts = {".png", ".jpg", ".jpeg"}
    grouped = {}

    if not output_dir.exists():
        return grouped

    for employee_dir in sorted(path for path in output_dir.iterdir() if path.is_dir()):
        nonstandard_dir = employee_dir / "非标或异常"
        if not nonstandard_dir.exists():
            continue

        images = sorted(
            path for path in nonstandard_dir.iterdir()
            if path.is_file() and path.suffix.lower() in image_exts
        )
        if images:
            grouped[employee_name_from_archive_folder(employee_dir)] = images

    return grouped


def resize_for_excel(image_path, max_width=300, max_height=220):
    with PilImage.open(image_path) as image:
        width, height = image.size
    scale = min(max_width / width, max_height / height, 1)
    return int(width * scale), int(height * scale)


def export_nonstandard_excel(images_by_employee, output_xlsx, missing_attachments=None, detail_rows=None):
    output_xlsx = Path(output_xlsx)
    output_xlsx.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "非标发票汇总"
    sheet.freeze_panes = "A2"
    sheet.sheet_view.showGridLines = False

    header_fill = PatternFill("solid", fgColor="D9EAF7")
    header_font = Font(bold=True, size=14)

    if not images_by_employee:
        sheet["A1"] = "本次没有非标或异常发票"
        sheet["A1"].font = header_font
        sheet["A1"].fill = header_fill
        sheet.column_dimensions["A"].width = 32
    else:
        for col_index, (employee_name, image_paths) in enumerate(sorted(images_by_employee.items()), start=1):
            header_cell = sheet.cell(row=1, column=col_index)
            header_cell.value = employee_name
            header_cell.font = header_font
            header_cell.fill = header_fill
            header_cell.alignment = Alignment(horizontal="center", vertical="center")
            sheet.column_dimensions[header_cell.column_letter].width = 42

            for row_index, image_path in enumerate(image_paths, start=2):
                width, height = resize_for_excel(image_path)
                sheet.row_dimensions[row_index].height = max(sheet.row_dimensions[row_index].height or 0, height * 0.75 + 12)
                excel_image = ExcelImage(str(image_path))
                excel_image.width = width
                excel_image.height = height
                sheet.add_image(excel_image, sheet.cell(row=row_index, column=col_index).coordinate)

    detail_rows = detail_rows or []
    detail_sheet = workbook.create_sheet("报销明细表")
    detail_sheet.sheet_view.showGridLines = False
    detail_headers = ["报销月份", "员工", "报销编号", "金额", "币种", "供应商", "PDF文件", "发票类型"]
    detail_sheet.append(detail_headers)
    for cell in detail_sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9EAD3")
        cell.alignment = Alignment(horizontal="center")
    for row in detail_rows:
        detail_sheet.append([
            row["Month"],
            row["Employee"],
            row["Trans_ID"],
            row["Amount"],
            row["Currency"],
            row["Vendor"],
            row["PDF_File"],
            row.get("Receipt_Type", ""),
        ])
    detail_widths = [12, 24, 42, 12, 10, 36, 42, 22]
    for idx, width in enumerate(detail_widths, start=1):
        detail_sheet.column_dimensions[detail_sheet.cell(row=1, column=idx).column_letter].width = width
    detail_sheet.freeze_panes = "A2"

    missing_attachments = missing_attachments or []
    missing_sheet = workbook.create_sheet("缺附件清单")
    missing_sheet.sheet_view.showGridLines = False
    headers = ["员工", "金额", "币种", "交易号", "供应商", "PDF文件", "说明"]
    missing_sheet.append(headers)
    for cell in missing_sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="FCE4D6")
        cell.alignment = Alignment(horizontal="center")
    for row in missing_attachments:
        missing_sheet.append([
            row["Employee"],
            row["Amount"],
            row["Currency"],
            row["Trans_ID"],
            row["Vendor"],
            row["PDF_File"],
            "汇总表有该交易，但PDF后续只有Transaction ID编号页或无附件页",
        ])
    widths = [20, 12, 10, 24, 36, 42, 52]
    for idx, width in enumerate(widths, start=1):
        missing_sheet.column_dimensions[missing_sheet.cell(row=1, column=idx).column_letter].width = width
    missing_sheet.freeze_panes = "A2"

    workbook.save(output_xlsx)
    return output_xlsx


def read_pdf_pages(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        return [PdfPage(number=i, text=page.extract_text() or "") for i, page in enumerate(pdf.pages)]


def extract_full_transaction_id(page_texts, page_indexes, prefix):
    for page_index in page_indexes:
        match = re.search(r"Transaction ID:\s*([a-zA-Z0-9-]+)", page_texts[page_index])
        if match and match.group(1).startswith(prefix):
            return match.group(1)
    return ""


def extract_full_transaction_ids_by_prefix(page_texts):
    full_ids = {}
    for text in page_texts:
        for match in re.finditer(r"Transaction ID:\s*([a-zA-Z0-9-]+)", text):
            full_id = match.group(1)
            full_ids[full_id[:8]] = full_id
    return full_ids


def archive_pdf(pdf_path, summaries, output_dir, normalize_name, valid_titles):
    pages = read_pdf_pages(pdf_path)
    written = []
    missing = []
    detail_rows = []

    with fitz.open(pdf_path) as pdf_doc:
        page_has_images = [len(pdf_doc[index].get_images(full=True)) > 0 for index in range(len(pdf_doc))]
        groups = group_receipt_pages([page.text for page in pages], set(summaries), page_has_images=page_has_images)

        for prefix in sorted(set(summaries) - set(groups)):
            row = summaries[prefix]
            missing.append({
                "Employee": normalize_name(row["Name"]),
                "Amount": row["Amount"],
                "Currency": row["Currency"],
                "Trans_ID": row["Trans_ID"],
                "Vendor": row["Vendor"],
                "PDF_File": row["PDF_File"],
            })

        page_texts = [page.text for page in pages]
        full_ids_by_prefix = extract_full_transaction_ids_by_prefix(page_texts)
        for prefix, page_indexes in groups.items():
            row = summaries[prefix]
            full_trans_id = (
                full_ids_by_prefix.get(prefix)
                or extract_full_transaction_id(page_texts, page_indexes, prefix)
                or row["Trans_ID"]
            )
            classified_pages = []
            for page_index in page_indexes:
                classified_pages.append(classify_receipt_page(
                    pdf_doc,
                    page_index,
                    pages[page_index].text,
                    valid_titles,
                    page_has_image=page_has_images[page_index],
                ))

            receipt_types = finalize_receipt_types_for_group(classified_pages)
            detail_rows.append({
                "Month": expense_month_from_pdf_name(row["PDF_File"]),
                "Employee": normalize_name(row["Name"]),
                "Trans_ID": full_trans_id,
                "Amount": row["Amount"],
                "Currency": row["Currency"],
                "Vendor": row["Vendor"],
                "PDF_File": row["PDF_File"],
                "Receipt_Type": summarize_receipt_types(receipt_types),
            })

            for sequence, (page_index, receipt_type) in enumerate(zip(page_indexes, receipt_types), start=1):
                folder, filename = receipt_output_parts(row, normalize_name, receipt_type, sequence)
                output_png = Path(output_dir) / folder / filename
                render_pdf_page(pdf_doc, page_index, output_png)
                written.append(output_png)

    return written, missing, detail_rows


def archive_all(pdf_dir, output_dir, source_config, clean=False, excel_output=DEFAULT_NONSTANDARD_EXCEL):
    pdf_dir = Path(pdf_dir)
    output_dir = Path(output_dir)
    pdf_paths = discover_pdf_paths(pdf_dir)
    if not pdf_paths:
        raise FileNotFoundError(f"No PDF files found in current folder: {pdf_dir.resolve()}")

    raw_mapping, valid_titles = load_rules(source_config)
    normalize_name = make_name_normalizer(raw_mapping)

    if clean and output_dir.exists():
        make_tree_writable(output_dir)
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    written = []
    missing_attachments = []
    detail_rows = []
    total_summary_rows = 0
    for pdf_path in pdf_paths:
        summaries = extract_pdf_summaries([pdf_path])
        total_summary_rows += len(summaries)
        pdf_written, pdf_missing, pdf_detail_rows = archive_pdf(
            pdf_path=pdf_path,
            summaries=summaries,
            output_dir=output_dir,
            normalize_name=normalize_name,
            valid_titles=valid_titles,
        )
        written.extend(pdf_written)
        missing_attachments.extend(pdf_missing)
        detail_rows.extend(pdf_detail_rows)

    if total_summary_rows == 0:
        raise ArchiveInputError(
            "没有在当前 PDF 中识别到 Transactions Summary。请使用 财务系统导出的 Expense report receipts 汇总 PDF；"
            "单张发票 PDF 或普通发票文件夹无法可靠按员工自动归档。"
        )

    images_by_employee = collect_nonstandard_images_by_employee(output_dir)
    export_nonstandard_excel(images_by_employee, excel_output, missing_attachments, detail_rows=detail_rows)
    return written


def main():
    parser = argparse.ArgumentParser(description="Archive PDF receipt screenshots by employee and receipt type.")
    parser.add_argument("--pdf-dir", default=DEFAULT_WORK_DIR, help="Folder containing reimbursement summary PDFs. Defaults to the current folder.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Folder for employee archive output.")
    parser.add_argument("--source-config", default=DEFAULT_SOURCE_CONFIG, help="Local JSON file with employee_aliases and valid_titles.")
    parser.add_argument("--excel-output", default=DEFAULT_NONSTANDARD_EXCEL, help="Excel output for non-standard receipt images.")
    parser.add_argument("--clean", action="store_true", help="Delete the output folder before rebuilding.")
    args = parser.parse_args()

    try:
        written = archive_all(
            pdf_dir=args.pdf_dir,
            output_dir=args.output_dir,
            source_config=args.source_config,
            clean=args.clean,
            excel_output=args.excel_output,
        )
    except (FileNotFoundError, ArchiveInputError) as exc:
        print(f"运行失败：{exc}")
        print("请把需要处理的 Expense report receipts 汇总 PDF 放在本脚本同一个文件夹下，然后重新运行。")
        return 1

    print(f"生成完成：{len(written)} 张凭证截图")
    print(f"输出目录：{Path(args.output_dir).resolve()}")
    print(f"非标汇总Excel：{Path(args.excel_output).resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
