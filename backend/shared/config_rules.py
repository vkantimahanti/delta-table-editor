"""
Config-driven business rule engine.
Reads rules from: your_catalog.dmz.dataeditor_business_rules
Reads column config from: your_catalog.dmz.dataeditor_column_config
Cache TTL: 5 minutes per table.

Returns structured {column, reason, fix, on_fail} per violation
instead of plain strings — used by Review panel to show reason + fix.
"""
import json
import logging
import re
import threading
import time
from typing import Any

import pandas as pd

from . import config_store, db_client

logger = logging.getLogger("delta_editor.config_rules")
CATALOG = db_client.CATALOG

_lock = threading.Lock()
_cache: dict = {}
_CACHE_TTL = 300


def _cache_key(schema: str, table: str) -> str:
    return f"{schema}.{table}"


def _is_fresh(entry: dict) -> bool:
    return time.time() - entry.get("loaded_at", 0) < _CACHE_TTL


def _parse_json(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, dict):
        return val
    try:
        return json.loads(str(val))
    except (json.JSONDecodeError, TypeError):
        return None


def _load_table_config(
    schema: str, table: str, user_token: str | None = None
) -> dict:
    """Load rules + column config for a table. Cached per table."""
    key = _cache_key(schema, table)

    with _lock:
        entry = _cache.get(key)
        if entry and _is_fresh(entry):
            return entry

    try:
        rules_df = db_client.query(
            f"SELECT * FROM {CATALOG}.dmz.dataeditor_business_rules "
            f"WHERE schema_name = '{schema}' AND table_name = '{table}' "
            f"AND col_is_active = TRUE AND cond_is_active = TRUE",
            user_token=user_token,
        )
        rules = rules_df.to_dict("records") if not rules_df.empty else []
    except Exception as exc:
        logger.warning("Could not load rules for %s.%s: %s", schema, table, exc)
        rules = []

    try:
        cols_df = db_client.query(
            f"SELECT column_name, column_type, dropdown_source, nullable "
            f"FROM {CATALOG}.dmz.dataeditor_column_config "
            f"WHERE schema_name = '{schema}' AND table_name = '{table}' "
            f"AND is_active = TRUE",
            user_token=user_token,
        )
        col_config = {}
        for _, row in cols_df.iterrows():
            col_config[row["column_name"]] = {
                "column_type": row.get("column_type"),
                "nullable": row.get("nullable", True),
                "dropdown_source": _parse_json(row.get("dropdown_source")),
            }
    except Exception as exc:
        logger.warning("Could not load column config for %s.%s: %s", schema, table, exc)
        col_config = {}

    entry = {"rules": rules, "col_config": col_config, "loaded_at": time.time()}
    with _lock:
        _cache[key] = entry
    return entry


def invalidate_cache(schema: str | None = None, table: str | None = None) -> None:
    """Invalidate cache — specific table or all."""
    with _lock:
        if schema and table:
            _cache.pop(_cache_key(schema, table), None)
        else:
            _cache.clear()
    logger.info(
        "Rule cache cleared: %s",
        f"{schema}.{table}" if schema and table else "all tables",
    )


def _to_dt(val: Any):
    if val is None or str(val).strip() in ("", "None", "NaT"):
        return None
    try:
        return pd.to_datetime(str(val))
    except Exception:
        return None


def _resolve(row: dict, edits: dict, col: str) -> Any:
    return edits.get(col, row.get(col))


def _str_val(val: Any) -> str:
    return str(val or "").strip()


