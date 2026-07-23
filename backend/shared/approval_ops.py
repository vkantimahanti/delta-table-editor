"""Approver workflow for staged uploads (Phase 6)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from . import change_request, change_request_lines, config_store, db_client, staging_ops
from . import approval_review_sql_ops

logger = logging.getLogger("delta_editor.approval_ops")

APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_utc_naive(value: datetime) -> datetime:
    """Normalize DB/pandas timestamps for safe comparison with stored UTC values."""
    if getattr(value, "tzinfo", None) is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _parse_emails(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(e).strip().lower() for e in data if str(e).strip()]
        except json.JSONDecodeError:
            pass
    return [e.strip().lower() for e in text.replace(";", ",").split(",") if e.strip()]


def needs_approval(policy: dict[str, Any], mode: str) -> bool:
    if not policy.get("requires_upload_approval"):
        return False
    approvers = _parse_emails(str(policy.get("approver_emails") or ""))
    return bool(approvers)


def is_approver(user_email: str, policy: dict[str, Any], *, rec: dict[str, Any] | None = None) -> bool:
    email = str(user_email or "").strip().lower()
    if not email or email == "local_dev":
        return False
    raw = str((rec or {}).get("approver_emails") or policy.get("approver_emails") or "")
    return email in _parse_emails(raw)


def new_approval_token() -> tuple[str, str]:
    plain = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(plain.encode()).hexdigest()
    return plain, token_hash


def approval_review_url(token: str) -> str:
    path = f"/?tab=approvals&token={quote(token)}"
    return f"{APP_BASE_URL}{path}" if APP_BASE_URL else path


def notify_approvers(
    *,
    change_request_id: str,
    schema: str,
    table: str,
    mode: str,
    submitted_by: str,
    approver_emails: list[str],
    review_url: str,
    summary: dict[str, Any] | None = None,
    compare_sql: str = "",
    user_token: str | None = None,
) -> None:
    """Queue approval alert (outbox) and log for operators."""
    diff_count = len((summary or {}).get("all_diffs") or [])
    rows_changing = (summary or {}).get("rows_with_changes") or (summary or {}).get("total_rows") or "?"
    subject = f"[MDS] Approval required — {schema}.{table}"
    body = (
        f"Change request: {change_request_id}\n"
        f"Table: {schema}.{table}\n"
        f"Mode: {mode}\n"
        f"Submitted by: {submitted_by}\n"
        f"Rows changing: {rows_changing}\n"
        f"Field changes: {diff_count}\n"
        f"Review in app: {review_url}\n"
    )
    if compare_sql:
        body += (
            f"\nCompare SQL (also logged to dmz.dataeditor_approval_review_sql):\n"
            f"{compare_sql}\n"
        )
    logger.info(
        "Approval required for %s — table %s.%s mode=%s submitted_by=%s approvers=%s url=%s",
        change_request_id, schema, table, mode, submitted_by,
        ", ".join(approver_emails), review_url,
    )
    _enqueue_notifications(
        change_request_id=change_request_id,
        recipients=approver_emails,
        subject=subject,
        body_text=body,
        user_token=user_token,
    )


def _enqueue_notifications(
    *,
    change_request_id: str,
    recipients: list[str],
    subject: str,
    body_text: str,
    user_token: str | None = None,
) -> None:
    """Log-only notification hook (no separate outbox table)."""
    del user_token
    for email in recipients:
        logger.info(
            "Notification queued (log only) cr=%s to=%s subject=%s",
            change_request_id, email, subject,
        )
        logger.debug("Notification body:\n%s", body_text)


def queue_for_approval(
    *,
    change_request_id: str,
    schema: str,
    table: str,
    mode: str,
    submitted_by: str,
    summary: dict[str, Any],
    request_type: str = "upload",
    user_token: str | None = None,
) -> dict[str, Any]:
    policy = config_store.get_approval_policy(schema, table, request_type, user_token=user_token)
    approvers = _parse_emails(str(policy.get("approver_emails") or ""))
    if not approvers:
        raise ValueError("Approval is required but no approver_emails configured for this table.")

    plain_token, token_hash = new_approval_token()
    hours = int(policy.get("upload_approval_expiry_hours") or 72)
    expires = _utc_now_naive() + timedelta(hours=hours)
    review_url = approval_review_url(plain_token)

    change_request.update_request(
        change_request_id,
        status=change_request.STATUS_PENDING_APPROVAL,
        requires_approval=True,
        approver_emails=",".join(approvers),
        approval_status="pending",
        expires_at=expires,
        approval_token_hash=token_hash,
        user_token=user_token,
    )
    sql_payload = approval_review_sql_ops.ensure_review_sql_for_approval(
        change_request_id, user_token=user_token,
    )
    notify_approvers(
        change_request_id=change_request_id,
        schema=schema,
        table=table,
        mode=mode,
        submitted_by=submitted_by,
        approver_emails=approvers,
        review_url=review_url,
        summary=summary,
        compare_sql=str(sql_payload.get("compare_sql") or ""),
        user_token=user_token,
    )
    return {
        "requires_approval": True,
        "can_apply": False,
        "status": change_request.STATUS_PENDING_APPROVAL,
        "approval_status": "pending",
        "approver_emails": approvers,
        "expires_at": expires.isoformat(),
        "review_url": review_url,
        "compare_sql": sql_payload.get("compare_sql"),
        "databricks_sql_editor_url": sql_payload.get("databricks_sql_editor_url"),
    }


def finalize_validation_result(
    result: dict[str, Any],
    *,
    schema: str,
    table: str,
    submitted_by: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    """After successful validation, optionally route to approver queue."""
    if not result.get("can_apply"):
        return result
    request_type = str(result.get("request_type") or "upload")
    policy = config_store.get_approval_policy(schema, table, request_type, user_token=user_token)
    mode = str(result.get("mode") or "")
    if not needs_approval(policy, mode):
        return result

    approval_info = queue_for_approval(
        change_request_id=str(result["change_request_id"]),
        schema=schema,
        table=table,
        mode=mode,
        submitted_by=submitted_by,
        summary=result.get("summary") or {},
        request_type=request_type,
        user_token=user_token,
    )
    return {**result, **approval_info}


def approve_request(
    change_request_id: str,
    *,
    approver: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    rec = change_request.get_request(change_request_id, user_token=user_token)
    if not rec:
        raise ValueError("Change request not found.")
    if str(rec.get("status")) != change_request.STATUS_PENDING_APPROVAL:
        raise ValueError(f"Request is not pending approval (status={rec.get('status')}).")

    schema = str(rec["schema_name"])
    table = str(rec["table_name"])
    policy = config_store.get_approval_policy(schema, table, str(rec.get("request_type") or "upload"), user_token=user_token)
    if not is_approver(approver, policy, rec=rec):
        raise ValueError("You are not an approver for this request.")

    expires = rec.get("expires_at")
    if expires is not None:
        if hasattr(expires, "to_pydatetime"):
            expires = expires.to_pydatetime()
        elif not isinstance(expires, datetime):
            expires = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        if _as_utc_naive(expires) < _utc_now_naive():
            change_request.update_request(
                change_request_id,
                status=change_request.STATUS_EXPIRED,
                user_token=user_token,
            )
            raise ValueError("This approval request has expired.")

    now = _utc_now_naive()
    change_request.update_request(
        change_request_id,
        status=change_request.STATUS_APPROVED,
        approval_status="approved",
        approved_by=approver,
        approved_at=now,
        user_token=user_token,
    )
    return {
        "change_request_id": change_request_id,
        "status": change_request.STATUS_APPROVED,
        "approval_status": "approved",
        "can_apply": True,
    }


def reject_request(
    change_request_id: str,
    *,
    approver: str,
    reason: str = "",
    user_token: str | None = None,
) -> dict[str, Any]:
    rec = change_request.get_request(change_request_id, user_token=user_token)
    if not rec:
        raise ValueError("Change request not found.")
    if str(rec.get("status")) != change_request.STATUS_PENDING_APPROVAL:
        raise ValueError(f"Request is not pending approval (status={rec.get('status')}).")

    schema = str(rec["schema_name"])
    table = str(rec["table_name"])
    policy = config_store.get_approval_policy(schema, table, str(rec.get("request_type") or "upload"), user_token=user_token)
    if not is_approver(approver, policy, rec=rec):
        raise ValueError("You are not an approver for this request.")

    now = _utc_now_naive()
    change_request.update_request(
        change_request_id,
        status=change_request.STATUS_REJECTED,
        approval_status="rejected",
        rejected_by=approver,
        rejected_at=now,
        rejection_reason=reason or "Rejected by approver.",
        user_token=user_token,
    )
    catalog = str(rec.get("catalog") or db_client.CATALOG)
    staging_ops.drop_staging_table(
        change_request_id, user_token=user_token,
        schema=schema, table_name=table, catalog=catalog,
    )
    return {
        "change_request_id": change_request_id,
        "status": change_request.STATUS_REJECTED,
        "approval_status": "rejected",
        "can_apply": False,
    }


def get_review_by_token(
    token: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    rec = change_request.find_by_approval_token_hash(token_hash, user_token=user_token)
    if not rec:
        raise ValueError("Invalid or expired approval link.")
    summary = {}
    try:
        summary = json.loads(rec.get("change_summary") or "{}")
    except json.JSONDecodeError:
        pass
    schema_name = str(rec.get("schema_name") or "")
    table_name = str(rec.get("table_name") or "")
    pk_cols = sorted(config_store.get_pk_cols(schema_name, table_name, user_token=user_token))
    business_key_cols = config_store.get_upload_unique_columns(schema_name, table_name) or pk_cols
    return {
        "change_request_id": rec.get("change_request_id"),
        "status": rec.get("status"),
        "mode": rec.get("mode"),
        "schema_name": schema_name,
        "table_name": table_name,
        "submitted_by": rec.get("submitted_by"),
        "submitted_at": str(rec.get("submitted_at") or ""),
        "row_count": rec.get("row_count"),
        "summary": summary,
        "approval_status": rec.get("approval_status"),
        "request_type": rec.get("request_type"),
        "pk_cols": pk_cols,
        "business_key_cols": business_key_cols,
        "diffs": get_change_request_diffs(str(rec.get("change_request_id")), user_token=user_token),
    }


def get_change_request_diffs(
    change_request_id: str,
    user_token: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Line-level diffs from change_summary.all_diffs."""
    del limit
    rec = change_request.get_request(change_request_id, user_token=user_token)
    if not rec:
        return []
    return change_request_lines.diffs_from_summary(rec)


def build_review_sql(
    change_request_id: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    """Return logged or freshly built compare SQL for approver review."""
    return approval_review_sql_ops.get_or_build_review_sql(
        change_request_id, user_token=user_token, persist=True,
    )
