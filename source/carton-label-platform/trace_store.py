"""Local SQLite storage with the original label application's business operations."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import os
import re
import sqlite3

import pandas as pd

DB_PATH = Path(os.environ.get("LABEL_DEMO_DB", Path(__file__).with_name("labels.sqlite3")))
SAMPLE_QR = "DEMO-S01#202611240001#40"
_active_batch = ContextVar("active_print_batch", default=None)


def get_erp_connection():
    return sqlite3.connect(DB_PATH, timeout=10)


def init_shared_business_tables():
    with get_erp_connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS lyc_print_logs(
            id INTEGER PRIMARY KEY, process_no TEXT, order_no TEXT, item_no TEXT, product_name TEXT,
            box_index INTEGER,total_boxes INTEGER,qty_in_box INTEGER,std_qty INTEGER,
            printer_name TEXT,operator_name TEXT,qr_content TEXT,shipment_time TEXT,
            action_type TEXT,box_type TEXT,is_counted INTEGER,is_deleted INTEGER DEFAULT 0,
            printed_at TEXT DEFAULT (datetime('now','localtime')), deleted_by_name TEXT,deleted_at TEXT,delete_reason TEXT);
        CREATE TABLE IF NOT EXISTS print_batches(token TEXT PRIMARY KEY);
        """)
        for table in ("lyc_scan_in_logs","lyc_scan_out_logs"):
            db.execute(f"""CREATE TABLE IF NOT EXISTS {table}(
                id INTEGER PRIMARY KEY, print_log_id INTEGER, scan_in_id INTEGER, qr_content TEXT,
                process_no TEXT, order_no TEXT, item_no TEXT, product_name TEXT, box_index INTEGER, total_boxes INTEGER,
                qty_in_box INTEGER,std_qty INTEGER,scanned_by_name TEXT,scanned_at TEXT DEFAULT (datetime('now','localtime')),
                source TEXT,status TEXT,remark TEXT)""")
            db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table} ON {table}(qr_content) WHERE status IN ('In','Out')")
        db.execute("""INSERT INTO lyc_print_logs(process_no,order_no,item_no,product_name,box_index,total_boxes,
                      qty_in_box,std_qty,printer_name,operator_name,qr_content,action_type,box_type,is_counted,printed_at)
                      SELECT '900002','PO-DEMO-002','DEMO-S01','扫码体验纸盒',1,1,40,40,'演示样签','demo',?,'new','full',1,'2026-11-24 09:00:00'
                      WHERE NOT EXISTS(SELECT 1 FROM lyc_print_logs WHERE qr_content=?)""",(SAMPLE_QR,SAMPLE_QR))


@contextmanager
def print_batch(token):
    db=get_erp_connection()
    try:
        db.execute("BEGIN IMMEDIATE")
        inserted=db.execute("INSERT OR IGNORE INTO print_batches(token) VALUES(?)",(token,)).rowcount
        ctx=_active_batch.set(db)
        try:
            yield bool(inserted)
        finally:
            _active_batch.reset(ctx)
        db.commit()
    finally:
        db.close()


