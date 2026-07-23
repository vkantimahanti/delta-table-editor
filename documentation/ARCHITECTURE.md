# Architecture Decision Records

## ADR-001 — Databricks Apps over standalone hosting

**Decision:** Deploy as a Databricks App, not a standalone container or AKS service.

**Reason:** Databricks Apps injects `X-Forwarded-Access-Token` per request, giving
every SQL call the logged-in user's identity without any token management code.
Unity Catalog row filters and column masks apply automatically. A standalone deployment
would require a service principal with broad permissions — weakening the security model.

**Trade-off:** App compute is bound to Databricks pricing. Acceptable for internal
tooling used during business hours with idle auto-stop enabled.

---

## ADR-002 — Config in Delta tables, not application database

**Decision:** Store table registry, column config, and business rules in Delta tables
(`dataeditor_table_registry`, `dataeditor_column_config`, `dataeditor_business_rules`).

**Reason:** Config lives where the data lives — in the same catalog, governed by the
same Unity Catalog grants, time-travel auditable. No separate database to manage.
YAML files are the source of truth; Delta tables are the deployed state.

**Trade-off:** Config reads hit the SQL Warehouse. Mitigated by 5-minute in-memory
cache for rules and column definitions.

---

## ADR-003 — Per-user token, not connection pool

**Decision:** Open one Databricks SQL connection per HTTP request using the forwarded
user token. No shared connection pool.

**Reason:** Databricks SQL connections are tied to a token. Pooling connections across
users would mix identities. Tokens expire (~1 hour), making long-lived pooled connections
unreliable. A `Semaphore(40)` limits concurrent warehouse connections instead.

**Trade-off:** Connection overhead per request (~50ms on warm warehouse). Acceptable
given the interactive (not high-throughput) nature of the app.

---

## ADR-004 — Staging table for bulk operations

**Decision:** Bulk CSV uploads stage to a temporary Delta table before applying to
the target. Validation runs against the staging table. Apply is all-or-nothing.

**Reason:** Partial failures on large uploads are worse than full failures. A 5,000-row
upload where 4,800 rows apply and 200 fail leaves the table in an unknown state.
Staging makes the intermediate state inspectable and the apply atomic.

**Trade-off:** Two Delta writes per bulk operation (stage + apply). Worth it for safety
and the natural fit with the approval workflow.

---

## ADR-005 — Optimistic locking via version column

**Decision:** Every target table has a `version BIGINT` column. UPDATE statements check
`WHERE pk = ? AND version = ?` and increment `version` in the same statement.

**Reason:** Pessimistic locking (row locks) is complex to implement across HTTP
requests and fails badly if a user closes their browser. Optimistic locking requires
no lock table and handles the common case (no conflict) with zero overhead.

**Trade-off:** Users occasionally see a conflict error if two people edit the same row
within seconds. The error message is clear: "Refresh and retry."

---

## Security model

```
Identity flow:
  Azure AD / Entra ID
       │  SSO login
       ▼
  Databricks Workspace
       │  SCIM sync → groups
       ▼
  Databricks Apps
       │  X-Forwarded-Access-Token per request
       ▼
  FastAPI backend
       │  Passes token to every SQL call
       ▼
  SQL Warehouse
       │  Executes as the logged-in user
       ▼
  Unity Catalog
       │  Table grants per AD group
       │  Row-level filters (optional)
       │  Column masks (optional)
       ▼
  Delta Tables
```

No data access bypasses Unity Catalog. The app has no privileged service account for
data reads or writes. Audit rows are written using the same user token — so `changed_by`
is always the real person, not a system account.

---

## Data flow for a single cell edit

```
1. User edits a cell in the React grid
2. User clicks "Review & Save"
3. Frontend shows diff panel: old → new per column
4. User confirms
5. Frontend POST /api/tables/{schema}/{table}/row
6. FastAPI validates:
   a. Mandatory fields
   b. Business rules (blocking)
   c. Business rules (warnings — shown but don't block)
7. FastAPI builds parameterized UPDATE:
      SET changed_col = ?,
          version = version + 1,
          updated_by = ?,
          updated_at = current_timestamp()
      WHERE pk_col = ?
      AND version = ?
8. Databricks SQL executes (as the user)
9. Unity Catalog enforces grants
10. FastAPI writes audit rows (one per changed column)
11. Frontend shows success / conflict / error
```

---

## Data flow for a bulk CSV upload

```
1. User selects file and upload mode (update / append / overwrite)
2. Frontend POST /api/upload with CSV payload
3. FastAPI:
   a. Parses CSV into pandas DataFrame
   b. Validates schema (column names match target)
   c. Creates change_request record (status = draft)
   d. Stages DataFrame to {target}_app_stage Delta table
   e. Runs all business rules against staged rows
   f. If errors: returns error report, deletes staging table
   g. If clean: applies to target (UPDATE by PK / INSERT / truncate+insert)
   h. Writes audit rows linked to change_request_id
   i. Updates change_request status = applied
4. Frontend shows summary: N updated, N inserted, N failed
```

---

## Capacity planning

| Metric | Value | Notes |
|---|---|---|
| Concurrent users | ~40 | Semaphore limit matches Medium App RAM |
| Rows per fetch | 500 default, 10,000 max | Configurable per table in registry |
| Bulk upload max | 10,000 rows / 10 MB | Configurable in app.yaml |
| Export max | 10,000 rows | Streaming — FastAPI RAM stays flat |
| Rule cache TTL | 5 minutes | Module-level dict; clears on admin refresh |
| Connection overhead | ~50ms | Warm warehouse; ~3-5 min cold start |
| Idle auto-stop | 10 minutes | Configurable — saves compute cost |
