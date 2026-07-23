"""
Read table/column metadata from dataeditor_* Delta config tables (deploy_config.py).
Maps DB column names → API shape expected by the React frontend.
"""
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from . import db_client

logger = logging.getLogger("delta_editor.config_store")

CATALOG = db_client.CATALOG
REGISTRY_TABLE = f"{CATALOG}.dmz.dataeditor_table_registry"
COLUMN_TABLE = f"{CATALOG}.dmz.dataeditor_column_config"
_CONFIG_TABLES_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "tables"

_COL_TYPE_MAP = {
    "text": "string",
    "number": "number",
    "date": "date",
    "timestamp": "date",
    "boolean": "boolean",
    "dropdown": "dropdown",
}

_CONFIG_STORAGE_TYPE_MAP = {
    "text": "string",
    "number": "bigint",
    "date": "date",
    "timestamp": "timestamp",
    "boolean": "boolean",
    "dropdown": "string",
}


def get_column_storage_types(
    schema: str, table: str, user_token: str | None = None
) -> dict[str, str]:
    """Map config column_type → SQL storage type for empty-cell coercion."""
    out: dict[str, str] = {}
    for row in get_column_config_raw(schema, table, user_token=user_token):
        name = str(row.get("column_name") or "").strip().lower()
        if not name:
            continue
        raw_type = str(row.get("column_type") or "text").lower()
        out[name] = _CONFIG_STORAGE_TYPE_MAP.get(raw_type, "string")
    return out


