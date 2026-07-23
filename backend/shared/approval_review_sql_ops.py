"""Persist and serve approver compare SQL (staged vs live) for each change request."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from . import change_request, config_store, db_client, staging_ops

logger = logging.getLogger("delta_editor.approval_review_sql_ops")

CATALOG = db_client.CATALOG
REVIEW_SQL_TABLE = f"{CATALOG}.dmz.dataeditor_approval_review_sql"
SQL_VERSION = 1
QUICK_PREVIEW_MAX_ROWS = int(os.environ.get("APPROVAL_QUICK_PREVIEW_MAX_ROWS", "25"))


def _sql_ident(name: str) -> str:
    return f"`{str(name).replace('`', '')}`"


def _sql_str(val: str) -> str:
    return str(val).replace("'", "''")


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def databricks_sql_editor_url() -> str:
    host = (
        os.environ.get("DATABRICKS_SQL_EDITOR_URL", "").strip()
        or os.environ.get("DATABRICKS_HOST", "").strip()
        or os.environ.get("DATABRICKS_SERVER_HOSTNAME", "").strip()
    )
    if not host:
        return ""
    if host.startswith("http"):
        base = host.rstrip("/")
    else:
        base = f"https://{host.rstrip('/')}"
    if base.endswith("/sql/editor"):
        return base
    return f"{base}/sql/editor"


def _compare_column_lists(
    schema: str,
    table: str,
    summary: dict[str, Any],
    *,
    user_token: str | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Return (pk_cols, changed_cols, select_identity_cols)."""
    pk_cols = sorted(config_store.get_pk_cols(schema, table, user_token=user_token))
    pk_lower = {c.lower() for c in pk_cols}
    changed_cols: list[str] = []
    for col in summary.get("columns_changing") or []:
        name = str(col)
        if name.lower() in pk_lower:
            continue
        if name and name not in changed_cols:
            changed_cols.append(name)
    identity_cols = list(pk_cols)
    return pk_cols, changed_cols, identity_cols


def compose_compare_sql(rec: dict[str, Any], *, user_token: str | None = None) -> dict[str, Any]:
    """Build single staged-vs-live compare SQL for a change request."""
    change_request_id = str(rec.get("change_request_id") or "")
    schema = str(rec.get("schema_name") or "")
    table = str(rec.get("table_name") or "")
    catalog = str(rec.get("catalog") or CATALOG)
    cr_id = _sql_str(change_request_id)

    staging_full = str(rec.get("staging_table_name") or "").strip()
    if not staging_full:
        staging_full = staging_ops.full_app_stage_table(schema, table, catalog=catalog)
    target_full = f"{catalog}.{schema}.{table}"

    summary: dict[str, Any] = {}
    try:
        summary = json.loads(rec.get("change_summary") or "{}")
    except json.JSONDecodeError:
        pass

    pk_cols, changed_cols, identity_cols = _compare_column_lists(
        schema, table, summary, user_token=user_token,
    )
    cr_col = staging_ops.STAGE_CR_ID
    op_col = staging_ops.STAGE_OPERATION

    if not pk_cols:
        compare_sql = (
            f"-- Compare staged vs live ({change_request_id})\n"
            f"-- No primary key configured — review staged rows only.\n"
            f"SELECT s.*\n"
            f"FROM {staging_full} s\n"
            f"WHERE s.{_sql_ident(cr_col)} = '{cr_id}';"
        )
    else:
        join_cond = " AND ".join(
            f"s.{_sql_ident(pk)} <=> t.{_sql_ident(pk)}" for pk in pk_cols
        )
        select_parts = [f"s.{_sql_ident(op_col)} AS stage_operation"]
        for col in identity_cols:
            select_parts.append(f"s.{_sql_ident(col)} AS {_sql_ident(col).strip('`')}")
        for col in changed_cols:
            select_parts.append(f"s.{_sql_ident(col)} AS staged_{col}")
            select_parts.append(f"t.{_sql_ident(col)} AS live_{col}")
        compare_sql = (
            f"-- Compare staged vs live ({change_request_id})\n"
            f"-- Primary keys plus changed columns only.\n"
            f"SELECT\n  "
            + ",\n  ".join(select_parts)
            + f"\nFROM {staging_full} s\n"
            f"LEFT JOIN {target_full} t ON {join_cond}\n"
            f"WHERE s.{_sql_ident(cr_col)} = '{cr_id}'\n"
            f"ORDER BY {', '.join(f's.{_sql_ident(c)}' for c in pk_cols)};"
        )

    return {
        "change_request_id": change_request_id,
        "schema_name": schema,
        "table_name": table,
        "display_name": config_store.get_table_display_name(schema, table, user_token=user_token),
        "catalog": catalog,
        "staging_table": staging_full,
        "target_table": target_full,
        "compare_sql": compare_sql,
        "pk_columns": pk_cols,
        "changed_columns": changed_cols,
        "compare_columns": identity_cols + changed_cols,
        "sql_version": SQL_VERSION,
        "databricks_sql_editor_url": databricks_sql_editor_url(),
        "quick_preview_eligible": _quick_preview_eligible(rec),
    }


