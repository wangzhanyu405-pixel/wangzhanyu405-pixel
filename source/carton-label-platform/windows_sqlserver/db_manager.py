# -*- coding: utf-8 -*-
"""
db_manager.py v2 - 示例工厂 ERP 多料号数据抓取层

暴露的对外接口(严格匹配 app.py / llm_engine.py 的调用):
    - fetch_production_data(keyword, type)
    - fetch_shipment_by_process_no(process_no, item_no=None)
    - init_shadow_db()
    - insert_to_shadow_db(product, order_header, process_no)
    - get_pending_records()
    - confirm_shadow_record(record_id, operator='system')
    - delete_shadow_record(record_id, operator='system')
    - log_print_action(process_no, item_no, box_index, total_boxes,
                       qty_in_box, printer_name, qr_content, operator='system')
    - calculate_production_summary(...)       # 供 test_conn / 离线测试
    - identify_category(product_name, procs)  # 供外部复用
    - convert_qty(qty, unit, modulus)         # 供外部复用

核心架构:
    1. 一张工艺单 → N 款料号(gydcp),各自独立输出最终数量
    2. LWQT.分批料号 区分前道共用 vs 后道分款
    3. 品类识别用品名关键字 + 工序名兜底
    4. bcpchd/bcpchdmx 出库查询用 工艺单号+半成品名称 双关联
"""

import pandas as pd
import sqlite3
import hashlib
import secrets
import json
import copy
import os

try:
    import pyodbc
except ImportError:
    pyodbc = None


# ============================================================
# 一、ERP 数据库连接
# ============================================================
DB_CONFIG = {
    "server": os.getenv("LYC_DB_SERVER", ""),
    "port": os.getenv("LYC_DB_PORT", "1433"),
    "user": os.getenv("LYC_DB_USER", ""),
    "password": os.getenv("LYC_DB_PASSWORD", ""),
    "database": os.getenv("LYC_DB_NAME", ""),
    "timeout": int(os.getenv("LYC_DB_TIMEOUT", "5")),
}

_SHARED_AUTH_TABLES_READY = False


def get_erp_connection():
    if not all(DB_CONFIG.get(key) for key in ("server", "database", "user", "password")):
        raise ConnectionError("请通过环境变量设置 SQL Server 主机、库名、账号和密码。")
    if pyodbc is None:
        raise ConnectionError("pyodbc 未安装,请执行: pip install pyodbc")
    try:
        timeout = int(DB_CONFIG.get("timeout", 5))
        conn_str = (
            "DRIVER={SQL Server};"
            f"SERVER={DB_CONFIG['server']},{DB_CONFIG['port']};"
            f"DATABASE={DB_CONFIG['database']};"
            f"UID={DB_CONFIG['user']};"
            f"PWD={DB_CONFIG['password']};"
            f"Connection Timeout={timeout};"
            f"Login Timeout={timeout};"
            "TrustServerCertificate=yes;"
        )
        conn = pyodbc.connect(conn_str, timeout=timeout)
        conn.timeout = timeout
        return conn
    except Exception as e:
        raise ConnectionError(f"无法连接到 ERP 数据库: {str(e)}")


# ============================================================
# 一-补充、共享系统用户与权限(SQL Server lyc_ 表)
# ============================================================
PERMISSION_MODULES = {
    "label_print": "标签打印",
    "scan_in": "扫码入库",
    "scan_out": "扫码出库",
    "trace_query": "溯源查询",
    "template": "打印模板",
    "accounts": "账号管理",
}

DEFAULT_ROLE_PERMISSIONS = {
    "admin": tuple(PERMISSION_MODULES.keys()),
    "operator": ("label_print", "scan_in", "scan_out", "trace_query"),
}


def _default_permissions_for_role(role_code):
    return set(DEFAULT_ROLE_PERMISSIONS.get(_to_str(role_code), DEFAULT_ROLE_PERMISSIONS["operator"]))


def _hash_password(password, salt=None, iterations=120000):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        'sha256',
        str(password).encode('utf-8'),
        salt.encode('utf-8'),
        iterations,
    ).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def _verify_password(password, password_hash):
    try:
        algo, iterations, salt, digest = str(password_hash).split('$', 3)
        if algo != 'pbkdf2_sha256':
            return False
        check = _hash_password(password, salt=salt, iterations=int(iterations))
        return secrets.compare_digest(check, password_hash)
    except Exception:
        return False


def init_shared_auth_tables():
    """初始化共享登录表。只创建 lyc_ 前缀表,不修改 ERP 原表。"""
    global _SHARED_AUTH_TABLES_READY
    if _SHARED_AUTH_TABLES_READY:
        return
    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute("""
        IF OBJECT_ID(N'dbo.lyc_users', N'U') IS NULL
        BEGIN
            CREATE TABLE dbo.lyc_users (
                id INT IDENTITY(1,1) PRIMARY KEY,
                username NVARCHAR(50) NOT NULL UNIQUE,
                password_hash NVARCHAR(255) NOT NULL,
                display_name NVARCHAR(50) NOT NULL,
                role_code NVARCHAR(20) NOT NULL,
                is_active BIT NOT NULL DEFAULT 1,
                created_at DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
                last_login_at DATETIME2 NULL
            )
        END
    """)
    cursor.execute("""
        IF OBJECT_ID(N'dbo.lyc_audit_logs', N'U') IS NULL
        BEGIN
            CREATE TABLE dbo.lyc_audit_logs (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NULL,
                action NVARCHAR(50) NOT NULL,
                target_type NVARCHAR(50) NULL,
                target_id INT NULL,
                process_no NVARCHAR(50) NULL,
                item_no NVARCHAR(100) NULL,
                detail NVARCHAR(MAX) NULL,
                created_at DATETIME2 NOT NULL DEFAULT SYSDATETIME()
            )
        END
    """)
    cursor.execute("""
        IF OBJECT_ID(N'dbo.lyc_user_permissions', N'U') IS NULL
        BEGIN
            CREATE TABLE dbo.lyc_user_permissions (
                id INT IDENTITY(1,1) PRIMARY KEY,
                username NVARCHAR(50) NOT NULL,
                module_code NVARCHAR(50) NOT NULL,
                can_access BIT NOT NULL DEFAULT 1,
                updated_at DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
                updated_by_name NVARCHAR(50) NULL,
                CONSTRAINT uq_lyc_user_permissions UNIQUE (username, module_code)
            )
        END
    """)
    bootstrap_accounts = [
        ("admin", "管理员", "admin", os.environ.get("LYC_BOOTSTRAP_ADMIN_PASSWORD")),
        ("operator", "操作员", "operator", os.environ.get("LYC_BOOTSTRAP_OPERATOR_PASSWORD")),
    ]
    for username, display_name, role_code, default_password in bootstrap_accounts:
        if not default_password:
            continue
        cursor.execute("SELECT COUNT(*) FROM dbo.lyc_users WHERE username = ?", username)
        exists = cursor.fetchone()[0]
        if not exists:
            cursor.execute(
                """INSERT INTO dbo.lyc_users
                   (username, password_hash, display_name, role_code, is_active)
                   VALUES (?, ?, ?, ?, 1)""",
                username,
                _hash_password(default_password),
                display_name,
                role_code,
            )
    conn.commit()
    conn.close()
    _SHARED_AUTH_TABLES_READY = True


def get_user_permissions(username, role_code='operator'):
    """Return allowed module codes for a login user."""
    username = _to_str(username).strip()
    role_code = _to_str(role_code).strip() or 'operator'
    defaults = _default_permissions_for_role(role_code)
    if role_code == 'admin':
        return set(PERMISSION_MODULES.keys())

    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT module_code, can_access
           FROM dbo.lyc_user_permissions
           WHERE username = ?""",
        username,
    )
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        return defaults
    allowed = {
        _to_str(module_code)
        for module_code, can_access in rows
        if bool(can_access) and _to_str(module_code) in PERMISSION_MODULES
    }
    return allowed


def get_user_permissions_with_cursor(cursor, username, role_code='operator'):
    """Return permissions using an existing SQL Server cursor."""
    username = _to_str(username).strip()
    role_code = _to_str(role_code).strip() or 'operator'
    defaults = _default_permissions_for_role(role_code)
    if role_code == 'admin':
        return set(PERMISSION_MODULES.keys())
    cursor.execute(
        """SELECT module_code, can_access
           FROM dbo.lyc_user_permissions
           WHERE username = ?""",
        username,
    )
    rows = cursor.fetchall()
    if not rows:
        return defaults
    return {
        _to_str(module_code)
        for module_code, can_access in rows
        if bool(can_access) and _to_str(module_code) in PERMISSION_MODULES
    }


def save_user_permissions(username, module_codes, operator='system'):
    """Replace one user's module permissions."""
    init_shared_auth_tables()
    username = _to_str(username).strip()
    allowed = {
        _to_str(code).strip()
        for code in (module_codes or [])
        if _to_str(code).strip() in PERMISSION_MODULES
    }
    conn = get_erp_connection()
    cursor = conn.cursor()
    for module_code in PERMISSION_MODULES:
        can_access = 1 if module_code in allowed else 0
        cursor.execute(
            """
            MERGE dbo.lyc_user_permissions AS target
            USING (SELECT ? AS username, ? AS module_code) AS source
            ON target.username = source.username AND target.module_code = source.module_code
            WHEN MATCHED THEN
                UPDATE SET can_access = ?, updated_at = SYSDATETIME(), updated_by_name = ?
            WHEN NOT MATCHED THEN
                INSERT (username, module_code, can_access, updated_by_name)
                VALUES (?, ?, ?, ?);
            """,
            username,
            module_code,
            can_access,
            _to_str(operator),
            username,
            module_code,
            can_access,
            _to_str(operator),
        )
    cursor.execute(
        """INSERT INTO dbo.lyc_audit_logs (action, target_type, detail)
           VALUES (?, ?, ?)""",
        'update_user_permissions',
        'user',
        json.dumps(
            {
                'username': username,
                'modules': sorted(allowed),
                'operator': _to_str(operator),
            },
            ensure_ascii=False,
        ),
    )
    conn.commit()
    conn.close()


