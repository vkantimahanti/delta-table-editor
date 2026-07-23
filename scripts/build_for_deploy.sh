#!/usr/bin/env bash
# Build the React frontend into static/ for Databricks App deployment.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/frontend"

echo "=== Building frontend for deploy ==="
if command -v npm >/dev/null 2>&1; then
  if [ -f package-lock.json ]; then
    npm ci
  else
    npm install
  fi
  npm run build
else
  echo "ERROR: npm not found. Install Node.js 18+ and retry." >&2
  exit 1
fi

if [ ! -f "$ROOT/static/index.html" ]; then
  echo "ERROR: build did not produce static/index.html" >&2
  exit 1
fi

echo "=== Ready for Databricks App deploy ==="
echo "Output: $ROOT/static/"
echo "Next: upload the data-editor/ folder as a Databricks App and bind a SQL warehouse."
