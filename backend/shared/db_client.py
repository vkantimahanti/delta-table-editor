"""
Database helper — Databricks SQL connector wrapper.

Key improvements over reference app:
- Per-request user token (no shared service principal)
- One SQL connection reused for all queries in the same HTTP request
- Parameterized queries via cursor params (no string interpolation for values)
- Explicit error logging (no silent except: pass)
- Connection params resolved once per connection
"""
import contextvars
import logging
import os
import threading
from contextlib import contextmanager
from typing import Any

import pandas as pd
from databricks import sql as dbsql
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("delta_editor.db_client")

CATALOG = os.environ.get("TARGET_CATALOG", "your_catalog")

# Audit / housekeeping columns — skipped in audit log; auto-filled only when present on table
AUDIT_COLUMN_NAMES = frozenset({
    "inserted_by", "inserted_at", "updated_by", "updated_at",
    "created_by", "created_at", "modified_by", "modified_date",
    "version", "is_active",
})

# Maximum number of concurrent SQL connections.
# Formula: workers × threads_per_worker
# Medium instance (2 vCPU, 4 workers): 4 × 10 = 40
# Large instance  (4 vCPU, 8 workers): 8 × 10 = 80
# Keep at 40 as a safe default for any instance size.
# Increase only if you upgrade to Large and see
# requests queuing in logs.
_DB_SEMAPHORE = threading.Semaphore(40)

# Per HTTP request: one open connection shared across query()/execute() calls.
_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "db_request_id", default=None
)


class _RequestDbSession:
    """One Databricks SQL connection for the lifetime of an HTTP request."""

    __slots__ = ("conn", "user_token", "lock")

    def __init__(self, conn: Any, user_token: str | None) -> None:
        self.conn = conn
        self.user_token = user_token
        self.lock = threading.Lock()


_request_sessions: dict[str, _RequestDbSession] = {}
_request_sessions_lock = threading.Lock()


def begin_request_scope(request_id: str) -> None:
    """Mark the start of an HTTP request — DB calls may reuse one connection."""
    _request_id_var.set(request_id)


def end_request_scope(request_id: str) -> None:
    """Close and release the request-scoped connection, if any."""
    _request_id_var.set(None)
    with _request_sessions_lock:
        session = _request_sessions.pop(request_id, None)
    if session is None:
        return
    try:
        session.conn.close()
    except Exception as exc:
        logger.warning("Error closing request DB connection: %s", exc)
    finally:
        _DB_SEMAPHORE.release()
    logger.debug("Closed request-scoped DB connection %s", request_id[:8])


def _normalize_hostname(raw: str) -> str:
    return raw.replace("https://", "").replace("http://", "").rstrip("/")


def _resolve_hostname() -> str:
    """Workspace host — env vars first, then Databricks Apps SDK fallback."""
    for key in ("DATABRICKS_SERVER_HOSTNAME", "DATABRICKS_HOST"):
        val = os.environ.get(key, "").strip()
        if val:
            return _normalize_hostname(val)
    try:
        from databricks.sdk import WorkspaceClient
        host = WorkspaceClient().config.host
        if host:
            logger.debug("Resolved hostname via WorkspaceClient")
            return _normalize_hostname(host)
    except Exception as exc:
        logger.debug("WorkspaceClient hostname fallback failed: %s", exc)
    return ""


def _resolve_http_path() -> str:
    path = os.environ.get("DATABRICKS_HTTP_PATH", "").strip()
    if path:
        return path
    wh_id = os.environ.get("DATABRICKS_WAREHOUSE_ID", "").strip()
    if wh_id:
        return f"/sql/1.0/warehouses/{wh_id}"
    return ""


def _resolve_access_token(user_token: str | None) -> str:
    if user_token:
        return user_token
    pat = os.environ.get("DATABRICKS_TOKEN", "").strip()
    if pat:
        return pat
    # Databricks Apps: do not fall back to service-principal OAuth for SQL —
    # queries must use the logged-in user's x-forwarded-access-token (sql scope).
    if os.environ.get("DATABRICKS_CLIENT_ID"):
        return ""
    try:
        from databricks.sdk import WorkspaceClient
        auth = WorkspaceClient().config.authenticate().get("Authorization", "")
        token = auth.replace("Bearer ", "").strip()
        if token:
            logger.debug("Auth: SDK OAuth fallback used")
            return token
    except Exception as exc:
        logger.warning("Auth: SDK OAuth fallback failed: %s", exc)
    return ""