def authenticate_user(username, password):
    """返回登录用户 dict;失败返回 None。"""
    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT id, username, password_hash, display_name, role_code, is_active
           FROM dbo.lyc_users WHERE username = ?""",
        _to_str(username),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    user_id, uname, pwd_hash, display_name, role_code, is_active = row
    if not is_active or not _verify_password(password, pwd_hash):
        conn.close()
        return None
    cursor.execute(
        "UPDATE dbo.lyc_users SET last_login_at = SYSDATETIME() WHERE id = ?",
        int(user_id),
    )
    cursor.execute(
        "INSERT INTO dbo.lyc_audit_logs (user_id, action, detail) VALUES (?, ?, ?)",
        int(user_id),
        'login',
        json.dumps({'username': uname}, ensure_ascii=False),
    )
    permissions = sorted(get_user_permissions_with_cursor(cursor, uname, role_code))
    conn.commit()
    conn.close()
    return {
        'id': int(user_id),
        'username': _to_str(uname),
        'display_name': _to_str(display_name),
        'role_code': _to_str(role_code),
        'permissions': permissions,
    }


def list_system_users():
    """Return shared login users from SQL Server."""
    init_shared_auth_tables()
    conn = get_erp_connection()
    sql = """
        SELECT id, username, display_name, role_code, is_active, created_at, last_login_at
        FROM dbo.lyc_users
        ORDER BY role_code ASC, username ASC
    """
    df = pd.read_sql(sql, conn)
    conn.close()
    if not df.empty:
        df['permissions'] = [
            ','.join(sorted(get_user_permissions(row['username'], row['role_code'])))
            for _, row in df.iterrows()
        ]
    return df


def save_system_user(username, display_name, password=None, role_code='operator',
                     is_active=True, operator='system', permissions=None):
    """Create or update a user. Password changes only when password is provided."""
    init_shared_auth_tables()
    username = _to_str(username).strip()
    display_name = _to_str(display_name).strip() or username
    role_code = _to_str(role_code).strip() or 'operator'
    if role_code not in ('admin', 'operator'):
        role_code = 'operator'
    if not username:
        raise ValueError("账号不能为空")
    if password is not None and str(password).strip() and len(str(password)) < 4:
        raise ValueError("密码至少 4 位")

    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT TOP 1 id FROM dbo.lyc_users WHERE username = ?", username)
    row = cursor.fetchone()
    active_bit = 1 if is_active else 0
    if row:
        user_id = int(row[0])
        if password is not None and str(password).strip():
            cursor.execute(
                """UPDATE dbo.lyc_users
                   SET display_name = ?, role_code = ?, is_active = ?, password_hash = ?
                   WHERE id = ?""",
                display_name,
                role_code,
                active_bit,
                _hash_password(password),
                user_id,
            )
        else:
            cursor.execute(
                """UPDATE dbo.lyc_users
                   SET display_name = ?, role_code = ?, is_active = ?
                   WHERE id = ?""",
                display_name,
                role_code,
                active_bit,
                user_id,
            )
        action = 'update_user'
    else:
        if not password or not str(password).strip():
            raise ValueError("新增账号必须填写密码")
        cursor.execute(
            """INSERT INTO dbo.lyc_users
               (username, password_hash, display_name, role_code, is_active)
               OUTPUT INSERTED.id
               VALUES (?, ?, ?, ?, ?)""",
            username,
            _hash_password(password),
            display_name,
            role_code,
            active_bit,
        )
        inserted_row = cursor.fetchone()
        if inserted_row and inserted_row[0] is not None:
            user_id = int(inserted_row[0])
        else:
            cursor.execute("SELECT TOP 1 id FROM dbo.lyc_users WHERE username = ?", username)
            lookup_row = cursor.fetchone()
            if not lookup_row or lookup_row[0] is None:
                raise RuntimeError("新增账号已提交失败：未能取得账号 ID")
            user_id = int(lookup_row[0])
        action = 'create_user'

    cursor.execute(
        """INSERT INTO dbo.lyc_audit_logs (action, target_type, target_id, detail)
           VALUES (?, ?, ?, ?)""",
        action,
        'user',
        user_id,
        json.dumps(
            {
                'username': username,
                'display_name': display_name,
                'role_code': role_code,
                'is_active': bool(is_active),
                'operator': _to_str(operator),
                'password_changed': bool(password and str(password).strip()),
            },
            ensure_ascii=False,
        ),
    )
    conn.commit()
    conn.close()
    if permissions is not None:
        save_user_permissions(username, permissions, operator=operator)


def delete_system_user(username, operator='system'):
    """Delete a shared login user. The built-in admin account is protected."""
    init_shared_auth_tables()
    username = _to_str(username).strip()
    if not username:
        raise ValueError("账号不能为空")
    if username == 'admin':
        raise ValueError("默认管理员账号不能删除")

    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT TOP 1 id, role_code FROM dbo.lyc_users WHERE username = ?", username)
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise ValueError("账号不存在或已被删除")

    user_id = int(row[0])
    role_code = _to_str(row[1])
    if role_code == 'admin':
        cursor.execute("SELECT COUNT(*) FROM dbo.lyc_users WHERE role_code = 'admin' AND is_active = 1 AND username <> ?", username)
        other_admin_count = int(cursor.fetchone()[0] or 0)
        if other_admin_count <= 0:
            conn.close()
            raise ValueError("至少需要保留一个可用管理员账号")

    cursor.execute("DELETE FROM dbo.lyc_users WHERE id = ?", user_id)
    cursor.execute(
        """INSERT INTO dbo.lyc_audit_logs (action, target_type, target_id, detail)
           VALUES (?, ?, ?, ?)""",
        'delete_user',
        'user',
        user_id,
        json.dumps(
            {
                'username': username,
                'operator': _to_str(operator),
            },
            ensure_ascii=False,
        ),
    )
    conn.commit()
    conn.close()
    return user_id


# ============================================================
# 二、类型安全工具
# ============================================================
def _to_int(val, default=0):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def _to_float(val, default=0.0):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _to_str(val, default=''):
    if val is None:
        return default
    if isinstance(val, float) and pd.isna(val):
        return default
    s = str(val).strip()
    if s.lower() in ('nan', 'none', 'null'):
        return default
    return s


def _to_optional_code(val):
    s = _to_str(val)
    if not s:
        return ""
    if s in ("0", "0.0", "0.00"):
        return ""
    return s


# ============================================================
# 三、工艺单号解析
# ============================================================
def resolve_process_no(conn, search_keyword, search_type="工艺单号"):
    cursor = conn.cursor()
    if search_type == "工艺单号":
        try:
            gyd_no = int(search_keyword)
        except ValueError:
            return None, f"工艺单号必须是数字,收到: {search_keyword}"
        cursor.execute("SELECT TOP 1 [工艺单号] FROM [gyd] WHERE [工艺单号] = ?", (gyd_no,))
        row = cursor.fetchone()
        if row:
            return int(row[0]), None
        return None, f"未在 gyd 表中找到工艺单号 {gyd_no}"
    elif search_type == "订单号":
        cursor.execute(
            "SELECT TOP 1 [工艺单号] FROM [gyd] WHERE [定单号码] = ?",
            (str(search_keyword),)
        )
        row = cursor.fetchone()
        if row:
            return int(row[0]), None
        return None, f"未在 gyd 表中找到定单号码 {search_keyword}"
    elif search_type == "料号":
        keyword = _to_str(search_keyword)
        cursor.execute(
            """
            SELECT TOP 1 [工艺单号]
            FROM [gydcp]
            WHERE CAST([料号] AS NVARCHAR(100)) = ?
               OR CAST([料号] AS NVARCHAR(100)) LIKE ?
            ORDER BY [工艺单号] DESC
            """,
            keyword,
            f"%{keyword}%",
        )
        row = cursor.fetchone()
        if row:
            return int(row[0]), None
        return None, f"未在 gydcp 表中找到料号 {keyword}"
    return None, f"不支持的检索类型: {search_type}"


# ============================================================
# 四、四级数据拉取
# ============================================================
def fetch_gyd_header(conn, process_no):
    sql = """
        SELECT [工艺单号], [定单号码], [料号], [产品名称], [客户名称],
               [规格], [订货数量], [投产数量], [产出数量], [单位],
               [工艺单备注], [后道工序], [后道备注], [印刷备注], [其他备注],
               [预定交期], [业务员], [分类]
        FROM [gyd]
        WHERE [工艺单号] = ?
    """
    df = pd.read_sql(sql, conn, params=[process_no])
    return df.iloc[0].to_dict() if not df.empty else {}


def fetch_gydcp_products(conn, process_no):
    sql = """
        SELECT cp.[工艺单号], cp.[订单ID], cp.[明细ID],
               d.[定单号码] AS [明细定单号码],
               cp.[产品编号], cp.[料号], cp.[产品名称], cp.[规格],
               cp.[订货数量], cp.[投产数量], cp.[产出数量], cp.[交货数量],
               cp.[单位], cp.[模数], cp.[拼版数], cp.[分类], cp.[箱规], cp.[包装方式]
        FROM [gydcp] cp
        LEFT JOIN [ddgl] d ON cp.[订单ID] = d.[订单ID]
        WHERE cp.[工艺单号] = ?
        ORDER BY cp.[序号] ASC
    """
    df = pd.read_sql(sql, conn, params=[process_no])
    df['SAP号'] = _fetch_material_codes_for_products(conn, df)
    return df


def _get_table_columns(conn, table_name):
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = ?
        """,
        table_name,
    )
    return {str(row[0]) for row in cursor.fetchall()}


def _fetch_one_sap_code(conn, table_name, columns, product_row):
    """从 ERP 明细表读取 SAP 编号。找不到时返回空字符串，不按订单ID粗匹配。"""
    if "编号" not in columns:
        return ""

    def has_value(product_col):
        return product_col in product_row.index and _to_str(product_row.get(product_col))

    match_groups = []

    if "订单ID" in columns and "明细ID" in columns and has_value("订单ID") and has_value("明细ID"):
        match_groups.append([
            ("订单ID", "订单ID"),
            ("明细ID", "明细ID"),
        ])
    if "订单ID" in columns and "产品编号" in columns and has_value("订单ID") and has_value("产品编号"):
        match_groups.append([
            ("订单ID", "订单ID"),
            ("产品编号", "产品编号"),
        ])
    if "订单ID" in columns and "料号" in columns and has_value("订单ID") and has_value("料号"):
        match_groups.append([
            ("订单ID", "订单ID"),
            ("料号", "料号"),
        ])
    if "产品编号" in columns and has_value("产品编号"):
        match_groups.append([
            ("产品编号", "产品编号"),
        ])
    if "料号" in columns and has_value("料号"):
        match_groups.append([
            ("料号", "料号"),
        ])

    if not match_groups:
        return ""

    cursor = conn.cursor()

    for group in match_groups:
        where_sql = " AND ".join(f"[{table_col}] = ?" for table_col, _ in group)
        params = [product_row.get(product_col) for _, product_col in group]
        try:
            cursor.execute(
                f"SELECT TOP 1 [编号] FROM [{table_name}] WHERE {where_sql}",
                params,
            )
            row = cursor.fetchone()
        except Exception:
            continue
        value = _to_optional_code(row[0]) if row else ""
        if value:
            return value
    return ""


def _fetch_material_codes_for_products(conn, gydcp_df):
    """SAP号来源于 ERP 明细表 ddglmx 中的“编号”。"""
    if gydcp_df.empty:
        return []
    table_columns = {}
    for table_name in ("ddglmx",):
        try:
            table_columns[table_name] = _get_table_columns(conn, table_name)
        except Exception:
            table_columns[table_name] = set()

    sap_values = []
    for _, row in gydcp_df.iterrows():
        sap_no = ""
        for table_name in ("ddglmx",):
            sap_no = _fetch_one_sap_code(conn, table_name, table_columns[table_name], row)
            if sap_no:
                break
        sap_values.append(sap_no)
    return sap_values


def fetch_cpck_summary(conn, process_no):
    """查询成品库 cpck 中该工艺单的最近入库/出货时间。"""
    sql = """
        SELECT
            [工艺单号],
            MAX([入库时间]) AS [最近入库时间],
            MAX([出货时间]) AS [最近出货时间]
        FROM [cpck]
        WHERE [工艺单号] = ?
        GROUP BY [工艺单号]
    """
    try:
        df = pd.read_sql(sql, conn, params=[process_no])
    except Exception:
        return {}
    if df.empty:
        return {}
    row = df.iloc[0].to_dict()
    return {
        '最近入库时间': _to_str(row.get('最近入库时间')),
        '最近出货时间': _to_str(row.get('最近出货时间')),
        '累计入库数量': 0,
        '累计出货数量': 0,
    }


def fetch_gydqt_processes(conn, process_no):
    sql = """
        SELECT [其他id], [工艺单号], [序号], [其他排序],
               [其他项目], [工序分类], [名称], [规格], [单位],
               [模数], [数量], [产出数量],
               [计划开工], [计划完工], [实际开工], [实际完工], [完工人]
        FROM [gydqt]
        WHERE [工艺单号] = ?
        ORDER BY [其他排序] ASC, [序号] ASC
    """
    df = pd.read_sql(sql, conn, params=[process_no])
    return df


def fetch_lwqt_reports(conn, process_no):
    """
    LWQT 报工表——分批料号/分批产品是关键:
        - 两者都为空 → 前道共用工序(印刷/打码/模切)
        - 有值 → 后道分款工序(糊盒/折页/检品)
    """
    sql = """
        SELECT [QTLWID], [其他id], [工艺单号], [时间], [完成数量],
               [操作人], [班组], [备注], [分批料号], [分批产品]
        FROM [LWQT]
        WHERE [工艺单号] = ?
        ORDER BY [时间] ASC
    """
    df = pd.read_sql(sql, conn, params=[process_no])
    return df


# ============================================================
# 五、品类识别(品名关键字 → 工序兜底)
# ============================================================
CATEGORY_KEYWORDS = {
    'manual': ['说明书', '使用说明', '手册', '指南'],
    'box':    ['盒', '彩盒', '外盒', '包装盒', '礼盒', '展示盒'],
    'card':   ['卡', '牌', '游戏卡', '卡片', '卡牌'],
}

