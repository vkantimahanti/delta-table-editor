# Building a Production-Grade Delta Table Editor on Databricks Apps

## How I replaced SQL patch scripts and Excel corrections with a self-service,
## governed data editing platform — using FastAPI, React, and Unity Catalog.

---

## The Problem Every Data Team Knows

Picture this: a business user spots an error in a reference table. They email a data
engineer. The engineer opens a Databricks notebook, writes a one-off UPDATE, runs it,
closes the notebook, and moves on. No validation. No audit trail. No review.

This pattern is everywhere in data platforms — and it breaks down fast when:
- The same table gets ten correction requests a week
- An engineer runs the wrong WHERE clause and corrupts 500 rows
- An auditor asks "who changed this value and when?" and the answer is "no idea"
- A new engineer joins and doesn't know which notebook to use

I built **Delta Table Editor** to solve this. It's a config-driven, self-service UI
that runs inside Databricks Apps — no new SaaS, no new infrastructure, no new security
boundary to manage.

---

## Architecture Overview

```
                        Azure AD / Entra ID
                     SSO | SCIM | Group Membership
                     X-Forwarded-Access-Token
                              │
              ┌───────────────▼────────────────┐
              │         DATABRICKS APPS         │
              │                                │
              │  ┌─────────────┐               │
              │  │    React    │  HTTP/JSON    │
              │  │  Frontend   │──────────────▶│
              │  │  (Vite)     │               │
              │  └─────────────┘               │
              │                                │
              │  ┌──────────────────────────┐  │
              │  │     FastAPI Backend       │  │
              │  │                          │  │
              │  │  Token extraction        │  │
              │  │  Config-driven routing   │  │──▶ SQL Warehouse
              │  │  Business rule engine    │  │       │
              │  │  CRUD + optimistic lock  │  │    Unity Catalog
              │  │  Audit log writes        │  │       │
              │  └──────────────────────────┘  │   Business Delta
              │                                │     Tables
              │  ┌──────────────────────────┐  │
              │  │   Config Delta Tables     │  │
              │  │                          │  │
              │  │  dataeditor_table_registry│  │
              │  │  dataeditor_column_config │  │
              │  │  dataeditor_business_rules│  │
              │  └──────────────────────────┘  │
              │                                │
              │  ┌──────────────────────────┐  │
              │  │      Audit Log            │  │
              │  │  Column-level history     │  │
              │  │  Real user identity       │  │
              │  └──────────────────────────┘  │
              └────────────────────────────────┘
```

Every component runs inside the Databricks workspace. The app surfaces Delta tables
through a governed UI without exposing the platform itself.

---

## The Three Core Design Principles

### 1. Never bypass Unity Catalog

The most common shortcut in internal tooling is a shared service account with broad
permissions. It's easy to set up and impossible to audit.

Every SQL query in Delta Table Editor runs under the **logged-in user's own token**,
forwarded by Databricks Apps via the `X-Forwarded-Access-Token` header:

```python
# db_client.py
def _resolve_access_token(user_token: str | None) -> str:
    if user_token:
        return user_token          # ← user's token, not service principal
    pat = os.environ.get("DATABRICKS_TOKEN", "").strip()
    if pat:
        return pat                 # ← local dev fallback only
```

Unity Catalog row-level filters and column masks apply automatically. If a user doesn't
have `SELECT` on a table, the query fails — exactly as it should.

### 2. Config-driven — no code per table

Early versions of tools like this hardcode table schemas in Python. Adding a new table
means a code change, a PR, a deploy. That's too slow for a platform serving many teams.

In Delta Table Editor, every table is described in a YAML file:

```yaml
table:
  schema:       your_schema
  name:         sample_entity
  primary_keys: [record_id]
  group:        General
  allow_insert: true
  allow_update: true
  allow_delete: false

columns:
  - column_name:  status
    column_type:  dropdown
    mandatory:    true
    dropdown:
      type:   fixed
      values: [ACTIVE, INACTIVE, PENDING]
    rules:
      - type:     allowed_values
        on_fail:  block
        reason:   "Status must be ACTIVE, INACTIVE or PENDING."
```

The YAML is deployed to Delta config tables via `deploy_config.py`. The FastAPI backend
reads config at runtime — no restart needed when a new table is onboarded.

Onboarding time: roughly 10 minutes per table.

### 3. Stage → Validate → Apply for bulk operations

Individual cell edits are simple: validate, UPDATE, audit. But bulk CSV uploads are
different — partial failures are dangerous.

The bulk upload flow uses a staging pattern:

```
Upload CSV
    │
    ▼
Parse + schema check
    │
    ▼
Stage to temporary Delta table
    │
    ▼
Run all validations (all-or-nothing)
    │
    ├── Errors found? → Return error report, no write to target
    │
    └── Clean? → Apply to target table (single transaction)
                     │
                     ▼
               Write audit rows
                     │
                     ▼
               (Optional) Queue for approval
```

This means a 5,000-row upload either fully succeeds or fully fails. No partial corruption.

---

## The Save Flow

Every cell edit goes through the same disciplined path:

```
User edits cell(s)
    │
    ▼
Click "Review & Save"
    │
    ▼
Frontend shows diff: old value → new value per column
    │
    ▼
User confirms
    │
    ▼
POST /api/tables/{schema}/{table}/row (PATCH for update)
    │
    ▼
FastAPI: mandatory field check
    │
FastAPI: business rule evaluation (blocking + warnings)
    │
    ├── Errors? → 422 response → UI shows errors, no write
    │
    └── Clean? → Parameterized UPDATE with version check:
                 UPDATE table
                 SET col = ?, version = version + 1
                 WHERE pk = ? AND version = ?
                     │
                     ├── 0 rows affected + version present → 409 Conflict
                     │   "Another user modified this row — refresh and retry"
                     │
                     └── 1 row affected → Write audit rows → 200 OK
```

