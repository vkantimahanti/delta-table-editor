"""Persist bulk upload/export jobs in dataeditor_change_requests."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from . import config_store, db_client

logger = logging.getLogger("delta_editor.change_request")

CATALOG = db_client.CATALOG
CHANGE_REQUEST_TABLE = f"{CATALOG}.dmz.dataeditor_change_requests"

STATUS_DRAFT = "draft"
STATUS_VALIDATED = "validated"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"

APPLYABLE_STATUSES = frozenset({STATUS_VALIDATED, STATUS_APPROVED})


def new_change_request_id() -> str:
    return f"cr-{uuid.uuid4().hex[:12]}"


def _json(val: Any) -> str | None:
    if val is None:
        return None
    return json.dumps(val, default=str)


def insert_request(
    *,
    change_request_id: str,
    request_type: str,
    mode: str | None,
    schema_name: str,
    table_name: str,
    submitted_by: str,
    catalog: str | None = None,
    user_token: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db_client.execute(
        f"""
        INSERT INTO {CHANGE_REQUEST_TABLE} (
          change_request_id, status, request_type, mode,
          catalog, schema_name, table_name,
          submitted_by, submitted_at, updated_at,
          requires_approval
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        params=(
            change_request_id,
            STATUS_DRAFT,
            request_type,
            mode,
            catalog or CATALOG,
            schema_name,
            table_name,
            submitted_by,
            now,
            now,
            False,
        ),
        user_token=user_token,
    )


def update_request(
    change_request_id: str,
    *,
    status: str | None = None,
    staging_table_name: str | None = None,
    source_file_volume_path: str | None = None,
    export_volume_path: str | None = None,
    row_count: int | None = None,
    validation_summary: dict | None = None,
    change_summary: dict | None = None,
    filter_snapshot: dict | None = None,
    errors_json: list | None = None,
    failure_reason: str | None = None,
    validated_at: datetime | None = None,
    applied_at: datetime | None = None,
    requires_approval: bool | None = None,
    approver_emails: str | None = None,
    approval_status: str | None = None,
    approved_by: str | None = None,
    approved_at: datetime | None = None,
    rejected_by: str | None = None,
    rejected_at: datetime | None = None,
    rejection_reason: str | None = None,
    expires_at: datetime | None = None,
    approval_token_hash: str | None = None,
    revision_id: str | None = None,
    user_token: str | None = None,
) -> None:
    sets: list[str] = ["updated_at = current_timestamp()"]
    params: list[Any] = []

    def add(col: str, val: Any) -> None:
        sets.append(f"{col} = ?")
        params.append(val)

    if status is not None:
        add("status", status)
    if staging_table_name is not None:
        add("staging_table_name", staging_table_name)
    if source_file_volume_path is not None:
        add("source_file_volume_path", source_file_volume_path)
    if export_volume_path is not None:
        add("export_volume_path", export_volume_path)
    if row_count is not None:
        add("row_count", row_count)
    if validation_summary is not None:
        add("validation_summary", _json(validation_summary))
    if change_summary is not None:
        add("change_summary", _json(change_summary))
    if filter_snapshot is not None:
        add("filter_snapshot", _json(filter_snapshot))
    if errors_json is not None:
        add("errors_json", _json(errors_json))
    if failure_reason is not None:
        add("failure_reason", failure_reason)
    if validated_at is not None:
        add("validated_at", validated_at)
    if applied_at is not None:
        add("applied_at", applied_at)
    if requires_approval is not None:
        add("requires_approval", requires_approval)
    if approver_emails is not None:
        add("approver_emails", approver_emails)
    if approval_status is not None:
        add("approval_status", approval_status)
    if approved_by is not None:
        add("approved_by", approved_by)
    if approved_at is not None:
        add("approved_at", approved_at)
    if rejected_by is not None:
        add("rejected_by", rejected_by)
    if rejected_at is not None:
        add("rejected_at", rejected_at)
    if rejection_reason is not None:
        add("rejection_reason", rejection_reason)
    if expires_at is not None:
        add("expires_at", expires_at)
    if approval_token_hash is not None:
        add("approval_token_hash", approval_token_hash)
    if revision_id is not None:
        add("revision_id", revision_id)

    params.append(change_request_id)
    db_client.execute(
        f"UPDATE {CHANGE_REQUEST_TABLE} SET {', '.join(sets)} WHERE change_request_id = ?",
        params=tuple(params),
        user_token=user_token,
    )


