"""Dispatch apply by upload mode (update / append / overwrite)."""
from __future__ import annotations

from typing import Any

from . import bulk_update_ops, bulk_upload_ops, bulk_upsert_ops, change_request, grid_staging_ops


def apply_change_request(
    change_request_id: str,
    *,
    applied_by: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    rec = change_request.get_request(change_request_id, user_token=user_token)
    if not rec:
        raise ValueError("Change request not found.")

    request_type = str(rec.get("request_type") or "")
    if request_type == "grid_edit":
        return grid_staging_ops.apply_grid_change_request(
            change_request_id, applied_by=applied_by, user_token=user_token
        )

    mode = str(rec.get("mode") or "")
    if mode == "upsert":
        return bulk_upsert_ops.apply_upsert_change_request(
            change_request_id, applied_by=applied_by, user_token=user_token
        )
    if mode == "update":
        return bulk_update_ops.apply_update_change_request(
            change_request_id, applied_by=applied_by, user_token=user_token
        )
    if mode == "append":
        return bulk_upload_ops.apply_append_change_request(
            change_request_id, applied_by=applied_by, user_token=user_token
        )
    if mode == "overwrite":
        return bulk_upload_ops.apply_overwrite_change_request(
            change_request_id, applied_by=applied_by, user_token=user_token
        )
    raise ValueError(f"Unsupported upload mode: {mode}")