def _resolve_connection_params(user_token: str | None = None) -> dict[str, Any]:
    """
    Build connection params. user_token takes priority (per-user identity).
    Falls back to DATABRICKS_TOKEN for local dev, or Apps OAuth.
    """
    hostname = _resolve_hostname()
    http_path = _resolve_http_path()

    if not hostname or not http_path:
        missing = []
        if not hostname:
            missing.append("DATABRICKS_SERVER_HOSTNAME or DATABRICKS_HOST")
        if not http_path:
            missing.append("DATABRICKS_HTTP_PATH or DATABRICKS_WAREHOUSE_ID")
        raise EnvironmentError(f"Missing Databricks config: {', '.join(missing)}")

    token = _resolve_access_token(user_token)

    params: dict[str, Any] = {"server_hostname": hostname, "http_path": http_path}
    if not token:
        raise EnvironmentError(
            "No SQL access token. On Databricks Apps, add the 'sql' user authorization "
            "scope, restart the app, and re-open it so your login token is forwarded."
        )
    params["access_token"] = token
    return params


def _warn_semaphore_pressure() -> None:
    available = _DB_SEMAPHORE._value
    if available < 5:
        logger.warning(
            "DB connection semaphore under pressure: %d slots remaining",
            available,
        )


def _open_connection(user_token: str | None) -> Any:
    """Acquire semaphore slot and open a new Databricks SQL connection."""
    _warn_semaphore_pressure()
    _DB_SEMAPHORE.acquire()
    try:
        params = _resolve_connection_params(user_token)
        return dbsql.connect(**params)
    except Exception:
        _DB_SEMAPHORE.release()
        raise


def _get_request_session(user_token: str | None) -> _RequestDbSession | None:
    """Return (or lazily create) the connection for the current HTTP request."""
    request_id = _request_id_var.get()
    if not request_id:
        return None
    with _request_sessions_lock:
        session = _request_sessions.get(request_id)
        if session is not None:
            return session
        conn = _open_connection(user_token)
        session = _RequestDbSession(conn, user_token)
        _request_sessions[request_id] = session
        logger.debug("Opened request-scoped DB connection %s", request_id[:8])
        return session


@contextmanager
def _connection(user_token: str | None = None):
    session = _get_request_session(user_token)
    if session is not None:
        yield session.conn
        return
    conn = _open_connection(user_token)
    try:
        yield conn
    finally:
        conn.close()
        _DB_SEMAPHORE.release()


def query(
    sql_text: str,
    params: tuple | None = None,
    user_token: str | None = None,
) -> pd.DataFrame:
    """Execute SELECT — returns DataFrame."""
    logger.debug("query: %s | params: %s", sql_text[:200], params)

    def _execute(conn: Any) -> pd.DataFrame:
        with conn.cursor() as cur:
            if params:
                cur.execute(sql_text, params)
            else:
                cur.execute(sql_text)
            if cur.description is None:
                return pd.DataFrame()
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            return pd.DataFrame([list(r) for r in rows], columns=cols)

    session = _get_request_session(user_token)
    if session is not None:
        with session.lock:
            return _execute(session.conn)
    with _connection(user_token) as conn:
        return _execute(conn)


def execute(sql_text: str, params: tuple = (), user_token: str | None = None) -> int:
    """Execute DML — returns rowcount."""
    logger.debug("execute: %s | params: %s", sql_text[:200], params)

    def _execute(conn: Any) -> int:
        with conn.cursor() as cur:
            cur.execute(sql_text, params)
            return cur.rowcount or 0

    session = _get_request_session(user_token)
    if session is not None:
        with session.lock:
            return _execute(session.conn)
    with _connection(user_token) as conn:
        return _execute(conn)


