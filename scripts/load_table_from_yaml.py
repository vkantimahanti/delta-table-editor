"""
load_table_from_yaml.py
Create or replace a physical Delta table from a table YAML schema (columns + audit cols).

This does NOT touch dataeditor_* config tables — only the business table in the catalog.

Usage:
  python scripts/load_table_from_yaml.py --file config/tables/dmz.dash_test_carrier.yaml
  python scripts/load_table_from_yaml.py --file config/tables/dmz.dash_test_carrier.yaml --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Allow `from backend.shared import db_client` when run from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from backend.shared import db_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("load_table_from_yaml")

CATALOG = os.environ.get("TARGET_CATALOG", "your_catalog")
CONFIG_DIR = ROOT / "config"
DEFAULTS_FILE = CONFIG_DIR / "defaults.yaml"
AUDIT_NAMES = {"version", "inserted_by", "inserted_at", "updated_by", "updated_at"}

YAML_TO_DELTA = {
    "text": "STRING",
    "dropdown": "STRING",
    "number": "DOUBLE",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "boolean": "BOOLEAN",
}


def load_defaults() -> list[dict]:
    if not DEFAULTS_FILE.exists():
        return []
    with open(DEFAULTS_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("audit_columns", [])


def load_table_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def merged_columns(data: dict) -> list[dict]:
    user_cols = data.get("columns", [])
    user_names = {c["column_name"] for c in user_cols}
    audit = [d for d in load_defaults() if d["column_name"] not in user_names]
    return user_cols + audit


def build_create_sql(schema: str, table: str, columns: list[dict]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for col in columns:
        name = col["column_name"]
        if name in seen:
            continue
        seen.add(name)
        raw = str(col.get("column_type") or "text").lower()
        dtype = YAML_TO_DELTA.get(raw, "STRING")
        parts.append(f"  `{name}` {dtype}")
    body = ",\n".join(parts)
    return f"CREATE OR REPLACE TABLE {CATALOG}.{schema}.{table} (\n{body}\n) USING DELTA"


def main() -> None:
    parser = argparse.ArgumentParser(description="Overwrite Delta table schema from YAML")
    parser.add_argument("--file", required=True, help="Path to table YAML file")
    parser.add_argument("--dry-run", action="store_true", help="Print SQL only")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        logger.error("File not found: %s", path)
        sys.exit(1)

    data = load_table_yaml(path)
    tbl = data.get("table", {})
    schema = tbl.get("schema")
    name = tbl.get("name")
    if not schema or not name:
        logger.error("YAML must define table.schema and table.name")
        sys.exit(1)

    columns = merged_columns(data)
    sql = build_create_sql(schema, name, columns)
    full = f"{CATALOG}.{schema}.{name}"

    logger.info("Target: %s (%d columns)", full, len(columns))
    if args.dry_run:
        print(sql)
        return

    logger.info("Overwriting table (all existing rows will be removed)...")
    db_client.execute(sql)
    logger.info("Done — %s recreated with schema from %s", full, path.name)

    verify = db_client.query(
        f"SELECT column_name FROM {CATALOG}.information_schema.columns "
        f"WHERE table_catalog = '{CATALOG}' AND table_schema = '{schema}' "
        f"AND table_name = '{name}' ORDER BY ordinal_position"
    )
    cols = verify["column_name"].tolist() if not verify.empty else []
    logger.info("Columns now on table (%d): %s", len(cols), ", ".join(cols))


if __name__ == "__main__":
    main()