def _eval_allowed_values(
    rule: dict, row: dict, edits: dict, col_config: dict, user_token: str | None
) -> tuple[bool, str, str]:
    col = rule["column_name"]
    if col not in edits:
        return True, "", ""
    val = _str_val(edits[col]).lower()
    if not val:
        return True, "", ""

    dd = col_config.get(col, {}).get("dropdown_source") or {}
    if dd.get("type") == "fixed":
        allowed = {str(v).lower() for v in (dd.get("values") or [])}
    elif dd.get("type") == "lookup":
        try:
            df = db_client.query(
                f"SELECT DISTINCT {dd['value_column']} AS v "
                f"FROM {CATALOG}.{dd['schema']}.{dd['table']} "
                f"WHERE {dd['value_column']} IS NOT NULL",
                user_token=user_token,
            )
            allowed = {str(x).lower() for x in df["v"].tolist()} if not df.empty else set()
        except Exception:
            return True, "", ""
    else:
        return True, "", ""

    if val not in allowed:
        return False, rule.get("reason", ""), rule.get("fix", "")
    return True, "", ""


def _eval_lookup(
    rule: dict, row: dict, edits: dict, col_config: dict, user_token: str | None
) -> tuple[bool, str, str]:
    col = rule["column_name"]
    if col not in edits:
        return True, "", ""
    val = _str_val(edits[col])
    if not val:
        return True, "", ""

    dd = col_config.get(col, {}).get("dropdown_source") or {}
    if dd.get("type") != "lookup":
        return True, "", ""

    try:
        escaped = val.replace("'", "''")
        df = db_client.query(
            f"SELECT 1 FROM {CATALOG}.{dd['schema']}.{dd['table']} "
            f"WHERE lower(trim({dd['value_column']})) = lower(trim('{escaped}')) "
            f"LIMIT 1",
            user_token=user_token,
        )
        if df.empty:
            return False, rule.get("reason", ""), rule.get("fix", "")
    except Exception as exc:
        logger.warning("Lookup validation failed for %s: %s", col, exc)
    return True, "", ""


def _strip_dependent_code_prefix(formatted: str) -> str:
    """'473 HIGHMARK    |    _ALL_' -> 'HIGHMARK    |    _ALL_'"""
    parts = formatted.split("|", 1)
    if len(parts) != 2:
        return formatted
    left = parts[0].strip()
    right = parts[1].strip()
    tokens = left.split(None, 1)
    if len(tokens) == 2 and tokens[0].isdigit():
        return f"{tokens[1]}    |    {right}"
    return formatted


def _eval_dependent_lookup(
    rule: dict, row: dict, edits: dict, col_config: dict, user_token: str | None
) -> tuple[bool, str, str]:
    col = rule["column_name"]
    if col not in edits:
        return True, "", ""
    val = _str_val(edits[col])
    if not val:
        return True, "", ""

    dd = col_config.get(col, {}).get("dropdown_source") or {}
    if isinstance(dd, str):
        dd = _parse_json(dd) or {}
    if dd.get("type") != "dependent_lookup":
        return True, "", ""

    parent_col = str(dd.get("parent_column") or "")
    if not parent_col:
        return True, "", ""

    parent_val = _str_val(_resolve(row, edits, parent_col))
    if not parent_val:
        return False, rule.get("reason", "Parent value is required."), rule.get(
            "fix", f"Select {parent_col} before {col}."
        )

    allowed = config_store.dependent_options_for_parent(
        dd, parent_val, default_schema=str(rule.get("schema_name") or ""), user_token=user_token
    )
    if not allowed:
        return False, rule.get("reason", ""), rule.get("fix", "")

    allowed_lower = {str(a).lower() for a in allowed}
    val_lower = val.lower()

    # Preserve unchanged legacy values already on the row
    existing = _str_val(row.get(col))
    if existing and val_lower == existing.lower():
        return True, "", ""

    if val_lower in allowed_lower:
        return True, "", ""

    # Legacy rows stored as "CARRIER    |    SUBCARRIER" (no mdssubcarriercode prefix)
    legacy_allowed = {
        _strip_dependent_code_prefix(str(a)).lower() for a in allowed
    }
    if val_lower in legacy_allowed:
        return True, "", ""

    for part in val.split("|"):
        part = part.strip()
        if part and part.lower() in allowed_lower:
            return True, "", ""

    return False, rule.get("reason", ""), rule.get("fix", "")


