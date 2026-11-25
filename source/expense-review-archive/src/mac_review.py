"""本地运行兼容版式审核流程。Vision 读取系统截图，原脚本执行凭证匹配与导出。"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pandas as pd
from PIL import Image

import auto_check as review

BASE = Path(__file__).resolve().parents[1]
review.FILE1_WORD_DIR = str(BASE / "输入_系统截图Word")
review.FILE2_PDF_DIR = str(BASE / "输入_汇总凭证PDF")


def recognize(executable, path):
    result = subprocess.run([executable, path], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def parse_screen(executable, path, temporary):
    screen = recognize(executable, path)
    heading_pattern = re.compile(r"^[A-Z]+\d+\s+(.+?)\s*\([A-Z]*\d+\)")
    heading = next((m for block in screen if (m := heading_pattern.match(block["text"]))), None)
    if not heading:
        raise ValueError(f"请复核截图上的员工姓名：{path.name}")
    name = heading.group(1)
    with Image.open(path) as image:
        width, height = image.size
        # 兼容版式系统截图的费用明细区域。放大以识别较窄的金额列。
        detail = image.crop((round(.145*width), round(.60*height), round(.40*width), height))
        zoomed = temporary / 'details.png'
        detail.resize((detail.width*3, detail.height*3), Image.Resampling.LANCZOS).save(zoomed)
    blocks = recognize(executable, zoomed)
    records = []
    for block in blocks:
        foreign = re.fullmatch(r'([\d,]+\.\d{2})\s*([A-Z]{3})', block['text'])
        if not foreign or block['x'] >= .40:
            continue
        amounts = [r for r in blocks if r['x'] > .62 and abs(r['y'] - block['y']) < .025
                   and re.fullmatch(r'[\d,]+\.\d{2}', r['text'])]
        if len(amounts) != 1:
            raise ValueError(f"请复核费用明细金额列：{path.name}")
        if foreign[2] != "CNY":
            raise ValueError(f"本地兼容版式样例遇到外币，需要人工复核：{path.name}")
        amount = amounts[0]['text'].replace(',', '')
        if amount != foreign[1].replace(',', ''):
            raise ValueError(f"两列人民币金额不符：{path.name}")
        records.append(dict(Name=name, Amount=amount, Currency="CNY", Remark="", Source=path.name))
    if not records:
        raise ValueError(f"费用明细为空：{path.name}")
    folder = Path(review.OUTPUT_EMP_DIR) / f"【本月有报销】_{review.normalize_name(name)}"
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, folder / f"系统报销单_{path.name}")
    return records


def main():
    review.init_env()
    paths = review.extract_images_from_word(review.FILE1_WORD_DIR, review.TEMP_IMG_DIR)
    records = []
    with tempfile.TemporaryDirectory() as directory:
        for file in paths:
            records.extend(parse_screen(BASE / '.build' / 'vision', Path(file), Path(directory)))
    if not records:
        raise ValueError("Word 文档中未提取到报销明细。")
    print(f"Mac Vision 离线读取 {len(paths)} 张截图，取得 {len(records)} 笔费用明细。", flush=True)
    df = pd.DataFrame(records)
    summary = review.extract_pdf_summaries(review.FILE2_PDF_DIR)
    if summary.empty:
        raise ValueError("PDF 中未识别到交易汇总。")
    # 使用原审核脚本的姓名、币种与金额三字段对账规则。
    left, right = df.copy(), summary.copy()
    for table in (left, right):
        table['Standard_Name'] = table['Name'].apply(review.normalize_name)
        table['Amount'] = table['Amount'].apply(review.clean_amount)
    merged = pd.merge(left, right, on=['Standard_Name', 'Currency', 'Amount'], how='left')
    prefixes = [str(key)[:8] for key in merged['Trans_ID'].dropna().unique()]
    reviewed = review.extract_images_for_valid_ids(review.FILE2_PDF_DIR, summary, prefixes)
    review.reconcile_and_report(df, reviewed)


if __name__ == '__main__':
    main()
