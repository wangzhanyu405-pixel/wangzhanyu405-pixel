import os
import json
import re
import glob
import zipfile
import time
import shutil  
from rules import ROOT, load_rules
import pandas as pd
import fitz  # PyMuPDF
# 尝试加载本地极速 OCR，用于对付纯图片扫描件发票
try:
    from rapidocr_onnxruntime import RapidOCR
    ocr_engine = RapidOCR()
    print("✅ 本地极速 OCR 引擎加载成功！")
except ImportError:
    print("❌ 警告：缺少 rapidocr_onnxruntime 库，请运行 pip install rapidocr_onnxruntime")
    ocr_engine = None

# ==========================================
# ⚙️ 核心配置区
# ==========================================


FILE1_WORD_DIR = str(ROOT / "输入_系统截图Word")        
FILE2_PDF_DIR = str(ROOT / "输入_汇总凭证PDF")          
TEMP_IMG_DIR = "./temp_系统截图缓存/"          
OUTPUT_EXCEL = "./输出_本月报销核对结果.xlsx" 
OUTPUT_IMAGE_DIR = "./输出_非标及异常小票提取/"      
OUTPUT_EMP_DIR = "./输出_按员工归档_所有凭证与截图/" 

RAW_MAPPING, VALID_TITLES = load_rules()

def get_name_mapping():
    mapping = {}
    for aliases in RAW_MAPPING:
        standard_name = aliases[0]
        for alias in aliases:
            clean_alias = str(alias).lower().replace(" ", "")
            mapping[clean_alias] = standard_name
            parts = str(alias).split()
            if len(parts) == 2:
                mapping[(parts[1] + parts[0]).lower()] = standard_name
    return mapping

NAME_DICT = get_name_mapping()

def normalize_name(name):
    n = str(name).lower().replace(" ", "")
    return NAME_DICT.get(n, n)

def init_env():
    for folder in[FILE1_WORD_DIR, FILE2_PDF_DIR, TEMP_IMG_DIR, OUTPUT_IMAGE_DIR, OUTPUT_EMP_DIR]:
        if not os.path.exists(folder):
            os.makedirs(folder)

def clean_amount(val):
    try:
        s = str(val).upper().replace('CNY', '').replace('USD', '').replace('EUR', '')
        s = re.sub(r'[^\d\.]', '', s) 
        return float(s) if s else 0.0
    except:
        return 0.0

# ==========================================
# 模块 1：处理 Word 截图
# ==========================================
def extract_images_from_word(word_dir, output_img_dir):
    print("\n🚀 步骤1/4：正在从 Word 文档中抠出系统截图...")
    extracted_images =[]
    word_files = glob.glob(os.path.join(word_dir, "*.docx"))
    
    for docx_path in word_files:
        try:
            with zipfile.ZipFile(docx_path, 'r') as docx_zip:
                for file_name in docx_zip.namelist():
                    if file_name.startswith('word/media/') and file_name.lower().endswith(('.png', '.jpeg', '.jpg')):
                        img_data = docx_zip.read(file_name)
                        base_name = os.path.basename(file_name)
                        save_path = os.path.join(output_img_dir, f"{os.path.splitext(os.path.basename(docx_path))[0]}_{base_name}")
                        with open(save_path, 'wb') as f:
                            f.write(img_data)
                        extracted_images.append(save_path)
        except (OSError, zipfile.BadZipFile) as error:
            raise ValueError(f"无法读取 Word 文件：{docx_path}") from error
    return extracted_images