def _eval_regex(
    rule: dict, row: dict, edits: dict, col_config: dict, user_token: str | None
) -> tuple[bool, str, str]:
    col = rule["column_name"]
    if col not in edits:
        return True, "", ""
    val = _str_val(edits[col])
    if not val:
        return True, "", ""
    params = _parse_json(rule.get("condition_params")) or {}
    pattern = params.get("pattern", "")
    if pattern and not re.match(pattern, val):
        return False, rule.get("reason", ""), rule.get("fix", "")
    return True, "", ""


def _eval_min_length(
    rule: dict, row: dict, edits: dict, col_config: dict, user_token: str | None
) -> tuple[bool, str, str]:
    col = rule["column_name"]
    if col not in edits:
        return True, "", ""
    val = _str_val(edits[col])
    params = _parse_json(rule.get("condition_params")) or {}
    min_len = int(params.get("value", 0))
    if val and len(val) < min_len:
        return False, rule.get("reason", ""), rule.get("fix", "")
    return True, "", ""


def _eval_max_length(
    rule: dict, row: dict, edits: dict, col_config: dict, user_token: str | None
) -> tuple[bool, str, str]:
    col = rule["column_name"]
    if col not in edits:
        return True, "", ""
    val = _str_val(edits[col])
    params = _parse_json(rule.get("condition_params")) or {}
    max_len = int(params.get("value", 9999))
    if val and len(val) > max_len:
        return False, rule.get("reason", ""), rule.get("fix", "")
    return True, "", ""


def _eval_min_value(
    rule: dict, row: dict, edits: dict, col_config: dict, user_token: str | None
) -> tuple[bool, str, str]:
    col = rule["column_name"]
    if col not in edits:
        return True, "", ""
    params = _parse_json(rule.get("condition_params")) or {}
    try:
        if float(_str_val(edits[col])) < float(params.get("value", 0)):
            return False, rule.get("reason", ""), rule.get("fix", "")
    except (ValueError, TypeError):
        pass
    return True, "", ""


def _eval_max_value(
    rule: dict, row: dict, edits: dict, col_config: dict, user_token: str | None
) -> tuple[bool, str, str]:
    col = rule["column_name"]
    if col not in edits:
        return True, "", ""
    params = _parse_json(rule.get("condition_params")) or {}
    try:
        if float(_str_val(edits[col])) > float(params.get("value", 0)):
            return False, rule.get("reason", ""), rule.get("fix", "")
    except (ValueError, TypeError):
        pass
    return True, "", ""


def _eval_date_order(
    rule: dict, row: dict, edits: dict, col_config: dict, user_token: str | None
) -> tuple[bool, str, str]:
    col = rule["column_name"]
    if col not in edits:
        return True, "", ""
    params = _parse_json(rule.get("condition_params")) or {}
    this_dt = _to_dt(_resolve(row, edits, col))
    if not this_dt:
        return True, "", ""

    if "before_column" in params:
        other_dt = _to_dt(_resolve(row, edits, params["before_column"]))
        if other_dt and this_dt > other_dt:
            return False, rule.get("reason", ""), rule.get("fix", "")

    if "after_column" in params:
        other_dt = _to_dt(_resolve(row, edits, params["after_column"]))
        if other_dt and this_dt < other_dt:
            return False, rule.get("reason", ""), rule.get("fix", "")

    return True, "", ""


def _eval_contains(
    rule: dict, row: dict, edits: dict, col_config: dict, user_token: str | None
) -> tuple[bool, str, str]:
    col = rule["column_name"]
    if col not in edits:
        return True, "", ""
    val = _str_val(edits[col]).lower()
    params = _parse_json(rule.get("condition_params")) or {}
    needle = str(params.get("value", ""))

    if needle.startswith("{") and needle.endswith("}"):
        ref_col = needle[1:-1]
        needle = _str_val(_resolve(row, edits, ref_col)).lower()

    if needle and needle not in val:
        return False, rule.get("reason", ""), rule.get("fix", "")
    return True, "", ""