def map_column_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map dataeditor_column_config row → frontend ColumnMeta."""
    name = str(row.get("column_name") or "")
    raw_type = str(row.get("column_type") or "text").lower()
    return {
        "table_schema": row.get("schema_name"),
        "table_name": row.get("table_name"),
        "column_name": name,
        "display_label": name,
        "col_order": int(row.get("col_order") or 0),
        "col_type": _COL_TYPE_MAP.get(raw_type, "string"),
        "is_visible": bool(row.get("visible", True)),
        "is_editable": bool(row.get("editable", True)),
        "is_mandatory": bool(row.get("mandatory", False)),
        "is_filter": bool(row.get("is_filter", False)),
        "is_pk": bool(row.get("is_primary_key", False)),
    }


def get_upload_unique_columns(schema: str, table: str) -> list[str]:
    """
    Business-key columns that must be unique per upload row (case-insensitive, trimmed).

    Declared in config/tables/{schema}.{table}.yaml as table.upload_unique_columns.
    """
    path = _CONFIG_TABLES_DIR / f"{schema}.{table}.yaml"
    if not path.is_file():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        cols = (data.get("table") or {}).get("upload_unique_columns")
        if isinstance(cols, list):
            return [str(c).strip() for c in cols if str(c).strip()]
    except Exception as exc:
        logger.warning("Could not read upload_unique_columns from %s: %s", path, exc)
    return []


def get_pk_cols(schema: str, table: str, user_token: str | None = None) -> set[str]:
    try:
        df = db_client.query(
            f"SELECT primary_keys FROM {REGISTRY_TABLE} "
            f"WHERE schema_name = '{schema}' AND table_name = '{table}' "
            "AND is_active = TRUE LIMIT 1",
            user_token=user_token,
        )
        if df.empty or not str(df.iloc[0].get("primary_keys") or "").strip():
            return set()
        return {p.strip() for p in str(df.iloc[0]["primary_keys"]).split(",") if p.strip()}
    except Exception as exc:
        logger.warning("Could not load primary_keys for %s.%s: %s", schema, table, exc)
        return set()


def get_columns(schema: str, table: str, user_token: str | None = None) -> list[dict[str, Any]]:
    df = db_client.query(
        f"SELECT * FROM {COLUMN_TABLE} "
        f"WHERE schema_name = '{schema}' AND table_name = '{table}' "
        "AND is_active = TRUE "
        "AND (visible = TRUE OR column_name = 'version') "
        "ORDER BY col_order, column_name",
        user_token=user_token,
    )
    return [map_column_row(r) for r in df.to_dict("records")] if not df.empty else []


def get_column_config_raw(schema: str, table: str, user_token: str | None = None) -> list[dict[str, Any]]:
    """Full column config rows (includes dropdown_source JSON)."""
    df = db_client.query(
        f"SELECT * FROM {COLUMN_TABLE} "
        f"WHERE schema_name = '{schema}' AND table_name = '{table}' AND is_active = TRUE",
        user_token=user_token,
    )
    return df.to_dict("records") if not df.empty else []


def _parse_dropdown_source(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        src = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return None
    return src if isinstance(src, dict) else None


def _resolve_lookup_options(
    src: dict[str, Any],
    *,
    default_schema: str,
    user_token: str | None,
) -> list[str]:
    ref_schema = str(src.get("schema") or default_schema)
    ref_table = str(src.get("table") or "")
    value_col = str(src.get("value_column") or "")
    if not ref_table or not value_col:
        return []
    try:
        df = db_client.query(
            f"SELECT DISTINCT `{value_col}` AS v FROM {CATALOG}.{ref_schema}.{ref_table} "
            f"WHERE `{value_col}` IS NOT NULL ORDER BY v",
            user_token=user_token,
        )
        return df["v"].astype(str).tolist() if not df.empty else []
    except Exception as exc:
        logger.warning("Lookup dropdown failed for %s.%s: %s", ref_table, value_col, exc)
        return []


def _format_dependent_value(
    template: str,
    *,
    carrier: str,
    subcarrier: str,
    code: str = "",
) -> str:
    """Build stored dropdown value from a template, e.g. '{code} {carrier}    |    {subcarrier}'."""
    return (
        template.replace("{carrier}", str(carrier))
        .replace("{subcarrier}", str(subcarrier))
        .replace("{code}", str(code))
    )


def _dependent_value_template(src: dict[str, Any]) -> str:
    explicit = str(src.get("value_template") or "").strip()
    if explicit:
        return explicit
    if src.get("code_column"):
        return "{code} {carrier}    |    {subcarrier}"
    return "{carrier}    |    {subcarrier}"


def _lookup_parent_options(
    options_map: dict[str, list[str]], parent_value: str
) -> list[str]:
    parent = str(parent_value or "").strip()
    if not parent:
        return []
    if parent in options_map:
        return list(options_map[parent])
    lower = parent.lower()
    for key, opts in options_map.items():
        if str(key).strip().lower() == lower:
            return list(opts)
    return []


def _build_dependent_options_map(
    src: dict[str, Any],
    *,
    default_schema: str,
    user_token: str | None,
) -> dict[str, list[str]]:
    """Prefetch {parent_value: [formatted_child_option, ...]} for dependent_lookup dropdowns."""
    child_schema = str(src.get("schema") or default_schema)
    child_table = str(src.get("table") or "")
    value_col = str(src.get("value_column") or "")
    code_col = str(src.get("code_column") or "")
    via = src.get("filter_via") or {}
    via_schema = str(via.get("schema") or default_schema)
    via_table = str(via.get("table") or "")
    parent_match_col = str(via.get("parent_match_column") or "")
    link_col = str(via.get("link_column") or "")
    if not all([child_table, value_col, via_table, parent_match_col, link_col]):
        return {}

    select_cols = [
        f"p.`{parent_match_col}` AS parent_val",
        f"c.`{value_col}` AS subcarrier_val",
        f"p.`{parent_match_col}` AS carrier_val",
    ]
    if code_col:
        select_cols.append(f"c.`{code_col}` AS code_val")

    sql = (
        f"SELECT DISTINCT {', '.join(select_cols)} "
        f"FROM {CATALOG}.{child_schema}.{child_table} c "
        f"INNER JOIN {CATALOG}.{via_schema}.{via_table} p "
        f"ON c.`{link_col}` = p.`{link_col}` "
        f"WHERE p.`{parent_match_col}` IS NOT NULL AND c.`{value_col}` IS NOT NULL "
        f"ORDER BY parent_val, subcarrier_val"
    )
    template = _dependent_value_template(src)
    try:
        df = db_client.query(sql, user_token=user_token)
    except Exception as exc:
        logger.warning("Dependent lookup prefetch failed: %s", exc)
        return {}

    out: dict[str, list[str]] = {}
    if df.empty:
        return out
    for _, row in df.iterrows():
        parent = str(row["parent_val"])
        formatted = _format_dependent_value(
            template,
            carrier=str(row["carrier_val"]),
            subcarrier=str(row["subcarrier_val"]),
            code=str(row["code_val"]) if code_col and "code_val" in row else "",
        )
        out.setdefault(parent, [])
        if formatted not in out[parent]:
            out[parent].append(formatted)
    return out


def dependent_options_for_parent(
    src: dict[str, Any],
    parent_value: str,
    *,
    default_schema: str,
    user_token: str | None,
) -> list[str]:
    """Return allowed child values for one parent (validation on save)."""
    parent = str(parent_value or "").strip()
    if not parent:
        return []
    options_map = _build_dependent_options_map(
        src, default_schema=default_schema, user_token=user_token
    )
    return _lookup_parent_options(options_map, parent_value)


def resolve_dropdowns(
    schema: str,
    table: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    """
    Build dropdown payload for the grid API.
    - dropdowns: flat {column: [option, ...]} for fixed/lookup
    - dependent: {column: {parent_column, options_by_parent}} for dependent_lookup
    """
    flat: dict[str, list[str]] = {}
    dependent: dict[str, dict[str, Any]] = {}

    for row in get_column_config_raw(schema, table, user_token):
        name = str(row.get("column_name") or "")
        col_type = str(row.get("column_type") or "").lower()
        is_filter = bool(row.get("is_filter", False))
        src = _parse_dropdown_source(row.get("dropdown_source"))
        if not src and col_type != "dropdown" and not is_filter:
            continue
        if not src:
            continue

        src_type = str(src.get("type") or "").lower()
        if src_type == "fixed":
            flat[name] = [str(v) for v in (src.get("values") or [])]
        elif src_type == "lookup":
            flat[name] = _resolve_lookup_options(src, default_schema=schema, user_token=user_token)
        elif src_type == "dependent_lookup":
            parent_col = str(src.get("parent_column") or "")
            options_by_parent = _build_dependent_options_map(
                src, default_schema=schema, user_token=user_token
            )
            dependent[name] = {
                "parent_column": parent_col,
                "options_by_parent": options_by_parent,
            }

    return {"dropdowns": flat, "dependent": dependent}


def resolve_dropdown_values(
    schema: str,
    table: str,
    user_token: str | None = None,
) -> dict[str, list[str]]:
    """Backward-compatible flat dropdown map only."""
    return resolve_dropdowns(schema, table, user_token=user_token).get("dropdowns", {})


def get_table_display_name(
    schema: str,
    table: str,
    user_token: str | None = None,
) -> str:
    """Human-readable table label from registry (falls back to table_name)."""
    try:
        df = db_client.query(
            f"SELECT display_name FROM {REGISTRY_TABLE} "
            f"WHERE schema_name = ? AND table_name = ? AND is_active = TRUE LIMIT 1",
            params=(schema, table),
            user_token=user_token,
        )
        if not df.empty:
            name = str(df.iloc[0].get("display_name") or "").strip()
            if name:
                return name
    except Exception as exc:
        logger.warning("Could not load display_name for %s.%s: %s", schema, table, exc)
    return table


def get_row_limit(schema: str, table: str, user_token: str | None = None, default: int = 500) -> int:
    try:
        df = db_client.query(
            f"SELECT row_limit FROM {REGISTRY_TABLE} "
            f"WHERE schema_name = '{schema}' AND table_name = '{table}' AND is_active = TRUE LIMIT 1",
            user_token=user_token,
        )
        if not df.empty and df.iloc[0].get("row_limit") is not None:
            return int(df.iloc[0]["row_limit"])
    except Exception:
        pass
    return default


def get_table_policy(
    schema: str,
    table: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    """Unified table + workflow policy from dataeditor_table_registry."""
    defaults: dict[str, Any] = {
        "catalog": CATALOG,
        "requires_approval": False,
        "approver_emails": "",
        "approval_expiry_hours": 72,
        "allow_overwrite": True,
        "max_staged_rows": 500,
        "auto_apply_on_approve": False,
    }
    try:
        df = db_client.query(
            f"""
            SELECT catalog, requires_approval, requires_upload_approval,
                   approver_emails, approval_expiry_hours, upload_approval_expiry_hours,
                   allow_overwrite, max_staged_rows, auto_apply_on_approve
            FROM {REGISTRY_TABLE}
            WHERE schema_name = '{schema}' AND table_name = '{table}'
              AND is_active = TRUE
            LIMIT 1
            """,
            user_token=user_token,
        )
        if df.empty:
            return defaults
        row = df.iloc[0]
        requires = bool(row.get("requires_approval") or row.get("requires_upload_approval") or False)
        expiry = int(row.get("approval_expiry_hours") or row.get("upload_approval_expiry_hours") or 72)
        return {
            "catalog": str(row.get("catalog") or CATALOG),
            "requires_approval": requires,
            "approver_emails": str(row.get("approver_emails") or "").strip(),
            "approval_expiry_hours": expiry,
            "allow_overwrite": bool(row.get("allow_overwrite") if row.get("allow_overwrite") is not None else True),
            "max_staged_rows": int(row.get("max_staged_rows") or 500),
            "auto_apply_on_approve": bool(row.get("auto_apply_on_approve") or False),
        }
    except Exception as exc:
        logger.warning("Could not load table policy for %s.%s: %s", schema, table, exc)
        return defaults


def get_upload_policy(
    schema: str,
    table: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    """Backward-compatible upload policy keys."""
    p = get_table_policy(schema, table, user_token=user_token)
    return {
        "requires_upload_approval": p["requires_approval"],
        "approver_emails": p["approver_emails"],
        "allow_overwrite": p["allow_overwrite"],
        "upload_approval_expiry_hours": p["approval_expiry_hours"],
    }


def get_edit_policy(
    schema: str,
    table: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    """Backward-compatible grid edit policy keys (same registry row)."""
    p = get_table_policy(schema, table, user_token=user_token)
    return {
        "requires_edit_approval": p["requires_approval"],
        "edit_approver_emails": p["approver_emails"],
        "edit_approval_expiry_hours": p["approval_expiry_hours"],
        "stage_before_apply": True,
        "auto_apply_on_approve": p["auto_apply_on_approve"],
        "max_staged_rows_per_request": p["max_staged_rows"],
    }


def get_approval_policy(
    schema: str,
    table: str,
    request_type: str = "upload",
    user_token: str | None = None,
) -> dict[str, Any]:
    """Unified approval policy for upload and grid_edit requests."""
    p = get_table_policy(schema, table, user_token=user_token)
    return {
        "requires_upload_approval": p["requires_approval"],
        "approver_emails": p["approver_emails"],
        "upload_approval_expiry_hours": p["approval_expiry_hours"],
        "auto_apply_on_approve": p["auto_apply_on_approve"],
    }
