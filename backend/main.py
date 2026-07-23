"""
Data Canvas — FastAPI backend.

Improvements over reference app:
- Per-user token forwarded to every SQL call (Unity Catalog per-user enforcement)
- Parameterized queries (no SQL injection via where_sql)
- Structured column filter endpoint (replaces free-text where_sql)
- Optimistic locking via version column
- Audit log uses parameterized INSERT (no string formatting)
- Proper error logging (no silent except: pass)
- Generic file upload with append / overwrite choice
"""
import asyncio
import base64
import io
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .shared import audit_cols, auth_helpers, approval_ops, bulk, bulk_update_ops, bulk_upload_ops, bulk_upsert_ops, change_request, config_rules, config_store, db_client, export_ops, filter_sql, grid_staging_ops, overview_ops, upload_apply_ops, upload_ops

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("delta_editor")

CATALOG = db_client.CATALOG
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "10"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_ROWS_LIMIT = int(os.environ.get("MAX_ROWS_LIMIT", "10000"))
MAX_ROWS_DEFAULT = int(os.environ.get("MAX_ROWS_DEFAULT", "500"))


def _upload_payload_bytes(*, csv_text: str = "", file_base64: str = "") -> int:
    if str(file_base64 or "").strip():
        return len(base64.b64decode(file_base64))
    return len(str(csv_text or "").encode("utf-8"))

# ── Idle auto-stop ──────────────────────────────
# Tracks last API activity timestamp.
# Background thread stops the app if idle
# for longer than IDLE_STOP_MINUTES.
# Disabled in dev environments automatically.
# Activity is persisted to a file so all uvicorn
# workers share the same last-activity clock.
_ACTIVITY_FILE = Path(os.environ.get(
    "IDLE_ACTIVITY_FILE", "/tmp/data_canvas_last_activity"
))
_last_activity: datetime = datetime.now()
_activity_lock = threading.Lock()


def _persist_activity(ts: datetime) -> None:
    try:
        _ACTIVITY_FILE.write_text(ts.isoformat())
    except Exception as exc:
        logger.debug("Could not persist activity timestamp: %s", exc)


def _load_activity() -> datetime:
    try:
        if _ACTIVITY_FILE.exists():
            return datetime.fromisoformat(_ACTIVITY_FILE.read_text().strip())
    except Exception as exc:
        logger.debug("Could not read activity timestamp: %s", exc)
    return datetime.now()


def _reset_activity_clock() -> None:
    """Fresh activity clock on process start (avoids stale /tmp file stopping the app)."""
    global _last_activity
    now = datetime.now()
    with _activity_lock:
        _last_activity = now
        _persist_activity(now)


def _record_activity() -> None:
    """Call on every authenticated API request."""
    global _last_activity
    now = datetime.now()
    with _activity_lock:
        _last_activity = now
        _persist_activity(now)


def _start_idle_watchdog_once() -> None:
    """Only one watchdog thread across uvicorn workers."""
    lock_path = Path("/tmp/data_canvas_watchdog.lock")
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return
    except OSError as exc:
        logger.debug("Idle watchdog lock skipped: %s", exc)
        return
    threading.Thread(
        target=_idle_watchdog,
        daemon=True,
        name="idle-watchdog",
    ).start()


def _idle_watchdog() -> None:
    idle_minutes = int(os.environ.get("IDLE_STOP_MINUTES", "5"))
    check_seconds = int(os.environ.get("IDLE_CHECK_SECONDS", "300"))
    enabled = os.environ.get("IDLE_AUTO_STOP", "false").lower() == "true"
    app_name = os.environ.get("DATABRICKS_APP_NAME", "")
    is_dev = any(x in app_name.lower()
                 for x in ["dev", "test", "staging", "local"])

    if not enabled or is_dev or not app_name:
        logger.info(
            "Idle watchdog disabled "
            "(IDLE_AUTO_STOP=%s app=%s is_dev=%s)",
            enabled, app_name or "(empty)", is_dev,
        )
        return

    logger.info(
        "Idle watchdog active — stops '%s' after %d min idle "
        "(check every %ds, activity_file=%s)",
        app_name, idle_minutes, check_seconds, _ACTIVITY_FILE,
    )

    while True:
        time.sleep(check_seconds)
        try:
            last = _load_activity()
            idle_for = datetime.now() - last
            remaining = timedelta(minutes=idle_minutes) - idle_for
            logger.info(
                "Idle watchdog check — idle %s, threshold %d min, "
                "remaining %s",
                str(idle_for).split(".")[0],
                idle_minutes,
                str(max(remaining, timedelta(0))).split(".")[0],
            )
            if idle_for >= timedelta(minutes=idle_minutes):
                logger.info(
                    "Idle %s — calling apps.stop('%s')",
                    str(idle_for).split(".")[0], app_name,
                )
                from databricks.sdk import WorkspaceClient
                WorkspaceClient().apps.stop(app_name)
                logger.info("apps.stop('%s') completed", app_name)
                return
        except Exception as exc:
            logger.error("Watchdog error: %s", exc, exc_info=True)


class ActivityMiddleware(BaseHTTPMiddleware):
    """Keep idle watchdog aware of API traffic (incl. /api/health polls during warehouse startup)."""

    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/api/"):
            _record_activity()
        return await call_next(request)


class TimeoutMiddleware(BaseHTTPMiddleware):
    """
    Returns 504 if any request takes longer than
    REQUEST_TIMEOUT_SECONDS.

    Default is 360s (5 minutes) to accommodate
    Classic SQL warehouse cold start time of 3-5 min.
    Use 60s only if you switch to Serverless SQL.

    Prevents slow queries or cold-starting warehouses
    from holding connections open indefinitely and
    crashing the app under load.
    """
    def __init__(self, app, timeout: int = 360):
        super().__init__(app)
        self.timeout = timeout

    async def dispatch(self, request, call_next):
        try:
            return await asyncio.wait_for(
                call_next(request),
                timeout=self.timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Request timeout after %ds: %s %s",
                self.timeout,
                request.method,
                request.url.path,
            )
            return JSONResponse(
                status_code=504,
                content={
                    "detail": (
                        f"Request timed out after {self.timeout}s. "
                        "The SQL warehouse may be starting up — "
                        "Classic warehouses can take 3-5 minutes. "
                        "Please wait and try again."
                    )
                },
            )


class DbRequestScopeMiddleware(BaseHTTPMiddleware):
    """
    Reuse one Databricks SQL connection for every query in the same
    /api/* HTTP request, then close it when the response is sent.
    """

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        request_id = str(uuid.uuid4())
        db_client.begin_request_scope(request_id)
        try:
            return await call_next(request)
        finally:
            db_client.end_request_scope(request_id)


def _rate_limit_key(request: Request) -> str:
    """
    Key rate limits by user email from Databricks
    Apps SSO header. Falls back to IP address for
    local development where the header is absent.
    Each user gets their own independent limit
    counter regardless of shared proxy IP.
    """
    email = (
        request.headers.get("x-forwarded-email")
        or request.headers.get("X-Forwarded-Email")
    )
    if email:
        return email
    # Local dev fallback — use IP
    return request.client.host if request.client else "local"