CATEGORY_BY_PROCESS_HINT = {
    '糊盒': 'box',
    '折页': 'manual',
    '装订': 'manual',
}

FINAL_PROCESS_BY_CATEGORY = {
    'card':    ['入库', '成品检', '检品', '分拣', '模切'],
    'box':     ['入库', '糊盒', '成型', '检品', '包装'],
    'manual':  ['入库', '折页', '装订', '检品'],
    'unknown': ['入库', '成品检', '检品', '包装', '装箱',
                '糊盒', '成型', '折页', '装订'],
}

# v3.1 新增:用 工序分类 字段做精准识别(主信号)
# 工序分类是 ERP 自身的分类标签,远比工序名称关键字可靠
FINAL_CATEGORY_BY_PRODUCT = {
    'card':    ['入库', '成品检', '检品', '分拣'],
    'box':     ['入库', '糊盒', '成型', '检品'],
    'manual':  ['入库', '折页', '装订', '检品'],
    'unknown': ['入库', '糊盒', '检品', '折页', '装订', '分拣', '成型'],
}

# 终点识别黑名单:这些"工序分类"绝对不能作为终点
# (它们是前道/中间工序,跟最终成品数无关)
FINAL_CATEGORY_BLACKLIST = {
    '印刷', '喷码', '打码', '覆膜', '过油', '烫金', '上光',
    '模切', '裁切', '裁纸', '压痕', '压凹凸', '丝印', '凹凸',
}


def identify_category(product_name, process_names=None):
    """
    按品名识别品类,品名识别不出时用工序名称兜底。
    说明书优先级最高(避免"XX说明书盒"误判为盒)。
    """
    name = _to_str(product_name)
    if process_names is None:
        process_names = []

    # 第一轮:品名关键字,按 manual > box > card 的优先级
    for cat in ['manual', 'box', 'card']:
        for kw in CATEGORY_KEYWORDS[cat]:
            if kw in name:
                return cat

    # 第二轮:工序名兜底
    proc_joined = ' '.join(_to_str(p) for p in process_names)
    for hint, cat in CATEGORY_BY_PROCESS_HINT.items():
        if hint in proc_joined:
            return cat
    if '模切' in proc_joined and '糊盒' not in proc_joined:
        return 'card'
    return 'unknown'


# ============================================================
# 六、模数折算
# ============================================================
UNITS_NEED_MODULUS = {'张', '大张', '大版', '版', '令', '米', '印张'}
UNITS_FINISHED = {'个', '只', '件', 'pcs', 'PCS', '套', '片', '条', '包', '箱'}


def convert_qty(qty, unit, modulus):
    """单位为"张/米/版"时需要×模数,"个/只/件"直接用。"""
    qty = _to_float(qty)
    modulus = _to_float(modulus)
    unit_clean = _to_str(unit).strip()
    if qty <= 0:
        return 0, False, None
    if unit_clean in UNITS_NEED_MODULUS:
        if modulus <= 0:
            return int(qty), False, f"单位[{unit_clean}]需要模数折算,但模数为 0"
        return int(qty * modulus), True, None
    if unit_clean in UNITS_FINISHED:
        return int(qty), False, None
    return int(qty), False, f"未知单位[{unit_clean}],未做模数折算"


# ============================================================
# 七、核心算法:多料号汇总
# ============================================================
def _match_batch_to_product(batch_item_no, batch_product_name, products):
    """LWQT 分批字段 → gydcp 料号 的匹配。"""
    batch_item = _to_str(batch_item_no)
    batch_name = _to_str(batch_product_name)
    if batch_item:
        for p in products:
            if _to_str(p['料号']) == batch_item:
                return p
    if batch_name:
        for p in products:
            pname = _to_str(p['品名'])
            if pname and (batch_name in pname or pname in batch_name):
                return p
    return None


def calculate_production_summary(process_no, gyd_header,
                                  gydcp_df, gydqt_df, lwqt_df):
    """
    核心汇总函数。输入四张表(一张 dict + 三张 DataFrame),输出结构化 summary。

    返回结构:
    {
      "process_no": "900001",
      "order_header": {定单号码,客户名称,工艺单备注,...},
      "products": [
        {
          "料号", "品名", "规格", "订货数量", "模数", "品类",
          "最后工序名称", "最终成品数",
          "后道工序": [ {工序名称, 实际报工(原单位), 折算成品(个), ...}, ...],
          "警告": [...]
        }, ...
      ],
      "shared_front_processes": [
        {工序名称, 实际报工(原单位), 计划投产, 机长列表, 报工明细, ...}
      ],
      "warnings": [...全局警告]
    }
    """
    warnings = []

    # ---- 1. 构建产品列表 ----
    products = []
    if gydcp_df.empty:
        # gydcp 为空降级为单款(用 gyd 主表)
        products.append({
            '料号': _to_str(gyd_header.get('料号')),
            'SAP号': _to_optional_code(gyd_header.get('SAP号') or gyd_header.get('编号')),
            '品名': _to_str(gyd_header.get('产品名称')),
            '规格': _to_str(gyd_header.get('规格')),
            '产品编号': 0,
            '订货数量': _to_int(gyd_header.get('订货数量', 0)),
            '投产数量': _to_int(gyd_header.get('投产数量', 0)),
            '模数': 0,
            '包装方式': _to_str(gyd_header.get('包装方式')),
            '单位': _to_str(gyd_header.get('单位')),
        })
        warnings.append("gydcp 无数据,已降级为单款产品")
    else:
        for _, row in gydcp_df.iterrows():
            products.append({
                '料号': _to_str(row['料号']),
                'SAP号': _to_optional_code(row.get('SAP号') or row.get('编号')),
                '定单号码': _to_str(row.get('明细定单号码')),
                '订单ID': _to_int(row.get('订单ID', 0)),
                '明细ID': _to_int(row.get('明细ID', 0)),
                '品名': _to_str(row['产品名称']),
                '规格': _to_str(row['规格']),
                '产品编号': _to_int(row['产品编号']),
                '订货数量': _to_int(row['订货数量']),
                '投产数量': _to_int(row['投产数量']),
                '模数': _to_int(row['模数']) or _to_int(row.get('拼版数', 0)),
                '箱规': _to_int(row.get('箱规', 0)),
                '包装方式': _to_str(row.get('包装方式', '')),
                '单位': _to_str(row['单位']),
            })

    # ---- 2. 遍历工序 ----
    processes_front = []
    per_product_back = {p['料号']: [] for p in products}

    for _, proc in gydqt_df.iterrows():
        qt_id = _to_int(proc['其他id'])
        unit = _to_str(proc['单位'])
        proc_modulus = _to_float(proc['模数'])
        plan_qty = _to_float(proc['数量'])
        proc_name = (_to_str(proc['名称'])
                     or _to_str(proc['工序分类'])
                     or f"工序#{qt_id}")
        proc_category = _to_str(proc['工序分类'])
        order_rank = _to_float(proc.get('其他排序', 0))

        # 本工序的所有报工
        if not lwqt_df.empty:
            this_reports = lwqt_df[lwqt_df['其他id'] == qt_id]
        else:
            this_reports = pd.DataFrame()

        # 分桶
        total_raw_shared = 0.0
        shared_reports = []
        per_product_raw = {p['料号']: 0.0 for p in products}
        per_product_reports_local = {p['料号']: [] for p in products}
        workers_shared = []
        time_shared = ''

        for _, rep in this_reports.iterrows():
            qty = _to_float(rep['完成数量'])
            worker = _to_str(rep['操作人'])
            team = _to_str(rep['班组'])
            t = _to_str(rep['时间'])
            batch_item = _to_str(rep['分批料号'])
            batch_product = _to_str(rep['分批产品'])

            rep_dict = {
                '数量': int(qty) if qty == int(qty) else qty,
                '操作人': worker,
                '班组': team,
                '时间': t,
                '分批料号': batch_item,
                '分批产品': batch_product,
            }

            if batch_item or batch_product:
                matched = _match_batch_to_product(batch_item, batch_product, products)
                if matched:
                    per_product_raw[matched['料号']] += qty
                    per_product_reports_local[matched['料号']].append(rep_dict)
                else:
                    total_raw_shared += qty
                    shared_reports.append(rep_dict)
                    if worker and worker not in workers_shared:
                        workers_shared.append(worker)
                    if t > time_shared:
                        time_shared = t
                    warnings.append(
                        f"工序【{proc_name}】报工分批料号[{batch_item}]未匹配到 gydcp 料号"
                    )
            else:
                total_raw_shared += qty
                shared_reports.append(rep_dict)
                if worker and worker not in workers_shared:
                    workers_shared.append(worker)
                if t > time_shared:
                    time_shared = t

        has_any_per_product = any(v > 0 for v in per_product_raw.values())

        # 特殊情况:只有一款产品时,所有"未分批"的报工自动归属到这款产品
        # 这避免了单款工艺单里车间不填分批料号、导致最后工序被错误当成前道的问题
        single_product_auto_assign = (len(products) == 1 and total_raw_shared > 0)

        if single_product_auto_assign:
            only_p = products[0]
            per_product_raw[only_p['料号']] += total_raw_shared
            per_product_reports_local[only_p['料号']].extend(shared_reports)
            total_raw_shared = 0.0
            shared_reports = []
            has_any_per_product = True

        # 判断这个工序是"前道共用"还是"后道分款"
        # 规则:工序分类是"印刷"的一定是前道;其他看是否有分款报工
        is_front_shared = (_to_str(proc_category) == '印刷')

        if is_front_shared or not has_any_per_product:
            # 前道共用工序
            # 注意:单款场景下即使是印刷,也把它的报工累计给该款产品作参考,但此工序仍登记在前道
            finished, _, warn = convert_qty(total_raw_shared, unit, proc_modulus)
            if warn:
                warnings.append(f"【{proc_name}】{warn}")
            processes_front.append({
                '工序名称': proc_name,
                '工序分类': proc_category,
                '单位': unit,
                '工序模数': int(proc_modulus),
                '计划投产': int(plan_qty),
                '实际报工(原单位)': int(total_raw_shared),
                '折算成品(按工序模数,仅参考)': finished,
                '机长列表': workers_shared,
                '完工时间': time_shared,
                '报工明细': shared_reports,
                '_其他id': qt_id,
                '_其他排序': order_rank,
            })
            # 单款场景下,印刷也要填充到产品的"后道"用于显示(但标记为前道类型)
            if single_product_auto_assign and is_front_shared:
                only_p = products[0]
                effective_modulus = proc_modulus if proc_modulus > 0 else (only_p['模数'] or 1)
                shared_finished, _, _ = convert_qty(total_raw_shared, unit, effective_modulus)
                # 把印刷也给该产品加一条,但不参与"最后工序"判断
                per_product_back[only_p['料号']].append({
                    '工序名称': proc_name + '(前道)',
                    '工序分类': proc_category,
                    '单位': unit,
                    '工序模数': int(effective_modulus),
                    '计划投产(仅供参考)': int(plan_qty),
                    '实际报工(原单位)': int(total_raw_shared),
                    '折算成品(个)': shared_finished,
                    '机长列表': workers_shared,
                    '完工时间': time_shared,
                    '报工明细': shared_reports,
                    '_其他id': qt_id,
                    '_其他排序': order_rank,
                    '是否最后工序': False,
                    '_是前道': True,
                })
        else:
            # 后道分款工序
            for p in products:
                raw_qty = per_product_raw[p['料号']]
                local_reports = per_product_reports_local[p['料号']]
                if raw_qty <= 0 and not local_reports:
                    continue
                effective_modulus = proc_modulus if proc_modulus > 0 else (p['模数'] or 1)
                finished, _, warn = convert_qty(raw_qty, unit, effective_modulus)
                if warn:
                    warnings.append(f"【{proc_name}/{p['料号']}】{warn}")
                per_product_back[p['料号']].append({
                    '工序名称': proc_name,
                    '工序分类': proc_category,
                    '单位': unit,
                    '工序模数': int(effective_modulus),
                    '计划投产(仅供参考)': int(plan_qty),
                    '实际报工(原单位)': int(raw_qty),
                    '折算成品(个)': finished,
                    '机长列表': list({r['操作人'] for r in local_reports if r['操作人']}),
                    '完工时间': max((r['时间'] for r in local_reports if r['时间']), default=''),
                    '报工明细': local_reports,
                    '_其他id': qt_id,
                    '_其他排序': order_rank,
                    '是否最后工序': False,
                })

    # ---- 3. 每款产品识别最后工序 & 最终数量 ----
    global_proc_names = [p['工序名称'] for p in processes_front]

    per_product_summary = []
    for p in products:
        back_procs = per_product_back.get(p['料号'], [])
        this_proc_names = [q['工序名称'] for q in back_procs] + global_proc_names
        category = identify_category(p['品名'], this_proc_names)

        # 候选只从"非前道"的工序里找(排除_是前道=True的)
        candidate_back = [q for q in back_procs if not q.get('_是前道')]

        # 应用黑名单:剔除工序分类是前道类型的
        candidate_back = [
            q for q in candidate_back
            if _to_str(q.get('工序分类', '')) not in FINAL_CATEGORY_BLACKLIST
        ]

        final_proc = None
        match_reason = ''

        # 第一级:按 工序分类 字段精准匹配(最可靠)
        if not final_proc:
            cat_keywords = FINAL_CATEGORY_BY_PRODUCT.get(category, FINAL_CATEGORY_BY_PRODUCT['unknown'])
            for kw in cat_keywords:
                matched = [
                    q for q in candidate_back
                    if kw in _to_str(q.get('工序分类', ''))
                ]
                if matched:
                    final_proc = max(matched, key=lambda q: q['_其他排序'])
                    match_reason = f"按工序分类[{kw}]匹配"
                    break

        # 第二级:工序分类没匹配上,用工序名称关键字
        if not final_proc:
            name_keywords = FINAL_PROCESS_BY_CATEGORY[category]
            for kw in name_keywords:
                matched = [q for q in candidate_back if kw in q['工序名称']]
                if matched:
                    final_proc = max(matched, key=lambda q: q['_其他排序'])
                    match_reason = f"按工序名称关键字[{kw}]匹配"
                    break

        # 第三级:都没匹配到,取排序最大的非黑名单工序
        if not final_proc and candidate_back:
            final_proc = max(candidate_back, key=lambda q: q['_其他排序'])
            match_reason = "按排序最大兜底"
            warnings.append(
                f"料号[{p['料号']}]未匹配到 {category} 类型的标准终点工序,"
                f"已按排序最大的工序【{final_proc['工序名称']}】作为终点"
            )

        if not final_proc:
            # 该料号无后道专属报工,用前道最大报工 × 自己的模数估算
            front_max_raw = max(
                (f['实际报工(原单位)'] for f in processes_front),
                default=0
            )
            if front_max_raw > 0 and p['模数'] > 0:
                final_qty = front_max_raw * p['模数']
                final_name = '(前道估算)'
                warnings.append(
                    f"料号[{p['料号']}]未找到后道专属报工,"
                    f"按前道最大报工 {front_max_raw} × 模数 {p['模数']} = {final_qty} 估算"
                )
            else:
                final_qty = 0
                final_name = '(无数据)'
        else:
            final_qty = final_proc['折算成品(个)']
            final_name = final_proc['工序名称']
            final_proc['是否最后工序'] = True

        per_product_summary.append({
            '料号': p['料号'],
            'SAP号': _to_optional_code(p.get('SAP号', '')),
            '定单号码': p.get('定单号码') or _to_str(gyd_header.get('定单号码')),
            '订单ID': p.get('订单ID', 0),
            '明细ID': p.get('明细ID', 0),
            '品名': p['品名'],
            '规格': p['规格'],
            '订货数量': p['订货数量'],
            '模数': p['模数'],
            '箱规': p.get('箱规', 0),
            '包装方式': p.get('包装方式', ''),
            '单位': p['单位'],
            '品类': category,
            '最后工序名称': final_name,
            '最终成品数': final_qty,
            '识别理由': match_reason,
            '后道工序': back_procs,
            '警告': [w for w in warnings if p['料号'] in w],
        })

    return {
        'process_no': process_no,
        'order_header': {
            '定单号码': _to_str(gyd_header.get('定单号码')),
            '客户名称': _to_str(gyd_header.get('客户名称')),
            '工艺单备注': _to_str(gyd_header.get('工艺单备注')),
            '后道备注': _to_str(gyd_header.get('后道备注')),
            '预定交期': _to_str(gyd_header.get('预定交期')),
            '业务员': _to_str(gyd_header.get('业务员')),
        },
        'products': per_product_summary,
        'shared_front_processes': processes_front,
        'warnings': warnings,
    }


