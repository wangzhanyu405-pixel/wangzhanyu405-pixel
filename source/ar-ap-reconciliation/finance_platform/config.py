from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional during bare compile checks
    load_dotenv = None


if load_dotenv is not None:
    load_dotenv()


@dataclass(frozen=True)
class Settings:
    storage_mode: str
    db_driver: str
    db_server: str
    db_port: str
    db_name: str
    db_user: str
    db_password: str
    db_timeout: int
    upload_dir: Path
    export_dir: Path
    local_db_path: Path

    @property
    def server_with_port(self) -> str:
        return f"{self.db_server},{self.db_port}"

    @property
    def has_database_credentials(self) -> bool:
        return bool(self.db_user and self.db_password)

    @property
    def uses_sqlserver_storage(self) -> bool:
        return self.storage_mode.strip().lower() in {"sqlserver", "sql_server", "mssql"}


def configured_company_name() -> str:
    return os.getenv("LYC_COMPANY_NAME", "").strip()


def get_settings() -> Settings:
    upload_dir = Path(os.getenv("LYC_FIN_UPLOAD_DIR", "data/uploads"))
    export_dir = Path(os.getenv("LYC_FIN_EXPORT_DIR", "data/exports"))
    local_db_path = Path(os.getenv("LYC_FIN_LOCAL_DB", "data/local_finance.db"))
    upload_dir.mkdir(parents=True, exist_ok=True)
    export_dir.mkdir(parents=True, exist_ok=True)
    local_db_path.parent.mkdir(parents=True, exist_ok=True)

    return Settings(
        storage_mode=os.getenv("LYC_FIN_STORAGE", "sqlite"),
        db_driver=os.getenv("LYC_DB_DRIVER", "SQL Server"),
        db_server=os.getenv("LYC_DB_SERVER", ""),
        db_port=os.getenv("LYC_DB_PORT", "1433"),
        db_name=os.getenv("LYC_DB_NAME", ""),
        db_user=os.getenv("LYC_DB_USER", ""),
        db_password=os.getenv("LYC_DB_PASSWORD", ""),
        db_timeout=int(os.getenv("LYC_DB_TIMEOUT", "5")),
        upload_dir=upload_dir,
        export_dir=export_dir,
        local_db_path=local_db_path,
    )
