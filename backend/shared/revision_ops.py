"""Revision ids for staged applies — tracked on change_requests + audit log."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from . import db_client

logger = logging.getLogger("delta_editor.revision_ops")

CATALOG = db_client.CATALOG
CHANGE_REQUESTS_TABLE = f"{CATALOG}.dmz.dataeditor_change_requests"


def new_revision_id() -> str:
    return f"rev-{uuid.uuid4().hex[:12]}"


def next_revision_no(schema: str, table: str, user_token: str | None = None) -> int:
    try:
        df = db_client.query(
            f"""
            SELECT COALESCE(MAX(revision_no), 0) + 1 AS n
            FROM {CHANGE_REQUESTS_TABLE}
            WHERE schema_name = ? AND table_name = ?
              AND status = 'applied'
            """,
            params=(schema, table),
            user_token=user_token,
        )
        if not df.empty:
            return int(df.iloc[0]["n"])
    except Exception as exc:
        logger.warning("revision_no lookup failed for %s.%s: %s", schema, table, exc)
    return 1


def create_revision(
    *,
    change_request_id: str,
    schema: str,
    table: str,
    applied_by: str,
    change_source: str,
    row_count: int,
    column_count: int,
    summary: dict[str, Any] | None = None,
    user_token: str | None = None,
) -> str:
    """Return a revision id; summary lives on change_request.change_summary."""
    del applied_by, change_source, row_count, column_count, summary
    revision_id = new_revision_id()
    rev_no = next_revision_no(schema, table, user_token=user_token)
    logger.info(
        "Revision %s (#%s) for %s on %s.%s",
        revision_id, rev_no, change_request_id, schema, table,
    )
    return revision_id
