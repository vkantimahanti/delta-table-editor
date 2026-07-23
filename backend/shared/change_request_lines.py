"""Line-level diffs — stored in change_requests.change_summary JSON (all_diffs)."""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("delta_editor.change_request_lines")


def save_lines(
    change_request_id: str,
    diffs: list[dict[str, Any]],
    *,
    user_token: str | None = None,
) -> None:
    """No-op: diffs are persisted in change_summary.all_diffs on the change request."""
    if diffs:
        logger.debug("Diffs for %s kept in change_summary (%d lines)", change_request_id, len(diffs))


def list_lines(
    change_request_id: str,
    *,
    user_token: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    return []


def diffs_from_summary(rec: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        summary = json.loads(rec.get("change_summary") or "{}")
    except json.JSONDecodeError:
        return []
    return list(summary.get("all_diffs") or [])
