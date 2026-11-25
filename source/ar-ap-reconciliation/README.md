# 财务智能对账平台

一个面向财务人员的应收应付工作台。将银行流水、采购及销售发票按往来单位归集，检查未匹配、逾期和回款情况，导出对账明细或付款模板。

## 本机运行

已在 Python 3.13 和 Streamlit 1.58 下验证。首次启动会在当前项目下创建空的 SQLite 账库。

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

浏览器打开 [本机平台](http://127.0.0.1:8502)。如端口已占用，可运行 `python run.py --port 8504`。Windows 使用 `py -3 -m venv .venv` 创建环境，再用 `.venv\Scripts\python.exe` 执行安装与启动命令。

## 使用流程

1. 在「往来单位」维护供应商、客户、账期和银行户名别名。该页支持下载目录模板。
2. 在「数据导入」选择文件类型，上传自行准备的 Excel。可在「银行流水」「发票台账」核对导入记录。
3. 在「应付工作台」或「应收工作台」限定日期并运行匹配。通过「异常处理」检查无法识别的名称和待处理的发票。
4. 在「付款模板」维护格式，再从应付工作台导出付款候选。银行实际支付由使用者另行处理。
5. 「看板」展示期间指标，「统计分析」提供单位余额和月度明细，「系统」展示存储配置。

## 存储与配置

本机账库默认位于 `data/local_finance.db`，上传和导出的工作簿分别位于 `data/uploads/` 和 `data/exports/`。测试使用临时目录及程序生成的工作簿。可用 `LYC_COMPANY_NAME` 指定页面显示的核算主体名称，也可在文件名缺少方向时据此比对购销方。

如需使用 SQL Server，准备独立数据库和本机可用的 ODBC 驱动，在本地环境变量中提供 `LYC_FIN_STORAGE=sqlserver`、`LYC_DB_DRIVER`、`LYC_DB_SERVER`、`LYC_DB_PORT`、`LYC_DB_NAME`、`LYC_DB_USER` 和 `LYC_DB_PASSWORD`。SQL Server 的业务表使用 `dbo.lyc_fin_*` 命名。迁移工具说明可运行 `python -m scripts.migrate_sqlite_to_sqlserver --help` 查看。

## 测试

```sh
python -m unittest discover -s tests
```

测试覆盖 Excel 解析、发票方向推断、匹配、余额计算、付款模板、导出格式和页面渲染。SQL Server 相关用例校验连接参数、表定义和迁移逻辑。
