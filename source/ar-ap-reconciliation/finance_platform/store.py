from __future__ import annotations

from finance_platform.config import Settings, get_settings
from finance_platform.local_store import LocalStore
from finance_platform.sqlserver_store import SqlServerStore


def create_store(settings: Settings | None = None) -> LocalStore:
    settings = settings or get_settings()
    if settings.uses_sqlserver_storage:
        store = SqlServerStore(settings)
        store.init()
        store.rebuild_party_links()
        return store

    store = LocalStore(settings.local_db_path)
    store.init()
    store.rebuild_party_links()
    return store


def sync_if_sqlserver(store: LocalStore) -> bool:
    return False
