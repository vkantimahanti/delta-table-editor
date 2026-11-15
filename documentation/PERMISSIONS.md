# Permissions & Identity Guide

This document is the authoritative reference for every permission step required
to deploy and operate the Managed Data Services (MDS) Delta Table Editor on
Databricks. It reflects the production codebase exactly, including the hybrid
SQL execution identity model, Azure AD group-based access, OAuth consent flow,
and Databricks Asset Bundle (DAB) deployment.

---

## Table of Contents

1. [Architecture — Hybrid SQL Execution Identity](#1-architecture--hybrid-sql-execution-identity)
2. [Azure AD Group Model](#2-azure-ad-group-model)
3. [Permission Layers (complete checklist)](#3-permission-layers-complete-checklist)
   - 3a. Unity Catalog — App Service Principal
   - 3b. Unity Catalog — User Groups
   - 3c. Databricks App — CAN_USE / CAN_MANAGE
   - 3d. SQL Warehouse — CAN_USE
   - 3e. OAuth Consent — `user_api_scopes`
4. [Environment-Specific Service Principals (DAB)](#4-environment-specific-service-principals-dab)
5. [How Dynamic Table Visibility Works](#5-how-dynamic-table-visibility-works)
6. [Local Development vs. Production](#6-local-development-vs-production)
7. [Deployment Workflow (DAB)](#7-deployment-workflow-dab)
8. [Troubleshooting — Symptoms by Missing Permission](#8-troubleshooting--symptoms-by-missing-permission)

---

## 1. Architecture — Hybrid SQL Execution Identity

The app runs with `SQL_EXECUTION_IDENTITY=hybrid`. This is not a toggle — it
is the core security design of the production deployment.

```
Browser (Azure AD / Entra ID SSO)
    │
    │  OAuth token (user identity)
    ▼
Databricks Apps runtime
    │  Injects X-Forwarded-Access-Token header on every HTTP request
    ▼
FastAPI backend (auth_helpers.py)
    │
    ├─── READ path (SELECT)
    │        Token forwarded to SQL Warehouse
    │        Unity Catalog evaluates grants for the REAL END USER
    │        Tables the user cannot SELECT are silently hidden
    │
    └─── WRITE path (INSERT / UPDATE / DELETE / audit log / change requests)
             App Service Principal credentials used
             Unity Catalog evaluates grants for the APP SP
             User identity is captured in the data (changed_by column)
```

**Why hybrid?**  
Reads enforce Unity Catalog group grants per user — each user sees only the
tables and rows their AD group membership permits. Writes are executed under
the app SP so that the app can always write audit records and change-request
entries even when the end user has no MODIFY grant on those system tables.

**Implementation reference:** `backend/shared/auth_helpers.py` —
`sql_execution_mode()`, `use_hybrid_sql_identity()`,
`default_read_sql_identity()`, `default_write_sql_identity()`.

---

## 2. Azure AD Group Model

Access to business tables is managed exclusively through Azure AD groups
synced to Databricks Unity Catalog via SCIM. **No per-user grants are made.**
A user gets access to a table only by being a member of the appropriate AD
group.

### Group naming convention

Establish a consistent, descriptive naming scheme for your AD groups. A
recommended pattern:

```
<PREFIX>_<DOMAIN>_<ROLE>
```

Where:
- `PREFIX` — your organization's standard group prefix (e.g., `GRP`, `AZ`, `SG`)
- `DOMAIN` — the business domain or data domain (e.g., `FINANCE`, `CLINICAL`, `OPS`)
- `ROLE` — the access level (e.g., `READERS`, `EDITORS`, `ADMINS`)

Example groups following this pattern:

| Group | Access level |
|---|---|
| `GRP_FINANCE_READERS` | SELECT on finance schemas |
| `GRP_FINANCE_EDITORS` | SELECT + MODIFY on finance schemas |
| `GRP_OPS_READERS` | SELECT on operations schemas |
| `GRP_PLATFORM_ADMINS` | CAN_MANAGE on the App |

Adapt the prefix and naming to your organization's existing Active Directory
conventions. The app enforces no particular naming scheme — any valid AD group
name works in SQL GRANT statements.

### Multi-group membership model

A single user can belong to more than one group, and each group can carry
different access levels for different tables or schemas. This is intentional:

```
User: analyst@your-org.com
  ├─ GRP_FINANCE_READERS
  │     → SELECT on catalog.finance_reports
  │     → SELECT on catalog.finance_lookup
  │
  └─ GRP_OPS_EDITORS
        → SELECT + MODIFY on catalog.operations
```

The effective permission for any given table is the **union** of all grants
the user's groups hold for that table. If any group grants MODIFY, the user
can edit.

### SCIM sync requirement

Groups must exist in the Databricks Unity Catalog metastore before SQL GRANT
statements targeting them will succeed. SCIM sync pushes Azure AD group
membership changes into Databricks automatically; no manual Databricks user
management is needed once SCIM is configured.

---

## 3. Permission Layers (complete checklist)

Five distinct permission layers must all be in place for the app to work
correctly. Missing any single layer produces a specific, diagnosable failure.

---

### 3a. Unity Catalog — App Service Principal

The App SP needs grants to read config/registry tables, write audit and
change-request records, and read source schemas.

Run the appropriate script from `scripts/onetime_sql_permissions/` as a
metastore admin. The scripts are environment-specific — use the dev script
in dev and the prod script in prod.

**What to grant (reference: `grant_mds_app_service_principal_<env>.sql`):**

```sql
-- ── Catalog-level (required before any schema grants)
GRANT USE CATALOG ON CATALOG <your_catalog>
  TO `<app-sp-application-id>`;

-- ── Schemas where the SP needs SELECT + MODIFY
--    (config tables, audit log, change requests, staging data)
GRANT USE SCHEMA ON SCHEMA <your_catalog>.<config_schema>      TO `<app-sp-application-id>`;
GRANT SELECT, MODIFY ON SCHEMA <your_catalog>.<config_schema>  TO `<app-sp-application-id>`;

GRANT USE SCHEMA ON SCHEMA <your_catalog>.<editable_schema_1>       TO `<app-sp-application-id>`;
GRANT SELECT, MODIFY ON SCHEMA <your_catalog>.<editable_schema_1>   TO `<app-sp-application-id>`;

-- Repeat for each schema the SP needs to read or write
-- ...

-- ── Schema where the SP needs SELECT only (read-only source data)
GRANT USE SCHEMA ON SCHEMA <your_catalog>.<readonly_schema>  TO `<app-sp-application-id>`;
GRANT SELECT ON SCHEMA <your_catalog>.<readonly_schema>      TO `<app-sp-application-id>`;
```

> **Schema-level vs. table-level grants:**  
> The production scripts use schema-level grants (`ON SCHEMA …`) rather than
> per-table grants. This ensures coverage of tables added to a schema after
> the initial grant. If your security policy requires tighter scoping,
> replace schema-level grants with explicit per-table grants — the app will
> continue to work; only new tables in that schema will not be automatically
> covered.

---

### 3b. Unity Catalog — User Groups

User groups need grants on the schemas (or tables) they will read and
optionally modify through the editor. The app never grants these — they
must be provisioned separately by a metastore admin.

**Reference script:** `grant_mds_analyst_select_<domain>_<env>.sql`

```sql
-- ── Read-only group: SELECT on one or more schemas
GRANT USE CATALOG ON CATALOG <your_catalog>
  TO `<your-readonly-group>`;

GRANT USE SCHEMA ON SCHEMA <your_catalog>.<schema_1>
  TO `<your-readonly-group>`;
GRANT SELECT ON SCHEMA <your_catalog>.<schema_1>
  TO `<your-readonly-group>`;

-- Repeat for each schema the read-only group covers
GRANT USE SCHEMA ON SCHEMA <your_catalog>.<schema_2>
  TO `<your-readonly-group>`;
GRANT SELECT ON SCHEMA <your_catalog>.<schema_2>
  TO `<your-readonly-group>`;
```

For a group that also needs write access, add `MODIFY` alongside `SELECT`:

```sql
GRANT USE CATALOG ON CATALOG <your_catalog>
  TO `<your-editor-group>`;

GRANT USE SCHEMA ON SCHEMA <your_catalog>.<editable_schema>
  TO `<your-editor-group>`;
GRANT SELECT, MODIFY ON SCHEMA <your_catalog>.<editable_schema>
  TO `<your-editor-group>`;
```

The user group grant controls what the user can see and do. The app SP grant
(§ 3a) is independent and only controls what the app system-infrastructure
can do (audit writes, config reads).

---

### 3c. Databricks App — CAN_USE / CAN_MANAGE

Every user or group that needs to open the App URL must be granted access on
the App object itself. This is separate from Unity Catalog and from warehouse
permissions.

**Configured via:** `bundle/resources/manageddataservice/manageddataservice.yml`
under the `permissions` key, populated from `${var.app_permissions}` per
environment.

```yaml
# manageddataservice.yml (excerpt)
permissions: ${var.app_permissions}
```

**Typical permission assignments:**

| Principal | Permission | Purpose |
|---|---|---|
| `<your-platform-admins-group>` | CAN_MANAGE | Platform team can restart/redeploy |
| `<your-analysts-group>` | CAN_USE | Analysts can open the app |
| App SP (all envs) | CAN_MANAGE | Required for the SP to manage the app process |

A user or group without at least `CAN_USE` on the App object will receive a
403 or be redirected away when navigating to the app URL — Unity Catalog
grants alone are not sufficient to open the app.

---

### 3d. SQL Warehouse — CAN_USE

The user group must be granted `CAN_USE` on the specific SQL Warehouse
bound to the app. The app binds the warehouse by resource reference:

```yaml
# manageddataservice.yml (excerpt)
resources:
  - name: sql-warehouse
    sql_warehouse:
      id: ${var.warehouse_id}
      permission: CAN_USE
```

The `${var.warehouse_id}` is resolved per environment from
`bundle/vars/manageddataservice/target_vars_<env>.yml` (see § 4).

Without `CAN_USE` on the warehouse, the user's SQL Warehouse connection
attempt fails with an authentication or authorization error even if all
Unity Catalog grants are correct.

---

### 3e. OAuth Consent — `user_api_scopes`

This is the most commonly missed permission step in new deployments.

The app declares the following OAuth scopes in its resource definition:

```yaml
# manageddataservice.yml (excerpt)
user_api_scopes:
  - sql
  - catalog.catalogs:read
  - catalog.schemas:read
  - catalog.tables:read
```

When a user opens the app for the **first time**, Databricks displays an
OAuth consent screen asking them to grant these scopes. The user must click
**Allow** before the backend receives a valid `X-Forwarded-Access-Token`.

**The `sql` scope is mandatory.** Without it:

- The backend receives no user token (or a token without the `sql` scope).
- In hybrid mode, all read queries fail because the user token cannot
  authenticate to the SQL Warehouse.
- The backend logs a warning indicating the missing scope and the user
  sees permission errors on every table load.

**If a user skips the consent screen or the scope was added after first login:**

1. Stop the Databricks App.
2. Restart the App.
3. Have the user sign out of Databricks (and SSO if applicable).
4. Have the user re-open the app URL.
5. The consent screen will reappear — the user must click Allow.

**Reference:** `auth_helpers.py` → `resolve_user_token()`,
`sql_auth_hint()`, `jwt_scopes()`.

---

## 4. Environment-Specific Service Principals (DAB)

Each deployment environment uses a distinct App Service Principal. The SP
application ID is set in the per-environment variable file and injected into
the app bundle at deploy time.

| Environment | Variable file | Key variables |
|---|---|---|
| dev | `bundle/vars/manageddataservice/target_vars_dev.yml` | `sp_application_id`, `warehouse_id` |
| qa | `bundle/vars/manageddataservice/target_vars_qa.yml` | `sp_application_id`, `warehouse_id` |
| stg | `bundle/vars/manageddataservice/target_vars_stg.yml` | `sp_application_id`, `warehouse_id` |
| prod | `bundle/vars/manageddataservice/target_vars_prod.yml` | `sp_application_id`, `warehouse_id` |

Each variable file sets the SP application ID and warehouse ID for that
environment. Use the SP ID from the target environment's variable file when
writing SQL GRANT statements — granting the prod SP in dev (or vice versa)
has no effect in the intended environment.

---

## 5. How Dynamic Table Visibility Works

Under hybrid mode, the navigation dropdown does **not** show all registered
tables. It shows only the tables the currently logged-in user can SELECT.

**Implementation:** `backend/shared/table_access.py` →
`filter_registry_rows()` and `user_can_select_table()`.

```python
def filter_registry_rows(records, user_token):
    """Drop registry entries for tables the user cannot SELECT (hybrid mode)."""
    if not auth_helpers.use_hybrid_sql_identity():
        return records
    out = []
    for row in records:
        cat    = str(row.get("catalog") or db_client.CATALOG).strip()
        schema = str(row.get("table_schema") or row.get("schema_name") or "").strip()
        name   = str(row.get("table_name") or "").strip()
        if not schema or not name:
            continue
        if user_can_select_table(cat, schema, name, user_token):
            out.append(row)
    return out
```

For each table in the registry, the backend issues a probe query:

```sql
SELECT 1 FROM <catalog>.<schema>.<table> LIMIT 0
```

using the forwarded user token. If Unity Catalog denies the query (the user's
groups have no SELECT grant), the table is silently dropped from the response.
The user never sees an error — the table simply does not appear.

**Result:** A user in `G_Azure_RBAC_CMS_READONLY` will see only CMS tables.
A user who is also in `G_Azure_RBAC_DMZ_EDITORS` will additionally see DMZ
tables. No app-level access control list is maintained — the source of truth
is Unity Catalog group membership.

**Caching:** Results are cached per request to avoid repeated warehouse
round-trips during a single page load. The cache does not persist across
requests — group membership changes are reflected on the user's next page
load.

---

## 6. Local Development vs. Production

| | Local development | Databricks Apps (production) |
|---|---|---|
| SQL execution mode | Determined by `DATABRICKS_CLIENT_ID` env var presence | `SQL_EXECUTION_IDENTITY=hybrid` (set in app.yaml) |
| User token source | `DATABRICKS_TOKEN` PAT from `.env` | `X-Forwarded-Access-Token` injected by Apps runtime |
| Identity presented to UC | PAT owner (the developer) | The logged-in end user |
| UC enforcement | Applies as the PAT owner's grants | Applies as the real end user's grants |
| SP required | No | Yes — per-environment SP for writes and infrastructure |
| Table visibility filter | Runs against developer's grants | Runs against the end user's group grants |
| `user_api_scopes` consent | Not applicable | Required on first App login |

> **Local dev tip:** Set `DATABRICKS_TOKEN` to your own personal access token,
> not a service principal token. This ensures Unity Catalog enforces the same
> group-based grants locally as in production. The `.env.example` file
> documents this convention.

---

## 7. Deployment Workflow (DAB)

The app is deployed using Databricks Asset Bundles. The bundle definition
lives in `bundle/databricks.yml` with per-environment variable overrides.

**Deploy to dev:**

```bash
databricks bundle deploy --target dev
```

**Deploy to production:**

```bash
databricks bundle deploy --target prod
```

**What the bundle deploy does:**

1. Resolves all `${var.*}` references from the target's variable file.
2. Creates or updates the Databricks App resource with the correct SP,
   warehouse binding, `user_api_scopes`, and `app_permissions`.
3. Uploads the app source (backend + static frontend build).
4. Applies the declared `permissions` block to the App object.

**After a first-time deploy, run the one-time SQL scripts manually** (in
order) in the target workspace using a metastore admin account:

```
scripts/onetime_sql_permissions/grant_mds_app_service_principal_<env>.sql
scripts/onetime_sql_permissions/grant_mds_analyst_select_cms_<env>.sql
```

These scripts are idempotent — they can be re-run safely if grants need to
be refreshed.

---

## 8. Troubleshooting — Symptoms by Missing Permission

| Missing permission | Observed symptom | Fix |
|---|---|---|
| Group has no `SELECT` on a source table | Table does not appear in the nav dropdown | GRANT USE SCHEMA + SELECT to the group on that schema (§ 3b) |
| Group has `SELECT` but no `MODIFY` on a source table | Table appears, rows load, but Save is rejected with UC permission error | GRANT MODIFY to the editor group on that schema (§ 3b) |
| App SP missing `SELECT` on config schema | App loads but table dropdown is empty; backend logs SQL 403 reading `dataeditor_table_registry` | GRANT SELECT on config schemas to the SP (§ 3a) |
| App SP missing `MODIFY` on audit schema | Saves appear to succeed but audit rows are not written; server logs 500 on audit write | GRANT MODIFY on audit/change-request schema to the SP (§ 3a) |
| User group missing `CAN_USE` on App object | User gets 403 or is redirected when opening the App URL | Add group to `app_permissions` in `target_vars_<env>.yml` and redeploy (§ 3c) |
| User group missing `CAN_USE` on SQL Warehouse | All SQL calls fail; user sees "warehouse unavailable" or connection error | Grant `CAN_USE` on the warehouse to the group (§ 3d) |
| User did not grant `sql` OAuth scope | All table reads fail with token error; nav shows no tables; backend logs "no x-forwarded-access-token" | Stop app, restart, user signs out and re-opens (§ 3e) |
| `sql` scope missing from `user_api_scopes` in app resource | Same as above but for all users | Add `sql` to `user_api_scopes` in `manageddataservice.yml` and redeploy |
| Wrong env SP in GRANT statements | SP grants exist but app writes fail in that environment | Re-run SP grant script using the correct SP ID for the target env (§ 4) |
| Group not yet synced via SCIM | GRANT to group succeeds but user still cannot SELECT | Wait for SCIM sync cycle or trigger a manual sync in Azure AD |
| App not restarted after scope change | Consent screen does not reappear for existing users | Stop and restart the App to force new OAuth token issuance (§ 3e) |

---

*Last updated: 2026-11 · Reflects production codebase `bundle/` and `backend/shared/` as deployed.*
