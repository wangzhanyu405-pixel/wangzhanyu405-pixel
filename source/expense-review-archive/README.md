# 报销审核与凭证归档

将系统截图中的员工、币种和费用明细与交易汇总 PDF 对账，再按员工和凭证类型归档附件。月度审核表记录截图中的报销项目，凭证归档处理整份 PDF，因此两个工作簿覆盖的交易数可能不同。

## 安装和配置

Mac 离线模式使用 Apple Vision 和 Swift 编译器。请安装 Python 3.10 或更高版本，以及包含 `swiftc` 的 Xcode Command Line Tools。在项目根目录执行以下命令。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.json config.local.json
mkdir -p 输入_系统截图Word 输入_汇总凭证PDF
```

编辑本地的 `config.local.json`。在 `valid_titles` 中填写认可的购方抬头，每个 `employee_aliases` 子数组第一项是归档名，后面是可能出现在截图和 PDF 中的别名。也可以用 `EXPENSE_CONFIG` 指定另一份本地 JSON。仓库中的示例配置使用虚构名称。

| 输入目录 | 所需文件 |
| --- | --- |
| 输入_系统截图Word | 内嵌费用明细页截图的 `.docx`，截图为 PNG 或 JPEG |
| 输入_汇总凭证PDF | 带交易汇总和逐笔凭证页的 `.pdf`，含截短交易号、金额、币种、员工、Yes/No 标识及供应商，凭证使用 `Transaction ID:` 标记 |

## 运行

- **Mac 离线审核与归档**。双击 `start.command`，或执行 `./start.command`。此模式使用本机 Vision 识别兼容版式的人民币费用明细，使用 RapidOCR 辅助识别扫描凭证抬头。
- **只归档 PDF**。执行 `./start.command archive`。此模式仅需交易汇总 PDF 和本地规则。
- **千问云端审核与归档**。先执行 `.venv/bin/python -m pip install -r requirements-cloud.txt`，在运行环境中设置 `DASHSCOPE_API_KEY`，再执行 `./start.command cloud`。此模式将 Word 中的费用截图发往 DashScope 的 `qwen-vl-plus`。PDF 解析与凭证归档在本机执行。

每次运行在 `结果/` 下创建一个新目录。打开 `审核/输出_本月报销核对结果.xlsx` 查看金额匹配、重复金额提示与凭证抬头。打开 `归档/非标发票汇总.xlsx` 可查看嵌入的异常凭证、报销明细和缺附件清单，逐页图片在 `归档/员工凭证/`。

## 支持范围

离线截图识别适用于固定表格版式，依赖页首的单号、姓名、括号内员工编号及表格中两列人民币金额。跨币种结算或不同排版需要调整识别方法。PDF 审核从前 15 页提取交易汇总，按标准化姓名、币种、金额匹配；同名同额项目会标记复看。归档阶段按交易号前八位关联凭证，在单个 PDF 内这些前缀应唯一。抬头命中表示在该页读到了配置的文字，业务合规结论仍由审核人确认。