def _quick_preview_eligible(rec: dict[str, Any]) -> bool:
    summary: dict[str, Any] = {}
    try:
        summary = json.loads(rec.get("change_summary") or "{}")
    except json.JSONDecodeError:
        pass
    row_count = rec.get("row_count")
    if row_count is None:
        row_count = summary.get("total_rows")
    try:
        return int(row_count or 0) <= QUICK_PREVIEW_MAX_ROWS
    except (TypeError, ValueError):
        return False


def persist_review_sql_log(
    rec: dict[str, Any],
    payload: dict[str, Any],
    *,
    user_token: str | None = None,
) -> None:
    """Upsert compare SQL for a change request (audit / future reference)."""
    cr_id = str(rec.get("change_request_id") or payload.get("change_request_id") or "")
    if not cr_id:
        return
    now = _utc_now_naive()
    compare_sql = str(payload.get("compare_sql") or "")
    if not compare_sql:
        return

    meta = {
        "pk_columns": payload.get("pk_columns") or [],
        "changed_columns": payload.get("changed_columns") or [],
        "compare_columns": payload.get("compare_columns") or [],
        "sql_version": payload.get("sql_version") or SQL_VERSION,
    }
    try:
        db_client.execute(
            f"DELETE FROM {REVIEW_SQL_TABLE} WHERE change_request_id = ?",
            params=(cr_id,),
            user_token=user_token,
        )
        db_client.execute(
            f"""
            INSERT INTO {REVIEW_SQL_TABLE} (
              change_request_id, catalog, schema_name, table_name,
              staging_table, target_table, compare_sql, compare_columns_json,
              sql_version, generated_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params=(
                cr_id,
                str(payload.get("catalog") or rec.get("catalog") or CATALOG),
                str(payload.get("schema_name") or rec.get("schema_name") or ""),
                str(payload.get("table_name") or rec.get("table_name") or ""),
                str(payload.get("staging_table") or rec.get("staging_table_name") or ""),
                str(payload.get("target_table") or ""),
                compare_sql,
                json.dumps(meta, default=str),
                int(payload.get("sql_version") or SQL_VERSION),
                now,
                now,
            ),
            user_token=user_token,
        )
    except Exception as exc:
        logger.warning("Could not persist approval review SQL for %s: %s", cr_id, exc)


def get_review_sql_log(
    change_request_id: str,
    *,
    user_token: str | None = None,
) -> dict[str, Any] | None:
    df = db_client.query(
        f"SELECT * FROM {REVIEW_SQL_TABLE} WHERE change_request_id = ? LIMIT 1",
        params=(change_request_id,),
        user_token=user_token,
    )
    if df.empty:
        return None
    row = df.iloc[0].to_dict()
    meta = {}
    try:
        meta = json.loads(row.get("compare_columns_json") or "{}")
    except json.JSONDecodeError:
        pass
    return {
        "change_request_id": row.get("change_request_id"),
        "catalog": row.get("catalog"),
        "schema_name": row.get("schema_name"),
        "table_name": row.get("table_name"),
        "staging_table": row.get("staging_table"),
        "target_table": row.get("target_table"),
        "compare_sql": row.get("compare_sql"),
        "pk_columns": meta.get("pk_columns") or [],
        "changed_columns": meta.get("changed_columns") or [],
        "compare_columns": meta.get("compare_columns") or [],
        "sql_version": meta.get("sql_version") or row.get("sql_version"),
        "generated_at": row.get("generated_at"),
        "updated_at": row.get("updated_at"),
    }


def get_or_build_review_sql(
    change_request_id: str,
    *,
    user_token: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Return compare SQL payload; read from log when present else compose and store."""
    rec = change_request.get_request(change_request_id, user_token=user_token)
    if not rec:
        raise ValueError("Change request not found.")

    logged = get_review_sql_log(change_request_id, user_token=user_token)
    if logged and logged.get("compare_sql"):
        payload = {
            **logged,
            "display_name": config_store.get_table_display_name(
                str(logged.get("schema_name") or ""),
                str(logged.get("table_name") or ""),
                user_token=user_token,
            ),
            "databricks_sql_editor_url": databricks_sql_editor_url(),
            "quick_preview_eligible": _quick_preview_eligible(rec),
            "logged_at": str(logged.get("generated_at") or ""),
        }
    else:
        payload = compose_compare_sql(rec, user_token=user_token)
        if persist:
            persist_review_sql_log(rec, payload, user_token=user_token)
            payload["logged_at"] = _utc_now_naive().isoformat()
        else:
            payload["logged_at"] = None

    payload["queries"] = [{
        "id": "compare",
        "label": "Staged vs live (PK + changed columns)",
        "description": "Run in Databricks SQL to compare staged rows with the live table.",
        "sql": payload["compare_sql"],
    }]
    return payload


def ensure_review_sql_for_approval(
    change_request_id: str,
    *,
    user_token: str | None = None,
) -> dict[str, Any]:
    """Generate and persist compare SQL when a request enters pending approval."""
    return get_or_build_review_sql(change_request_id, user_token=user_token, persist=True)