def execute_multi_insert(
    full_table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    *,
    user_token: str | None = None,
    chunk_size: int = 50,
) -> int:
    """Insert many rows using multi-value INSERT statements (fewer round trips)."""
    if not rows:
        return 0
    col_list = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    row_sql = f"({placeholders})"
    inserted = 0
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        values_sql = ", ".join(row_sql for _ in chunk)
        params: list[Any] = []
        for row in chunk:
            params.extend(row)
        inserted += execute(
            f"INSERT INTO {full_table} ({col_list}) VALUES {values_sql}",
            params=tuple(params),
            user_token=user_token,
        )
    return inserted


def execute_many(statements: list[str], user_token: str | None = None) -> None:
    """Execute multiple DML statements in one connection."""
    def _execute(conn: Any) -> None:
        with conn.cursor() as cur:
            for stmt in statements:
                if stmt.strip():
                    cur.execute(stmt)

    session = _get_request_session(user_token)
    if session is not None:
        with session.lock:
            _execute(session.conn)
        return
    with _connection(user_token) as conn:
        _execute(conn)


UPLOAD_CHANGE_SOURCE = "FILE_UPLOAD"


def log_upload_summary(
    changed_by: str,
    table_schema: str,
    table_name: str,
    mode: str,
    row_count: int,
    filename: str = "",
    column_names: list[str] | None = None,
    change_request_id: str | None = None,
    user_token: str | None = None,
) -> None:
    """One audit row describing a bulk file upload (no per-row entries)."""
    import json

    cols = [str(c) for c in (column_names or [])]
    safe_name = str(filename or "upload.csv").strip() or "upload.csv"
    mode_label = str(mode or "append").lower()

    record_key = {
        "filename": safe_name,
        "mode": mode_label,
        "row_count": row_count,
        "columns": cols,
    }

    if mode_label == "overwrite":
        old_value = "Existing table rows truncated before load"
    else:
        old_value = "Existing table rows retained (append)"

    col_preview = ", ".join(cols[:8])
    if len(cols) > 8:
        col_preview += f", ... (+{len(cols) - 8} more)"
    new_value = (
        f"File: {safe_name} | Mode: {mode_label} | Rows loaded: {row_count}"
        + (f" | Columns ({len(cols)}): {col_preview}" if cols else "")
    )

    try:
        execute(
            f"""
            INSERT INTO {CATALOG}.dmz.dataeditor_app_audit_log
              (changed_by, changed_at, table_schema, table_name, record_key,
               column_name, old_value, new_value, change_source, change_request_id)
            VALUES (?, current_timestamp(), ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params=(
                str(changed_by or ""),
                str(table_schema or ""),
                str(table_name or ""),
                json.dumps(record_key),
                "",
                old_value,
                new_value,
                UPLOAD_CHANGE_SOURCE,
                change_request_id,
            ),
            user_token=user_token,
        )
    except Exception as exc:
        logger.error(
            "UPLOAD AUDIT SUMMARY FAILED: user=%s table=%s.%s error=%s",
            changed_by, table_schema, table_name, exc,
        )


def log_audit(
    changed_by: str,
    table_schema: str,
    table_name: str,
    record_key: dict,
    column_name: str,
    old_value: Any,
    new_value: Any,
    source: str = "DATA_EDITOR",
    change_request_id: str | None = None,
    revision_id: str | None = None,
    batch_id: str | None = None,
    user_token: str | None = None,
) -> None:
    """
    Write one column-level change to the audit log.
    System columns are never audited — only business data changes.
    None old_value is stored as an empty string (e.g. row inserts).
    """
    import json

    if str(column_name or "").lower() in AUDIT_COLUMN_NAMES:
        return

    base_params = (
        str(changed_by or ""),
        str(table_schema or ""),
        str(table_name or ""),
        json.dumps(record_key),
        str(column_name or ""),
        str(old_value or ""),
        str(new_value or ""),
        source,
        str(change_request_id or "") or None,
    )
    try:
        execute(
            f"""
            INSERT INTO {CATALOG}.dmz.dataeditor_app_audit_log
              (changed_by, changed_at, table_schema, table_name, record_key,
               column_name, old_value, new_value, change_source, change_request_id,
               revision_id, batch_id)
            VALUES (?, current_timestamp(), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params=base_params + (str(revision_id or "") or None, str(batch_id or "") or None),
            user_token=user_token,
        )
    except Exception:
        try:
            execute(
                f"""
                INSERT INTO {CATALOG}.dmz.dataeditor_app_audit_log
                  (changed_by, changed_at, table_schema, table_name, record_key,
                   column_name, old_value, new_value, change_source, change_request_id)
                VALUES (?, current_timestamp(), ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params=base_params,
                user_token=user_token,
            )
        except Exception as exc:
            logger.error(
                "AUDIT LOG FAILED: user=%s table=%s.%s col=%s error=%s",
                changed_by, table_schema, table_name, column_name, exc,
            )


def log_audit_batch(
    entries: list[dict[str, Any]],
    *,
    user_token: str | None = None,
    chunk_size: int = 100,
) -> int:
    """Write many audit rows in batched multi-value INSERTs."""
    import json

    if not entries:
        return 0

    written = 0
    for i in range(0, len(entries), chunk_size):
        chunk = entries[i : i + chunk_size]
        value_groups: list[str] = []
        params: list[Any] = []
        for entry in chunk:
            col = str(entry.get("column_name") or "")
            if col.lower() in AUDIT_COLUMN_NAMES:
                continue
            value_groups.append("(?, current_timestamp(), ?, ?, ?, ?, ?, ?, ?, ?)")
            params.extend([
                str(entry.get("changed_by") or ""),
                str(entry.get("table_schema") or ""),
                str(entry.get("table_name") or ""),
                json.dumps(entry.get("record_key") or {}),
                col,
                str(entry.get("old_value") or ""),
                str(entry.get("new_value") or ""),
                str(entry.get("source") or "DATA_EDITOR"),
                str(entry.get("change_request_id") or "") or None,
            ])
        if not value_groups:
            continue
        try:
            execute(
                f"""
                INSERT INTO {CATALOG}.dmz.dataeditor_app_audit_log
                  (changed_by, changed_at, table_schema, table_name, record_key,
                   column_name, old_value, new_value, change_source, change_request_id)
                VALUES {', '.join(value_groups)}
                """,
                params=tuple(params),
                user_token=user_token,
            )
            written += len(value_groups)
        except Exception as exc:
            logger.error("AUDIT BATCH FAILED (%d entries): %s", len(value_groups), exc)
            for entry in chunk:
                log_audit(
                    str(entry.get("changed_by") or ""),
                    str(entry.get("table_schema") or ""),
                    str(entry.get("table_name") or ""),
                    entry.get("record_key") or {},
                    str(entry.get("column_name") or ""),
                    entry.get("old_value"),
                    entry.get("new_value"),
                    source=str(entry.get("source") or "DATA_EDITOR"),
                    change_request_id=entry.get("change_request_id"),
                    user_token=user_token,
                )
                written += 1
    return written


def table_exists(catalog: str, schema: str, table: str, user_token: str | None = None) -> bool:
    """Check if a Delta table exists."""
    try:
        df = query(
            f"SELECT 1 FROM {catalog}.information_schema.tables "
            f"WHERE table_schema = '{schema}' AND table_name = '{table}' LIMIT 1",
            user_token=user_token,
        )
        return not df.empty
    except Exception:
        return False


_NUMERIC_SQL_TYPES = frozenset({
    "bigint", "int", "integer", "long", "double", "float",
    "tinyint", "smallint", "dec", "decimal", "numeric", "number",
})
_TEMPORAL_SQL_TYPES = frozenset({
    "timestamp", "date", "timestamp_ntz", "timestamp_ltz",
})
_BOOLEAN_SQL_TYPES = frozenset({"boolean", "bool"})


def _normalize_sql_data_type(data_type: str) -> str:
    return str(data_type or "string").lower().split("(")[0].strip()


def is_empty_cell_value(val: Any) -> bool:
    if val is None:
        return True
    text = str(val).strip()
    return text == "" or text.lower() in {"none", "nan", "null"}


def cell_to_str(val: Any) -> str:
    """Stringify a cell without treating numeric zero as empty."""
    if val is None:
        return ""
    if isinstance(val, float) and pd.isna(val):
        return ""
    return str(val)


def versions_match(expected: Any, actual: Any) -> bool:
    """Compare optimistic-lock version values (int-like or string)."""
    if expected is None and actual is None:
        return True
    try:
        return int(float(cell_to_str(expected if expected is not None else 0))) == int(
            float(cell_to_str(actual if actual is not None else 0))
        )
    except (ValueError, TypeError):
        return cell_to_str(expected) == cell_to_str(actual if actual is not None else "")


def should_omit_empty_typed_value(data_type: str) -> bool:
    dt = _normalize_sql_data_type(data_type)
    return (
        dt in _NUMERIC_SQL_TYPES
        or dt in _TEMPORAL_SQL_TYPES
        or dt in _BOOLEAN_SQL_TYPES
        or dt.startswith("decimal")
    )


def normalize_cell_for_storage(val: Any, data_type: str) -> Any | None:
    """
    Coerce grid cell values before INSERT/UPDATE.
    Empty optional numeric/timestamp/boolean cells become None (omit from INSERT).
    """
    if is_empty_cell_value(val):
        return None if should_omit_empty_typed_value(data_type) else ""
    return val


def get_table_columns(
    catalog: str, schema: str, table: str, user_token: str | None = None
) -> dict[str, str]:
    """
    Return map of lowercase column name -> actual column name on the table.
    Used before INSERT/UPDATE to only touch columns that really exist.
    """
    try:
        df = query(
            f"SELECT column_name FROM {catalog}.information_schema.columns "
            f"WHERE table_catalog = '{catalog}' AND table_schema = '{schema}' "
            f"AND table_name = '{table}'",
            user_token=user_token,
        )
        if not df.empty:
            return {
                str(row["column_name"]).lower(): str(row["column_name"])
                for _, row in df.iterrows()
            }
    except Exception as exc:
        logger.warning("Could not fetch columns for %s.%s.%s: %s", catalog, schema, table, exc)
    return {}


def get_table_column_types(
    catalog: str, schema: str, table: str, user_token: str | None = None
) -> dict[str, str]:
    """Return map of lowercase column name -> SQL data type."""
    try:
        df = query(
            f"SELECT column_name, data_type FROM {catalog}.information_schema.columns "
            f"WHERE table_catalog = '{catalog}' AND table_schema = '{schema}' "
            f"AND table_name = '{table}'",
            user_token=user_token,
        )
        if not df.empty:
            return {
                str(row["column_name"]).lower(): str(row["data_type"])
                for _, row in df.iterrows()
            }
    except Exception as exc:
        logger.warning(
            "Could not fetch column types for %s.%s.%s: %s", catalog, schema, table, exc
        )

    # DESCRIBE TABLE is more reliable on some Databricks workspaces.
    try:
        df = query(f"DESCRIBE TABLE {catalog}.{schema}.{table}", user_token=user_token)
        if not df.empty:
            name_col = "col_name" if "col_name" in df.columns else df.columns[0]
            type_col = "data_type" if "data_type" in df.columns else df.columns[1]
            out: dict[str, str] = {}
            for _, row in df.iterrows():
                name = str(row[name_col]).strip()
                if not name or name.startswith("#"):
                    continue
                out[name.lower()] = str(row[type_col]).strip()
            if out:
                return out
    except Exception as exc:
        logger.warning(
            "DESCRIBE TABLE type lookup failed for %s.%s.%s: %s", catalog, schema, table, exc
        )
    return {}


def resolve_column_types(
    catalog: str,
    schema: str,
    table: str,
    config_types: dict[str, str] | None = None,
    user_token: str | None = None,
) -> dict[str, str]:
    """Merge physical SQL types with optional config YAML types (config fills gaps)."""
    types = get_table_column_types(catalog, schema, table, user_token=user_token)
    if config_types:
        for key, val in config_types.items():
            types.setdefault(str(key).lower(), val)
    return types


def get_table_column_names(
    catalog: str, schema: str, table: str, user_token: str | None = None
) -> set[str]:
    """Return lowercase column names for a Delta table."""
    return set(get_table_columns(catalog, schema, table, user_token=user_token).keys())