# ============================================================
# 八、对外主入口
# ============================================================
def fetch_production_data(search_keyword, search_type="工艺单号"):
    conn = None
    try:
        conn = get_erp_connection()
        process_no, err = resolve_process_no(conn, search_keyword, search_type)
        if err:
            return {"status": "error", "msg": f"❌ {err}"}

        gyd_header = fetch_gyd_header(conn, process_no)
        if not gyd_header:
            return {"status": "error", "msg": f"❌ 工艺单 {process_no} 无基础信息"}

        gydcp_df = fetch_gydcp_products(conn, process_no)
        gydqt_df = fetch_gydqt_processes(conn, process_no)
        lwqt_df = fetch_lwqt_reports(conn, process_no)
        cpck_summary = fetch_cpck_summary(conn, process_no)

        summary = calculate_production_summary(
            process_no, gyd_header, gydcp_df, gydqt_df, lwqt_df
        )
        summary['cpck_summary'] = cpck_summary

        if search_type == "料号":
            item_keyword = _to_str(search_keyword).lower()
            exact_products = []
            fuzzy_products = []
            for product in summary['products']:
                item_no = _to_str(product.get('料号')).lower()
                if item_no == item_keyword:
                    exact_products.append(product)
                elif item_keyword in item_no:
                    fuzzy_products.append(product)
            matched_products = exact_products or fuzzy_products
            if not matched_products:
                return {
                    "status": "error",
                    "msg": f"❌ 工艺单 {process_no} 中未找到料号 {search_keyword}"
                }
            summary['products'] = matched_products
            summary['warnings'].append(f"料号搜索已只保留匹配料号: {search_keyword}")

        for product in summary['products']:
            product['最近入库时间'] = cpck_summary.get('最近入库时间', '')
            product['最近出货时间'] = cpck_summary.get('最近出货时间', '')
            product['累计入库数量'] = cpck_summary.get('累计入库数量', 0)
            product['累计出货数量'] = cpck_summary.get('累计出货数量', 0)

        product_count = len(summary['products'])
        has_warning = bool(summary['warnings'])
        status = "warning" if has_warning else "success"
        msg = f"✅ 抓取成功 | 工艺单 {process_no} 含 {product_count} 款产品"
        if has_warning:
            msg += f" | ⚠️ {len(summary['warnings'])} 条提示"
        return {"status": status, "msg": msg, "summary": summary}
    except Exception as e:
        import traceback
        return {
            "status": "error",
            "msg": f"❌ 代码异常: {str(e)}\n{traceback.format_exc()}"
        }
    finally:
        if conn:
            conn.close()


# ============================================================
# 九、出库查询(双关联)
# ============================================================
def fetch_shipment_by_process_no(process_no, product_item_no=None):
    conn = None
    try:
        conn = get_erp_connection()
        if product_item_no:
            sql = """
                SELECT h.[出货单号], h.[开单日期], h.[领料部门],
                       h.[提货人], h.[备注],
                       m.[半成品编号], m.[半成品名称], m.[规格],
                       m.[单位], m.[出货数量]
                FROM [bcpchd] h
                INNER JOIN [bcpchdmx] m ON h.[出货单号] = m.[出货单号]
                WHERE h.[工艺单号] = ?
                  AND (m.[半成品名称] LIKE ? OR CAST(m.[半成品编号] AS NVARCHAR(50)) = ?)
                ORDER BY h.[开单日期] DESC
            """
            df = pd.read_sql(sql, conn, params=[
                int(process_no), f"%{product_item_no}%", str(product_item_no),
            ])
        else:
            sql = """
                SELECT h.[出货单号], h.[开单日期], h.[领料部门],
                       h.[提货人], h.[备注],
                       m.[半成品编号], m.[半成品名称], m.[规格],
                       m.[单位], m.[出货数量]
                FROM [bcpchd] h
                LEFT JOIN [bcpchdmx] m ON h.[出货单号] = m.[出货单号]
                WHERE h.[工艺单号] = ?
                ORDER BY h.[开单日期] DESC
            """
            df = pd.read_sql(sql, conn, params=[int(process_no)])
        return df
    except Exception:
        return pd.DataFrame()
    finally:
        if conn:
            conn.close()


# ============================================================
# 十、影子库
# ============================================================
SHADOW_DB_PATH = 'shadow_db.sqlite'