def _eval_starts_with(
    rule: dict, row: dict, edits: dict, col_config: dict, user_token: str | None
) -> tuple[bool, str, str]:
    col = rule["column_name"]
    if col not in edits:
        return True, "", ""
    val = _str_val(edits[col])
    params = _parse_json(rule.get("condition_params")) or {}
    prefix = str(params.get("value", ""))
    if prefix and not val.startswith(prefix):
        return False, rule.get("reason", ""), rule.get("fix", "")
    return True, "", ""


def _eval_ends_with(
    rule: dict, row: dict, edits: dict, col_config: dict, user_token: str | None
) -> tuple[bool, str, str]:
    col = rule["column_name"]
    if col not in edits:
        return True, "", ""
    val = _str_val(edits[col])
    params = _parse_json(rule.get("condition_params")) or {}
    suffix = str(params.get("value", ""))
    if suffix and not val.endswith(suffix):
        return False, rule.get("reason", ""), rule.get("fix", "")
    return True, "", ""


def _eval_readonly_after_insert(
    rule: dict, row: dict, edits: dict, col_config: dict, user_token: str | None
) -> tuple[bool, str, str]:
    col = rule["column_name"]
    original_val = _str_val(row.get(col, ""))
    if original_val and col in edits and _str_val(edits[col]) != original_val:
        return False, rule.get("reason", ""), rule.get("fix", "")
    return True, "", ""


_EVALUATORS = {
    "allowed_values":        _eval_allowed_values,
    "lookup":                _eval_lookup,
    "dependent_lookup":      _eval_dependent_lookup,
    "regex":                 _eval_regex,
    "min_length":            _eval_min_length,
    "max_length":            _eval_max_length,
    "min_value":             _eval_min_value,
    "max_value":             _eval_max_value,
    "date_order":            _eval_date_order,
    "contains":              _eval_contains,
    "starts_with":           _eval_starts_with,
    "ends_with":             _eval_ends_with,
    "readonly_after_insert": _eval_readonly_after_insert,
}


def run_all_rules(
    schema: str,
    table: str,
    row: dict,
    edits: dict,
    user_token: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Evaluate all active rules for changed columns.

    Returns:
        blocking : list of {column, reason, fix}  — save must be rejected
        warnings : list of {column, reason, fix}  — save proceeds but user is warned
    """
    try:
        config = _load_table_config(schema, table, user_token=user_token)
    except Exception as exc:
        return [{"column": "", "reason": f"Cannot load rules: {exc}", "fix": "Contact the platform team."}], []

    rules = config.get("rules", [])
    col_config = config.get("col_config", {})
    blocking, warnings = [], []

    for rule in rules:
        col = rule.get("column_name", "")
        cond_type = str(rule.get("condition_type", "")).lower()
        evaluator = _EVALUATORS.get(cond_type)

        if not evaluator:
            logger.warning("Unknown condition type: %s", cond_type)
            continue

        if col and col not in edits:
            continue

        try:
            valid, reason, fix = evaluator(rule, row, edits, col_config, user_token)
        except Exception as exc:
            logger.error("Rule eval error col=%s type=%s: %s", col, cond_type, exc)
            continue

        if not valid:
            entry = {"column": col, "reason": reason, "fix": fix}
            if str(rule.get("on_fail", "block")).lower() == "block":
                blocking.append(entry)
            else:
                warnings.append(entry)

    for col, val in edits.items():
        if str(col).lower() in db_client.AUDIT_COLUMN_NAMES:
            continue
        cfg = col_config.get(col, {})
        if not cfg.get("nullable", True) and not _str_val(val):
            blocking.append({
                "column": col,
                "reason": f"'{col}' is required and cannot be empty.",
                "fix": f"Enter a value for '{col}' before saving.",
            })

    return blocking, warnings