def read_images_with_ai(image_list):
    import dashscope
    dashscope.api_key = os.environ["DASHSCOPE_API_KEY"]
    print("\n🚀 步骤2/4：正在呼叫阿里AI视觉模型读取截图金额...")
    sys_data =[]
    
    if not image_list:
        return pd.DataFrame()

    for img_path in image_list:
        file_name = os.path.basename(img_path)
        print(f"  👁️ AI正在精细扫描: {file_name} ...")
        
        prompt = """
        你是一个极其严谨的财务审核专家。请仔细阅读这张报销系统的截图。
        【极其重要的规则】：
        1. 绝对不允许提取截图右侧或上方 "Summary" 汇总面板里的金额！
        2. 只允许提取下方列表（Expenses 表格）中的【每一行明细记录】。
        3. "Name"：提取最上方唯一的员工姓名拼音。
        4. "Amount"：提取该行的最终结算金额（纯数字）。
        5. "Currency"：提取最终结算的币种（通常为 CNY）。
        6. "Remark"：如果有外币金额（即包含 FOREIGN AMOUNT 列，如USD等），请在此处记录，例如填写 "包含外币: 100.00 USD"；如果没有外币，填 ""。
        
        返回严格的 JSON 格式，如下所示：
        {
            "Name": "Demo Person",
            "Details":[
                {"Amount": 720.00, "Currency": "CNY", "Remark": "包含外币: 100.00 USD"}
            ]
        }
        """
        for attempt in range(3):
            try:
                messages =[{"role": "user", "content":[{"image": f"file://{os.path.abspath(img_path)}"}, {"text": prompt}]}]
                response = dashscope.MultiModalConversation.call(model='qwen-vl-plus', messages=messages)
                result_text = response.output.choices[0].message.content[0]['text']
                
                json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
                if not json_match:
                    raise ValueError("视觉服务未返回 JSON")
                if json_match:
                    data = json.loads(json_match.group())
                    emp_name = data["Name"].strip()
                    if not emp_name or not data.get("Details"):
                        raise ValueError("姓名或费用明细为空")
                    std_name = normalize_name(emp_name)
                    
                    # 只要进了这里，就说明他在本月系统里有报销截图！
                    emp_folder = os.path.join(OUTPUT_EMP_DIR, f"【本月有报销】_{std_name}")
                    if not os.path.exists(emp_folder):
                        os.makedirs(emp_folder)
                        
                    dest_img_path = os.path.join(emp_folder, f"系统报销单_{file_name}")
                    if not os.path.exists(dest_img_path):
                        shutil.copy(img_path, dest_img_path)
                        
                    for d in data.get("Details",[]):
                        # 【终极外币洗选器】：暴力匹配，只保留真正的外币，粉碎所有 CNY
                        raw_remark = str(d.get("Remark", "")).strip().upper()
                        foreign_matches = re.findall(r'([\d\.,]+\s*(?:USD|EUR|GBP|JPY|SGD|HKD))', raw_remark)
                        
                        if foreign_matches:
                            final_remark = "包含外币: " + ", ".join(foreign_matches)
                        else:
                            final_remark = ""
                            
                        sys_data.append({
                            "Name": emp_name, 
                            "Amount": d.get("Amount"), 
                            "Currency": d.get("Currency", "CNY"), 
                            "Remark": final_remark, 
                            "Source": file_name
                        })
                break 
            except Exception as error:
                if attempt == 2:
                    raise RuntimeError(f"截图识别失败：{file_name}") from error
                time.sleep(2)
        time.sleep(1.5)
            
    df_sys = pd.DataFrame(sys_data)
    if not df_sys.empty:
        df_sys = df_sys.drop_duplicates()
    return df_sys

# ==========================================
# 模块 2：精准匹配当月流水号 & 处理多页凭证
# ==========================================
def extract_pdf_summaries(pdf_dir):
    """只提取PDF的汇总表数据"""
    pdf_files = glob.glob(os.path.join(pdf_dir, "*.pdf"))
    all_summary_list =[]
    
    for pdf_path in pdf_files:
        doc = fitz.open(pdf_path)
        for page_num in range(min(15, len(doc))): 
            text = doc[page_num].get_text("text")
            parts = re.split(r"([0-9a-f]{8}\.\.\.[0-9a-f]{7})", text, flags=re.IGNORECASE)
            for i in range(1, len(parts), 2):
                trans_id = parts[i]
                m = re.search(r"([\d\.,]+)\s+([A-Z]{3})\s+(.+?)\s+(Yes|No)\s+(.+)", parts[i+1][:200].replace('\n', ' '))
                if m:
                    all_summary_list.append({
                        "Trans_ID": trans_id, "Amount": float(m.group(1).replace(',', '')), "Currency": m.group(2).upper(),
                        "Name": m.group(3).strip(), "Vendor": m.group(5).strip(),
                        "Title_Check": "未提取", "Receipt_Type": "未知", "PDF_File": os.path.basename(pdf_path), "Page_Num": "未找着"
                    })
    return pd.DataFrame(all_summary_list)

