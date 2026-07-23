#!/usr/bin/env bash
set -e

echo "=== Data Canvas startup ==="

export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1

# ── Environment detection ─────────────────────────────────────
# DATABRICKS_APP_NAME is auto-injected by Databricks Apps.
# We use it to detect dev vs prod automatically.
# Convention:
#   name contains dev / test / staging / local → dev (2 workers)
#   name is clean e.g. "datacanvas"            → prod (4 workers)

APP_NAME="${DATABRICKS_APP_NAME:-local}"
APP_VERSION="${APP_VERSION:-unknown}"

if echo "$APP_NAME" | grep -qiE "(dev|test|staging|local)"; then
    DETECTED_ENV="dev"
    DEFAULT_WORKERS=2
else
    DETECTED_ENV="prod"
    DEFAULT_WORKERS=2
fi

# WEB_CONCURRENCY in app.yaml or Databricks Apps UI
# can override the auto-detected default.
WORKERS="${WEB_CONCURRENCY:-$DEFAULT_WORKERS}"

echo "--- Runtime config ---"
echo "APP_NAME:    ${APP_NAME}"
echo "APP_VERSION: ${APP_VERSION}"
echo "ENV:         ${DETECTED_ENV}"
echo "WORKERS:     ${WORKERS}"
echo "PORT:        ${PORT:-8000}"
echo "Python:      $(python --version 2>&1)"

# ── Clean stale bytecode ──────────────────────────────────────
find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null; true
find . -name '*.pyc' -delete 2>/dev/null; true

# ── Install dependencies ──────────────────────────────────────
echo "--- Installing dependencies ---"
python -m pip install -r requirements.txt --quiet

# ── Verify imports ────────────────────────────────────────────
echo "--- Verifying imports ---"
python - <<'PYCHECK'
import sys
try:
    import fastapi, uvicorn, pandas, pydantic
    from databricks import sql
    print("Imports OK")
except Exception as e:
    print(f"Import FAILED: {e}", file=sys.stderr)
    sys.exit(1)
PYCHECK

# ── Verify Databricks env vars ────────────────────────────────
echo "--- Databricks env (presence only) ---"
echo "DATABRICKS_HOST:             ${DATABRICKS_HOST:+set}"
echo "DATABRICKS_SERVER_HOSTNAME:  ${DATABRICKS_SERVER_HOSTNAME:+set}"
echo "DATABRICKS_WAREHOUSE_ID:     ${DATABRICKS_WAREHOUSE_ID:+set}"
echo "DATABRICKS_HTTP_PATH:        ${DATABRICKS_HTTP_PATH:+set}"
echo "DATABRICKS_CLIENT_ID:        ${DATABRICKS_CLIENT_ID:+set}"
echo "TARGET_CATALOG:              ${TARGET_CATALOG:+set}"

# ── Verify frontend build ─────────────────────────────────────
echo "--- Checking frontend build ---"
if [ ! -f static/index.html ]; then
    echo "ERROR: static/index.html not found." >&2
    echo "Build the frontend before deploying:" >&2
    echo "  cd frontend && npm install && npm run build" >&2
    exit 1
fi
echo "Frontend build OK (static/index.html found)"

# ── Start server ──────────────────────────────────────────────
echo "--- Starting uvicorn ---"
echo "Workers: ${WORKERS} | Port: ${PORT:-8000} | Env: ${DETECTED_ENV}"

exec python -m uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers "${WORKERS}" \
    --proxy-headers \
    --log-level info
