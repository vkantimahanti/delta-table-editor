# Contributing to Delta Table Editor

Thanks for taking the time to contribute.

## Before you start

For anything beyond a typo fix or small bug, **open an issue first** to discuss
your approach. This avoids duplicate work and makes reviews faster.

## Development setup

### Prerequisites

- Python 3.11+
- Node.js 18+
- Access to a Databricks workspace with a SQL Warehouse

### Backend

```bash
# From the repo root
pip install -r requirements.txt

cp .env.example .env
# Edit .env — set DATABRICKS_HOST, DATABRICKS_HTTP_PATH,
#              DATABRICKS_TOKEN (local dev only), TARGET_CATALOG

# Run one-time workspace setup (in Databricks SQL editor):
#   open setup.sql, replace your_catalog / your_schema, run all

uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
# Swagger UI: http://localhost:8000/docs
```

### Frontend

```bash
cd frontend
npm install
npm run dev
# App: http://localhost:5173  (proxied to backend at :8000 automatically)
```

### Agent builder (optional)

```bash
cd agent-builder
pip install -r requirements.txt
cp .env.example .env   # set LITELLM_* vars
```

## Code style

- **Python**: follow the existing module structure; use type hints; keep
  functions small and named for what they do
- **SQL**: parameterized queries only — no string formatting into SQL
- **JavaScript**: functional React components; no class components

No linter is enforced yet. Keep diffs clean and avoid unrelated reformatting.

## Branching and commits

- Branch from `main`: `git checkout -b feature/short-description`
- Commit messages: imperative present tense — "Add approval expiry check",
  not "Added" or "Adding"
- One logical change per commit

## Pull request checklist

- [ ] Tested locally (backend + frontend, not just one side)
- [ ] `setup.sql` updated if config tables changed
- [ ] `app.yaml` updated if new env vars are added
- [ ] README updated if the feature changes setup steps or project structure
- [ ] No `.env` files, tokens, or credentials committed

## Security

- Never commit `.env`, tokens, or credentials
- All SQL must use parameterized queries (no f-strings or `.format()` into SQL)
- New endpoints must extract the user token via `auth_helpers.get_token()`

## Reporting bugs

Open a GitHub issue with:
1. What you did
2. What you expected
3. What actually happened
4. Databricks Runtime / Python / Node versions if relevant
