"""Dashboard metrics for the Overview landing page."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import change_request, db_client

logger = logging.getLogger("delta_editor.overview_ops")

CATALOG = db_client.CATALOG
CHANGE_REQUEST_TABLE = f"{CATALOG}.dmz.dataeditor_change_requests"
AUDIT_TABLE = f"{CATALOG}.dmz.dataeditor_app_audit_log"
REGISTRY_TABLE = f"{CATALOG}.dmz.dataeditor_table_registry"

_CACHE_TTL_SEC = 60
_overview_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _records(df) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    return df.to_dict("records")


def _cache_get(user_email: str) -> dict[str, Any] | None:
    key = str(user_email or "").strip().lower() or "anonymous"
    with _cache_lock:
        entry = _overview_cache.get(key)
        if not entry:
            return None
        ts, payload = entry
        if time.time() - ts > _CACHE_TTL_SEC:
            _overview_cache.pop(key, None)
            return None
        return payload


def _cache_set(user_email: str, payload: dict[str, Any]) -> None:
    key = str(user_email or "").strip().lower() or "anonymous"
    with _cache_lock:
        _overview_cache[key] = (time.time(), payload)


def invalidate_cache(user_email: str | None = None) -> None:
    """Clear overview cache (all users or one user)."""
    with _cache_lock:
        if user_email:
            key = str(user_email).strip().lower()
            _overview_cache.pop(key, None)
        else:
            _overview_cache.clear()


def _fetch_metrics(user_token: str | None) -> dict[str, int]:
    """Single round-trip for all overview count metrics."""
    df = db_client.query(
        f"""
        SELECT
          (SELECT COUNT(*) FROM {REGISTRY_TABLE} WHERE is_active = TRUE) AS registered_tables,
          (SELECT COUNT(*) FROM {AUDIT_TABLE}
            WHERE changed_at >= current_timestamp() - INTERVAL 1 DAY) AS edits_last_24h,
          (SELECT COUNT(*) FROM {CHANGE_REQUEST_TABLE}
            WHERE status IN (?, ?)) AS staged_requests
        """,
        params=(
            change_request.STATUS_PENDING_APPROVAL,
            change_request.STATUS_VALIDATED,
        ),
        user_token=user_token,
    )
    if df.empty:
        return {
            "registered_tables": 0,
            "edits_last_24h": 0,
            "staged_requests": 0,
        }
    row = df.iloc[0]
    return {
        "registered_tables": int(row.get("registered_tables") or 0),
        "edits_last_24h": int(row.get("edits_last_24h") or 0),
        "staged_requests": int(row.get("staged_requests") or 0),
    }


def _fetch_recent_edits(
    user_token: str | None,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    df = db_client.query(
        f"""
        SELECT changed_by, changed_at, table_schema, table_name, record_key,
               column_name, change_source, change_request_id
        FROM {AUDIT_TABLE}
        ORDER BY changed_at DESC
        LIMIT {int(limit)}
        """,
        user_token=user_token,
    )
    return _records(df)


def _fetch_recent_requests(
    user_token: str | None,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    df = db_client.query(
        f"""
        SELECT change_request_id, status, request_type, mode, catalog, schema_name, table_name,
               submitted_by, submitted_at, updated_at, row_count, requires_approval
        FROM {CHANGE_REQUEST_TABLE}
        WHERE status IN (?, ?, ?, ?, ?)
        ORDER BY COALESCE(updated_at, submitted_at) DESC
        LIMIT {int(limit)}
        """,
        params=(
            change_request.STATUS_PENDING_APPROVAL,
            change_request.STATUS_VALIDATED,
            change_request.STATUS_APPROVED,
            change_request.STATUS_APPLIED,
            change_request.STATUS_REJECTED,
        ),
        user_token=user_token,
    )
    return _records(df)


def get_overview(
    user_email: str,
    *,
    user_token: str | None = None,
    recent_edits_limit: int = 15,
    recent_requests_limit: int = 10,
    force_refresh: bool = False,
) -> dict[str, Any]:
    if not force_refresh:
        cached = _cache_get(user_email)
        if cached is not None:
            logger.debug("Overview cache hit for %s", user_email)
            return cached

    pending: list[dict[str, Any]] = []
    metrics: dict[str, int] = {
        "registered_tables": 0,
        "edits_last_24h": 0,
        "staged_requests": 0,
    }
    recent_edits: list[dict[str, Any]] = []
    recent_requests: list[dict[str, Any]] = []

    tasks = {
        "pending": lambda: change_request.list_pending_for_approver(
            user_email, user_token=user_token, page_size=50, page=1,
        )["items"],
        "metrics": lambda: _fetch_metrics(user_token),
        "recent_edits": lambda: _fetch_recent_edits(
            user_token, limit=recent_edits_limit
        ),
        "recent_requests": lambda: _fetch_recent_requests(
            user_token, limit=recent_requests_limit
        ),
    }

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="overview") as pool:
        futures = {pool.submit(fn): name for name, fn in tasks.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                result = fut.result()
                if name == "pending":
                    pending = result
                elif name == "metrics":
                    metrics = result
                elif name == "recent_edits":
                    recent_edits = result
                else:
                    recent_requests = result
            except Exception as exc:
                logger.warning("Overview %s query failed: %s", name, exc)

    payload = {
        "metrics": {
            "pending_approvals": len(pending),
            "registered_tables": metrics["registered_tables"],
            "edits_last_24h": metrics["edits_last_24h"],
            "staged_requests": metrics["staged_requests"],
        },
        "pending_approvals": pending,
        "recent_edits": recent_edits,
        "recent_requests": recent_requests,
    }
    _cache_set(user_email, payload)
    return payload