The `version` column check is the key — it's atomic optimistic locking with no
separate lock table needed.

---

## Business Rule Engine

Rules are stored in a Delta table and evaluated at save time. No code changes needed
to add or change a rule:

```python
_EVALUATORS = {
    "allowed_values":    _eval_allowed_values,
    "required_if":       _eval_required_if,
    "date_order":        _eval_date_order,
    "readonly":          _eval_readonly,
    "regex":             _eval_regex,
    "min_length":        _eval_min_length,
    "max_length":        _eval_max_length,
    "min_value":         _eval_min_value,
    "max_value":         _eval_max_value,
    "lookup":            _eval_lookup,
    "starts_with":       _eval_starts_with,
    "ends_with":         _eval_ends_with,
    "contains":          _eval_contains,
    "readonly_after_insert": _eval_readonly_after_insert,
}
```

Rules have two severity levels: `blocking` (prevents save) and `warning` (allows save
with a visible notice). A single column can have multiple rules. Rules reference other
columns — a `date_order` rule can check that `effective_date` is before `term_date`.

---

## The Audit Log

Every save writes one row **per changed column** to the audit table:

```
changed_by    | changed_at          | table_name  | column_name | old_value | new_value
──────────────┼─────────────────────┼─────────────┼─────────────┼───────────┼──────────
user@org.com  | 2025-03-15 14:23:11 | sample_tbl  | status      | PENDING   | ACTIVE
user@org.com  | 2025-03-15 14:23:11 | sample_tbl  | contract_val| 100000    | 125000
```

Because the user's real identity flows through via Unity Catalog, `changed_by` is always
accurate — even if the same person uses a different machine or browser.

Bulk operations link all their audit rows to a `change_request_id`, so an auditor can
see all columns changed in one upload as a single logical event.

---

## Concurrency Model

Databricks Apps runs on a medium compute instance (6 GB RAM). The concurrency model is:

```python
# Semaphore limits simultaneous SQL Warehouse connections
_sem = asyncio.Semaphore(40)

# Per request: one connection, multiple queries
async with _sem:
    with _connection(user_token) as conn:
        # All queries in this request share one connection
        result1 = query(sql1, conn)
        result2 = query(sql2, conn)
```

40 concurrent warehouse connections × ~50 bytes/row × 10,000 max rows = ~400 MB peak
on the FastAPI side. The SQL Warehouse handles query execution on its own cluster RAM.

---

## Bulk Export Architecture

Exporting large datasets requires streaming — loading 100K rows into FastAPI RAM would
be a memory spike that could crash the app:

```python
# export_ops.py — streaming response
async def stream_csv(sql_text, user_token, batch_size=500):
    with _connection(user_token) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text)
            # Yield header
            cols = [d[0] for d in cur.description]
            yield ",".join(cols) + "\n"
            # Yield batches — FastAPI RAM holds only one batch at a time
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                chunk = pd.DataFrame(rows, columns=cols)
                yield chunk.to_csv(index=False, header=False)
```

Memory footprint stays flat regardless of export size: one batch in RAM, streamed to
the browser, discarded, next batch.

---

## AI-Assisted Config Generation

Onboarding a 40-column table by hand is tedious. The `agent-builder` module sends the
`DESCRIBE TABLE` output to an LLM and gets back a complete YAML config:

```bash
python agents/table_config/generate.py \
  --schema your_schema \
  --table your_table \
  --primary-keys record_id \
  --fetch-describe \
  --validate
```

The LLM infers column types, suggests validation rules, and formats the YAML to the
app's schema. A data engineer reviews it, tweaks it, and deploys — saving roughly an
hour per table on large schemas.

The agent uses LiteLLM so it works with any LLM provider: Azure OpenAI, AWS Bedrock,
Anthropic, or a local model.

---

## What I Learned

**1. Databricks Apps is genuinely production-ready.**
The SSO integration, token forwarding, and warehouse binding work seamlessly. The biggest
surprise was how clean the per-user identity story is — no token management code needed.

**2. Config-driven design compounds over time.**
The first table takes 30 minutes to onboard (writing and testing the YAML). By table 10
it takes 10 minutes. By table 30 you can hand it to a non-engineer with the template.

**3. Staging beats transactions for bulk operations.**
Using a temporary Delta staging table rather than a database transaction gave us better
error reporting, a reviewable intermediate state, and natural support for the approval
workflow. Delta's ACID guarantees handle the final apply atomically.

**4. Column-level audit beats row-level audit.**
A row-level audit (`INSERT INTO audit SELECT * FROM table WHERE id = ?`) is simpler but
useless for answering "who changed the status field?" Column-level audit costs more
writes but is far more useful in practice.

**5. Streaming is non-negotiable for data apps.**
The first version loaded all rows into a pandas DataFrame before returning. It worked
fine for 500 rows and crashed at 50,000. Streaming should be the default, not an
optimisation.

---

## Try It

The project is open source on GitHub. You'll need a Databricks workspace with Unity
Catalog enabled and a SQL Warehouse.

**GitHub:** [github.com/your-username/delta-table-editor](https://github.com)

**Tech stack:** FastAPI · React 18 · Vite · Databricks Apps · Unity Catalog ·
Delta Lake · LiteLLM

---

*Senior Data Engineer with 16+ years building data platforms.*
*This project explores Databricks Apps as a deployment target for internal data tooling.*
