-- =============================================================================
-- Step 1: Reset Data Manager tables (run in Databricks SQL)
-- =============================================================================
-- Then run: scripts/setup_config_tables.sql
-- Then run: python scripts/deploy_config.py --all
-- =============================================================================

-- Obsolete tables from earlier iterations
DROP TABLE IF EXISTS your_catalog.dmz.dataeditor_edit_policy;
DROP TABLE IF EXISTS your_catalog.dmz.dataeditor_change_request_lines;
DROP TABLE IF EXISTS your_catalog.dmz.dataeditor_revision_log;
DROP TABLE IF EXISTS your_catalog.dmz.dataeditor_notification_outbox;

-- Config tables (reloaded from Git YAML)
DROP TABLE IF EXISTS your_catalog.dmz.dataeditor_business_rules;
DROP TABLE IF EXISTS your_catalog.dmz.dataeditor_column_config;
DROP TABLE IF EXISTS your_catalog.dmz.dataeditor_table_registry;

-- Operational tables (recreated empty by setup script)
DROP TABLE IF EXISTS your_catalog.dmz.dataeditor_change_requests;
DROP TABLE IF EXISTS your_catalog.dmz.dataeditor_app_audit_log;