def init_shared_business_tables():
    """初始化共享业务表。所有电脑共用 SQL Server 中的 lyc_ 表。"""
    init_shared_auth_tables()
    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute("""
        IF OBJECT_ID(N'dbo.lyc_pending_labels', N'U') IS NULL
        BEGIN
            CREATE TABLE dbo.lyc_pending_labels (
                id INT IDENTITY(1,1) PRIMARY KEY,
                process_no NVARCHAR(50) NOT NULL,
                order_no NVARCHAR(100) NULL,
                order_id INT NULL,
                detail_id INT NULL,
                item_no NVARCHAR(100) NOT NULL,
                pack_method NVARCHAR(100) NULL,
                product_name NVARCHAR(255) NULL,
                customer NVARCHAR(255) NULL,
                order_qty INT NULL,
                done_qty INT NULL,
                box_spec INT NULL,
                modulus INT NULL,
                category NVARCHAR(50) NULL,
                last_process NVARCHAR(100) NULL,
                stock_in_time NVARCHAR(50) NULL,
                stock_out_time NVARCHAR(50) NULL,
                status NVARCHAR(20) NOT NULL DEFAULT N'Pending',
                created_by_name NVARCHAR(50) NULL,
                created_at DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
                updated_by_name NVARCHAR(50) NULL,
                updated_at DATETIME2 NULL,
                source_synced_at DATETIME2 NULL,
                source_note NVARCHAR(100) NULL
            )
        END
    """)
    cursor.execute("""
        IF OBJECT_ID(N'dbo.lyc_print_batches', N'U') IS NULL
        BEGIN
            CREATE TABLE dbo.lyc_print_batches (
                id INT IDENTITY(1,1) PRIMARY KEY,
                batch_no NVARCHAR(50) NOT NULL UNIQUE,
                process_no NVARCHAR(50) NULL,
                order_no NVARCHAR(100) NULL,
                mode NVARCHAR(20) NOT NULL,
                printer_name NVARCHAR(255) NULL,
                total_label_count INT NOT NULL DEFAULT 0,
                total_qty INT NOT NULL DEFAULT 0,
                shipment_time NVARCHAR(50) NULL,
                printed_by_name NVARCHAR(50) NULL,
                printed_at DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
                action_type NVARCHAR(20) NOT NULL DEFAULT N'new',
                status NVARCHAR(20) NOT NULL DEFAULT N'Printed',
                remark NVARCHAR(500) NULL
            )
        END
    """)
    cursor.execute("""
        IF OBJECT_ID(N'dbo.lyc_print_logs', N'U') IS NULL
        BEGIN
            CREATE TABLE dbo.lyc_print_logs (
                id INT IDENTITY(1,1) PRIMARY KEY,
                batch_id INT NULL,
                process_no NVARCHAR(50) NOT NULL,
                order_no NVARCHAR(100) NULL,
                item_no NVARCHAR(100) NOT NULL,
                product_name NVARCHAR(255) NULL,
                box_index INT NOT NULL,
                total_boxes INT NOT NULL,
                qty_in_box INT NOT NULL,
                std_qty INT NULL,
                box_type NVARCHAR(20) NOT NULL DEFAULT N'full',
                qr_content NVARCHAR(1000) NULL,
                shipment_time NVARCHAR(50) NULL,
                printer_name NVARCHAR(255) NULL,
                operator_name NVARCHAR(50) NULL,
                printed_at DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
                action_type NVARCHAR(20) NOT NULL DEFAULT N'new',
                is_counted BIT NOT NULL DEFAULT 1,
                is_deleted BIT NOT NULL DEFAULT 0,
                deleted_by_name NVARCHAR(50) NULL,
                deleted_at DATETIME2 NULL,
                delete_reason NVARCHAR(500) NULL
            )
        END
    """)
    cursor.execute("""
        IF OBJECT_ID(N'dbo.lyc_print_templates', N'U') IS NULL
        BEGIN
            CREATE TABLE dbo.lyc_print_templates (
                id INT IDENTITY(1,1) PRIMARY KEY,
                template_name NVARCHAR(100) NOT NULL UNIQUE,
                template_type NVARCHAR(50) NOT NULL DEFAULT N'label',
                config_json NVARCHAR(MAX) NOT NULL,
                is_default BIT NOT NULL DEFAULT 0,
                is_active BIT NOT NULL DEFAULT 1,
                version_no INT NOT NULL DEFAULT 1,
                created_by_name NVARCHAR(50) NULL,
                created_at DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
                updated_by_name NVARCHAR(50) NULL,
                updated_at DATETIME2 NULL
            )
        END
    """)
    cursor.execute("""
        IF OBJECT_ID(N'dbo.lyc_scan_in_logs', N'U') IS NULL
        BEGIN
            CREATE TABLE dbo.lyc_scan_in_logs (
                id INT IDENTITY(1,1) PRIMARY KEY,
                print_log_id INT NULL,
                qr_content NVARCHAR(1000) NOT NULL,
                process_no NVARCHAR(50) NULL,
                order_no NVARCHAR(100) NULL,
                item_no NVARCHAR(100) NULL,
                product_name NVARCHAR(255) NULL,
                box_index INT NULL,
                total_boxes INT NULL,
                qty_in_box INT NULL,
                std_qty INT NULL,
                scanned_by_name NVARCHAR(50) NULL,
                scanned_at DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
                source NVARCHAR(100) NULL,
                status NVARCHAR(20) NOT NULL DEFAULT N'In',
                remark NVARCHAR(500) NULL
            )
        END
    """)
    cursor.execute("""
        IF OBJECT_ID(N'dbo.lyc_scan_out_logs', N'U') IS NULL
        BEGIN
            CREATE TABLE dbo.lyc_scan_out_logs (
                id INT IDENTITY(1,1) PRIMARY KEY,
                print_log_id INT NULL,
                scan_in_id INT NULL,
                qr_content NVARCHAR(1000) NOT NULL,
                process_no NVARCHAR(50) NULL,
                order_no NVARCHAR(100) NULL,
                item_no NVARCHAR(100) NULL,
                product_name NVARCHAR(255) NULL,
                box_index INT NULL,
                total_boxes INT NULL,
                qty_in_box INT NULL,
                std_qty INT NULL,
                scanned_by_name NVARCHAR(50) NULL,
                scanned_at DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
                source NVARCHAR(100) NULL,
                status NVARCHAR(20) NOT NULL DEFAULT N'Out',
                remark NVARCHAR(500) NULL
            )
        END
    """)
    for ddl in [
        "IF COL_LENGTH('dbo.lyc_pending_labels','sap_no') IS NULL ALTER TABLE dbo.lyc_pending_labels ADD sap_no NVARCHAR(100) NULL",
        "IF COL_LENGTH('dbo.lyc_pending_labels','order_id') IS NULL ALTER TABLE dbo.lyc_pending_labels ADD order_id INT NULL",
        "IF COL_LENGTH('dbo.lyc_pending_labels','detail_id') IS NULL ALTER TABLE dbo.lyc_pending_labels ADD detail_id INT NULL",
        "IF COL_LENGTH('dbo.lyc_pending_labels','pack_method') IS NULL ALTER TABLE dbo.lyc_pending_labels ADD pack_method NVARCHAR(100) NULL",
        "IF COL_LENGTH('dbo.lyc_pending_labels','created_by_name') IS NULL ALTER TABLE dbo.lyc_pending_labels ADD created_by_name NVARCHAR(50) NULL",
        "IF COL_LENGTH('dbo.lyc_pending_labels','updated_by_name') IS NULL ALTER TABLE dbo.lyc_pending_labels ADD updated_by_name NVARCHAR(50) NULL",
        "IF COL_LENGTH('dbo.lyc_pending_labels','source_synced_at') IS NULL ALTER TABLE dbo.lyc_pending_labels ADD source_synced_at DATETIME2 NULL",
        "IF COL_LENGTH('dbo.lyc_pending_labels','source_note') IS NULL ALTER TABLE dbo.lyc_pending_labels ADD source_note NVARCHAR(100) NULL",
        "IF COL_LENGTH('dbo.lyc_print_logs','order_no') IS NULL ALTER TABLE dbo.lyc_print_logs ADD order_no NVARCHAR(100) NULL",
        "IF COL_LENGTH('dbo.lyc_print_logs','product_name') IS NULL ALTER TABLE dbo.lyc_print_logs ADD product_name NVARCHAR(255) NULL",
        "IF COL_LENGTH('dbo.lyc_print_logs','std_qty') IS NULL ALTER TABLE dbo.lyc_print_logs ADD std_qty INT NULL",
        "IF COL_LENGTH('dbo.lyc_print_logs','shipment_time') IS NULL ALTER TABLE dbo.lyc_print_logs ADD shipment_time NVARCHAR(50) NULL",
        "IF COL_LENGTH('dbo.lyc_print_batches','printed_by_name') IS NULL ALTER TABLE dbo.lyc_print_batches ADD printed_by_name NVARCHAR(50) NULL",
        "IF COL_LENGTH('dbo.lyc_print_logs','operator_name') IS NULL ALTER TABLE dbo.lyc_print_logs ADD operator_name NVARCHAR(50) NULL",
        "IF COL_LENGTH('dbo.lyc_print_logs','deleted_by_name') IS NULL ALTER TABLE dbo.lyc_print_logs ADD deleted_by_name NVARCHAR(50) NULL",
        "IF COL_LENGTH('dbo.lyc_print_templates','created_by_name') IS NULL ALTER TABLE dbo.lyc_print_templates ADD created_by_name NVARCHAR(50) NULL",
        "IF COL_LENGTH('dbo.lyc_print_templates','updated_by_name') IS NULL ALTER TABLE dbo.lyc_print_templates ADD updated_by_name NVARCHAR(50) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_in_logs','print_log_id') IS NULL ALTER TABLE dbo.lyc_scan_in_logs ADD print_log_id INT NULL",
        "IF COL_LENGTH('dbo.lyc_scan_in_logs','qr_content') IS NULL ALTER TABLE dbo.lyc_scan_in_logs ADD qr_content NVARCHAR(1000) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_in_logs','process_no') IS NULL ALTER TABLE dbo.lyc_scan_in_logs ADD process_no NVARCHAR(50) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_in_logs','order_no') IS NULL ALTER TABLE dbo.lyc_scan_in_logs ADD order_no NVARCHAR(100) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_in_logs','item_no') IS NULL ALTER TABLE dbo.lyc_scan_in_logs ADD item_no NVARCHAR(100) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_in_logs','product_name') IS NULL ALTER TABLE dbo.lyc_scan_in_logs ADD product_name NVARCHAR(255) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_in_logs','box_index') IS NULL ALTER TABLE dbo.lyc_scan_in_logs ADD box_index INT NULL",
        "IF COL_LENGTH('dbo.lyc_scan_in_logs','total_boxes') IS NULL ALTER TABLE dbo.lyc_scan_in_logs ADD total_boxes INT NULL",
        "IF COL_LENGTH('dbo.lyc_scan_in_logs','qty_in_box') IS NULL ALTER TABLE dbo.lyc_scan_in_logs ADD qty_in_box INT NULL",
        "IF COL_LENGTH('dbo.lyc_scan_in_logs','std_qty') IS NULL ALTER TABLE dbo.lyc_scan_in_logs ADD std_qty INT NULL",
        "IF COL_LENGTH('dbo.lyc_scan_in_logs','scanned_by_name') IS NULL ALTER TABLE dbo.lyc_scan_in_logs ADD scanned_by_name NVARCHAR(50) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_in_logs','scanned_at') IS NULL ALTER TABLE dbo.lyc_scan_in_logs ADD scanned_at DATETIME2 NULL",
        "IF COL_LENGTH('dbo.lyc_scan_in_logs','source') IS NULL ALTER TABLE dbo.lyc_scan_in_logs ADD source NVARCHAR(100) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_in_logs','status') IS NULL ALTER TABLE dbo.lyc_scan_in_logs ADD status NVARCHAR(20) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_in_logs','remark') IS NULL ALTER TABLE dbo.lyc_scan_in_logs ADD remark NVARCHAR(500) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','print_log_id') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD print_log_id INT NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','scan_in_id') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD scan_in_id INT NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','qr_content') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD qr_content NVARCHAR(1000) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','process_no') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD process_no NVARCHAR(50) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','order_no') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD order_no NVARCHAR(100) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','item_no') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD item_no NVARCHAR(100) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','product_name') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD product_name NVARCHAR(255) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','box_index') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD box_index INT NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','total_boxes') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD total_boxes INT NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','qty_in_box') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD qty_in_box INT NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','std_qty') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD std_qty INT NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','scanned_by_name') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD scanned_by_name NVARCHAR(50) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','scanned_at') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD scanned_at DATETIME2 NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','source') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD source NVARCHAR(100) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','status') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD status NVARCHAR(20) NULL",
        "IF COL_LENGTH('dbo.lyc_scan_out_logs','remark') IS NULL ALTER TABLE dbo.lyc_scan_out_logs ADD remark NVARCHAR(500) NULL",
    ]:
        cursor.execute(ddl)
    conn.commit()
    conn.close()