def extract_images_for_valid_ids(pdf_dir, df_summary, valid_id_prefixes):
    """只截取命中当月报销的凭证，且支持多页截取"""
    print("\n🚀 步骤3/4：正在过滤历史废单，仅精细扫描当月合规凭证...")
    if df_summary.empty or not valid_id_prefixes:
        return df_summary

    pdf_files = glob.glob(os.path.join(pdf_dir, "*.pdf"))
    for pdf_path in pdf_files:
        doc = fitz.open(pdf_path)
        
        current_trans_id = None
        page_count = 0  # 用于记录当前流水号下了多少张凭证
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text("text")
            
            # 是否碰到新的流水引导页？
            trans_match = re.search(r"Transaction ID:\s*([a-zA-Z0-9\-]+)", text)
            if trans_match:
                current_trans_id = trans_match.group(1)[:8]
                page_count = 0  # 重置凭证计数器
                
                # 记录这笔凭证的起始页码
                mask = df_summary['Trans_ID'].str.startswith(current_trans_id)
                df_summary.loc[mask, 'Page_Num'] = f"第 {page_num + 1} 页起" 
                
                clean_text = re.sub(r"Transaction ID:\s*([a-zA-Z0-9\-]+)", "", text).strip()
                if len(clean_text) < 100 and len(page.get_images()) == 0:
                    continue # 纯引导页跳过，去下一页截
            
            # 【核心过滤】：如果这个凭证不在当月有效列表里，直接跳过！绝不保存！
            if current_trans_id and current_trans_id in valid_id_prefixes:
                page_count += 1
                
                mask = df_summary['Trans_ID'].str.startswith(current_trans_id)
                vendor_name = df_summary.loc[mask, 'Vendor'].values[0] if not df_summary[mask].empty else ""
                emp_name = df_summary.loc[mask, 'Name'].values[0] if not df_summary[mask].empty else ""
                amt = df_summary.loc[mask, 'Amount'].values[0] if not df_summary[mask].empty else 0
                std_name = normalize_name(emp_name)
                
                extracted_title = next((t for t in VALID_TITLES if t in text), None)
                if not extracted_title and ocr_engine:
                    pix = page.get_pixmap(dpi=150)
                    ocr_result, _ = ocr_engine(pix.tobytes("png"))
                    if ocr_result:
                        ocr_text = " ".join([res[1] for res in ocr_result])
                        extracted_title = next((t for t in VALID_TITLES if t in ocr_text), None)
                
                # 既然命中了有效ID，说明员工必然有【本月有报销】的文件夹
                emp_folder = os.path.join(OUTPUT_EMP_DIR, f"【本月有报销】_{std_name}")
                if not os.path.exists(emp_folder):
                    os.makedirs(emp_folder) # 兜底建文件夹
                
                pix = page.get_pixmap(dpi=150)
                # 加上页码后缀，解决多张发票遗漏问题！
                suffix = f"P{page_count}"

                if extracted_title:
                    df_summary.loc[mask, 'Receipt_Type'] = "标准发票"
                    df_summary.loc[mask, 'Title_Check'] = "✅ 抬头正确"
                    img_name_emp = f"发票_{amt}_{current_trans_id}_{suffix}.png"
                    pix.save(os.path.join(emp_folder, img_name_emp))
                else:
                    # 更新状态时，如果之前某一页查出了合规，不要被异常页覆盖
                    current_status = df_summary.loc[mask, 'Title_Check'].values[0]
                    if current_status != "✅ 抬头正确":
                        df_summary.loc[mask, 'Receipt_Type'] = "非标件或异常"
                        df_summary.loc[mask, 'Title_Check'] = "⚠️ 需人工核实"
                    
                    img_name_abnormal = f"{std_name}_{amt}_{current_trans_id}_异常或非标_{suffix}.png"
                    pix.save(os.path.join(OUTPUT_IMAGE_DIR, img_name_abnormal))
                    
                    img_name_emp = f"非标或异常_{amt}_{current_trans_id}_{suffix}.png"
                    pix.save(os.path.join(emp_folder, img_name_emp))
                    
    return df_summary

