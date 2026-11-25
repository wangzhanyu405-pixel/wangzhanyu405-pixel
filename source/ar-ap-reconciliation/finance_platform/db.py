from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from finance_platform.config import Settings, get_settings
from finance_platform.schema import SCHEMA_STATEMENTS

try:
    import pyodbc
except ImportError:  # pragma: no cover - depends on server runtime
    pyodbc = None


class DatabaseUnavailable(RuntimeError):
    """Raised when SQL Server access cannot be established."""


def build_connection_string(settings: Settings) -> str:
    return (
        f"DRIVER={{{settings.db_driver}}};"
        f"SERVER={settings.server_with_port};"
        f"DATABASE={settings.db_name};"
        f"UID={settings.db_user};"
        f"PWD={settings.db_password};"
        f"Connection Timeout={settings.db_timeout};"
        f"Login Timeout={settings.db_timeout};"
        "TrustServerCertificate=yes;"
    )


def get_connection(settings: Settings | None = None):
    settings = settings or get_settings()
    if pyodbc is None:
        raise DatabaseUnavailable("pyodbc 未安装，请先执行 pip install -r requirements.txt")
    if not settings.has_database_credentials:
        raise DatabaseUnavailable("数据库账号或密码未配置，请设置 LYC_DB_USER 和 LYC_DB_PASSWORD")

    conn = pyodbc.connect(build_connection_string(settings), timeout=settings.db_timeout)
    conn.timeout = settings.db_timeout
    return conn


@contextmanager
def connection_scope(settings: Settings | None = None) -> Iterator:
    conn = get_connection(settings)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def test_connection(settings: Settings | None = None) -> tuple[bool, str]:
    try:
        with connection_scope(settings) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            value = cursor.fetchone()[0]
        return value == 1, "数据库连接正常"
    except Exception as exc:
        return False, str(exc)


def init_db(settings: Settings | None = None) -> tuple[bool, str]:
    try:
        with connection_scope(settings) as conn:
            cursor = conn.cursor()
            for statement in SCHEMA_STATEMENTS:
                cursor.execute(statement)
        return True, "财务平台数据表初始化完成"
    except Exception as exc:
        return False, str(exc)
