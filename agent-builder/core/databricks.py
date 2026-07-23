"""Fetch table schema from Databricks SQL warehouse."""
from __future__ import annotations

import logging
import os

from databricks import sql as dbsql

from .config import databricks_catalog, load_env

logger = logging.getLogger("agent_builder.databricks")


def _connection():
    load_env()
    hostname = (
        os.environ.get("DATABRICKS_SERVER_HOSTNAME")
        or os.environ.get("DATABRICKS_HOST", "").replace("https://", "").rstrip("/")
    )
    http_path = (
        os.environ.get("DATABRICKS_HTTP_PATH")
        or f"/sql/1.0/warehouses/{os.environ.get('DATABRICKS_WAREHOUSE_ID', '')}"
    )
    token = os.environ.get("DATABRICKS_TOKEN", "")
    if not hostname or not token or not http_path:
        raise EnvironmentError(
            "Set DATABRICKS_HOST, DATABRICKS_HTTP_PATH (or WAREHOUSE_ID), DATABRICKS_TOKEN"
        )
    return dbsql.connect(
        server_hostname=hostname,
        http_path=http_path,
        access_token=token,
    )


def describe_table(
    schema: str,
    table: str,
    *,
    catalog: str | None = None,
) -> str:
    """Return DESCRIBE TABLE output as plain text for the LLM."""
    cat = catalog or databricks_catalog()
    full = f"{cat}.{schema}.{table}"
    logger.info("DESCRIBE %s", full)
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DESCRIBE TABLE {full}")
            rows = cur.fetchall()
            cols = [d[0] for d in (cur.description or [])]

    if not rows:
        return f"(empty describe for {full})"

    lines = [f"Table: {full}", "col_name | data_type", "---"]
    for row in rows:
        if cols:
            name = row[0] if len(row) > 0 else ""
            dtype = row[1] if len(row) > 1 else ""
            lines.append(f"{name} | {dtype}")
        else:
            lines.append(" | ".join(str(c) for c in row))
    return "\n".join(lines)