limiter = Limiter(key_func=_rate_limit_key)


# ── startup ───────────────────────────────────────────────────────────────────

def _log_startup() -> None:
    checks = {
        "DATABRICKS_HOST / SERVER_HOSTNAME": bool(
            os.environ.get("DATABRICKS_HOST") or os.environ.get("DATABRICKS_SERVER_HOSTNAME")
        ),
        "DATABRICKS_TOKEN": bool(os.environ.get("DATABRICKS_TOKEN")),
        "DATABRICKS_HTTP_PATH / WAREHOUSE_ID": bool(
            os.environ.get("DATABRICKS_HTTP_PATH") or os.environ.get("DATABRICKS_WAREHOUSE_ID")
        ),
        "TARGET_CATALOG": os.environ.get("TARGET_CATALOG", "(not set)"),
        "PORT": os.environ.get("PORT", "8000"),
    }
    logger.info("=== Data Canvas startup ===")
    for k, v in checks.items():
        logger.info("  %s: %s", k, "SET" if isinstance(v, bool) and v else ("NOT SET" if isinstance(v, bool) else v))
    logger.info("DB connectivity validated on first request — check /api/health")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _reset_activity_clock()
    _log_startup()
    _start_idle_watchdog_once()
    yield


app = FastAPI(title="Data Canvas API", version="2.0.0", lifespan=lifespan)

app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

@app.exception_handler(RateLimitExceeded)
async def _rate_limit_exceeded(
    request: Request, exc: RateLimitExceeded
) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "detail": (
                "Too many requests. "
                "Please wait a moment and try again. "
                f"Limit: {exc.detail}"
            )
        },
    )

cors_origins = [
    origin.strip()
    for origin in os.environ.get(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip() and origin.strip() != "*"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    TimeoutMiddleware,
    timeout=int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "360"))
)

app.add_middleware(ActivityMiddleware)

app.add_middleware(DbRequestScopeMiddleware)


@app.exception_handler(Exception)
async def _global_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected server error occurred."},
    )


# ── auth helpers ──────────────────────────────────────────────────────────────

def _safe_name(name: str) -> str:
    """Sanitize catalog/schema/table names — allow only alphanumeric and underscore."""
    import re
    if not re.match(r'^[\w.]+$', name):
        raise HTTPException(status_code=400, detail=f"Invalid identifier: '{name}'")
    return name


def _validate_columns(columns: str) -> str:
    """Validate comma-separated column names before use in a SELECT clause."""
    stripped = columns.strip()
    if not stripped or stripped == "*":
        return "*"
    parts = [part.strip() for part in stripped.split(",") if part.strip()]
    if not parts:
        return "*"
    return ", ".join(_safe_name(part) for part in parts)


def _expand_select_columns(
    columns: str,
    *,
    schema: str,
    table: str,
    catalog: str,
    user_token: str | None,
) -> str:
    """Always include PK + version in grid SELECT (hidden optimistic-lock cols)."""
    cols_sql = _validate_columns(columns)
    if cols_sql == "*":
        return cols_sql
    requested = {part.strip().lower() for part in columns.split(",") if part.strip()}
    extras: list[str] = []
    try:
        table_cols = db_client.get_table_columns(catalog, schema, table, user_token=user_token)
        pk_cols = config_store.get_pk_cols(schema, table, user_token=user_token)
        for pk in sorted(pk_cols):
            if pk.lower() not in requested and pk.lower() in table_cols:
                extras.append(table_cols[pk.lower()])
        if "version" not in requested and "version" in table_cols:
            extras.append(table_cols["version"])
    except Exception as exc:
        logger.warning("Could not expand select columns for %s.%s: %s", schema, table, exc)
    if not extras:
        return cols_sql
    return cols_sql + ", " + ", ".join(_safe_name(c) for c in extras)


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    return [
        {k: (None if (v != v or v is None) else (int(v) if isinstance(v, float) and v == int(v) else v))
         for k, v in row.items()}
        for row in df.to_dict("records")
    ]


def _paginated_response(
    rows: list[dict[str, Any]],
    total_count: int,
    page: int,
    page_size: int,
    *,
    filtered: bool | None = None,
) -> dict[str, Any]:
    """Standard paginated data payload for get_data / filter_data."""
    payload: dict[str, Any] = {
        "rows": rows,
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < total_count,
    }
    if filtered is not None:
        payload["filtered"] = filtered
    return payload


# Auto-fill on INSERT — only applied when the column exists on the target table
_append_insert_audit_cols = audit_cols.append_insert_audit_cols
_append_update_audit_cols = audit_cols.append_update_audit_cols


# ── Pydantic models ───────────────────────────────────────────────────────────

class ColumnFilter(BaseModel):
    column: str
    value: str | None = None          # None → IS NULL


class UpdatePayload(BaseModel):
    original: dict[str, Any]
    edits: dict[str, Any]
    pk_cols: list[str]
    editable_cols: list[str]
    mandatory_cols: list[str] = []
    version_col: str | None = "version"    # optimistic lock column


class InsertPayload(BaseModel):
    values: dict[str, Any]
    mandatory_cols: list[str] = []
    pk_cols: list[str] = []


class ValidatePayload(BaseModel):
    original: dict[str, Any] = {}
    edits: dict[str, Any]
    pk_cols: list[str] = []
    editable_cols: list[str] = []
    mandatory_cols: list[str] = []


class BulkCsvPayload(BaseModel):
    csv_text: str


class BulkInsertPayload(BaseModel):
    rows: list[dict[str, Any]]
    mode: str = "append"               # "append" | "overwrite"


class GenericUploadPayload(BaseModel):
    catalog: str
    schema: str
    table: str
    csv_text: str = ""
    file_base64: str = ""
    file_format: str = ""
    mode: str = "append"               # "append" | "overwrite"
    has_header: bool = True            # first row contains column names
    delimiter: str = ","
    filename: str = ""                 # original uploaded file name


class UploadValidatePayload(BaseModel):
    catalog: str = ""
    mode: str = "upsert"               # upsert | overwrite (legacy: update | append)
    csv_text: str = ""
    file_base64: str = ""
    file_format: str = ""
    has_header: bool = True
    delimiter: str = ","
    filename: str = "upload.csv"
    filter_snapshot: dict[str, Any] | None = None


class UploadApplyPayload(BaseModel):
    change_request_id: str


class GridRowUpdate(BaseModel):
    original: dict[str, Any] = {}
    edits: dict[str, Any] = {}


class GridRowInsert(BaseModel):
    values: dict[str, Any] = {}


class GridStagePayload(BaseModel):
    catalog: str = ""
    updates: list[GridRowUpdate] = []
    inserts: list[GridRowInsert] = []


class ExportPayload(BaseModel):
    catalog: str = ""
    format: str = "csv"               # csv | xlsx | excel | tsv | txt
    columns: list[str] = []           # visible columns; PK + version added server-side
    filters: list[ColumnFilter] = []
    filter_snapshot: dict[str, Any] | None = None


