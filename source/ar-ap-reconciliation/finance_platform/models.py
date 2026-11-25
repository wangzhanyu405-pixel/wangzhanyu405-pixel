from __future__ import annotations


PARTY_SUPPLIER = "supplier"
PARTY_CUSTOMER = "customer"

DIRECTION_PAYABLE = "payable"
DIRECTION_RECEIVABLE = "receivable"

BANK_INFLOW = "inflow"
BANK_OUTFLOW = "outflow"

FILE_ROLE_LABELS = {
    "party_directory": "供应商/客户目录",
    "invoice_auto": "发票明细（自动识别）",
    "payable_invoice": "应付发票明细",
    "receivable_invoice": "应收开票明细",
    "bank_statement": "银行流水",
}

FILE_ROLE_OPTIONS = list(FILE_ROLE_LABELS.keys())
