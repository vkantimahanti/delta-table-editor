-- ============================================================
-- Delta Table Editor — One-time workspace setup
-- ============================================================
-- Run this in a Databricks SQL editor or notebook ONCE.
--
-- Before running:
--   Replace 'your_catalog' with your Unity Catalog name.
--   Replace 'your_schema' with your target schema (e.g. 'admin').
--
-- After running:
--   Deploy table configs:  python scripts/deploy_config.py --all
-- ============================================================

-- 1. Table registry
--    One row per table you want to expose in the editor.
CREATE TABLE IF NOT EXISTS your_catalog.your_schema.dataeditor_table_registry (
    schema_name             STRING  NOT NULL,
    table_name              STRING  NOT NULL,
    display_name            STRING,
    description             STRING,
    primary_keys            STRING,           -- comma-separated: "id,type"
    default_where           STRING,           -- e.g. "is_active = TRUE"
    catalog                 STRING  DEFAULT 'your_catalog',
    row_limit               INT     DEFAULT 500,
    allow_insert            BOOLEAN DEFAULT TRUE,
    allow_update            BOOLEAN DEFAULT TRUE,
    allow_delete            BOOLEAN DEFAULT FALSE,
    is_active               BOOLEAN DEFAULT TRUE,
    app_group               STRING  DEFAULT 'General',
    requires_approval       BOOLEAN DEFAULT FALSE,
    approver_emails         STRING,
    approval_expiry_hours   INT     DEFAULT 72,
    allow_overwrite         BOOLEAN DEFAULT TRUE,
    max_staged_rows         INT     DEFAULT 500,
    auto_apply_on_approve   BOOLEAN DEFAULT FALSE
) USING DELTA;

-- 2. Column config
--    One row per column per table — controls label, type, editability, dropdowns.
CREATE TABLE IF NOT EXISTS your_catalog.your_schema.dataeditor_column_config (
    schema_name     STRING  NOT NULL,
    table_name      STRING  NOT NULL,
    column_name     STRING  NOT NULL,
    display_label   STRING,
    col_order       INT     DEFAULT 0,
    column_type     STRING  DEFAULT 'text',   -- text|number|date|timestamp|boolean|dropdown
    visible         BOOLEAN DEFAULT TRUE,
    editable        BOOLEAN DEFAULT TRUE,
    mandatory       BOOLEAN DEFAULT FALSE,
    is_filter       BOOLEAN DEFAULT FALSE,    -- show inline column filter
    is_primary_key  BOOLEAN DEFAULT FALSE,
    nullable        BOOLEAN DEFAULT TRUE,
    default_value   STRING,
    dropdown_source STRING,                   -- JSON: {type,values} or {type,schema,table,value_column}
    is_active       BOOLEAN DEFAULT TRUE
) USING DELTA;

-- 3. Business rules
--    Config-driven validation — no code changes to add rules.
--    rule_type: allowed_values | required_if | date_order | readonly |
--               required_edit | conditional_check | regex | min_length |
--               max_length | min_value | max_value | lookup |
--               starts_with | ends_with | contains | readonly_after_insert
CREATE TABLE IF NOT EXISTS your_catalog.your_schema.dataeditor_business_rules (
    rule_id         BIGINT,
    schema_name     STRING  NOT NULL,
    table_name      STRING  NOT NULL,
    column_name     STRING,
    rule_type       STRING  NOT NULL,
    rule_params     STRING,                   -- JSON parameters for the rule
    severity        STRING  DEFAULT 'blocking', -- blocking | warning
    error_message   STRING,
    fix_hint        STRING,
    is_active       BOOLEAN DEFAULT TRUE
) USING DELTA;

-- 4. Audit log
--    Column-level change history — one row per changed column per save.
CREATE TABLE IF NOT EXISTS your_catalog.your_schema.dataeditor_app_audit_log (
    changed_by          STRING,
    changed_at          TIMESTAMP,
    table_schema        STRING,
    table_name          STRING,
    record_key          STRING,               -- JSON: {"id": "ABC"}
    column_name         STRING,
    old_value           STRING,
    new_value           STRING,
    change_source       STRING DEFAULT 'DATA_EDITOR',
    change_request_id   STRING                -- FK to dataeditor_change_requests
) USING DELTA;