class RejectPayload(BaseModel):
    reason: str = ""


# ── auth diagnostics ──────────────────────────────────────────────────────────

@app.get("/api/auth/status")
def auth_status(
    request: Request,
    token: str | None = Depends(auth_helpers.resolve_user_token),
    user: str = Depends(auth_helpers.resolve_user_email),
) -> dict[str, Any]:
    """Safe auth diagnostics — never returns the token itself."""
    scopes = auth_helpers.jwt_scopes(token)
    return {
        "on_databricks_app": bool(os.environ.get("DATABRICKS_CLIENT_ID")),
        "user_email": user if user != "local_dev" else None,
        "user_token_present": bool(token),
        "token_scopes": scopes,
        "sql_scope_present": "sql" in scopes,
        "warehouse_id_set": bool(os.environ.get("DATABRICKS_WAREHOUSE_ID")),
        "hostname_set": bool(
            os.environ.get("DATABRICKS_HOST") or os.environ.get("DATABRICKS_SERVER_HOSTNAME")
        ),
        "forwarded_headers": {
            "x-forwarded-access-token": bool(request.headers.get("x-forwarded-access-token")),
            "X-Forwarded-Access-Token": bool(request.headers.get("X-Forwarded-Access-Token")),
            "x-forwarded-email": bool(request.headers.get("x-forwarded-email")),
        },
    }


# ── health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health(token: str | None = Depends(auth_helpers.resolve_user_token)) -> dict[str, Any]:
    scopes = auth_helpers.jwt_scopes(token)
    status: dict[str, Any] = {
        "status": "ok",
        "catalog": CATALOG,
        "upload_audit": True,
        "auth": {
            "user_token_present": bool(token),
            "sql_scope_present": "sql" in scopes,
        },
    }
    try:
        await asyncio.to_thread(
            lambda: db_client.query("SELECT 1 AS ping", user_token=token)
        )
        status["db"] = "ok"
    except Exception as exc:
        status["status"] = "degraded"
        status["db"] = auth_helpers.sql_auth_hint(exc, token)
        logger.error("Health check DB error: %s", exc)
    return status


# ── catalog / schema / table discovery ───────────────────────────────────────

@app.get("/api/catalogs")
def list_catalogs(token: str | None = Depends(auth_helpers.resolve_user_token)) -> list[str]:
    try:
        df = db_client.query("SHOW CATALOGS", user_token=token)
        return df.iloc[:, 0].tolist() if not df.empty else []
    except Exception as exc:
        detail = auth_helpers.sql_auth_hint(exc, token)
        logger.error("list_catalogs failed: %s", detail)
        raise HTTPException(status_code=500, detail=detail)


