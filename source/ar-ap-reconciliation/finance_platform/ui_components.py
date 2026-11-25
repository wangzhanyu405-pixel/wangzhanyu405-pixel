from __future__ import annotations

from collections.abc import Callable
from typing import Any

import streamlit as st


def show_table(rows: list[dict[str, Any]], empty_message: str = "暂无数据") -> None:
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info(empty_message)


def show_operation_error(exc: Exception) -> None:
    st.error(str(exc))
    st.caption("如果这是数据库相关错误，请先到“系统设置”检查连接和初始化数据表。")


def guarded_action(label: str, action: Callable[[], Any]) -> Any | None:
    if not st.button(label):
        return None
    try:
        result = action()
        st.success("操作完成")
        return result
    except Exception as exc:  # pragma: no cover - UI surface
        show_operation_error(exc)
        return None
