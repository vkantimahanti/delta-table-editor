"""
deploy_config.py
Reads YAML config files and MERGEs into three Delta tables:
  - dataeditor_table_registry
  - dataeditor_column_config
  - dataeditor_business_rules

Usage:
  # Deploy one table
  python scripts/deploy_config.py --file config/tables/dmz.dash_test_carrier.yaml

  # Deploy all tables
  python scripts/deploy_config.py --all

  # Validate only — no DB writes
  python scripts/deploy_config.py --all --dry-run
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import yaml
from databricks import sql as dbsql
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("deploy_config")

CATALOG = os.environ.get("TARGET_CATALOG", "your_catalog")
CONFIG_DIR = Path(__file__).parent.parent / "config"
DEFAULTS_FILE = CONFIG_DIR / "defaults.yaml"

# ── System audit columns — auto-applied to every table ───────────────────────
AUDIT_COL_NAMES = {"version", "inserted_by", "inserted_at", "updated_by", "updated_at"}

# ── All valid rule types ──────────────────────────────────────────────────────
VALID_RULE_TYPES = {
    "allowed_values", "lookup", "dependent_lookup", "regex",
    "min_length", "max_length", "min_value", "max_value",
    "date_order", "contains", "starts_with", "ends_with",
    "readonly_after_insert",
}

# ── Valid column types ────────────────────────────────────────────────────────
VALID_COL_TYPES = {"text", "number", "date", "timestamp", "boolean", "dropdown"}

# Registry defaults (Delta DDL has no column DEFAULT — set explicitly on deploy)
REGISTRY_DEFAULTS = {
    "catalog": CATALOG,
    "row_limit": 500,
    "allow_insert": True,
    "allow_update": True,
    "allow_delete": False,
    "is_active": True,
    "requires_approval": False,
    "approver_emails": "",
    "approval_expiry_hours": 72,
    "allow_overwrite": True,
    "max_staged_rows": 500,
    "auto_apply_on_approve": False,
}


def sql_literal_str(value) -> str:
    """Escape a value for inline SQL string literals."""
    if value is None:
        return ""
    return str(value).replace("'", "''").replace("\n", " ").strip()


def sql_bool(value) -> str:
    return "TRUE" if bool(value) else "FALSE"


# ── DB connection ─────────────────────────────────────────────────────────────

def get_connection():
    hostname = (
        os.environ.get("DATABRICKS_SERVER_HOSTNAME")
        or os.environ.get("DATABRICKS_HOST", "").replace("https://", "").rstrip("/")
    )
    http_path = (
        os.environ.get("DATABRICKS_HTTP_PATH")
        or f"/sql/1.0/warehouses/{os.environ.get('DATABRICKS_WAREHOUSE_ID', '')}"
    )
    token = os.environ.get("DATABRICKS_TOKEN", "")
    if not hostname or not token:
        raise EnvironmentError(
            "Set DATABRICKS_HOST, DATABRICKS_HTTP_PATH, DATABRICKS_TOKEN in .env"
        )
    return dbsql.connect(
        server_hostname=hostname,
        http_path=http_path,
        access_token=token,
    )


def execute(conn, sql: str, params: tuple = ()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount or 0


# ── YAML loading ──────────────────────────────────────────────────────────────

def load_defaults() -> list[dict]:
    """Load audit column definitions from defaults.yaml."""
    if not DEFAULTS_FILE.exists():
        logger.warning("defaults.yaml not found — audit columns will not be auto-added.")
        return []
    with open(DEFAULTS_FILE) as f:
        data = yaml.safe_load(f)
    return data.get("audit_columns", [])


def load_yaml(filepath: Path) -> dict:
    with open(filepath) as f:
        return yaml.safe_load(f)


# ── Validation ────────────────────────────────────────────────────────────────

def validate(data: dict, filepath: str) -> list[str]:
    """Validate YAML structure. Returns list of errors."""
    errors = []
    prefix = f"[{filepath}]"

    # Table section
    tbl = data.get("table", {})
    for field in ["schema", "name", "display_name", "primary_keys"]:
        if not tbl.get(field):
            errors.append(f"{prefix} table.{field} is required.")
    if not isinstance(tbl.get("primary_keys", []), list):
        errors.append(f"{prefix} table.primary_keys must be a list.")

    # Columns section
    cols = data.get("columns", [])
    if not cols:
        errors.append(f"{prefix} At least one column is required.")

    col_names = set()
    for col in cols:
        name = col.get("column_name", "?")

        # Block audit columns from being defined in the table file
        if name in AUDIT_COL_NAMES:
            errors.append(
                f"{prefix} Column '{name}' is a system audit column — "
                f"remove it. It is auto-added from defaults.yaml."
            )

        if name in col_names:
            errors.append(f"{prefix} Duplicate column_name: '{name}'.")
        col_names.add(name)

        if col.get("column_type") not in VALID_COL_TYPES:
            errors.append(
                f"{prefix} column '{name}': invalid column_type "
                f"'{col.get('column_type')}'. Must be one of: {', '.join(VALID_COL_TYPES)}."
            )

        # Dropdown validation
        if col.get("column_type") == "dropdown":
            dd = col.get("dropdown")
            if not dd:
                errors.append(f"{prefix} column '{name}': column_type is dropdown but dropdown is null.")
            elif dd.get("type") == "fixed" and not dd.get("values"):
                errors.append(f"{prefix} column '{name}': fixed dropdown has no values.")
            elif dd.get("type") == "lookup":
                for f in ["schema", "table", "value_column"]:
                    if not dd.get(f):
                        errors.append(f"{prefix} column '{name}': lookup dropdown missing '{f}'.")
            elif dd.get("type") == "dependent_lookup":
                for f in ["parent_column", "schema", "table", "value_column"]:
                    if not dd.get(f):
                        errors.append(
                            f"{prefix} column '{name}': dependent_lookup missing '{f}'."
                        )
                via = dd.get("filter_via") or {}
                for f in ["schema", "table", "parent_match_column", "link_column"]:
                    if not via.get(f):
                        errors.append(
                            f"{prefix} column '{name}': dependent_lookup.filter_via missing '{f}'."
                        )

        # Rules validation
        for i, rule in enumerate(col.get("rules", []) or []):
            rtype = rule.get("type")
            if rtype not in VALID_RULE_TYPES:
                errors.append(
                    f"{prefix} column '{name}' rule[{i}]: invalid type '{rtype}'. "
                    f"Must be one of: {', '.join(sorted(VALID_RULE_TYPES))}."
                )
            if rule.get("on_fail") not in ("block", "warn"):
                errors.append(
                    f"{prefix} column '{name}' rule[{i}]: on_fail must be 'block' or 'warn'."
                )
            if not rule.get("reason"):
                errors.append(f"{prefix} column '{name}' rule[{i}]: reason is required.")
            if not rule.get("fix"):
                errors.append(f"{prefix} column '{name}' rule[{i}]: fix is required.")

    return errors


# ── Deploy ────────────────────────────────────────────────────────────────────

def deploy(data: dict, conn, dry_run: bool = False):
    tbl      = data["table"]
    schema   = tbl["schema"]
    name     = tbl["name"]
    full     = f"{schema}.{name}"
    defaults = load_defaults()

    # Merge audit columns into column list (appended at end, order 990+)
    user_col_names = {c["column_name"] for c in data.get("columns", [])}
    extra_cols = [d for d in defaults if d["column_name"] not in user_col_names]
    all_columns = data.get("columns", []) + extra_cols

    logger.info("Deploying %s (%d columns, %d audit cols added from defaults)",
                full, len(data.get("columns", [])), len(extra_cols))

    if dry_run:
        logger.info("[DRY RUN] Would deploy %s — skipping DB writes.", full)
        return

    # ── 1. Table registry ─────────────────────────────────────────────────────
    catalog_val = tbl.get("catalog") or REGISTRY_DEFAULTS["catalog"]
    display = sql_literal_str(tbl.get("display_name", name))
    description = sql_literal_str(tbl.get("description", ""))
    pk_csv = ",".join(tbl.get("primary_keys", []))
    app_group = sql_literal_str(tbl.get("group", "General"))

    requires_approval = tbl.get("requires_approval", REGISTRY_DEFAULTS["requires_approval"])
    approval_expiry = int(tbl.get("approval_expiry_hours", REGISTRY_DEFAULTS["approval_expiry_hours"]))

    execute(conn, f"""
        MERGE INTO {CATALOG}.dmz.dataeditor_table_registry AS target
        USING (SELECT
            '{schema}' AS schema_name,
            '{name}' AS table_name,
            '{display}' AS display_name,
            '{description}' AS description,
            '{sql_literal_str(catalog_val)}' AS catalog,
            '{pk_csv}' AS primary_keys,
            '{app_group}' AS app_group,
            {int(tbl.get("row_limit", REGISTRY_DEFAULTS["row_limit"]))} AS row_limit,
            {sql_bool(tbl.get("allow_insert", REGISTRY_DEFAULTS["allow_insert"]))} AS allow_insert,
            {sql_bool(tbl.get("allow_update", REGISTRY_DEFAULTS["allow_update"]))} AS allow_update,
            {sql_bool(tbl.get("allow_delete", REGISTRY_DEFAULTS["allow_delete"]))} AS allow_delete,
            {sql_bool(tbl.get("is_active", REGISTRY_DEFAULTS["is_active"]))} AS is_active,
            {sql_bool(requires_approval)} AS requires_approval,
            '{sql_literal_str(tbl.get("approver_emails", REGISTRY_DEFAULTS["approver_emails"]))}' AS approver_emails,
            {approval_expiry} AS approval_expiry_hours,
            {sql_bool(tbl.get("allow_overwrite", REGISTRY_DEFAULTS["allow_overwrite"]))} AS allow_overwrite,
            {int(tbl.get("max_staged_rows", REGISTRY_DEFAULTS["max_staged_rows"]))} AS max_staged_rows,
            {sql_bool(tbl.get("auto_apply_on_approve", REGISTRY_DEFAULTS["auto_apply_on_approve"]))} AS auto_apply_on_approve,
            {sql_bool(requires_approval)} AS requires_upload_approval,
            {approval_expiry} AS upload_approval_expiry_hours
        ) AS source
        ON target.schema_name = source.schema_name
        AND target.table_name = source.table_name
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)
    logger.info("  ✓ Table registry updated")

    # ── 2. Column config ──────────────────────────────────────────────────────
    # Delete existing columns for this table then re-insert (clean MERGE)
    execute(conn,
        f"DELETE FROM {CATALOG}.dmz.dataeditor_column_config "
        f"WHERE schema_name = '{schema}' AND table_name = '{name}'"
    )
    for col in all_columns:
        dd = col.get("dropdown")
        dd_json = json.dumps(dd) if dd else None
        execute(conn, f"""
            INSERT INTO {CATALOG}.dmz.dataeditor_column_config
              (schema_name, table_name, column_name, col_order,
               is_primary_key, visible, editable, mandatory, nullable,
               default_value, column_type, dropdown_source, is_filter, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            schema, name,
            col["column_name"],
            col.get("order", 99),
            col.get("is_primary_key", False),
            col.get("visible", True),
            col.get("editable", True),
            col.get("mandatory", False),
            col.get("nullable", True),
            col.get("default_value"),
            col.get("column_type", "text"),
            dd_json,
            col.get("is_filter", False),
            col.get("is_active", True),
        ))
    logger.info("  ✓ Column config updated (%d columns)", len(all_columns))

    # ── 3. Business rules ─────────────────────────────────────────────────────
    execute(conn,
        f"DELETE FROM {CATALOG}.dmz.dataeditor_business_rules "
        f"WHERE schema_name = '{schema}' AND table_name = '{name}'"
    )
    rule_count = 0
    for col in all_columns:
        col_active = col.get("is_active", True)
        for rule in (col.get("rules") or []):
            # Build condition params — type-specific fields
            params = {}
            for key in ["pattern", "value", "before_column", "after_column", "values"]:
                if key in rule:
                    params[key] = rule[key]
            # For lookup rules — reuse dropdown source
            if rule.get("type") == "lookup" and col.get("dropdown"):
                params["dropdown_source"] = col["dropdown"]
            if rule.get("type") == "dependent_lookup" and col.get("dropdown"):
                params["dropdown_source"] = col["dropdown"]

            execute(conn, f"""
                INSERT INTO {CATALOG}.dmz.dataeditor_business_rules
                  (schema_name, table_name, column_name,
                   condition_type, condition_params,
                   on_fail, reason, fix,
                   col_is_active, cond_is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                schema, name,
                col["column_name"],
                rule["type"],
                json.dumps(params) if params else "{}",
                rule.get("on_fail", "block"),
                rule.get("reason", ""),
                rule.get("fix", ""),
                col_active,
                rule.get("is_active", True),
            ))
            rule_count += 1
    logger.info("  ✓ Business rules updated (%d rules)", rule_count)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Deploy YAML config to Databricks Delta tables")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="Path to a single YAML file")
    group.add_argument("--all",  action="store_true", help="Deploy all YAML files in config/tables/")
    parser.add_argument("--dry-run", action="store_true", help="Validate only — no DB writes")
    args = parser.parse_args()

    # Collect files to process
    if args.all:
        files = sorted(
            p for p in (CONFIG_DIR / "tables").glob("*.yaml")
            if p.name != "defaults.yaml"
        )
        if not files:
            logger.error("No YAML files found in config/tables/")
            sys.exit(1)
    else:
        files = [Path(args.file)]

    # Validate all first — fail early before writing anything
    all_errors = []
    all_data   = {}
    for filepath in files:
        if not filepath.exists():
            logger.error("File not found: %s", filepath)
            sys.exit(1)
        data = load_yaml(filepath)
        errors = validate(data, str(filepath))
        if errors:
            all_errors.extend(errors)
        else:
            all_data[filepath] = data

    if all_errors:
        logger.error("Validation failed — fix these errors before deploying:\n")
        for e in all_errors:
            logger.error("  %s", e)
        sys.exit(1)

    logger.info("Validation passed for %d file(s)", len(all_data))

    if args.dry_run:
        logger.info("Dry run complete — no changes written.")
        return

    # Deploy
    conn = get_connection()
    try:
        failed = []
        for filepath, data in all_data.items():
            try:
                deploy(data, conn)
                logger.info("✓ Deployed: %s", filepath.name)
            except Exception as exc:
                logger.error("✗ Failed: %s — %s", filepath.name, exc)
                failed.append(filepath.name)
        if failed:
            logger.error("Deploy completed with errors: %s", ", ".join(failed))
            sys.exit(1)
        else:
            logger.info("All files deployed successfully.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
