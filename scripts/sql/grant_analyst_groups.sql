-- ============================================================
-- Delta Table Editor — User group grants
-- ============================================================
-- Run once per environment as a metastore admin.
-- Re-running is safe (grants are idempotent).
--
-- Replace:
--   <your_catalog>         → your Unity Catalog name
--   <your-readonly-group>  → AD group for read-only users
--   <your-editor-group>    → AD group for users who can edit
--   <schema_N>             → schema(s) each group should access
--
-- Groups are synced from Azure AD / Entra ID via SCIM.
-- No per-user grants are needed — users inherit access from
-- their AD group membership.
--
-- ACCESS MODEL:
--   READ-ONLY  → SELECT on schema → table appears in nav, grid is read-only
--   EDITOR     → SELECT + MODIFY  → table appears in nav, edits are allowed
--
-- A user can belong to multiple groups with different access levels
-- on different schemas. The app enforces this automatically via
-- per-user token passthrough (hybrid SQL execution identity).
-- ============================================================

-- ── Read-only group: SELECT on one or more schemas ───────────────

GRANT USE CATALOG ON CATALOG <your_catalog>
  TO `<your-readonly-group>`;

GRANT USE SCHEMA ON SCHEMA <your_catalog>.<schema_1>
  TO `<your-readonly-group>`;
GRANT SELECT ON SCHEMA <your_catalog>.<schema_1>
  TO `<your-readonly-group>`;

-- Repeat for additional schemas this read-only group covers:
-- GRANT USE SCHEMA ON SCHEMA <your_catalog>.<schema_2>  TO `<your-readonly-group>`;
-- GRANT SELECT ON SCHEMA <your_catalog>.<schema_2>       TO `<your-readonly-group>`;


-- ── Editor group: SELECT + MODIFY on editable schemas ────────────

GRANT USE CATALOG ON CATALOG <your_catalog>
  TO `<your-editor-group>`;

GRANT USE SCHEMA ON SCHEMA <your_catalog>.<editable_schema>
  TO `<your-editor-group>`;
GRANT SELECT, MODIFY ON SCHEMA <your_catalog>.<editable_schema>
  TO `<your-editor-group>`;

-- Repeat for additional schemas this editor group can modify:
-- GRANT USE SCHEMA ON SCHEMA <your_catalog>.<editable_schema_2>       TO `<your-editor-group>`;
-- GRANT SELECT, MODIFY ON SCHEMA <your_catalog>.<editable_schema_2>   TO `<your-editor-group>`;


-- ── SQL Warehouse access (required for hybrid mode reads) ─────────
-- Grant CAN_USE on the warehouse in the Databricks UI:
--   SQL Warehouses → <your warehouse> → Permissions → Add group → Can use
--
-- Or via CLI:
--   databricks permissions set sql-warehouses <warehouse_id> \
--     --json '{"access_control_list": [{"group_name": "<your-readonly-group>", "permission_level": "CAN_USE"}]}'