# ==========================================
# 模块 3：数据合并与导出
# ==========================================
def reconcile_and_report(df_sys, df_pdf_summary):
    print("\n🚀 步骤4/4：最终交叉合并出报表...")
    if df_sys.empty: return
        
    df_sys['Amount'] = df_sys['Amount'].apply(clean_amount)
    if not df_pdf_summary.empty: df_pdf_summary['Amount'] = df_pdf_summary['Amount'].apply(clean_amount)
    
    df_sys['Standard_Name'] = df_sys['Name'].apply(normalize_name)
    if not df_pdf_summary.empty: df_pdf_summary['Standard_Name'] = df_pdf_summary['Name'].apply(normalize_name)
    
    df_sys['Currency'] = df_sys['Currency'].astype(str).str.strip().str.upper()
    if not df_pdf_summary.empty: df_pdf_summary['Currency'] = df_pdf_summary['Currency'].astype(str).str.strip().str.upper()

    df_sys['Is_Duplicate'] = df_sys.duplicated(subset=['Standard_Name', 'Currency', 'Amount'], keep=False)
    if not df_pdf_summary.empty: df_pdf_summary['Is_Duplicate'] = df_pdf_summary.duplicated(subset=['Standard_Name', 'Currency', 'Amount'], keep=False)

    merged_df = pd.merge(df_sys, df_pdf_summary, on=['Standard_Name', 'Currency', 'Amount'], how='left', indicator=True) if not df_pdf_summary.empty else df_sys.assign(_merge='left_only')
    merged_df['Name'] = merged_df['Name_x'] if 'Name_x' in merged_df.columns else merged_df['Name']
    
    def check_status(row):
        if row['_merge'] == 'both':
            return "⚠️ 金额重复需复看" if row.get('Is_Duplicate_x') or row.get('Is_Duplicate_y') else "✅ 匹配成功"
        return "❌ 异常：系统截图有，PDF汇总没找到"

    merged_df['审核结果'] = merged_df.apply(check_status, axis=1)
    
    if 'Remark' not in merged_df.columns:
        merged_df['Remark'] = ""
    merged_df['Remark'] = merged_df['Remark'].fillna("")
    
    cols =['Name', 'Standard_Name', 'Amount', 'Currency', 'Remark', '审核结果', 'Title_Check', 'Receipt_Type', 'Vendor', 'Source', 'PDF_File', 'Page_Num', 'Trans_ID']
    report_df = merged_df[[c for c in cols if c in merged_df.columns]].sort_values(by=['审核结果', 'Name'], ascending=[False, True]) 
    report_df.to_excel(OUTPUT_EXCEL, index=False)
    
    print(f"\n🎉 报告已生成：{OUTPUT_EXCEL}")
    print(f"📂 员工电子档案已精准清理并归档至：{OUTPUT_EMP_DIR} 文件夹。")

if __name__ == "__main__":
    # 清理并初始化环境
    init_env()
    
    # 1. 解析Word图片
    extracted_imgs = extract_images_from_word(FILE1_WORD_DIR, TEMP_IMG_DIR)
    
    # 2. 拿到当月报销清单 df_sys
    if not extracted_imgs:
        raise ValueError("Word 中没有可用的费用截图")
    df_system = read_images_with_ai(extracted_imgs)
    
    # 3. 读取PDF总表
    df_summary_raw = extract_pdf_summaries(FILE2_PDF_DIR)
    if df_summary_raw.empty:
        raise ValueError("PDF 中没有可用的交易汇总")
    
    # 4. 【对账查真凶】：找出真正属于这个月的 Trans_ID 列表
    valid_ids =[]
    if not df_system.empty and not df_summary_raw.empty:
        sys_temp = df_system.copy()
        sum_temp = df_summary_raw.copy()
        sys_temp['Standard_Name'] = sys_temp['Name'].apply(normalize_name)
        sum_temp['Standard_Name'] = sum_temp['Name'].apply(normalize_name)
        sys_temp['Amount'] = sys_temp['Amount'].apply(clean_amount)
        sum_temp['Amount'] = sum_temp['Amount'].apply(clean_amount)
        
        temp_merge = pd.merge(sys_temp, sum_temp, on=['Standard_Name', 'Currency', 'Amount'], how='left')
        valid_ids = temp_merge['Trans_ID'].dropna().unique().tolist()
    
    valid_id_prefixes = [str(vid)[:8] for vid in valid_ids]
    
    # 5. 拿着对账清单去PDF里“抓人”，生成最新的 df_summary
    df_summary_updated = extract_images_for_valid_ids(FILE2_PDF_DIR, df_summary_raw, valid_id_prefixes)
    
    # 6. 生成最终报告
    reconcile_and_report(df_system, df_summary_updated)