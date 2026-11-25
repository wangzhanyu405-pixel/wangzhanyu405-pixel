from __future__ import annotations


def _add_column(table_name: str, column_name: str, column_definition: str) -> str:
    return f"""
    IF COL_LENGTH(N'{table_name}', N'{column_name}') IS NULL
    BEGIN
        ALTER TABLE {table_name} ADD {column_name} {column_definition}
    END
    """


SCHEMA_STATEMENTS = [
    """
    IF OBJECT_ID(N'dbo.lyc_fin_parties', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.lyc_fin_parties (
            id INT IDENTITY(1,1) PRIMARY KEY,
            party_type NVARCHAR(20) NOT NULL,
            name NVARCHAR(255) NOT NULL,
            short_name NVARCHAR(255) NULL,
            default_term_months INT NOT NULL DEFAULT 30,
            grace_months INT NOT NULL DEFAULT 0,
            bank_account NVARCHAR(100) NULL,
            bank_name NVARCHAR(255) NULL,
            is_active BIT NOT NULL DEFAULT 1,
            remark NVARCHAR(500) NULL,
            created_at DATETIME NOT NULL DEFAULT GETDATE(),
            updated_at DATETIME NULL,
            CONSTRAINT uq_lyc_fin_parties_type_name UNIQUE (party_type, name)
        )
    END
    """,
    _add_column("dbo.lyc_fin_parties", "bank_account", "NVARCHAR(100) NULL"),
    _add_column("dbo.lyc_fin_parties", "bank_name", "NVARCHAR(255) NULL"),
    """
    IF OBJECT_ID(N'dbo.lyc_fin_party_aliases', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.lyc_fin_party_aliases (
            id INT IDENTITY(1,1) PRIMARY KEY,
            party_id INT NOT NULL,
            alias_name NVARCHAR(255) NOT NULL,
            source_type NVARCHAR(50) NOT NULL DEFAULT N'manual',
            is_active BIT NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT GETDATE(),
            CONSTRAINT fk_lyc_fin_alias_party FOREIGN KEY (party_id)
                REFERENCES dbo.lyc_fin_parties(id),
            CONSTRAINT uq_lyc_fin_alias_party_name UNIQUE (party_id, alias_name)
        )
    END
    """,
    """
    IF OBJECT_ID(N'dbo.lyc_fin_import_files', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.lyc_fin_import_files (
            id INT IDENTITY(1,1) PRIMARY KEY,
            period_month NVARCHAR(7) NULL,
            file_role NVARCHAR(50) NOT NULL,
            original_filename NVARCHAR(255) NOT NULL,
            stored_path NVARCHAR(1000) NULL,
            file_hash NVARCHAR(64) NULL,
            row_count INT NOT NULL DEFAULT 0,
            parsed_count INT NOT NULL DEFAULT 0,
            error_count INT NOT NULL DEFAULT 0,
            saved_count INT NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT GETDATE()
        )
    END
    """,
    _add_column("dbo.lyc_fin_import_files", "period_month", "NVARCHAR(7) NULL"),
    _add_column("dbo.lyc_fin_import_files", "saved_count", "INT NOT NULL DEFAULT 0"),
    _add_column("dbo.lyc_fin_import_files", "created_at", "DATETIME NOT NULL DEFAULT GETDATE()"),
    """
    IF OBJECT_ID(N'dbo.lyc_fin_invoices', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.lyc_fin_invoices (
            id INT IDENTITY(1,1) PRIMARY KEY,
            direction NVARCHAR(20) NOT NULL,
            party_id INT NULL,
            party_name_raw NVARCHAR(255) NOT NULL,
            invoice_no NVARCHAR(100) NULL,
            invoice_date DATE NOT NULL,
            invoice_amount DECIMAL(18,2) NOT NULL,
            term_months INT NOT NULL DEFAULT 30,
            grace_months INT NOT NULL DEFAULT 0,
            due_date DATE NOT NULL,
            grace_deadline DATE NOT NULL,
            status NVARCHAR(30) NOT NULL DEFAULT N'unmatched',
            invoice_status_raw NVARCHAR(100) NULL,
            is_positive BIT NULL,
            original_invoice_no NVARCHAR(100) NULL,
            red_confirmation_no NVARCHAR(100) NULL,
            invoice_type NVARCHAR(100) NULL,
            amount_without_tax DECIMAL(18,2) NULL,
            tax_amount DECIMAL(18,2) NULL,
            remark NVARCHAR(500) NULL,
            source_file_id INT NULL,
            source_row_no INT NULL,
            created_at DATETIME NOT NULL DEFAULT GETDATE(),
            CONSTRAINT fk_lyc_fin_invoice_party FOREIGN KEY (party_id)
                REFERENCES dbo.lyc_fin_parties(id)
        )
    END
    """,
    _add_column("dbo.lyc_fin_invoices", "invoice_status_raw", "NVARCHAR(100) NULL"),
    _add_column("dbo.lyc_fin_invoices", "is_positive", "BIT NULL"),
    _add_column("dbo.lyc_fin_invoices", "original_invoice_no", "NVARCHAR(100) NULL"),
    _add_column("dbo.lyc_fin_invoices", "red_confirmation_no", "NVARCHAR(100) NULL"),
    _add_column("dbo.lyc_fin_invoices", "invoice_type", "NVARCHAR(100) NULL"),
    _add_column("dbo.lyc_fin_invoices", "amount_without_tax", "DECIMAL(18,2) NULL"),
    _add_column("dbo.lyc_fin_invoices", "tax_amount", "DECIMAL(18,2) NULL"),
    _add_column("dbo.lyc_fin_invoices", "remark", "NVARCHAR(500) NULL"),
    """
    IF OBJECT_ID(N'dbo.lyc_fin_bank_transactions', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.lyc_fin_bank_transactions (
            id INT IDENTITY(1,1) PRIMARY KEY,
            transaction_date DATE NOT NULL,
            income_amount DECIMAL(18,2) NULL,
            expense_amount DECIMAL(18,2) NULL,
            counterparty_account NVARCHAR(100) NULL,
            counterparty_name_raw NVARCHAR(255) NULL,
            counterparty_bank NVARCHAR(255) NULL,
            subject NVARCHAR(100) NULL,
            summary NVARCHAR(500) NULL,
            party_id INT NULL,
            direction NVARCHAR(20) NOT NULL,
            match_status NVARCHAR(30) NOT NULL DEFAULT N'unmatched',
            source_file_id INT NULL,
            source_row_no INT NULL,
            created_at DATETIME NOT NULL DEFAULT GETDATE(),
            CONSTRAINT fk_lyc_fin_bank_party FOREIGN KEY (party_id)
                REFERENCES dbo.lyc_fin_parties(id)
        )
    END
    """,
    """
    IF OBJECT_ID(N'dbo.lyc_fin_match_results', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.lyc_fin_match_results (
            id INT IDENTITY(1,1) PRIMARY KEY,
            run_no NVARCHAR(100) NOT NULL,
            direction NVARCHAR(20) NOT NULL,
            party_id INT NULL,
            party_name NVARCHAR(255) NULL,
            match_type NVARCHAR(50) NOT NULL,
            status NVARCHAR(50) NOT NULL,
            invoice_ids NVARCHAR(MAX) NULL,
            bank_transaction_ids NVARCHAR(MAX) NULL,
            invoice_total DECIMAL(18,2) NOT NULL DEFAULT 0,
            bank_total DECIMAL(18,2) NOT NULL DEFAULT 0,
            difference_amount DECIMAL(18,2) NOT NULL DEFAULT 0,
            confidence DECIMAL(8,4) NOT NULL DEFAULT 0,
            remark NVARCHAR(1000) NULL,
            settlement_method NVARCHAR(50) NULL,
            settlement_date DATE NULL,
            created_at DATETIME NOT NULL DEFAULT GETDATE(),
            CONSTRAINT fk_lyc_fin_result_party FOREIGN KEY (party_id)
                REFERENCES dbo.lyc_fin_parties(id)
        )
    END
    """,
    _add_column("dbo.lyc_fin_match_results", "settlement_method", "NVARCHAR(50) NULL"),
    _add_column("dbo.lyc_fin_match_results", "settlement_date", "DATE NULL"),
    """
    IF OBJECT_ID(N'dbo.lyc_fin_payment_templates', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.lyc_fin_payment_templates (
            id INT IDENTITY(1,1) PRIMARY KEY,
            bank_code NVARCHAR(50) NOT NULL,
            bank_name NVARCHAR(100) NOT NULL,
            template_name NVARCHAR(255) NOT NULL,
            sheet_name NVARCHAR(255) NOT NULL,
            default_purpose NVARCHAR(500) NULL,
            column_config NVARCHAR(MAX) NOT NULL,
            is_active BIT NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT GETDATE(),
            updated_at DATETIME NULL,
            CONSTRAINT uq_lyc_fin_payment_templates UNIQUE (bank_code, template_name)
        )
    END
    """,
    """
    IF OBJECT_ID(N'dbo.lyc_fin_manual_adjustments', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.lyc_fin_manual_adjustments (
            id INT IDENTITY(1,1) PRIMARY KEY,
            target_type NVARCHAR(50) NOT NULL,
            target_id INT NULL,
            action NVARCHAR(50) NOT NULL,
            before_json NVARCHAR(MAX) NULL,
            after_json NVARCHAR(MAX) NULL,
            operator_name NVARCHAR(50) NULL,
            created_at DATETIME NOT NULL DEFAULT GETDATE(),
            remark NVARCHAR(500) NULL
        )
    END
    """,
    """
    IF OBJECT_ID(N'dbo.lyc_fin_balance_adjustments', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.lyc_fin_balance_adjustments (
            id INT IDENTITY(1,1) PRIMARY KEY,
            direction NVARCHAR(20) NOT NULL,
            party_id INT NOT NULL,
            adjustment_type NVARCHAR(50) NOT NULL,
            adjustment_date DATE NOT NULL,
            amount DECIMAL(18,2) NOT NULL,
            source_type NVARCHAR(50) NOT NULL DEFAULT N'manual',
            remark NVARCHAR(500) NULL,
            created_at DATETIME NOT NULL DEFAULT GETDATE(),
            updated_at DATETIME NULL,
            CONSTRAINT fk_lyc_fin_balance_adjustment_party FOREIGN KEY (party_id)
                REFERENCES dbo.lyc_fin_parties(id)
        )
    END
    """,
    """
    IF OBJECT_ID(N'dbo.lyc_fin_audit_logs', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.lyc_fin_audit_logs (
            id INT IDENTITY(1,1) PRIMARY KEY,
            action NVARCHAR(50) NOT NULL,
            target_type NVARCHAR(50) NULL,
            target_id INT NULL,
            detail NVARCHAR(MAX) NULL,
            created_at DATETIME NOT NULL DEFAULT GETDATE()
        )
    END
    """,
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = N'ix_lyc_fin_invoices_party_date'
          AND object_id = OBJECT_ID(N'dbo.lyc_fin_invoices')
    )
    BEGIN
        CREATE INDEX ix_lyc_fin_invoices_party_date
        ON dbo.lyc_fin_invoices (party_id, invoice_date)
    END
    """,
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = N'ix_lyc_fin_bank_party_date'
          AND object_id = OBJECT_ID(N'dbo.lyc_fin_bank_transactions')
    )
    BEGIN
        CREATE INDEX ix_lyc_fin_bank_party_date
        ON dbo.lyc_fin_bank_transactions (party_id, transaction_date)
    END
    """,
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = N'ix_lyc_fin_balance_adjustments_party_date'
          AND object_id = OBJECT_ID(N'dbo.lyc_fin_balance_adjustments')
    )
    BEGIN
        CREATE INDEX ix_lyc_fin_balance_adjustments_party_date
        ON dbo.lyc_fin_balance_adjustments (party_id, adjustment_date)
    END
    """,
]
