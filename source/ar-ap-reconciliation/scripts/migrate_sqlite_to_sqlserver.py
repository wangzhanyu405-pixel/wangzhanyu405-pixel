from __future__ import annotations

import argparse
from pathlib import Path

from finance_platform.config import get_settings
from finance_platform.sqlserver_migration import copy_sqlite_to_sqlserver


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="把本地 SQLite 财务数据迁移到 SQL Server。")
    parser.add_argument(
        "--sqlite",
        default=str(settings.local_db_path),
        help="本地 SQLite 数据库路径，默认读取 LYC_FIN_LOCAL_DB。",
    )
    parser.add_argument(
        "--reset-target",
        action="store_true",
        help="迁移前清空 dbo.lyc_fin_* 目标业务表。首次正式迁移建议使用。",
    )
    args = parser.parse_args()

    counts = copy_sqlite_to_sqlserver(Path(args.sqlite), settings=settings, reset_target=args.reset_target)
    print("SQLite 到 SQL Server 迁移完成：")
    for table_name, count in counts.items():
        print(f"- {table_name}: {count}")


if __name__ == "__main__":
    main()
