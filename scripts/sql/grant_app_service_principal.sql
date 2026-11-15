-- ============================================================
-- Delta Table Editor — App Service Principal grants
-- ============================================================
-- Run once per environment as a metastore admin AFTER deploying
-- the app bundle. Re-running is safe (grants are idempotent).
--
-- Replace:
--   <your_catalog>   → your Unity Catalog name
--   <config_schema>  → schema holding dataeditor_* tables
--   <sp_app_id>      → App Service Principal application (client) ID
--                      from bundle/vars/<env>.yml → sp_application_id
--
-- The SP needs:
--   SELECT + MODIFY on the config/audit schema  (writes audit log, change requests)
--   SELECT + MODIFY on each editable schema     (executes user DML via hybrid mode)
--   SELECT only     on read-only source schemas (registry lookups)
-- ============================================================

-- ── 1. Catalog access (required before any schema grants)
GRANT USE CATALOG ON CATALOG <your_catalog>
  TO `<sp_app_id>`;

-- ── 2. Config / audit schema — SP reads config and writes audit records
GRANT USE SCHEMA ON SCHEMA <your_catalog>.<config_schema>
  TO `<sp_app_id>`;
GRANT SELECT, MODIFY ON SCHEMA <your_catalog>.<config_schema>
  TO `<sp_app_id>`;

-- ── 3. Editable business schemas — repeat for each schema users can edit
--      (SP executes INSERT/UPDATE/DELETE on behalf of users in hybrid mode)
-- GRANT USE SCHEMA ON SCHEMA <your_catalog>.<editable_schema_1>  TO `<sp_app_id>`;
-- GRANT SELECT, MODIFY ON SCHEMA <your_catalog>.<editable_schema_1>  TO `<sp_app_id>`;

-- GRANT USE SCHEMA ON SCHEMA <your_catalog>.<editable_schema_2>  TO `<sp_app_id>`;
-- GRANT SELECT, MODIFY ON SCHEMA <your_catalog>.<editable_schema_2>  TO `<sp_app_id>`;

-- ── 4. Read-only source schemas — SP reads for registry / lookup resolution
-- GRANT USE SCHEMA ON SCHEMA <your_catalog>.<readonly_schema>  TO `<sp_app_id>`;
-- GRANT SELECT ON SCHEMA <your_catalog>.<readonly_schema>       TO `<sp_app_id>`;