def get_request(change_request_id: str, user_token: str | None = None) -> dict[str, Any] | None:
    df = db_client.query(
        f"SELECT * FROM {CHANGE_REQUEST_TABLE} WHERE change_request_id = ? LIMIT 1",
        params=(change_request_id,),
        user_token=user_token,
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def _pending_approver_filter(email: str) -> tuple[str, tuple[Any, ...]]:
    return (
        "status = ? AND request_type IN ('upload', 'grid_edit') AND LOWER(approver_emails) LIKE ?",
        (STATUS_PENDING_APPROVAL, f"%{email}%"),
    )


def list_pending_for_approver(
    approver_email: str,
    *,
    user_token: str | None = None,
    page: int = 1,
    page_size: int = 20,
    schema_name: str | None = None,
    table_name: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Paginated pending approvals inbox for an approver."""
    email = str(approver_email or "").strip().lower()
    if not email:
        return {"items": [], "total": 0, "page": 1, "page_size": page_size, "tables": []}

    if limit is not None:
        page_size = int(limit)
        page = 1

    page_size = min(max(1, int(page_size)), 50)
    page = max(1, int(page))
    offset = (page - 1) * page_size

    filter_sql, base_params = _pending_approver_filter(email)
    where_sql = filter_sql
    params: list[Any] = list(base_params)
    if schema_name:
        where_sql += " AND schema_name = ?"
        params.append(schema_name)
    if table_name:
        where_sql += " AND table_name = ?"
        params.append(table_name)

    count_df = db_client.query(
        f"SELECT COUNT(*) AS n FROM {CHANGE_REQUEST_TABLE} WHERE {where_sql}",
        params=tuple(params),
        user_token=user_token,
    )
    total = int(count_df.iloc[0]["n"]) if not count_df.empty else 0

    df = db_client.query(
        f"""
        SELECT * FROM {CHANGE_REQUEST_TABLE}
        WHERE {where_sql}
        ORDER BY submitted_at DESC
        LIMIT {page_size} OFFSET {offset}
        """,
        params=tuple(params),
        user_token=user_token,
    )
    items = df.to_dict("records") if not df.empty else []
    for row in items:
        row["display_name"] = config_store.get_table_display_name(
            str(row.get("schema_name") or ""),
            str(row.get("table_name") or ""),
            user_token=user_token,
        )

    tables_df = db_client.query(
        f"""
        SELECT schema_name, table_name, COUNT(*) AS pending_count
        FROM {CHANGE_REQUEST_TABLE}
        WHERE {filter_sql}
        GROUP BY schema_name, table_name
        ORDER BY schema_name, table_name
        """,
        params=base_params,
        user_token=user_token,
    )
    tables: list[dict[str, Any]] = []
    if not tables_df.empty:
        for _, trow in tables_df.iterrows():
            sch = str(trow.get("schema_name") or "")
            tbl = str(trow.get("table_name") or "")
            tables.append({
                "schema_name": sch,
                "table_name": tbl,
                "display_name": config_store.get_table_display_name(sch, tbl, user_token=user_token),
                "pending_count": int(trow.get("pending_count") or 0),
            })

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "tables": tables,
    }


def find_by_approval_token_hash(
    token_hash: str,
    user_token: str | None = None,
) -> dict[str, Any] | None:
    df = db_client.query(
        f"SELECT * FROM {CHANGE_REQUEST_TABLE} WHERE approval_token_hash = ? LIMIT 1",
        params=(token_hash,),
        user_token=user_token,
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()