def ensure_default_print_template():
    from template_engine import list_builtin_templates
    init_shared_business_tables()
    templates = list_builtin_templates()
    conn = get_erp_connection()
    cursor = conn.cursor()
    for idx, template in enumerate(templates):
        cursor.execute(
            "SELECT COUNT(*) FROM dbo.lyc_print_templates WHERE template_name = ?",
            template['name'],
        )
        exists = cursor.fetchone()[0]
        if not exists:
            cursor.execute(
                """INSERT INTO dbo.lyc_print_templates
                   (template_name, template_type, config_json, is_default, is_active, created_by_name)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                template['name'],
                template.get('template_type', 'label'),
                json.dumps(template, ensure_ascii=False),
                1 if idx == 0 else 0,
                'system',
            )
    conn.commit()
    conn.close()


def get_print_templates(active_only=True):
    ensure_default_print_template()
    conn = get_erp_connection()
    sql = """
        SELECT id, template_name, template_type, config_json, is_default,
               is_active, version_no, updated_by_name, updated_at
        FROM dbo.lyc_print_templates
    """
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY is_default DESC, template_name ASC"
    df = pd.read_sql(sql, conn)
    conn.close()
    templates = []
    for _, row in df.iterrows():
        try:
            config = json.loads(row['config_json'])
        except Exception:
            continue
        config['_db_id'] = int(row['id'])
        config['name'] = _to_str(row['template_name']) or config.get('name', '')
        config['_is_default'] = bool(row['is_default'])
        config['_version_no'] = _to_int(row.get('version_no', 1), 1)
        templates.append(config)
    return templates


def save_print_template(template_config, operator='system', set_default=False):
    init_shared_business_tables()
    cfg = copy.deepcopy(template_config)
    name = _to_str(cfg.get('name') or cfg.get('template_name'))
    if not name:
        raise ValueError("模板名称不能为空")
    cfg['name'] = name
    config_json = json.dumps(cfg, ensure_ascii=False)
    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT TOP 1 id, version_no FROM dbo.lyc_print_templates WHERE template_name = ?",
        name,
    )
    row = cursor.fetchone()
    if set_default:
        cursor.execute("UPDATE dbo.lyc_print_templates SET is_default = 0")
    if row:
        template_id, version_no = int(row[0]), _to_int(row[1], 1)
        if set_default:
            cursor.execute(
                """UPDATE dbo.lyc_print_templates
                   SET template_type = ?, config_json = ?, is_default = 1,
                       version_no = ?, updated_by_name = ?, updated_at = SYSDATETIME()
                   WHERE id = ?""",
                cfg.get('template_type', 'label'),
                config_json,
                version_no + 1,
                _to_str(operator),
                template_id,
            )
        else:
            cursor.execute(
                """UPDATE dbo.lyc_print_templates
                   SET template_type = ?, config_json = ?,
                       version_no = ?, updated_by_name = ?, updated_at = SYSDATETIME()
                   WHERE id = ?""",
                cfg.get('template_type', 'label'),
                config_json,
                version_no + 1,
                _to_str(operator),
                template_id,
            )
    else:
        cursor.execute(
            """INSERT INTO dbo.lyc_print_templates
               (template_name, template_type, config_json, is_default, is_active, created_by_name)
               VALUES (?, ?, ?, ?, 1, ?)""",
            name,
            cfg.get('template_type', 'label'),
            config_json,
            1 if set_default else 0,
            _to_str(operator),
        )
    conn.commit()
    conn.close()


def delete_print_template(template_name, operator='system'):
    """Soft-delete a print template so other computers no longer load it."""
    init_shared_business_tables()
    name = _to_str(template_name)
    if not name:
        raise ValueError("模板名称不能为空")
    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT TOP 1 id, is_default FROM dbo.lyc_print_templates WHERE template_name = ? AND is_active = 1",
        name,
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return 0
    template_id, is_default = int(row[0]), bool(row[1])
    cursor.execute(
        """UPDATE dbo.lyc_print_templates
           SET is_active = 0, is_default = 0, updated_by_name = ?, updated_at = SYSDATETIME()
           WHERE id = ?""",
        _to_str(operator),
        template_id,
    )
    affected = cursor.rowcount
    if is_default:
        cursor.execute(
            """UPDATE dbo.lyc_print_templates
               SET is_default = 1, updated_by_name = ?, updated_at = SYSDATETIME()
               WHERE id = (
                   SELECT TOP 1 id FROM dbo.lyc_print_templates
                   WHERE is_active = 1
                   ORDER BY template_name ASC
               )""",
            _to_str(operator),
        )
    conn.commit()
    conn.close()
    return affected


def init_shadow_db():
    conn = sqlite3.connect(SHADOW_DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS pending_labels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        process_no TEXT,
        order_no TEXT,
        item_no TEXT,
        pack_method TEXT,
        product_name TEXT,
        order_qty INTEGER,
        done_qty INTEGER,
        last_process TEXT,
        customer TEXT,
        category TEXT,
        modulus INTEGER,
        box_spec INTEGER DEFAULT 0,
        stock_in_time TEXT,
        stock_out_time TEXT,
        status TEXT DEFAULT 'Pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id INTEGER,
        action TEXT,
        operator TEXT,
        ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS print_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        process_no TEXT,
        item_no TEXT,
        box_index INTEGER,
        total_boxes INTEGER,
        qty_in_box INTEGER,
        printer_name TEXT,
        operator TEXT,
        ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        qr_content TEXT,
        action_type TEXT DEFAULT 'new',
        box_type TEXT DEFAULT 'full'
    )''')
    # 老库升级:如果列不存在则补加
    try:
        cursor.execute("ALTER TABLE print_logs ADD COLUMN action_type TEXT DEFAULT 'new'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE print_logs ADD COLUMN box_type TEXT DEFAULT 'full'")
    except sqlite3.OperationalError:
        pass
    for ddl in [
        "ALTER TABLE pending_labels ADD COLUMN box_spec INTEGER DEFAULT 0",
        "ALTER TABLE pending_labels ADD COLUMN sap_no TEXT",
        "ALTER TABLE pending_labels ADD COLUMN pack_method TEXT",
        "ALTER TABLE pending_labels ADD COLUMN stock_in_time TEXT",
        "ALTER TABLE pending_labels ADD COLUMN stock_out_time TEXT",
    ]:
        try:
            cursor.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def insert_to_shadow_db(product, order_header, process_no):
    """按料号粒度入库,一张工艺单多款产品 = 多条记录。"""
    init_shared_business_tables()
    conn = get_erp_connection()
    cursor = conn.cursor()

    process_no_s = _to_str(process_no)
    order_no_s = _to_str(product.get('定单号码') or order_header.get('定单号码', ''))
    item_no_s = _to_str(product.get('料号', ''))
    order_id = _to_int(product.get('订单ID', 0), 0)
    detail_id = _to_int(product.get('明细ID', 0), 0)

    existing = None
    if order_id or detail_id:
        cursor.execute('''
            SELECT TOP 1 id FROM dbo.lyc_pending_labels
            WHERE process_no = ? AND item_no = ? AND status = N'Pending'
              AND ISNULL(order_id, 0) = ? AND ISNULL(detail_id, 0) = ?
            ORDER BY updated_at DESC, created_at DESC, id DESC
        ''', (process_no_s, item_no_s, order_id, detail_id))
        existing = cursor.fetchone()

    if existing is None and order_no_s:
        cursor.execute('''
            SELECT TOP 1 id FROM dbo.lyc_pending_labels
            WHERE process_no = ? AND item_no = ? AND order_no = ? AND status = N'Pending'
            ORDER BY updated_at DESC, created_at DESC, id DESC
        ''', (process_no_s, item_no_s, order_no_s))
        existing = cursor.fetchone()

    if existing is None:
        cursor.execute('''
            SELECT TOP 1 id FROM dbo.lyc_pending_labels
            WHERE process_no = ? AND item_no = ? AND status = N'Pending'
              AND ISNULL(order_id, 0) = 0 AND ISNULL(detail_id, 0) = 0
            ORDER BY updated_at DESC, created_at DESC, id DESC
        ''', (process_no_s, item_no_s))
        existing = cursor.fetchone()

    values = (
        process_no_s,
        order_no_s,
        order_id,
        detail_id,
        item_no_s,
        _to_str(product.get('包装方式', '')),
        _to_optional_code(product.get('SAP号', '')),
        _to_str(product.get('品名', '')),
        _to_int(product.get('订货数量', 0)),
        _to_int(product.get('最终成品数', 0)),
        _to_str(product.get('最后工序名称', '')),
        _to_str(order_header.get('客户名称', '')),
        _to_str(product.get('品类', '')),
        _to_int(product.get('模数', 0)),
        _to_int(product.get('箱规', 0)),
        _to_str(product.get('最近入库时间', '')),
        _to_str(product.get('最近出货时间', '')),
        'ERP.gydcp',
    )
    current_id = None
    if existing:
        current_id = int(existing[0])
        cursor.execute('''UPDATE dbo.lyc_pending_labels
            SET process_no = ?, order_no = ?, order_id = ?, detail_id = ?,
                item_no = ?, pack_method = ?, sap_no = ?, product_name = ?,
                order_qty = ?, done_qty = ?, last_process = ?, customer = ?,
                category = ?, modulus = ?, box_spec = ?, stock_in_time = ?,
                stock_out_time = ?, source_note = ?, source_synced_at = SYSDATETIME(),
                updated_at = SYSDATETIME()
            WHERE id = ?''', values + (current_id,))
    else:
        cursor.execute('''INSERT INTO dbo.lyc_pending_labels
        (process_no, order_no, order_id, detail_id, item_no, pack_method, sap_no, product_name, order_qty, done_qty,
         last_process, customer, category, modulus, box_spec, stock_in_time,
         stock_out_time, source_note, source_synced_at)
        OUTPUT INSERTED.id
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, SYSDATETIME())''', values)
        inserted = cursor.fetchone()
        if inserted and inserted[0] is not None:
            current_id = int(inserted[0])

    if current_id and (order_id or detail_id):
        cursor.execute('''
            DELETE FROM dbo.lyc_pending_labels
            WHERE process_no = ? AND item_no = ? AND status = N'Pending' AND id <> ?
              AND ISNULL(order_id, 0) = ? AND ISNULL(detail_id, 0) = ?
        ''', (process_no_s, item_no_s, current_id, order_id, detail_id))
        cursor.execute('''
            DELETE FROM dbo.lyc_pending_labels
            WHERE process_no = ? AND item_no = ? AND status = N'Pending' AND id <> ?
              AND ISNULL(order_id, 0) = 0 AND ISNULL(detail_id, 0) = 0
        ''', (process_no_s, item_no_s, current_id))
    conn.commit()
    conn.close()


def get_pending_records():
    init_shared_business_tables()
    conn = get_erp_connection()
    df = pd.read_sql(
        "SELECT * FROM dbo.lyc_pending_labels WHERE status = N'Pending' "
        "ORDER BY source_synced_at DESC, updated_at DESC, created_at DESC",
        conn
    )
    conn.close()
    return df


def confirm_shadow_record(record_id, operator='system'):
    init_shared_business_tables()
    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE dbo.lyc_pending_labels SET status = N'Confirmed', updated_by_name = ?, updated_at = SYSDATETIME() WHERE id = ?",
        (_to_str(operator), int(record_id))
    )
    cursor.execute(
        "INSERT INTO dbo.lyc_audit_logs (action, target_type, target_id, detail) VALUES (?, ?, ?, ?)",
        ('confirm_pending', 'pending_label', int(record_id), json.dumps({'operator': operator}, ensure_ascii=False))
    )
    conn.commit()
    conn.close()


def confirm_shadow_records_batch(record_ids, operator='system'):
    if not record_ids:
        return 0
    init_shared_business_tables()
    conn = get_erp_connection()
    cursor = conn.cursor()
    ids = [int(i) for i in record_ids]
    placeholders = ','.join(['?'] * len(ids))
    cursor.execute(
        f"UPDATE dbo.lyc_pending_labels SET status = N'Confirmed', updated_by_name = ?, updated_at = SYSDATETIME() WHERE id IN ({placeholders})",
        [_to_str(operator)] + ids,
    )
    affected = cursor.rowcount
    for record_id in ids:
        cursor.execute(
            "INSERT INTO dbo.lyc_audit_logs (action, target_type, target_id, detail) VALUES (?, ?, ?, ?)",
            ('confirm_pending', 'pending_label', int(record_id), json.dumps({'operator': operator}, ensure_ascii=False))
        )
    conn.commit()
    conn.close()
    return affected


def delete_shadow_record(record_id, operator='system'):
    init_shared_business_tables()
    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO dbo.lyc_audit_logs (action, target_type, target_id, detail) VALUES (?, ?, ?, ?)",
        ('delete_pending', 'pending_label', int(record_id), json.dumps({'operator': operator}, ensure_ascii=False))
    )
    cursor.execute("DELETE FROM dbo.lyc_pending_labels WHERE id = ?", int(record_id))
    conn.commit()
    conn.close()


def delete_shadow_records_batch(record_ids, operator='system'):
    if not record_ids:
        return 0
    init_shared_business_tables()
    conn = get_erp_connection()
    cursor = conn.cursor()
    ids = [int(i) for i in record_ids]
    placeholders = ','.join(['?'] * len(ids))
    for record_id in ids:
        cursor.execute(
            "INSERT INTO dbo.lyc_audit_logs (action, target_type, target_id, detail) VALUES (?, ?, ?, ?)",
            ('delete_pending', 'pending_label', int(record_id), json.dumps({'operator': operator}, ensure_ascii=False))
        )
    cursor.execute(f"DELETE FROM dbo.lyc_pending_labels WHERE id IN ({placeholders})", ids)
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected


def _legacy_confirm_shadow_record(record_id, operator='system'):
    conn = sqlite3.connect(SHADOW_DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE pending_labels SET status = 'Confirmed' WHERE id = ?",
        (record_id,)
    )
    cursor.execute(
        "INSERT INTO audit_log (record_id, action, operator) VALUES (?, ?, ?)",
        (record_id, 'confirm', operator)
    )
    conn.commit()
    conn.close()


def _legacy_confirm_shadow_records_batch(record_ids, operator='system'):
    if not record_ids:
        return 0
    conn = sqlite3.connect(SHADOW_DB_PATH)
    cursor = conn.cursor()
    ids = [int(i) for i in record_ids]
    placeholders = ','.join(['?'] * len(ids))
    cursor.execute(
        f"UPDATE pending_labels SET status = 'Confirmed' WHERE id IN ({placeholders})",
        ids,
    )
    affected = cursor.rowcount
    cursor.executemany(
        "INSERT INTO audit_log (record_id, action, operator) VALUES (?, ?, ?)",
        [(i, 'confirm', operator) for i in ids],
    )
    conn.commit()
    conn.close()
    return affected


def _legacy_delete_shadow_record(record_id, operator='system'):
    conn = sqlite3.connect(SHADOW_DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO audit_log (record_id, action, operator) VALUES (?, ?, ?)",
        (record_id, 'delete', operator)
    )
    cursor.execute("DELETE FROM pending_labels WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()


def _legacy_delete_shadow_records_batch(record_ids, operator='system'):
    if not record_ids:
        return 0
    conn = sqlite3.connect(SHADOW_DB_PATH)
    cursor = conn.cursor()
    ids = [int(i) for i in record_ids]
    placeholders = ','.join(['?'] * len(ids))
    cursor.executemany(
        "INSERT INTO audit_log (record_id, action, operator) VALUES (?, ?, ?)",
        [(i, 'delete', operator) for i in ids],
    )
    cursor.execute(f"DELETE FROM pending_labels WHERE id IN ({placeholders})", ids)
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected


def should_count_print_log(action_type='new', box_type='full'):
    """Return 1 only for new production box labels that should occupy cumulative boxes."""
    return 1 if (_to_str(action_type) == 'new' and _to_str(box_type) in ('full', 'tail', 'pack')) else 0


def log_print_action(process_no, item_no, box_index, total_boxes,
                     qty_in_box, printer_name, qr_content,
                     operator='system', action_type='new', box_type='full',
                     order_no='', product_name='', std_qty=None,
                     shipment_time=''):
    """记录一次打印行为,用于追溯。

    action_type:
        'new'      首次打印,占用一个新箱号(参与累计)
        'reprint'  重打,不占用新箱号(不参与累计)
    box_type:
        'full'  满箱
        'tail'  尾箱(整包但不满箱)
        'pack'  尾包(不足整包)
        'tray'  拖贴/板贴(不参与累计)
    """
    init_shared_business_tables()
    conn = get_erp_connection()
    cursor = conn.cursor()
    is_counted = should_count_print_log(action_type, box_type)
    cursor.execute('''INSERT INTO dbo.lyc_print_logs
        (process_no, order_no, item_no, product_name, box_index, total_boxes,
         qty_in_box, std_qty, printer_name, operator_name, qr_content,
         shipment_time, action_type, box_type, is_counted)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            _to_str(process_no),
            _to_str(order_no),
            _to_str(item_no),
            _to_str(product_name),
            _to_int(box_index),
            _to_int(total_boxes),
            _to_int(qty_in_box),
            _to_int(std_qty) if std_qty is not None else None,
            _to_str(printer_name),
            _to_str(operator),
            _to_str(qr_content),
            _to_str(shipment_time),
            _to_str(action_type),
            _to_str(box_type),
            int(is_counted),
        )
    )
    conn.commit()
    conn.close()


def get_print_history(process_no, item_no):
    """
    查询某 工艺单+料号 组合的打印历史(用于装箱参数区展示)。

    返回:
    {
        'printed_box_count':  已打印的不同箱号数(去重,不含重打),
        'printed_qty_total':  已打印的累计件数(不含重打),
        'max_box_index':      已打印的最大箱号(用于推算下一个起始箱号),
        'printed_box_list':   已打印的箱号列表(去重排序),
        'reprint_count':      重打次数,
    }
    """
    init_shared_business_tables()
    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT box_index, qty_in_box, action_type, box_type, is_counted
        FROM dbo.lyc_print_logs
        WHERE process_no = ? AND item_no = ? AND is_deleted = 0
    ''', (_to_str(process_no), _to_str(item_no)))
    rows = cursor.fetchall()
    conn.close()

    new_boxes = {}  # box_index -> qty,只存 action_type='new' 的
    reprint_count = 0
    excluded_tail_count = 0
    excluded_tail_qty = 0
    for box_idx, qty, action_type, box_type, is_counted in rows:
        if action_type == 'reprint':
            reprint_count += 1
            continue
        if not is_counted:
            excluded_tail_count += 1
            excluded_tail_qty += int(qty or 0)
            continue
        # 同一个箱号被多次记录(理论上不该,但兜底取最后一次)
        new_boxes[int(box_idx)] = int(qty or 0)

    printed_box_list = sorted(new_boxes.keys())
    return {
        'printed_box_count': len(new_boxes),
        'printed_qty_total': sum(new_boxes.values()),
        'max_box_index':     max(new_boxes.keys()) if new_boxes else 0,
        'printed_box_list':  printed_box_list,
        'reprint_count':     reprint_count,
        'excluded_tail_count': excluded_tail_count,
        'excluded_tail_qty': excluded_tail_qty,
    }


def calculate_box_distribution(total_qty, std_qty, pack_info=None):
    """
    根据本次打包总数和装箱参数,计算箱型分布。
    返回 list,每个元素是一个 dict:
        {'box_type': 'full'|'tail'|'pack', 'qty': N, 'pack_count': M(选填)}

    规则(参考你定义的尾数三级处理):
        - 不启用一箱多包:满箱 + 尾箱
        - 启用一箱多包:
            * 满箱:每箱 std_qty (= 每包 × 每箱包数)
            * 尾箱:不满整箱但是整包数(几个完整包)
            * 尾包:剩余的零头散件(不到一包)
    """
    boxes = []
    total = int(total_qty)
    std = int(std_qty)

    if total <= 0 or std <= 0:
        return boxes

    if not pack_info:
        # 简单模式:满箱 + 尾箱
        full_count = total // std
        tail_qty = total % std
        for _ in range(full_count):
            boxes.append({'box_type': 'full', 'qty': std})
        if tail_qty > 0:
            boxes.append({'box_type': 'tail', 'qty': tail_qty})
        return boxes

    # 一箱多包模式
    per_pack = int(pack_info.get('per_pack', 0))
    packs_per_box = int(pack_info.get('packs_per_box', 0))
    if per_pack <= 0 or packs_per_box <= 0:
        return boxes
    box_qty = per_pack * packs_per_box  # = std

    full_packs = total // per_pack          # 整包数
    leftover = total % per_pack             # 零头散件(不到一包)

    full_box_count = full_packs // packs_per_box      # 满箱数
    remaining_packs = full_packs % packs_per_box      # 不够满箱的整包数

    # 满箱
    for _ in range(full_box_count):
        boxes.append({
            'box_type': 'full',
            'qty': box_qty,
            'pack_count': packs_per_box,
        })
    # 尾箱(整包但不满箱)
    if remaining_packs > 0:
        boxes.append({
            'box_type': 'tail',
            'qty': remaining_packs * per_pack,
            'pack_count': remaining_packs,
        })
    # 尾包(不足整包)
    if leftover > 0:
        boxes.append({
            'box_type': 'pack',
            'qty': leftover,
            'pack_count': 1,  # 当一包贴一张
        })
    return boxes


# ============================================================
# 十一、打印记录管理(v3.1 新增)
# ============================================================
def get_print_log_details(process_no, item_no):
    """
    返回某 工艺单+料号 的所有打印记录(用于UI展示和删除)。
    返回 DataFrame:id, box_index, qty_in_box, box_type, action_type, ts, printer_name
    """
    init_shared_business_tables()
    conn = get_erp_connection()
    df = pd.read_sql('''
        SELECT id, box_index, qty_in_box, box_type, action_type,
               printed_at AS ts, printer_name, operator_name, qr_content
        FROM dbo.lyc_print_logs
        WHERE process_no = ? AND item_no = ? AND is_deleted = 0
        ORDER BY box_index ASC, printed_at ASC
    ''', conn, params=[_to_str(process_no), _to_str(item_no)])
    conn.close()
    return df


def fetch_system_print_history(process_no=None, item_no=None, limit=200, include_deleted=False):
    """查询本系统打印历史(lyc_print_logs),不同于 ERP 出货记录。"""
    init_shared_business_tables()
    conn = get_erp_connection()
    where = []
    params = []
    if process_no:
        where.append("process_no = ?")
        params.append(_to_str(process_no))
    if item_no:
        where.append("item_no = ?")
        params.append(_to_str(item_no))
    if not include_deleted:
        where.append("is_deleted = 0")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT TOP {int(limit)}
               id, process_no, order_no, item_no, product_name,
               box_index, total_boxes, qty_in_box, std_qty, box_type,
               action_type, is_counted, is_deleted,
               printer_name, operator_name, printed_at, qr_content
        FROM dbo.lyc_print_logs
        {where_sql}
        ORDER BY printed_at DESC, id DESC
    """
    df = pd.read_sql(sql, conn, params=params)
    conn.close()
    return df


def fetch_trace_print_logs(keyword=None, limit=1000, include_deleted=False):
    """读取本系统写入 SQL Server 的打印流水,用于溯源查询第一阶段。"""
    init_shared_business_tables()
    conn = get_erp_connection()
    where = []
    params = []
    kw = _to_str(keyword).strip()
    if kw:
        like_kw = f"%{kw}%"
        where.append(
            """(
                process_no LIKE ? OR order_no LIKE ? OR item_no LIKE ?
                OR product_name LIKE ? OR qr_content LIKE ?
                OR operator_name LIKE ? OR printer_name LIKE ?
            )"""
        )
        params.extend([like_kw] * 7)
    if not include_deleted:
        where.append("is_deleted = 0")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT TOP {int(limit)}
               id, process_no, order_no, item_no, product_name,
               box_index, total_boxes, qty_in_box, std_qty, box_type,
               action_type, is_counted, is_deleted,
               printer_name, operator_name, printed_at, qr_content,
               shipment_time, deleted_by_name, deleted_at, delete_reason
        FROM dbo.lyc_print_logs
        {where_sql}
        ORDER BY printed_at DESC, id DESC
    """
    df = pd.read_sql(sql, conn, params=params)
    conn.close()
    return df


def fetch_trace_records(keyword=None, limit=1000, include_deleted=False):
    """Combine print, scan-in, and scan-out logs into one trace view."""
    init_shared_business_tables()
    conn = get_erp_connection()
    where = []
    params = []
    kw = _to_str(keyword).strip()
    if kw:
        like_kw = f"%{kw}%"
        where.append(
            """(
                p.process_no LIKE ? OR p.order_no LIKE ? OR p.item_no LIKE ?
                OR p.product_name LIKE ? OR p.qr_content LIKE ?
                OR p.operator_name LIKE ? OR p.printer_name LIKE ?
                OR si.scanned_by_name LIKE ? OR so.scanned_by_name LIKE ?
                OR si.remark LIKE ? OR so.remark LIKE ?
            )"""
        )
        params.extend([like_kw] * 11)
    if not include_deleted:
        where.append("p.is_deleted = 0")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT TOP {int(limit)}
               p.id AS print_log_id,
               p.process_no, p.order_no, p.item_no, p.product_name,
               p.box_index, p.total_boxes, p.qty_in_box, p.std_qty,
               p.box_type, p.action_type, p.is_counted, p.is_deleted,
               p.printer_name, p.operator_name AS printed_by_name,
               p.printed_at, p.shipment_time, p.qr_content,
               si.id AS scan_in_id,
               si.scanned_by_name AS scan_in_by_name,
               si.scanned_at AS scan_in_at,
               si.remark AS scan_in_remark,
               so.id AS scan_out_id,
               so.scanned_by_name AS scan_out_by_name,
               so.scanned_at AS scan_out_at,
               so.remark AS scan_out_remark,
               CASE
                   WHEN p.is_deleted = 1 THEN N'打印已删除'
                   WHEN so.id IS NOT NULL THEN N'已出库'
                   WHEN si.id IS NOT NULL THEN N'已入库未出库'
                   ELSE N'已打印未入库'
               END AS trace_status
        FROM dbo.lyc_print_logs p
        OUTER APPLY (
            SELECT TOP 1 id, scanned_by_name, scanned_at, remark
            FROM dbo.lyc_scan_in_logs
            WHERE qr_content = p.qr_content AND status = N'In'
            ORDER BY scanned_at DESC, id DESC
        ) si
        OUTER APPLY (
            SELECT TOP 1 id, scanned_by_name, scanned_at, remark
            FROM dbo.lyc_scan_out_logs
            WHERE qr_content = p.qr_content AND status = N'Out'
            ORDER BY scanned_at DESC, id DESC
        ) so
        {where_sql}
        ORDER BY p.printed_at DESC, p.id DESC
    """
    df = pd.read_sql(sql, conn, params=params)
    conn.close()
    return df


def parse_qr_content(qr_content):
    """Parse the current box-label QR payload without changing the QR format."""
    qr = _to_str(qr_content)
    parts = qr.split('#') if qr else []
    parsed = {
        'raw': qr,
        'item_no': parts[0] if len(parts) > 0 else '',
        'print_date': parts[1] if len(parts) > 1 else '',
        'box_index': _to_int(parts[2], None) if len(parts) > 2 else None,
        'qty_in_box': _to_int(parts[3], None) if len(parts) > 3 else None,
        'print_time': parts[4] if len(parts) > 4 else '',
        'shipment_time': '',
        'is_valid': bool(qr and len(parts) >= 4),
        'parts': parts,
    }
    if len(parts) > 5:
        parsed['shipment_time'] = _to_str(parts[5]).removeprefix('SHIP')
    return parsed


def find_print_log_by_qr(qr_content):
    """Find the system print log for a scanned box-label QR payload."""
    qr = _to_str(qr_content)
    if not qr:
        return None
    init_shared_business_tables()
    conn = get_erp_connection()
    sql = """
        SELECT TOP 1
               id, process_no, order_no, item_no, product_name,
               box_index, total_boxes, qty_in_box, std_qty, box_type,
               action_type, is_counted, is_deleted,
               printer_name, operator_name, printed_at, qr_content,
               shipment_time
        FROM dbo.lyc_print_logs
        WHERE qr_content = ? AND is_deleted = 0
        ORDER BY is_counted DESC, printed_at DESC, id DESC
    """
    df = pd.read_sql(sql, conn, params=[qr])
    conn.close()
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def record_scan_in(qr_content, operator='system', source='manual', remark=''):
    """Record one stock-in scan. Duplicate active scans are rejected."""
    qr = _to_str(qr_content)
    parsed = parse_qr_content(qr)
    if not qr or not parsed.get('is_valid'):
        return {
            'ok': False,
            'code': 'invalid_qr',
            'message': '二维码内容为空或格式不正确',
            'parsed': parsed,
        }

    print_log = find_print_log_by_qr(qr)
    if not print_log:
        return {
            'ok': False,
            'code': 'print_not_found',
            'message': '没有找到对应的系统打印记录，暂不允许入库',
            'parsed': parsed,
        }

    init_shared_business_tables()
    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT TOP 1 id, scanned_by_name, scanned_at
        FROM dbo.lyc_scan_in_logs
        WHERE status = N'In' AND (print_log_id = ? OR qr_content = ?)
        ORDER BY scanned_at DESC, id DESC
        """,
        (_to_int(print_log.get('id')), qr),
    )
    duplicate = cursor.fetchone()
    if duplicate:
        conn.close()
        return {
            'ok': False,
            'code': 'duplicate_scan_in',
            'message': '该箱已入库，不能重复扫码入库',
            'existing_id': int(duplicate[0]),
            'existing_operator': _to_str(duplicate[1]),
            'existing_scanned_at': _to_str(duplicate[2]),
            'print_log': print_log,
            'parsed': parsed,
        }

    cursor.execute(
        """
        INSERT INTO dbo.lyc_scan_in_logs
            (print_log_id, qr_content, process_no, order_no, item_no,
             product_name, box_index, total_boxes, qty_in_box, std_qty,
             scanned_by_name, source, status, remark)
        OUTPUT INSERTED.id
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, N'In', ?)
        """,
        (
            _to_int(print_log.get('id')),
            qr,
            _to_str(print_log.get('process_no')),
            _to_str(print_log.get('order_no')),
            _to_str(print_log.get('item_no')),
            _to_str(print_log.get('product_name')),
            _to_int(print_log.get('box_index'), None),
            _to_int(print_log.get('total_boxes'), None),
            _to_int(print_log.get('qty_in_box'), None),
            _to_int(print_log.get('std_qty'), None),
            _to_str(operator),
            _to_str(source),
            _to_str(remark),
        ),
    )
    scan_id = int(cursor.fetchone()[0])
    conn.commit()
    conn.close()
    return {
        'ok': True,
        'code': 'scan_in_recorded',
        'message': '入库记录已写入',
        'scan_id': scan_id,
        'print_log': print_log,
        'parsed': parsed,
    }


def fetch_scan_in_logs(keyword=None, limit=500, include_void=False):
    """Read stock-in scan logs for the future scan-in page and trace query."""
    init_shared_business_tables()
    conn = get_erp_connection()
    where = []
    params = []
    kw = _to_str(keyword).strip()
    if kw:
        like_kw = f"%{kw}%"
        where.append(
            """(
                process_no LIKE ? OR order_no LIKE ? OR item_no LIKE ?
                OR product_name LIKE ? OR qr_content LIKE ?
                OR scanned_by_name LIKE ? OR source LIKE ?
            )"""
        )
        params.extend([like_kw] * 7)
    if not include_void:
        where.append("(status IS NULL OR status <> N'Void')")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT TOP {int(limit)}
               id, print_log_id, process_no, order_no, item_no, product_name,
               box_index, total_boxes, qty_in_box, std_qty, scanned_by_name,
               scanned_at, source, status, remark, qr_content
        FROM dbo.lyc_scan_in_logs
        {where_sql}
        ORDER BY scanned_at DESC, id DESC
    """
    df = pd.read_sql(sql, conn, params=params)
    conn.close()
    return df


