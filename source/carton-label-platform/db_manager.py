"""Session-scoped data source for the local label demonstration."""
from copy import deepcopy

import pandas as pd
import streamlit as st

from box_distribution import calculate_box_distribution
from template_engine import list_builtin_templates

DEMO_USER = dict(username="demo", display_name="本机演示", role_code="operator", permissions=["label_print", "template", "scan_in", "scan_out", "trace_query"])


def demo_rows():
    common = dict(customer="演示客户", order_no="PO-DEMO-001", process_no="900001",
                  last_process="装箱", modulus=1, pack_method="纸箱")
    return [dict(common, id=1, item_no="DEMO-A01", sap_no="DEMO1001", product_name="演示折叠纸盒",
                 order_qty=250, done_qty=250, box_spec=100),
            dict(common, id=2, item_no="DEMO-A02", sap_no="DEMO1002", product_name="演示展示架",
                 order_qty=120, done_qty=120, box_spec=40)]


def get_pending_records():
    return pd.DataFrame(st.session_state.setdefault("demo_pending", demo_rows()))


def fetch_production_data(search_keyword, search_type=None):
    key = search_keyword.strip().casefold()
    rows = [r for r in demo_rows() if any(key in str(r[k]).casefold() for k in ("item_no","product_name","process_no","order_no","sap_no"))]
    if not rows:
        return dict(status="error", msg="本地示例无匹配记录")
    products = [dict(料号=r["item_no"], 品名=r["product_name"], 订货数量=r["order_qty"], 最终成品数=r["done_qty"], 箱规=r["box_spec"]) for r in rows]
    return dict(status="success", msg=f"匹配 {len(rows)} 款本地示例产品", summary=dict(products=products, order_header={"定单号码":rows[0]["order_no"]}, process_no=rows[0]["process_no"]))


def insert_to_shadow_db(product, order_header, process_no):
    rows = st.session_state.setdefault("demo_pending", demo_rows())
    row = next(r for r in demo_rows() if r["item_no"] == product["料号"])
    if row not in rows:
        rows.append(row)


def delete_shadow_records_batch(record_ids, operator=None):
    rows = st.session_state.setdefault("demo_pending", demo_rows())
    keep = [r for r in rows if r["id"] not in record_ids]
    st.session_state["demo_pending"] = keep
    return len(rows) - len(keep)


confirm_shadow_records_batch = delete_shadow_records_batch


def delete_shadow_record(record_id, operator=None):
    return delete_shadow_records_batch([record_id], operator)


confirm_shadow_record = delete_shadow_record


def get_print_templates(active_only=True):
    return deepcopy(st.session_state.setdefault("demo_templates", list_builtin_templates()))


def save_print_template(template_config, operator=None, set_default=False):
    templates = [t for t in get_print_templates() if t["name"] != template_config["name"]]
    if set_default:
        for t in templates:
            t["_is_default"] = False
    item=deepcopy(template_config)
    item["_is_default"]=set_default or item.get("_is_default",False)
    templates.insert(0, item)
    st.session_state["demo_templates"]=templates


def delete_print_template(template_name, operator=None):
    templates = get_print_templates()
    kept=[t for t in templates if t["name"] != template_name]
    st.session_state["demo_templates"]=kept
    return len(templates)-len(kept)


from trace_store import (
    SAMPLE_QR, init_shared_business_tables as init_shadow_db,
    log_print_action, get_print_history, get_print_log_details,
    delete_all_print_logs, delete_print_logs_by_ids,
    fetch_trace_print_logs, fetch_trace_records, fetch_system_print_history,
    parse_qr_content, record_scan_in, record_scan_out,
    fetch_scan_in_logs, fetch_scan_out_logs, print_batch,
)


def fetch_shipment_by_process_no(*args):
    return pd.DataFrame()


def authenticate_user(*args):
    return None
