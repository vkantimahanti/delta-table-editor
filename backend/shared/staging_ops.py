"""Per-table app staging: {table_name}_app_stage with audit metadata columns."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from . import db_client

logger = logging.getLogger("delta_editor.staging_ops")

CATALOG = db_client.CATALOG
STAGING_SCHEMA = os.environ.get("STAGING_SCHEMA", "dmz")
STAGE_SUFFIX = os.environ.get("STAGE_TABLE_SUFFIX", "_app_stage")
STAGING_VOLUME_PATH = os.environ.get(
    "STAGING_VOLUME_PATH",
    "/Volumes/your_catalog/your_schema/data_canvas_staging",
)
EXPORT_VOLUME_PATH = os.environ.get(
    "EXPORT_VOLUME_PATH",
    "/Volumes/your_catalog/your_schema/data_canvas_exports",
)
INSERT_BATCH_SIZE = int(os.environ.get("STAGING_INSERT_BATCH_SIZE", "200"))

# Staging metadata columns (audit trail for pending changes)
STAGE_CR_ID = "_stage_cr_id"
STAGE_OPERATION = "_stage_operation"
STAGE_VERSION = "_stage_version"
STAGE_USER = "_stage_user"
STAGE_AT = "_stage_at"

STAGE_META_COLS = [STAGE_CR_ID, STAGE_OPERATION, STAGE_VERSION, STAGE_USER, STAGE_AT]


def _safe_ident(name: str) -> str:
    if not re.match(r"^[\w]+$", name):
        raise ValueError(f"Invalid identifier: {name}")
    return name


def app_stage_table_name(table_name: str, suffix: str | None = None) -> str:
    """e.g. dash_test_carrier → dash_test_carrier_app_stage"""
    suf = suffix or STAGE_SUFFIX
    base = _safe_ident(table_name)
    if base.endswith(suf):
        return base
    return f"{base}{suf}"


def full_app_stage_table(
    schema: str,
    table_name: str,
    *,
    catalog: str | None = None,
    suffix: str | None = None,
) -> str:
    cat = _safe_ident(catalog or CATALOG)
    sch = _safe_ident(schema)
    tbl = _safe_ident(app_stage_table_name(table_name, suffix))
    return f"{cat}.{sch}.{tbl}"


def staging_merge_source(full_stage: str, change_request_id: str) -> str:
    """Subquery scoped to one change request for MERGE."""
    safe_cr = str(change_request_id).replace("'", "''")
    return f"(SELECT * FROM {full_stage} WHERE `{STAGE_CR_ID}` = '{safe_cr}')"


def ensure_app_stage_table(
    schema: str,
    table_name: str,
    *,
    catalog: str | None = None,
    suffix: str | None = None,
    user_token: str | None = None,
) -> str:
    """Create {table}_app_stage if missing: target business columns + stage metadata."""
    full = full_app_stage_table(schema, table_name, catalog=catalog, suffix=suffix)
    target = f"{catalog or CATALOG}.{schema}.{table_name}"
    try:
        db_client.query(f"DESCRIBE TABLE {full}", user_token=user_token)
        return full
    except Exception:
        pass

    target_cols = db_client.get_table_columns(catalog or CATALOG, schema, table_name, user_token=user_token)
    if not target_cols:
        raise ValueError(f"Target table {schema}.{table_name} not found — cannot create app stage.")

    col_defs = [f"`{_safe_ident(actual)}` STRING" for actual in target_cols.values()]
    col_defs.extend([
        f"`{STAGE_CR_ID}` STRING",
        f"`{STAGE_OPERATION}` STRING",
        f"`{STAGE_VERSION}` STRING",
        f"`{STAGE_USER}` STRING",
        f"`{STAGE_AT}` TIMESTAMP",
    ])
    db_client.execute(
        f"CREATE TABLE {full} ({', '.join(col_defs)}) USING DELTA",
        user_token=user_token,
    )
    logger.info("Created app stage table %s", full)
    return full


def clear_app_stage_for_request(
    schema: str,
    table_name: str,
    change_request_id: str,
    *,
    catalog: str | None = None,
    suffix: str | None = None,
    user_token: str | None = None,
) -> None:
    full = full_app_stage_table(schema, table_name, catalog=catalog, suffix=suffix)
    try:
        db_client.execute(
            f"DELETE FROM {full} WHERE `{STAGE_CR_ID}` = ?",
            params=(change_request_id,),
            user_token=user_token,
        )
    except Exception as exc:
        logger.warning("Could not clear app stage %s for %s: %s", full, change_request_id, exc)


def load_app_stage(
    schema: str,
    table_name: str,
    change_request_id: str,
    df: pd.DataFrame,
    *,
    catalog: str | None = None,
    suffix: str | None = None,
    submitted_by: str = "",
    operation: str = "update",
    user_token: str | None = None,
) -> str:
    """Load rows into {table}_app_stage for this change request."""
    if df.empty:
        raise ValueError("No rows to stage.")

    full = ensure_app_stage_table(
        schema, table_name, catalog=catalog, suffix=suffix, user_token=user_token
    )
    clear_app_stage_for_request(
        schema, table_name, change_request_id,
        catalog=catalog, suffix=suffix, user_token=user_token,
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    work = df.copy()
    work[STAGE_CR_ID] = change_request_id
    work[STAGE_OPERATION] = operation
    ver_col = next((c for c in work.columns if str(c).lower() == "version"), None)
    work[STAGE_VERSION] = work[ver_col].astype(str) if ver_col else ""
    work[STAGE_USER] = submitted_by
    work[STAGE_AT] = now

    columns = list(work.columns)
    col_list = ", ".join(f"`{_safe_ident(str(c))}`" for c in columns)
    placeholders = ", ".join("?" for _ in columns)

    batch: list[tuple[Any, ...]] = []
    for _, row in work.iterrows():
        vals = []
        for c in columns:
            v = row[c]
            if isinstance(v, (pd.Timestamp, datetime)):
                vals.append(str(v))
            elif c == STAGE_AT:
                vals.append(str(v) if v is not None else "")
            elif pd.isna(v) or str(v).strip() == "":
                vals.append("")
            else:
                vals.append(str(v).strip())
        batch.append(tuple(vals))
        if len(batch) >= INSERT_BATCH_SIZE:
            _insert_batch(full, col_list, placeholders, batch, user_token)
            batch = []
    if batch:
        _insert_batch(full, col_list, placeholders, batch, user_token)

    logger.info("Loaded %d rows into %s for %s", len(work), full, change_request_id)
    return full


# ── Legacy aliases (change_request_id-only callers → need schema+table at call site) ──

def drop_staging_table(
    change_request_id: str,
    user_token: str | None = None,
    *,
    schema: str | None = None,
    table_name: str | None = None,
    catalog: str | None = None,
) -> None:
    if schema and table_name:
        clear_app_stage_for_request(
            schema, table_name, change_request_id, catalog=catalog, user_token=user_token
        )
    else:
        logger.debug("drop_staging_table(%s) — no schema/table; skipped", change_request_id)


def create_staging_from_dataframe(
    change_request_id: str,
    df: pd.DataFrame,
    user_token: str | None = None,
    *,
    schema: str | None = None,
    table_name: str | None = None,
    catalog: str | None = None,
    submitted_by: str = "",
    operation: str = "update",
) -> str:
    if not schema or not table_name:
        raise ValueError("schema and table_name are required for app stage loading.")
    return load_app_stage(
        schema, table_name, change_request_id, df,
        catalog=catalog, submitted_by=submitted_by, operation=operation,
        user_token=user_token,
    )


def _insert_batch(
    full_table: str,
    col_list: str,
    placeholders: str,
    rows: list[tuple[Any, ...]],
    user_token: str | None,
) -> None:
    if not rows:
        return
    # col_list is backtick-quoted column names; derive plain names for execute_multi_insert
    columns = [c.strip().strip("`") for c in col_list.split(",")]
    db_client.execute_multi_insert(
        full_table, columns, rows, user_token=user_token, chunk_size=INSERT_BATCH_SIZE,
    )


def list_staging_columns(staging_full: str, user_token: str | None = None) -> list[str]:
    df = db_client.query(f"DESCRIBE TABLE {staging_full}", user_token=user_token)
    if df.empty:
        return []
    name_col = "col_name" if "col_name" in df.columns else df.columns[0]
    return [str(r[name_col]) for _, r in df.iterrows()]


def business_staging_columns(staging_full: str, user_token: str | None = None) -> list[str]:
    """Target business columns only — excludes _stage_* metadata."""
    meta = set(STAGE_META_COLS)
    return [c for c in list_staging_columns(staging_full, user_token=user_token) if c not in meta]


def volume_upload_path(change_request_id: str, filename: str) -> str:
    safe = re.sub(r"[^\w.\-]", "_", filename or "upload.csv")
    return f"{STAGING_VOLUME_PATH.rstrip('/')}/{change_request_id}/{safe}"


def try_write_volume_file(path: str, content: str) -> str | None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        logger.info("Wrote upload copy to volume path %s", path)
        return path
    except Exception as exc:
        logger.warning("Volume write skipped (%s): %s", path, exc)
        return None


def try_write_volume_bytes(path: str, content: bytes) -> str | None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        logger.info("Wrote binary upload copy to volume path %s", path)
        return path
    except Exception as exc:
        logger.warning("Volume write skipped (%s): %s", path, exc)
        return None