def find_scan_in_by_qr(qr_content):
    """Find the active stock-in record for a scanned QR payload."""
    qr = _to_str(qr_content)
    if not qr:
        return None
    init_shared_business_tables()
    conn = get_erp_connection()
    sql = """
        SELECT TOP 1
               id, print_log_id, process_no, order_no, item_no, product_name,
               box_index, total_boxes, qty_in_box, std_qty, scanned_by_name,
               scanned_at, source, status, remark, qr_content
        FROM dbo.lyc_scan_in_logs
        WHERE qr_content = ? AND status = N'In'
        ORDER BY scanned_at DESC, id DESC
    """
    df = pd.read_sql(sql, conn, params=[qr])
    conn.close()
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def record_scan_out(qr_content, operator='system', source='manual', remark='',
                    allow_without_scan_in=True):
    """Record one stock-out scan. Duplicate active out scans are rejected."""
    qr = _to_str(qr_content)
    parsed = parse_qr_content(qr)
    if not qr or not parsed.get('is_valid'):
        return {
            'ok': False,
            'code': 'invalid_qr',
            'message': '二维码内容为空或格式不正确',
            'parsed': parsed,
        }

    print_log = find_print_log_by_qr(qr)
    if not print_log:
        return {
            'ok': False,
            'code': 'print_not_found',
            'message': '没有找到对应的系统打印记录，暂不允许出库',
            'parsed': parsed,
        }

    scan_in = find_scan_in_by_qr(qr)
    if not scan_in and not allow_without_scan_in:
        return {
            'ok': False,
            'code': 'scan_in_required',
            'message': '该箱还没有入库记录，暂不允许出库',
            'print_log': print_log,
            'parsed': parsed,
        }

    init_shared_business_tables()
    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT TOP 1 id, scanned_by_name, scanned_at
        FROM dbo.lyc_scan_out_logs
        WHERE status = N'Out' AND (print_log_id = ? OR qr_content = ?)
        ORDER BY scanned_at DESC, id DESC
        """,
        (_to_int(print_log.get('id')), qr),
    )
    duplicate = cursor.fetchone()
    if duplicate:
        conn.close()
        return {
            'ok': False,
            'code': 'duplicate_scan_out',
            'message': '该箱已出库，不能重复扫码出库',
            'existing_id': int(duplicate[0]),
            'existing_operator': _to_str(duplicate[1]),
            'existing_scanned_at': _to_str(duplicate[2]),
            'print_log': print_log,
            'scan_in': scan_in,
            'parsed': parsed,
        }

    cursor.execute(
        """
        INSERT INTO dbo.lyc_scan_out_logs
            (print_log_id, scan_in_id, qr_content, process_no, order_no, item_no,
             product_name, box_index, total_boxes, qty_in_box, std_qty,
             scanned_by_name, source, status, remark)
        OUTPUT INSERTED.id
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, N'Out', ?)
        """,
        (
            _to_int(print_log.get('id')),
            _to_int(scan_in.get('id'), None) if scan_in else None,
            qr,
            _to_str(print_log.get('process_no')),
            _to_str(print_log.get('order_no')),
            _to_str(print_log.get('item_no')),
            _to_str(print_log.get('product_name')),
            _to_int(print_log.get('box_index'), None),
            _to_int(print_log.get('total_boxes'), None),
            _to_int(print_log.get('qty_in_box'), None),
            _to_int(print_log.get('std_qty'), None),
            _to_str(operator),
            _to_str(source),
            _to_str(remark),
        ),
    )
    scan_id = int(cursor.fetchone()[0])
    conn.commit()
    conn.close()
    return {
        'ok': True,
        'code': 'scan_out_recorded',
        'message': '出库记录已写入' if scan_in else '出库记录已写入；注意：未找到该箱入库记录',
        'warning': None if scan_in else 'missing_scan_in',
        'scan_id': scan_id,
        'print_log': print_log,
        'scan_in': scan_in,
        'parsed': parsed,
    }


def fetch_scan_out_logs(keyword=None, limit=500, include_void=False):
    """Read stock-out scan logs for the scan-out page and trace query."""
    init_shared_business_tables()
    conn = get_erp_connection()
    where = []
    params = []
    kw = _to_str(keyword).strip()
    if kw:
        like_kw = f"%{kw}%"
        where.append(
            """(
                process_no LIKE ? OR order_no LIKE ? OR item_no LIKE ?
                OR product_name LIKE ? OR qr_content LIKE ?
                OR scanned_by_name LIKE ? OR source LIKE ?
            )"""
        )
        params.extend([like_kw] * 7)
    if not include_void:
        where.append("(status IS NULL OR status <> N'Void')")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT TOP {int(limit)}
               id, print_log_id, scan_in_id, process_no, order_no, item_no,
               product_name, box_index, total_boxes, qty_in_box, std_qty,
               scanned_by_name, scanned_at, source, status, remark, qr_content
        FROM dbo.lyc_scan_out_logs
        {where_sql}
        ORDER BY scanned_at DESC, id DESC
    """
    df = pd.read_sql(sql, conn, params=params)
    conn.close()
    return df


def delete_print_logs_by_ids(log_ids, operator='system'):
    """按 id 列表作废打印记录(软删除)。返回处理条数。"""
    if not log_ids:
        return 0
    init_shared_business_tables()
    conn = get_erp_connection()
    cursor = conn.cursor()
    placeholders = ','.join(['?'] * len(log_ids))
    cursor.execute(
        f"UPDATE dbo.lyc_print_logs SET is_deleted = 1, deleted_by_name = ?, deleted_at = SYSDATETIME() WHERE id IN ({placeholders})",
        [_to_str(operator)] + [int(i) for i in log_ids]
    )
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected


def delete_all_print_logs(process_no, item_no, operator='system'):
    """作废某 工艺单+料号 的所有打印记录(软删除)。返回处理条数。"""
    init_shared_business_tables()
    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE dbo.lyc_print_logs SET is_deleted = 1, deleted_by_name = ?, deleted_at = SYSDATETIME() WHERE process_no = ? AND item_no = ? AND is_deleted = 0",
        (_to_str(operator), _to_str(process_no), _to_str(item_no))
    )
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected
