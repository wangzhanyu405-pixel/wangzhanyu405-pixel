# 箱贴打印与生产追溯平台

首页包含标签打印、扫码入库、扫码出库和溯源查询四个模块。可以按订单拆分满箱和尾箱，预览二维码标签，导出 PDF、ZPL，并查询每箱的打印、入库和出库时间。仓库根目录是可独立运行的 macOS 演示版，使用虚构的订单和本机 SQLite。

## 运行演示

在此目录使用 Python 3.13 创建虚拟环境并安装依赖。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./start.command
```

打开 [本机首页](http://127.0.0.1:18501/)。运行期间保持终端开启，按 Ctrl+C 停止服务。如果端口被占用，可使用 `LABEL_DEMO_PORT=18511 ./start.command`，并访问相应端口。

页面和标签使用系统中文字体。macOS 默认读取本机冬青黑体或华文黑体，也可在启动前设置 `CARTON_LABEL_FONT` 指向已安装的中文字体文件。在其他系统可指定 Noto Sans CJK。

## 走一遍业务流程

1. 打开「标签打印」，工艺单 `900001` 的第一款产品已载入。默认订货 250 件、每箱 100 件，生成后可查看两个满箱和一个 50 件尾箱。原模板另附空标签、备品标签、库存标签各一张，可在页面调整份数。
2. 下载 PDF 或 ZPL，然后点击「登记本批箱贴到本地打印流水」，复制其中一个生产箱码。也可直接使用下方已登记的体验箱。
3. 在「扫码入库」粘贴箱码并确认，再到「扫码出库」输入同一码确认。重复入库或重复出库会收到提示。依原操作规则，已登记的打印箱可在缺少入库记录时直接出库，页面会提示。
4. 打开「溯源查询」，按箱码或订单号查看当前状态、操作人和时间。打印、入库与出库流水存于 `labels.sqlite3`，重启服务后仍可查询。模板编辑和待打印区的修改随浏览器会话保存。可用 `LABEL_DEMO_DB` 指定另一个本地数据库文件。

首次启动会准备工艺单 `900002`、订单 `PO-DEMO-002`、每箱 40 件的体验箱。它的初始状态是已打印未入库。扫码时可直接复制 `DEMO-S01#202611240001#40`。需要纸质样签时，可下载 [扫码体验箱贴 PDF](samples/扫码体验箱贴_900002.pdf)，或运行 `.venv/bin/python generate_sample.py` 重新生成。

## Windows 与 SQL Server 源码

[windows_sqlserver](windows_sqlserver) 保留了 Windows 版的 Streamlit 界面、ERP 查询和打印流水、箱贴与模板引擎、Zebra 打印适配和可选 AI 审计。SQLite 演示版补充了对“料号#日期和四位箱号#数量”三段式箱码的识别。Windows 版保留归档时的旧扫码解析逻辑，部署时需验证现场模板与扫码规则的兼容性。

Windows 环境需要 Python、目录内的 `requirements.txt`、SQL Server ODBC 驱动和匹配的 ERP 数据库表结构。配置连接需设置 `LYC_DB_SERVER`、`LYC_DB_NAME`、`LYC_DB_USER` 和 `LYC_DB_PASSWORD`，可选 `LYC_DB_PORT`（默认 1433）与 `LYC_DB_TIMEOUT`。新库首次创建管理员及操作员账号可分别设置 `LYC_BOOTSTRAP_ADMIN_PASSWORD` 和 `LYC_BOOTSTRAP_OPERATOR_PASSWORD`，创建后可移除该环境设置。需要第三方 AI 审计时使用独立的 `DASHSCOPE_API_KEY`。

在配置好的独立测试库环境中，从 `windows_sqlserver` 目录运行 `python -m streamlit run app.py`。Windows 版启动及操作会创建或写入业务表。配套的 [只读视图 SQL](windows_sqlserver/sql/readonly_views.sql) 包含九个视图定义，供 DBA 对照测试库字段。实体打印需安装相应打印驱动。此备份的业务验证范围为 macOS 演示版，Windows、SQL Server 和实体 Zebra 的联合运行需在目标环境验证。