-- 5. Change requests
--    Tracks bulk upload / export jobs through staging → validate → apply.
--    v1: auto-approve (requires_approval = FALSE).
--    Phase 2: set requires_approval = TRUE for sensitive tables.
CREATE TABLE IF NOT EXISTS your_catalog.your_schema.dataeditor_change_requests (
    change_request_id       STRING    NOT NULL COMMENT 'UUID — logical primary key',
    status                  STRING    NOT NULL COMMENT 'draft|validated|pending_approval|approved|rejected|applied|failed|expired',
    request_type            STRING    NOT NULL COMMENT 'upload|export',
    mode                    STRING    COMMENT 'update|append|overwrite (upload); NULL for export',

    catalog                 STRING    DEFAULT 'your_catalog',
    schema_name             STRING    NOT NULL,
    table_name              STRING    NOT NULL,

    submitted_by            STRING,
    submitted_at            TIMESTAMP,
    validated_at            TIMESTAMP,
    applied_at              TIMESTAMP,
    updated_at              TIMESTAMP,

    -- UC Volume paths (set STAGING_VOLUME_PATH / EXPORT_VOLUME_PATH in app.yaml)
    source_file_volume_path STRING    COMMENT 'uploads/{change_request_id}/file.csv',
    staging_table_name      STRING    COMMENT 'e.g. your_catalog.your_schema.entity_app_stage',
    export_volume_path      STRING    COMMENT 'exports/{change_request_id}/file.csv',

    row_count               INT,
    validation_summary      STRING    COMMENT 'JSON: parse/schema/rule check results',
    change_summary          STRING    COMMENT 'JSON: before/after stats for approver UI',
    errors_json             STRING    COMMENT 'JSON: validation errors',
    filter_snapshot         STRING    COMMENT 'JSON: column filters active at submit time',
    failure_reason          STRING,

    -- Approval workflow (Phase 2 — leave defaults in Phase 1)
    requires_approval       BOOLEAN   DEFAULT FALSE,
    approver_emails         STRING    COMMENT 'Comma-separated approver emails',
    approval_status         STRING    COMMENT 'pending|approved|rejected',
    approved_by             STRING,
    approved_at             TIMESTAMP,
    rejected_by             STRING,
    rejected_at             TIMESTAMP,
    rejection_reason        STRING,
    expires_at              TIMESTAMP COMMENT 'Pending request expiry',
    approval_token_hash     STRING    COMMENT 'Hashed token for email approval link'
) USING DELTA;

-- ============================================================
-- Sample: onboard one table
-- ============================================================
-- Replace schema, table, and column names with your own.
-- Then run: python scripts/deploy_config.py --all

-- Register the table
INSERT INTO your_catalog.your_schema.dataeditor_table_registry
    (schema_name, table_name, display_name, description,
     primary_keys, default_where, catalog, row_limit,
     allow_insert, allow_update, allow_delete, is_active, app_group)
VALUES
    ('your_schema', 'sample_entity',
     'Sample Entity', 'Example reference table for the data editor',
     'record_id', 'is_active = TRUE',
     'your_catalog', 500, TRUE, TRUE, FALSE, TRUE, 'General');

-- Define columns
INSERT INTO your_catalog.your_schema.dataeditor_column_config
    (schema_name, table_name, column_name, display_label,
     col_order, column_type, visible, editable, mandatory,
     is_filter, is_primary_key, is_active)
VALUES
    ('your_schema', 'sample_entity', 'record_id',   'Record ID',   1, 'text',    TRUE, FALSE, TRUE,  TRUE,  TRUE,  TRUE),
    ('your_schema', 'sample_entity', 'name',        'Name',        2, 'text',    TRUE, TRUE,  TRUE,  TRUE,  FALSE, TRUE),
    ('your_schema', 'sample_entity', 'entity_type', 'Type',        3, 'dropdown',TRUE, TRUE,  TRUE,  TRUE,  FALSE, TRUE),
    ('your_schema', 'sample_entity', 'status',      'Status',      4, 'dropdown',TRUE, TRUE,  TRUE,  TRUE,  FALSE, TRUE),
    ('your_schema', 'sample_entity', 'is_active',   'Active?',     5, 'boolean', TRUE, TRUE,  FALSE, TRUE,  FALSE, TRUE),
    ('your_schema', 'sample_entity', 'notes',       'Notes',       6, 'text',    TRUE, TRUE,  FALSE, FALSE, FALSE, TRUE);

-- Sample business rule: name is required when record is active
INSERT INTO your_catalog.your_schema.dataeditor_business_rules
    (rule_id, schema_name, table_name, column_name, rule_type,
     rule_params, severity, error_message, is_active)
VALUES
    (1, 'your_schema', 'sample_entity', 'name', 'required_if',
     '{"condition_col": "is_active", "condition_val": "true"}',
     'blocking', 'Name is required when the record is active.', TRUE);