@app.get("/api/catalogs/{catalog}/schemas")
def list_schemas(
    catalog: str,
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> list[str]:
    _safe_name(catalog)
    try:
        df = db_client.query(f"SHOW SCHEMAS IN {catalog}", user_token=token)
        return df.iloc[:, 0].tolist() if not df.empty else []
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/catalogs/{catalog}/schemas/{schema}/tables")
def list_tables(
    catalog: str,
    schema: str,
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> list[str]:
    _safe_name(catalog); _safe_name(schema)
    try:
        df = db_client.query(f"SHOW TABLES IN {catalog}.{schema}", user_token=token)
        if df.empty:
            return []
        # SHOW TABLES: tableName is usually col index 1
        col = "tableName" if "tableName" in df.columns else df.columns[1]
        return df[col].tolist()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── table registry (config-driven) ───────────────────────────────────────────

@app.get("/api/tables")
@limiter.limit("30/minute")
async def get_tables(
    request: Request,
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> list[dict[str, Any]]:
    _record_activity()
    app_name = os.environ.get("DATABRICKS_APP_NAME", "datacanvas")
    # app_name filter disabled — single app deployment; nav uses app_group only.
    # Re-enable USE_APP_NAME_FILTER when a second app is onboarded.
    use_app_name_filter = False
    if use_app_name_filter:
        df = await asyncio.to_thread(
            lambda: db_client.query(
                f"SELECT * FROM {config_store.REGISTRY_TABLE} "
                "WHERE app_name = ? AND is_active = TRUE "
                "ORDER BY app_group, display_name",
                params=(app_name,),
                user_token=token,
            )
        )
    else:
        df = await asyncio.to_thread(
            lambda: db_client.query(
                f"SELECT * FROM {config_store.REGISTRY_TABLE} "
                "WHERE is_active = TRUE "
                "ORDER BY app_group, display_name",
                user_token=token,
            )
        )
    return _records(df)


@app.get("/api/groups")
@limiter.limit("30/minute")
async def get_groups(
    request: Request,
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> dict[str, list[str]]:
    """
    Returns distinct app_group values from
    dataeditor_table_registry for this app_name.
    Used to populate the Group filter dropdown
    in the navigation bar.
    Always includes "All" as the first entry.
    """
    _record_activity()
    catalog = os.environ.get("TARGET_CATALOG", "your_catalog")

    # app_name column not yet in registry.
    # Returns groups for all registered tables.
    # Add app_name filter when second app is
    # onboarded and column is added to the table.
    sql = f"""
        SELECT DISTINCT app_group
        FROM {catalog}.dmz.dataeditor_table_registry
        WHERE is_active = true
          AND app_group IS NOT NULL
          AND app_group != ''
        ORDER BY app_group ASC
    """

    df = await asyncio.to_thread(db_client.query, sql, user_token=token)

    groups = ["All"] + [
        str(row["app_group"])
        for _, row in df.iterrows()
    ]
    return {"groups": groups}


def _pk_cols_for_table(schema: str, table: str, user_token: str | None) -> set[str]:
    return config_store.get_pk_cols(schema, table, user_token=user_token)


def _map_col_type(data_type: str) -> str:
    dt = str(data_type or "").lower()
    if "bool" in dt:
        return "boolean"
    if any(k in dt for k in ("int", "decimal", "double", "float", "numeric", "long")):
        return "number"
    if any(k in dt for k in ("date", "time", "timestamp")):
        return "date"
    return "string"


def _infer_columns(
    schema: str, table: str, catalog: str, user_token: str | None
) -> list[dict[str, Any]]:
    """Build column metadata from information_schema when dataeditor_column_config is empty."""
    pk_set = _pk_cols_for_table(schema, table, user_token)
    df = db_client.query(
        f"SELECT column_name, ordinal_position, data_type "
        f"FROM {catalog}.information_schema.columns "
        f"WHERE table_catalog = '{catalog}' AND table_schema = '{schema}' AND table_name = '{table}' "
        "ORDER BY ordinal_position",
        user_token=user_token,
    )
    if df.empty:
        return []
    cols: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        name = str(row["column_name"])
        cols.append({
            "table_schema": schema,
            "table_name": table,
            "column_name": name,
            "display_label": name,
            "col_order": int(row.get("ordinal_position") or len(cols) + 1),
            "col_type": _map_col_type(str(row.get("data_type") or "")),
            "is_visible": True,
            "is_editable": True,
            "is_mandatory": False,
            "is_filter": False,
            "is_pk": name in pk_set,
        })
    return cols


@app.get("/api/tables/{schema}/{table}/columns")
@limiter.limit("30/minute")
async def get_columns(
    request: Request,
    schema: str,
    table: str,
    catalog: str = Query(default=""),
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> list[dict[str, Any]]:
    _record_activity()
    _safe_name(schema); _safe_name(table)
    effective_catalog = _safe_name(catalog.strip()) if catalog.strip() else CATALOG
    records = await asyncio.to_thread(
        lambda: config_store.get_columns(schema, table, user_token=token)
    )
    if records:
        pk_set = await asyncio.to_thread(
            lambda: config_store.get_pk_cols(schema, table, user_token=token)
        )
        for rec in records:
            if rec.get("column_name") in pk_set:
                rec["is_pk"] = True
        return records
    logger.info(
        "No column config for %s.%s — inferring columns from %s.information_schema",
        schema, table, effective_catalog,
    )
    return await asyncio.to_thread(
        lambda: _infer_columns(schema, table, effective_catalog, token)
    )


@app.get("/api/tables/{schema}/{table}/dropdowns")
@limiter.limit("30/minute")
async def get_dropdowns(
    request: Request,
    schema: str, table: str,
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> dict[str, Any]:
    _record_activity()
    _safe_name(schema); _safe_name(table)
    return await asyncio.to_thread(
        lambda: config_store.resolve_dropdowns(schema, table, user_token=token)
    )


# ── data fetch (structured filters, column selection) ────────────────────────

@app.get("/api/tables/{schema}/{table}/data")
@limiter.limit("30/minute")
async def get_data(
    request: Request,
    schema: str,
    table: str,
    catalog: str = Query(default=""),
    columns: str = Query(default="*"),         # comma-separated column names
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=MAX_ROWS_DEFAULT, ge=1, le=MAX_ROWS_LIMIT),
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> dict[str, Any]:
    """
    Fetch one page of table data. Columns are selected explicitly — no free-text WHERE.
    Use POST /api/tables/{schema}/{table}/filter for filtered queries.
    """
    _record_activity()
    _safe_name(schema); _safe_name(table)
    effective_catalog = _safe_name(catalog.strip()) if catalog.strip() else CATALOG
    cols_sql = await asyncio.to_thread(
        lambda: _expand_select_columns(
            columns, schema=schema, table=table, catalog=effective_catalog, user_token=token
        )
    )
    from_table = f"{effective_catalog}.{schema}.{table}"
    offset = (page - 1) * page_size

    def _run_paginated() -> tuple[pd.DataFrame, int]:
        count_df = db_client.query(
            f"SELECT COUNT(*) AS total FROM {from_table}",
            user_token=token,
        )
        total = int(count_df.iloc[0]["total"]) if not count_df.empty else 0
        df = db_client.query(
            f"SELECT {cols_sql} FROM {from_table} LIMIT {page_size} OFFSET {offset}",
            user_token=token,
        )
        return df, total

    df, total_count = await asyncio.to_thread(_run_paginated)
    return _paginated_response(_records(df), total_count, page, page_size)


@app.post("/api/tables/{schema}/{table}/filter")
@limiter.limit("30/minute")
async def filter_data(
    request: Request,
    schema: str,
    table: str,
    filters: list[ColumnFilter],
    catalog: str = Query(default=""),
    columns: str = Query(default="*"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=MAX_ROWS_DEFAULT, ge=1, le=MAX_ROWS_LIMIT),
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> dict[str, Any]:
    """
    Structured column filters — substring match via LIKE on each column.
    Each filter is {column, value}. value=None → IS NULL.
    Returns rows, total_count, page info, and filtered flag.
    """
    _record_activity()
    _safe_name(schema); _safe_name(table)
    effective_catalog = _safe_name(catalog.strip()) if catalog.strip() else CATALOG
    cols_sql = await asyncio.to_thread(
        lambda: _expand_select_columns(
            columns, schema=schema, table=table, catalog=effective_catalog, user_token=token
        )
    )
    from_table = f"{effective_catalog}.{schema}.{table}"
    offset = (page - 1) * page_size

    if not filters:
        def _run_unfiltered() -> tuple[pd.DataFrame, int]:
            count_df = db_client.query(
                f"SELECT COUNT(*) AS total FROM {from_table}",
                user_token=token,
            )
            total = int(count_df.iloc[0]["total"]) if not count_df.empty else 0
            df = db_client.query(
                f"SELECT {cols_sql} FROM {from_table} LIMIT {page_size} OFFSET {offset}",
                user_token=token,
            )
            return df, total

        df, total_count = await asyncio.to_thread(_run_unfiltered)
        return _paginated_response(
            _records(df), total_count, page, page_size, filtered=False
        )

    where_clause, params_tuple = filter_sql.build_filter_where(filters, safe_column=_safe_name)
    select_sql = (
        f"SELECT {cols_sql} FROM {from_table} "
        f"WHERE {where_clause} LIMIT {page_size} OFFSET {offset}"
    )
    count_sql = (
        f"SELECT COUNT(*) AS total FROM {from_table} "
        f"WHERE {where_clause}"
    )

    def _run_filtered_query() -> tuple[pd.DataFrame, int]:
        df = db_client.query(select_sql, params=params_tuple, user_token=token)
        count_df = db_client.query(count_sql, params=params_tuple, user_token=token)
        total = int(count_df.iloc[0]["total"]) if not count_df.empty else 0
        return df, total

    df, total_count = await asyncio.to_thread(_run_filtered_query)
    return _paginated_response(
        _records(df), total_count, page, page_size, filtered=True
    )


# ── server export (filtered rows → Volume / local file) ─────────────────────

@app.post("/api/tables/{schema}/{table}/export")
@limiter.limit("10/minute")
async def export_data(
    request: Request,
    schema: str,
    table: str,
    payload: ExportPayload,
    token: str | None = Depends(auth_helpers.resolve_user_token),
    user: str = Depends(auth_helpers.resolve_user_email),
) -> dict[str, Any]:
    """
    Export all rows matching structured filters (up to EXPORT_MAX_ROWS).
    Writes CSV/XLSX to UC Volume when available; returns download URL.
    """
    _record_activity()
    _safe_name(schema)
    _safe_name(table)
    catalog = _safe_name(payload.catalog.strip()) if payload.catalog.strip() else CATALOG
    try:
        return await asyncio.to_thread(
            lambda: export_ops.run_filtered_export(
                catalog=catalog,
                schema=schema,
                table=table,
                filters=payload.filters,
                columns=payload.columns,
                fmt=payload.format,
                submitted_by=user,
                filter_snapshot=payload.filter_snapshot,
                user_token=token,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/api/exports/{change_request_id}/download")
@limiter.limit("30/minute")
async def download_export(
    request: Request,
    change_request_id: str,
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> FileResponse:
    """Download a completed export file."""
    _record_activity()
    try:
        path, filename, media_type = await asyncio.to_thread(
            lambda: export_ops.read_export_file(change_request_id, user_token=token)
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return FileResponse(path, filename=filename, media_type=media_type)


@app.get("/api/tables/{schema}/{table}/history")
@limiter.limit("30/minute")
async def get_history(
    request: Request,
    schema: str, table: str,
    limit: int = Query(default=300, ge=1, le=1000),
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> list[dict[str, Any]]:
    _record_activity()
    _safe_name(schema); _safe_name(table)
    df = await asyncio.to_thread(
        lambda: db_client.query(
            f"SELECT changed_by, changed_at, record_key, column_name, old_value, new_value, change_source "
            f"FROM {CATALOG}.dmz.dataeditor_app_audit_log "
            f"WHERE table_schema = '{schema}' AND table_name = '{table}' "
            f"ORDER BY changed_at DESC LIMIT {limit}",
            user_token=token,
        )
    )
    return _records(df)


def _changed_edits(original: dict[str, Any], edits: dict[str, Any]) -> dict[str, str]:
    """Columns explicitly edited whose value differs from the original row."""
    return {
        col: str(edits.get(col, "") or "")
        for col in edits
        if str(original.get(col, "") or "") != str(edits.get(col, "") or "")
    }


def _merged_row(original: dict[str, Any], edits: dict[str, Any]) -> dict[str, Any]:
    return {**original, **edits}


def _row_value(row: dict[str, Any], col: str) -> Any:
    if col in row:
        return row[col]
    col_l = col.lower()
    for k, v in row.items():
        if str(k).lower() == col_l:
            return v
    return None


def _resolve_pk_cols_list(
    schema: str, table: str, pk_cols: list[str], user_token: str | None
) -> list[str]:
    if pk_cols:
        return pk_cols
    pk_set = config_store.get_pk_cols(schema, table, user_token=user_token)
    if not pk_set:
        raise HTTPException(
            status_code=400,
            detail="No primary key configured for this table. Cannot update a single row safely.",
        )
    return sorted(pk_set)

@app.post("/api/tables/{schema}/{table}/validate")
@limiter.limit("30/minute")
async def validate(
    request: Request,
    schema: str, table: str,
    payload: ValidatePayload,
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> dict[str, Any]:
    """
    Returns structured errors:
    {"blocking": [{"column", "reason", "fix"}], "warnings": [...]}
    """
    _record_activity()
    _safe_name(schema); _safe_name(table)
    if payload.original:
        changed = _changed_edits(payload.original, payload.edits)
        merged = _merged_row(payload.original, payload.edits)
    else:
        changed = {k: str(v or "") for k, v in payload.edits.items()}
        merged = payload.edits

    blocking: list[dict[str, str]] = []
    for col in payload.mandatory_cols:
        val = str(merged.get(col, "") or "")
        if not val.strip() or val.strip() in ("None", "nan"):
            blocking.append({
                "column": col,
                "reason": f"'{col}' is required and cannot be empty.",
                "fix": f"Enter a value for '{col}' before saving.",
            })

    b, w = await asyncio.to_thread(
        lambda: config_rules.run_all_rules(
            schema, table, payload.original or {}, changed, user_token=token
        )
    )
    blocking.extend(b)
    return {"blocking": blocking, "warnings": w}


# ── CRUD ──────────────────────────────────────────────────────────────────────

@app.patch("/api/tables/{schema}/{table}/row")
@limiter.limit("10/minute")
async def update_row(
    request: Request,
    schema: str, table: str,
    payload: UpdatePayload,
    token: str | None = Depends(auth_helpers.resolve_user_token),
    user: str = Depends(auth_helpers.resolve_user_email),
) -> dict[str, Any]:
    _record_activity()
    _safe_name(schema); _safe_name(table)

    changed = {
        col: (str(payload.original.get(col, "") or ""), str(payload.edits.get(col, "") or ""))
        for col in payload.edits
        if str(payload.original.get(col, "") or "") != str(payload.edits.get(col, "") or "")
    }
    if not changed:
        return {"saved": False, "message": "No changes detected."}

    merged = _merged_row(payload.original, payload.edits)
    pk_cols = await asyncio.to_thread(
        lambda: _resolve_pk_cols_list(schema, table, payload.pk_cols, token)
    )
    table_cols = await asyncio.to_thread(
        lambda: db_client.get_table_columns(CATALOG, schema, table, user_token=token)
    )

    # Mandatory + rule validation
    errors: list[dict[str, str]] = []
    for col in payload.mandatory_cols:
        val = str(merged.get(col, "") or "")
        if not val.strip() or val.strip() in ("None", "nan"):
            errors.append({
                "column": col,
                "reason": f"'{col}' is required and cannot be empty.",
                "fix": f"Enter a value for '{col}' before saving.",
            })
    blocking, _ = await asyncio.to_thread(
        lambda: config_rules.run_all_rules(
            schema, table, payload.original,
            {c: n for c, (_, n) in changed.items()},
            user_token=token,
        )
    )
    errors.extend(blocking)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    # Build parameterized UPDATE — WHERE clause uses primary key only
    set_parts = []
    set_vals = []
    for col, (_, new_val) in changed.items():
        col_name = table_cols.get(col.lower(), col)
        _safe_name(col_name)
        set_parts.append(f"{col_name} = ?")
        set_vals.append(new_val)

    _append_update_audit_cols(set_parts, set_vals, table_cols, user)

    where_parts: list[str] = []
    where_vals: list[Any] = []
    for pk in pk_cols:
        pk_val = _row_value(payload.original, pk)
        if pk_val is None or str(pk_val).strip() in ("", "None", "nan"):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot update row: missing primary key value for '{pk}'. Refresh and try again.",
            )
        col_name = table_cols.get(pk.lower(), pk)
        _safe_name(col_name)
        where_parts.append(f"{col_name} = ?")
        where_vals.append(str(pk_val))

    version_col = payload.version_col
    original_version = _row_value(payload.original, version_col) if version_col else None
    if version_col and original_version is not None and version_col.lower() in table_cols:
        vcol = table_cols[version_col.lower()]
        set_parts.append(f"{vcol} = {vcol} + 1")
        where_parts.append(f"{vcol} = ?")
        where_vals.append(str(original_version))

    sql = (
        f"UPDATE {CATALOG}.{schema}.{table} SET {', '.join(set_parts)} "
        f"WHERE {' AND '.join(where_parts)}"
    )
    logger.info("UPDATE row %s.%s WHERE pk=%s", schema, table, where_vals[: len(pk_cols)])

    rows_affected = await asyncio.to_thread(
        lambda: db_client.execute(sql, params=tuple(set_vals + where_vals), user_token=token)
    )

    if rows_affected > 1:
        raise HTTPException(
            status_code=409,
            detail=f"Update matched {rows_affected} rows — check primary key configuration.",
        )
    if rows_affected == 0 and original_version is not None:
        raise HTTPException(
            status_code=409,
            detail="Conflict: row was modified by another user. Please refresh and try again."
        )

    # Audit log — one row per changed column
    pk_info = {pk: _row_value(payload.original, pk) for pk in pk_cols}
    for col, (old_val, new_val) in changed.items():
        await asyncio.to_thread(
            lambda c=col, o=old_val, n=new_val: db_client.log_audit(
                user, schema, table, pk_info, c, o, n, user_token=token
            )
        )

    return {"saved": True, "changed_columns": list(changed.keys())}


@app.post("/api/tables/{schema}/{table}/row")
@limiter.limit("10/minute")
async def insert_row(
    request: Request,
    schema: str, table: str,
    payload: InsertPayload,
    token: str | None = Depends(auth_helpers.resolve_user_token),
    user: str = Depends(auth_helpers.resolve_user_email),
) -> dict[str, Any]:
    _record_activity()
    _safe_name(schema); _safe_name(table)

    missing = [c for c in payload.mandatory_cols if not str(payload.values.get(c, "") or "").strip()]
    if missing:
        raise HTTPException(status_code=422, detail=[f"Required: {', '.join(missing)}"])

    table_cols = await asyncio.to_thread(
        lambda: db_client.get_table_columns(CATALOG, schema, table, user_token=token)
    )
    if not table_cols:
        raise HTTPException(
            status_code=422,
            detail=f"Could not read column list for {schema}.{table}. Check catalog permissions.",
        )

    col_types = await asyncio.to_thread(
        lambda: db_client.resolve_column_types(
            CATALOG,
            schema,
            table,
            config_store.get_column_storage_types(schema, table, user_token=token),
            user_token=token,
        )
    )

    cols: list[str] = []
    vals: list[Any] = []
    for key, val in payload.values.items():
        lk = str(key).lower()
        if lk in db_client.AUDIT_COLUMN_NAMES or lk not in table_cols:
            if lk not in table_cols:
                logger.info("Skipping unknown insert column '%s' for %s.%s", key, schema, table)
            continue
        normalized = db_client.normalize_cell_for_storage(
            val, col_types.get(lk, "string")
        )
        if normalized is None:
            continue
        cols.append(table_cols[lk])
        vals.append(normalized)

    _append_insert_audit_cols(cols, vals, table_cols, user)

    if not cols:
        raise HTTPException(status_code=422, detail="No valid columns to insert.")

    placeholders = []
    param_vals = []
    for c, v in zip(cols, vals):
        if v == "current_timestamp()":
            placeholders.append("current_timestamp()")
        else:
            placeholders.append("?")
            param_vals.append(v)

    await asyncio.to_thread(
        lambda: db_client.execute(
            f"INSERT INTO {CATALOG}.{schema}.{table} ({', '.join(cols)}) "
            f"VALUES ({', '.join(placeholders)})",
            params=tuple(param_vals),
            user_token=token,
        )
    )

    # Audit log — one entry per inserted business column
    pk_info = {
        pk: payload.values.get(pk)
        for pk in payload.pk_cols or []
    }
    for col, val in payload.values.items():
        await asyncio.to_thread(
            db_client.log_audit,
            user,
            schema,
            table,
            pk_info,
            col,
            None,        # old_value is None for inserts
            str(val or ""),
            "INSERT",
            user_token=token,
        )

    return {"inserted": True}


@app.delete("/api/tables/{schema}/{table}/row")
@limiter.limit("10/minute")
async def delete_row(
    request: Request,
    schema: str, table: str,
    pk_values: dict[str, Any],
    soft: bool = Query(default=True),   # soft=True sets is_active=FALSE; soft=False hard deletes
    token: str | None = Depends(auth_helpers.resolve_user_token),
    user: str = Depends(auth_helpers.resolve_user_email),
) -> dict[str, Any]:
    _record_activity()
    _safe_name(schema); _safe_name(table)

    where_parts = [f"{_safe_name(k)} = ?" for k in pk_values]
    where_vals = list(pk_values.values())

    if soft:
        table_cols = await asyncio.to_thread(
            lambda: db_client.get_table_columns(CATALOG, schema, table, user_token=token)
        )
        set_parts = []
        set_vals = []
        if "is_active" in table_cols:
            set_parts.append(f"{table_cols['is_active']} = FALSE")
        if "updated_by" in table_cols:
            set_parts.append(f"{table_cols['updated_by']} = ?"); set_vals.append(user)
        elif "modified_by" in table_cols:
            set_parts.append(f"{table_cols['modified_by']} = ?"); set_vals.append(user)
        if "updated_at" in table_cols:
            set_parts.append(f"{table_cols['updated_at']} = current_timestamp()")
        elif "modified_date" in table_cols:
            set_parts.append(f"{table_cols['modified_date']} = current_timestamp()")
        if not set_parts:
            await asyncio.to_thread(
                lambda: db_client.execute(
                    f"DELETE FROM {CATALOG}.{schema}.{table} WHERE {' AND '.join(where_parts)}",
                    params=tuple(where_vals),
                    user_token=token,
                )
            )
            return {"deleted": True, "mode": "hard_fallback"}
        await asyncio.to_thread(
            lambda: db_client.execute(
                f"UPDATE {CATALOG}.{schema}.{table} SET {', '.join(set_parts)} "
                f"WHERE {' AND '.join(where_parts)}",
                params=tuple(set_vals + where_vals),
                user_token=token,
            )
        )
        return {"deleted": True, "mode": "soft"}
    else:
        await asyncio.to_thread(
            lambda: db_client.execute(
                f"DELETE FROM {CATALOG}.{schema}.{table} WHERE {' AND '.join(where_parts)}",
                params=tuple(where_vals),
                user_token=token,
            )
        )
        return {"deleted": True, "mode": "hard"}


# ── bulk reference upload ─────────────────────────────────────────────────────

@app.get("/api/bulk/tables")
def bulk_tables() -> list[dict]:
    return bulk.list_configs()


@app.get("/api/bulk/{schema}/{table}/template")
def bulk_template(schema: str, table: str) -> dict[str, str]:
    config = bulk.get_config(schema, table)
    if not config:
        raise HTTPException(status_code=404, detail="No bulk config for this table.")
    return {"csv_text": bulk.template_csv(config)}


@app.post("/api/bulk/{schema}/{table}/validate")
def bulk_validate(
    schema: str, table: str,
    payload: BulkCsvPayload,
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> dict[str, Any]:
    config = bulk.get_config(schema, table)
    if not config:
        raise HTTPException(status_code=404, detail="No bulk config for this table.")
    return bulk.validate_csv(payload.csv_text, config, user_token=token)


@app.post("/api/bulk/{schema}/{table}/insert")
@limiter.limit("5/minute")
def bulk_insert(
    request: Request,
    schema: str, table: str,
    payload: BulkInsertPayload,
    token: str | None = Depends(auth_helpers.resolve_user_token),
    user: str = Depends(auth_helpers.resolve_user_email),
) -> dict[str, Any]:
    config = bulk.get_config(schema, table)
    if not config:
        raise HTTPException(status_code=404, detail="No bulk config for this table.")
    if payload.mode not in ("append", "overwrite"):
        raise HTTPException(status_code=400, detail="mode must be 'append' or 'overwrite'")
    inserted = bulk.insert_rows(
        config, payload.rows, user,
        mode=payload.mode, user_token=token
    )
    return {"inserted": inserted, "mode": payload.mode}


# ── staged bulk upload (validate → apply) ─────────────────────────────────────

@app.post("/api/tables/{schema}/{table}/upload/validate")
@limiter.limit("10/minute")
async def upload_validate(
    request: Request,
    schema: str,
    table: str,
    payload: UploadValidatePayload,
    token: str | None = Depends(auth_helpers.resolve_user_token),
    user: str = Depends(auth_helpers.resolve_user_email),
) -> dict[str, Any]:
    """Stage file, validate all rows (all-or-nothing). May queue for approver."""
    _record_activity()
    _safe_name(schema)
    _safe_name(table)
    if payload.mode not in ("upsert", "update", "append", "overwrite"):
        raise HTTPException(status_code=400, detail="mode must be upsert or overwrite.")
    catalog = _safe_name(payload.catalog.strip()) if payload.catalog.strip() else CATALOG
    content_bytes = _upload_payload_bytes(
        csv_text=payload.csv_text,
        file_base64=payload.file_base64,
    )
    if content_bytes > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds {MAX_UPLOAD_MB}MB limit.")

    upload_mode = payload.mode
    if upload_mode in ("update", "append"):
        upload_mode = "upsert"

    def _run_validate() -> dict[str, Any]:
        common = dict(
            catalog=catalog,
            schema=schema,
            table=table,
            csv_text=payload.csv_text,
            file_base64=payload.file_base64,
            file_format=payload.file_format,
            delimiter=payload.delimiter,
            has_header=payload.has_header,
            filename=payload.filename,
            submitted_by=user,
            user_token=token,
        )
        if upload_mode == "upsert":
            result = bulk_upsert_ops.validate_upsert_upload(**common)
        elif payload.mode == "update":
            result = bulk_update_ops.validate_update_upload(**common)
        elif payload.mode == "append":
            result = bulk_upload_ops.validate_append_upload(**common)
        else:
            result = bulk_upload_ops.validate_overwrite_upload(**common)
        return approval_ops.finalize_validation_result(
            result,
            schema=schema,
            table=table,
            submitted_by=user,
            user_token=token,
        )

    result = await asyncio.to_thread(_run_validate)
    if not result.get("can_apply") and not result.get("requires_approval"):
        raise HTTPException(status_code=422, detail=result)
    return result


@app.post("/api/tables/{schema}/{table}/upload/apply")
@limiter.limit("5/minute")
async def upload_apply(
    request: Request,
    schema: str,
    table: str,
    payload: UploadApplyPayload,
    token: str | None = Depends(auth_helpers.resolve_user_token),
    user: str = Depends(auth_helpers.resolve_user_email),
) -> dict[str, Any]:
    """Apply a validated (or approved) change request."""
    _record_activity()
    _safe_name(schema)
    _safe_name(table)
    try:
        return await asyncio.to_thread(
            lambda: upload_apply_ops.apply_change_request(
                payload.change_request_id,
                applied_by=user,
                user_token=token,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/tables/{schema}/{table}/edits/stage")
@limiter.limit("15/minute")
async def stage_grid_edits(
    request: Request,
    schema: str,
    table: str,
    payload: GridStagePayload,
    token: str | None = Depends(auth_helpers.resolve_user_token),
    user: str = Depends(auth_helpers.resolve_user_email),
) -> dict[str, Any]:
    """Stage grid edits to Delta staging table; validate; optionally queue approval."""
    _record_activity()
    _safe_name(schema)
    _safe_name(table)
    catalog = _safe_name(payload.catalog.strip()) if payload.catalog.strip() else CATALOG

    def _run() -> dict[str, Any]:
        result = grid_staging_ops.validate_grid_edits(
            catalog=catalog,
            schema=schema,
            table=table,
            updates=[u.model_dump() for u in payload.updates],
            inserts=[i.model_dump() for i in payload.inserts],
            submitted_by=user,
            user_token=token,
        )
        return approval_ops.finalize_validation_result(
            result,
            schema=schema,
            table=table,
            submitted_by=user,
            user_token=token,
        )

    result = await asyncio.to_thread(_run)
    if not result.get("can_apply") and not result.get("requires_approval"):
        raise HTTPException(status_code=422, detail=result)
    return result


@app.post("/api/tables/{schema}/{table}/edits/apply")
@limiter.limit("5/minute")
async def apply_grid_edits(
    request: Request,
    schema: str,
    table: str,
    payload: UploadApplyPayload,
    token: str | None = Depends(auth_helpers.resolve_user_token),
    user: str = Depends(auth_helpers.resolve_user_email),
) -> dict[str, Any]:
    """Apply a validated (or approved) grid edit change request."""
    _record_activity()
    _safe_name(schema)
    _safe_name(table)
    try:
        return await asyncio.to_thread(
            lambda: upload_apply_ops.apply_change_request(
                payload.change_request_id,
                applied_by=user,
                user_token=token,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/change-requests/{change_request_id}/diffs")
@limiter.limit("30/minute")
async def get_change_request_diffs(
    request: Request,
    change_request_id: str,
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> dict[str, Any]:
    """Line-level diffs for approver review UI."""
    _record_activity()
    rec = await asyncio.to_thread(
        lambda: change_request.get_request(change_request_id, user_token=token)
    )
    if not rec:
        raise HTTPException(status_code=404, detail="Change request not found.")
    diffs = await asyncio.to_thread(
        lambda: approval_ops.get_change_request_diffs(change_request_id, user_token=token)
    )
    revision_id = rec.get("revision_id")
    revision = None
    if revision_id:
        from .shared import revision_ops
        revision = await asyncio.to_thread(
            lambda: revision_ops.get_revision(str(revision_id), user_token=token)
        )
    schema_name = str(rec.get("schema_name") or "")
    table_name = str(rec.get("table_name") or "")
    pk_cols = sorted(config_store.get_pk_cols(schema_name, table_name, user_token=token))
    business_key_cols = config_store.get_upload_unique_columns(schema_name, table_name) or pk_cols
    return {
        "change_request_id": change_request_id,
        "status": rec.get("status"),
        "request_type": rec.get("request_type"),
        "schema_name": schema_name,
        "table_name": table_name,
        "submitted_by": rec.get("submitted_by"),
        "revision_id": revision_id,
        "revision": revision,
        "pk_cols": pk_cols,
        "business_key_cols": business_key_cols,
        "diffs": diffs,
    }


@app.get("/api/overview")
@limiter.limit("30/minute")
async def get_overview(
    request: Request,
    refresh: bool = Query(default=False),
    token: str | None = Depends(auth_helpers.resolve_user_token),
    user: str = Depends(auth_helpers.resolve_user_email),
) -> dict[str, Any]:
    """Landing dashboard: pending approvals, recent edits, activity metrics."""
    _record_activity()
    return await asyncio.to_thread(
        lambda: overview_ops.get_overview(
            user, user_token=token, force_refresh=refresh
        )
    )


@app.get("/api/change-requests/pending")
@limiter.limit("30/minute")
async def list_pending_change_requests(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    schema_name: str | None = Query(default=None),
    table_name: str | None = Query(default=None),
    token: str | None = Depends(auth_helpers.resolve_user_token),
    user: str = Depends(auth_helpers.resolve_user_email),
) -> dict[str, Any]:
    """Paginated approval inbox for the current approver."""
    _record_activity()
    return await asyncio.to_thread(
        lambda: change_request.list_pending_for_approver(
            user,
            user_token=token,
            page=page,
            page_size=page_size,
            schema_name=schema_name,
            table_name=table_name,
        )
    )


@app.get("/api/change-requests/{change_request_id}/review-sql")
@limiter.limit("30/minute")
async def get_change_request_review_sql(
    request: Request,
    change_request_id: str,
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> dict[str, Any]:
    """Databricks SQL snippets to review staged vs live data before approval."""
    _record_activity()
    try:
        return await asyncio.to_thread(
            lambda: approval_ops.build_review_sql(change_request_id, user_token=token)
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/change-requests/{change_request_id}/approve")
@limiter.limit("10/minute")
async def approve_change_request(
    request: Request,
    change_request_id: str,
    token: str | None = Depends(auth_helpers.resolve_user_token),
    user: str = Depends(auth_helpers.resolve_user_email),
) -> dict[str, Any]:
    _record_activity()
    try:
        return await asyncio.to_thread(
            lambda: approval_ops.approve_request(
                change_request_id, approver=user, user_token=token
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@app.post("/api/change-requests/{change_request_id}/reject")
@limiter.limit("10/minute")
async def reject_change_request(
    request: Request,
    change_request_id: str,
    payload: RejectPayload,
    token: str | None = Depends(auth_helpers.resolve_user_token),
    user: str = Depends(auth_helpers.resolve_user_email),
) -> dict[str, Any]:
    _record_activity()
    try:
        return await asyncio.to_thread(
            lambda: approval_ops.reject_request(
                change_request_id,
                approver=user,
                reason=payload.reason,
                user_token=token,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@app.get("/api/approvals/review")
@limiter.limit("30/minute")
async def review_by_token(
    request: Request,
    token_value: str = Query(..., alias="token"),
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> dict[str, Any]:
    """Load change-request summary from email approval link token."""
    _record_activity()
    try:
        return await asyncio.to_thread(
            lambda: approval_ops.get_review_by_token(token_value, user_token=token)
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/change-requests/{change_request_id}")
async def get_change_request(
    change_request_id: str,
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> dict[str, Any]:
    rec = await asyncio.to_thread(
        lambda: change_request.get_request(change_request_id, user_token=token)
    )
    if not rec:
        raise HTTPException(status_code=404, detail="Change request not found.")
    return rec


# ── generic file upload ───────────────────────────────────────────────────────

@app.get("/api/upload/check")
@app.post("/api/upload/check")
def upload_check(
    catalog: str = Query(...),
    schema: str = Query(...),
    table: str = Query(...),
    token: str | None = Depends(auth_helpers.resolve_user_token),
) -> dict[str, Any]:
    """Check if a target table exists before upload — lets UI prompt for append/overwrite."""
    exists = db_client.table_exists(_safe_name(catalog), _safe_name(schema), _safe_name(table), user_token=token)
    return {"exists": exists, "table": f"{catalog}.{schema}.{table}"}


@app.post("/api/upload")
@limiter.limit("5/minute")
async def upload_file(
    request: Request,
    payload: GenericUploadPayload,
    token: str | None = Depends(auth_helpers.resolve_user_token),
    user: str = Depends(auth_helpers.resolve_user_email),
) -> dict[str, Any]:
    """
    Generic CSV upload to any table.
    If table exists, mode must be 'append' or 'overwrite' (UI enforces the choice).
    """
    _record_activity()
    if payload.mode not in ("append", "overwrite"):
        raise HTTPException(status_code=400, detail="mode must be 'append' or 'overwrite'")

    content_bytes = _upload_payload_bytes(
        csv_text=payload.csv_text,
        file_base64=payload.file_base64,
    )
    if content_bytes > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File too large. Maximum size is "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)}MB. "
                f"Your file is "
                f"{content_bytes // (1024 * 1024)}MB. "
                "Split into smaller files and upload separately."
            ),
        )

    try:
        df = bulk.parse_upload_dataframe(
            csv_text=payload.csv_text,
            file_base64=payload.file_base64,
            filename=payload.filename,
            file_format=payload.file_format,
            delimiter=payload.delimiter,
            has_header=payload.has_header,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid upload file: {exc}")

    catalog = _safe_name(payload.catalog)
    schema = _safe_name(payload.schema)
    table = _safe_name(payload.table)

    validation_errors = await upload_ops.validate_upload_schema(
        df, catalog, schema, table, user_token=token
    )
    if validation_errors:
        table_cols = await asyncio.to_thread(
            lambda: db_client.get_table_columns(
                catalog, schema, table, user_token=token
            ) or {}
        )
        raise HTTPException(
            status_code=422,
            detail={
                "message": "CSV schema does not match table schema.",
                "errors": validation_errors,
                "expected_columns": list(table_cols.keys()),
                "csv_columns": list(df.columns),
            },
        )

    try:
        result = await asyncio.to_thread(
            lambda: upload_ops.upload_dataframe(
                catalog, schema, table,
                df, payload.mode, user, user_token=token,
                filename=payload.filename or "upload.csv",
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return result


# ── admin: invalidate rule cache ──────────────────────────────────────────────

@app.post("/api/admin/rules/refresh")
def refresh_rules(
    schema: str | None = Query(default=None),
    table: str | None = Query(default=None),
) -> dict[str, str]:
    """Invalidate rule + config cache. Pass schema+table for one table, or clear all."""
    config_rules.invalidate_cache(schema, table)
    target = f"{schema}.{table}" if schema and table else "all tables"
    return {"status": f"cache cleared for {target}"}


# ── serve frontend ────────────────────────────────────────────────────────────

APP_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = APP_ROOT / "static"
FRONTEND_DEV_DIR = APP_ROOT / "frontend"


def _resolve_frontend_dir() -> Path | None:
    """Prefer built static/ for production (Databricks Apps); fall back to frontend/ for local dev."""
    if (STATIC_DIR / "index.html").exists():
        logger.info("Serving production frontend from %s", STATIC_DIR)
        return STATIC_DIR
    if (FRONTEND_DEV_DIR / "index.html").exists():
        logger.warning(
            "static/index.html not found — serving unbuilt frontend/ (local dev only). "
            "Run 'cd frontend && npm run build' before deploying to Databricks."
        )
        return FRONTEND_DEV_DIR
    return None


_frontend_dir = _resolve_frontend_dir()
if _frontend_dir is not None:
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
else:
    @app.get("/", include_in_schema=False)
    def _no_frontend() -> HTMLResponse:
        return HTMLResponse(
            "<h2>Frontend not built.</h2>"
            "<p>Run <code>cd frontend && npm install && npm run build</code>, "
            "then restart the app.</p>"
        )