def _to_int(val, default=0):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    try:
        return int(float(val))
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
    batch = _active_batch.get()
    conn = batch or get_erp_connection()
    cursor = conn.cursor()
    is_counted = should_count_print_log(action_type, box_type)
    cursor.execute('''INSERT INTO lyc_print_logs
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
    if batch is None:
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
    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT box_index, qty_in_box, action_type, box_type, is_counted
        FROM lyc_print_logs
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


def get_print_log_details(process_no, item_no):
    """
    返回某 工艺单+料号 的所有打印记录(用于UI展示和删除)。
    返回 DataFrame:id, box_index, qty_in_box, box_type, action_type, ts, printer_name
    """
    conn = get_erp_connection()
    df = pd.read_sql('''
        SELECT id, box_index, qty_in_box, box_type, action_type,
               printed_at AS ts, printer_name, operator_name, qr_content
        FROM lyc_print_logs
        WHERE process_no = ? AND item_no = ? AND is_deleted = 0
        ORDER BY box_index ASC, printed_at ASC
    ''', conn, params=[_to_str(process_no), _to_str(item_no)])
    conn.close()
    return df


def fetch_system_print_history(process_no=None, item_no=None, limit=200, include_deleted=False):
    """查询本系统打印历史(lyc_print_logs),不同于 ERP 出货记录。"""
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
        SELECT
               id, process_no, order_no, item_no, product_name,
               box_index, total_boxes, qty_in_box, std_qty, box_type,
               action_type, is_counted, is_deleted,
               printer_name, operator_name, printed_at, qr_content
        FROM lyc_print_logs
        {where_sql}
        ORDER BY printed_at DESC, id DESC LIMIT {int(limit)}
    """
    df = pd.read_sql(sql, conn, params=params)
    conn.close()
    return df


def fetch_trace_print_logs(keyword=None, limit=1000, include_deleted=False):
    """读取本系统写入 SQL Server 的打印流水,用于溯源查询第一阶段。"""
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
        SELECT
               id, process_no, order_no, item_no, product_name,
               box_index, total_boxes, qty_in_box, std_qty, box_type,
               action_type, is_counted, is_deleted,
               printer_name, operator_name, printed_at, qr_content,
               shipment_time, deleted_by_name, deleted_at, delete_reason
        FROM lyc_print_logs
        {where_sql}
        ORDER BY printed_at DESC, id DESC LIMIT {int(limit)}
    """
    df = pd.read_sql(sql, conn, params=params)
    conn.close()
    return df


def fetch_trace_records(keyword=None, limit=1000, include_deleted=False):
    """Combine print, scan-in, and scan-out logs into one trace view."""
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
        SELECT
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
                   WHEN p.is_deleted = 1 THEN '打印已删除'
                   WHEN so.id IS NOT NULL THEN '已出库'
                   WHEN si.id IS NOT NULL THEN '已入库未出库'
                   ELSE '已打印未入库'
               END AS trace_status
        FROM lyc_print_logs p
        LEFT JOIN lyc_scan_in_logs si ON si.qr_content=p.qr_content AND si.status='In'
        LEFT JOIN lyc_scan_out_logs so ON so.qr_content=p.qr_content AND so.status='Out'
        {where_sql}
        ORDER BY p.printed_at DESC, p.id DESC LIMIT {int(limit)}
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
    # The shipping template combines date and four-digit box index into one field.
    if len(parts) == 3 and parts[0] and re.fullmatch(r"[0-9]{12}", parts[1]) and parts[2].isdigit():
        parsed.update(print_date=parts[1][:8], box_index=int(parts[1][8:]), qty_in_box=int(parts[2]), is_valid=True)
    return parsed


def find_print_log_by_qr(qr_content):
    """Find the system print log for a scanned box-label QR payload."""
    qr = _to_str(qr_content)
    if not qr:
        return None
    conn = get_erp_connection()
    sql = """
        SELECT
               id, process_no, order_no, item_no, product_name,
               box_index, total_boxes, qty_in_box, std_qty, box_type,
               action_type, is_counted, is_deleted,
               printer_name, operator_name, printed_at, qr_content,
               shipment_time
        FROM lyc_print_logs
        WHERE qr_content = ? AND is_deleted = 0
        ORDER BY is_counted DESC, printed_at DESC, id DESC LIMIT 1
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

    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    cursor.execute(
        """
        SELECT id, scanned_by_name, scanned_at
        FROM lyc_scan_in_logs
        WHERE status = 'In' AND (print_log_id = ? OR qr_content = ?)
        ORDER BY scanned_at DESC, id DESC LIMIT 1
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
        INSERT INTO lyc_scan_in_logs
            (print_log_id, qr_content, process_no, order_no, item_no,
             product_name, box_index, total_boxes, qty_in_box, std_qty,
             scanned_by_name, source, status, remark)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'In', ?)
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
    scan_id = cursor.lastrowid
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
        where.append("(status IS NULL OR status <> 'Void')")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT
               id, print_log_id, process_no, order_no, item_no, product_name,
               box_index, total_boxes, qty_in_box, std_qty, scanned_by_name,
               scanned_at, source, status, remark, qr_content
        FROM lyc_scan_in_logs
        {where_sql}
        ORDER BY scanned_at DESC, id DESC LIMIT {int(limit)}
    """
    df = pd.read_sql(sql, conn, params=params)
    conn.close()
    return df


def find_scan_in_by_qr(qr_content):
    """Find the active stock-in record for a scanned QR payload."""
    qr = _to_str(qr_content)
    if not qr:
        return None
    conn = get_erp_connection()
    sql = """
        SELECT
               id, print_log_id, process_no, order_no, item_no, product_name,
               box_index, total_boxes, qty_in_box, std_qty, scanned_by_name,
               scanned_at, source, status, remark, qr_content
        FROM lyc_scan_in_logs
        WHERE qr_content = ? AND status = 'In'
        ORDER BY scanned_at DESC, id DESC LIMIT 1
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

    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    cursor.execute(
        """
        SELECT id, scanned_by_name, scanned_at
        FROM lyc_scan_out_logs
        WHERE status = 'Out' AND (print_log_id = ? OR qr_content = ?)
        ORDER BY scanned_at DESC, id DESC LIMIT 1
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
        INSERT INTO lyc_scan_out_logs
            (print_log_id, scan_in_id, qr_content, process_no, order_no, item_no,
             product_name, box_index, total_boxes, qty_in_box, std_qty,
             scanned_by_name, source, status, remark)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Out', ?)
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
    scan_id = cursor.lastrowid
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
        where.append("(status IS NULL OR status <> 'Void')")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT
               id, print_log_id, scan_in_id, process_no, order_no, item_no,
               product_name, box_index, total_boxes, qty_in_box, std_qty,
               scanned_by_name, scanned_at, source, status, remark, qr_content
        FROM lyc_scan_out_logs
        {where_sql}
        ORDER BY scanned_at DESC, id DESC LIMIT {int(limit)}
    """
    df = pd.read_sql(sql, conn, params=params)
    conn.close()
    return df


def delete_print_logs_by_ids(log_ids, operator='system'):
    """按 id 列表作废打印记录(软删除)。返回处理条数。"""
    if not log_ids:
        return 0
    conn = get_erp_connection()
    cursor = conn.cursor()
    placeholders = ','.join(['?'] * len(log_ids))
    cursor.execute(
        f"UPDATE lyc_print_logs SET is_deleted = 1, deleted_by_name = ?, deleted_at = datetime('now','localtime') WHERE id IN ({placeholders})",
        [_to_str(operator)] + [int(i) for i in log_ids]
    )
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected


def delete_all_print_logs(process_no, item_no, operator='system'):
    """作废某 工艺单+料号 的所有打印记录(软删除)。返回处理条数。"""
    conn = get_erp_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE lyc_print_logs SET is_deleted = 1, deleted_by_name = ?, deleted_at = datetime('now','localtime') WHERE process_no = ? AND item_no = ? AND is_deleted = 0",
        (_to_str(operator), _to_str(process_no), _to_str(item_no))
    )
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected
