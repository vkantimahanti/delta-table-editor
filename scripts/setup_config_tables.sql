-- =============================================================================
-- Step 2: Create Data Manager tables (run after reset_config_tables.sql)
-- =============================================================================
-- Databricks Delta: no column DEFAULT clauses — values set by deploy_config.py
-- and application code.
--
-- Config (3):   table_registry, column_config, business_rules
-- Operational (2): change_requests, app_audit_log
-- Staging:      {table_name}_app_stage (created by app on first use)
--
-- Then: python scripts/deploy_config.py --all
-- =============================================================================

-- ── 1. Table registry ─────────────────────────────────────────────────────────
CREATE TABLE your_catalog.dmz.dataeditor_table_registry (
    schema_name                   STRING NOT NULL,
    table_name                    STRING NOT NULL,
    display_name                  STRING,
    description                   STRING,
    catalog                       STRING,
    primary_keys                  STRING,
    app_group                     STRING,
    row_limit                     INT,
    allow_insert                  BOOLEAN,
    allow_update                  BOOLEAN,
    allow_delete                  BOOLEAN,
    is_active                     BOOLEAN,
    requires_approval             BOOLEAN,
    approver_emails               STRING,
    approval_expiry_hours         INT,
    allow_overwrite               BOOLEAN,
    max_staged_rows               INT,
    auto_apply_on_approve         BOOLEAN,
    requires_upload_approval      BOOLEAN,
    upload_approval_expiry_hours  INT
) USING DELTA;

-- ── 2. Column config ────────────────────────────────────────────────────────
CREATE TABLE your_catalog.dmz.dataeditor_column_config (
    schema_name      STRING NOT NULL,
    table_name       STRING NOT NULL,
    column_name      STRING NOT NULL,
    col_order        INT,
    is_primary_key   BOOLEAN,
    visible          BOOLEAN,
    editable         BOOLEAN,
    mandatory        BOOLEAN,
    nullable         BOOLEAN,
    default_value    STRING,
    column_type      STRING,
    dropdown_source  STRING,
    is_filter        BOOLEAN,
    is_active        BOOLEAN
) USING DELTA;

-- ── 3. Business rules ───────────────────────────────────────────────────────
CREATE TABLE your_catalog.dmz.dataeditor_business_rules (
    schema_name      STRING NOT NULL,
    table_name       STRING NOT NULL,
    column_name      STRING NOT NULL,
    condition_type   STRING NOT NULL,
    condition_params STRING,
    on_fail          STRING,
    reason           STRING,
    fix              STRING,
    col_is_active    BOOLEAN,
    cond_is_active   BOOLEAN
) USING DELTA;

-- ── 4. Change requests ────────────────────────────────────────────────────────
CREATE TABLE your_catalog.dmz.dataeditor_change_requests (
    change_request_id         STRING    NOT NULL,
    status                    STRING    NOT NULL,
    request_type              STRING    NOT NULL,
    mode                      STRING,
    catalog                   STRING,
    schema_name               STRING    NOT NULL,
    table_name                STRING    NOT NULL,
    submitted_by              STRING,
    submitted_at              TIMESTAMP,
    validated_at              TIMESTAMP,
    applied_at                TIMESTAMP,
    updated_at                TIMESTAMP,
    staging_table_name        STRING,
    source_file_volume_path   STRING,
    export_volume_path        STRING,
    row_count                 INT,
    validation_summary        STRING,
    change_summary            STRING,
    errors_json               STRING,
    filter_snapshot           STRING,
    failure_reason            STRING,
    revision_id               STRING,
    revision_no               BIGINT,
    requires_approval         BOOLEAN,
    approver_emails           STRING,
    approval_status           STRING,
    approved_by               STRING,
    approved_at               TIMESTAMP,
    rejected_by               STRING,
    rejected_at               TIMESTAMP,
    rejection_reason          STRING,
    expires_at                TIMESTAMP,
    approval_token_hash       STRING
) USING DELTA;

-- ── 5. Audit log ────────────────────────────────────────────────────────────
CREATE TABLE your_catalog.dmz.dataeditor_app_audit_log (
    changed_by            STRING,
    changed_at            TIMESTAMP,
    table_schema          STRING,
    table_name            STRING,
    record_key            STRING,
    column_name           STRING,
    old_value             STRING,
    new_value             STRING,
    change_source         STRING,
    change_request_id     STRING,
    revision_id           STRING,
    revision_no           BIGINT
) USING DELTA;
