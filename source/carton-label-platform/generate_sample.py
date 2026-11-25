"""Generate the single sample label corresponding to the seeded SQLite print record."""
from datetime import datetime
from pathlib import Path

from label_engine import generate_single_label, make_default_fields_from_data
from template_engine import build_qr_content_from_context, build_render_context, get_default_template
from trace_store import SAMPLE_QR


def main():
    when = datetime(2026, 11, 24, 9)
    fields = make_default_fields_from_data(
        {"品名": "扫码体验纸盒", "料号": "DEMO-S01", "订货数量": 40, "最终成品数": 40, "包装方式": "纸箱"},
        {"定单号码": "PO-DEMO-002", "客户名称": "演示客户"},
    )
    for field in fields:
        if field["key"] == "date":
            field["value"] = when.strftime("%Y.%m.%d")
    template = get_default_template()
    args = dict(fields=fields, current_box_index=1, total_boxes=1, current_box_qty=40,
                std_qty=40, pack_info=None, box_type="full", print_time=when,
                shipment_time="20261124090000", template=template)
    assert build_qr_content_from_context(template, build_render_context(**args)) == SAMPLE_QR
    dest = Path(__file__).resolve().parent / "samples" / "扫码体验箱贴_900002.pdf"
    dest.parent.mkdir(exist_ok=True)
    generate_single_label(**args).save(dest, "PDF", resolution=203)


if __name__ == "__main__":
    main()
