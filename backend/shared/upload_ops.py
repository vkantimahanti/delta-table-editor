"""CSV / DataFrame upload — parameterized INSERT with audit column auto-fill."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import pandas as pd

from . import audit_cols, db_client

logger = logging.getLogger("delta_editor.upload_ops")


async def validate_upload_schema(
    df: pd.DataFrame,
    catalog: str,
    schema: str,
    table: str,
    user_token: str | None = None,
) -> list[str]:
    """
    Compare CSV columns against actual table columns.
    Returns list of error strings. Empty list = valid.
    """
    errors: list[str] = []

    table_cols = await asyncio.to_thread(
        db_client.get_table_columns,
        catalog, schema, table,
        user_token=user_token,
    )

    if not table_cols:
        # Table does not exist yet — skip validation
        # Will be created from CSV schema
        return []

    actual_cols = set(table_cols.keys())  # lowercase
    csv_cols = {c.lower().strip() for c in df.columns}

    unknown = csv_cols - actual_cols - db_client.AUDIT_COLUMN_NAMES
    if unknown:
        errors.append(
            f"CSV contains columns not in the table: "
            f"{', '.join(sorted(unknown))}. "
            f"Check column names match exactly."
        )

    missing = actual_cols - csv_cols - db_client.AUDIT_COLUMN_NAMES
    if missing:
        errors.append(
            f"CSV is missing columns that exist in the table: "
            f"{', '.join(sorted(missing))}. "
            f"Add these columns to your CSV or they will be null."
        )

    return errors


def _run_insert(
    full_name: str,
    cols: list[str],
    vals: list[Any],
    user_token: str | None,
) -> None:
    placeholders: list[str] = []
    param_vals: list[Any] = []
    for v in vals:
        if v == "current_timestamp()":
            placeholders.append("current_timestamp()")
        elif v is None:
            placeholders.append("NULL")
        else:
            placeholders.append("?")
            param_vals.append(v)
    db_client.execute(
        f"INSERT INTO {full_name} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})",
        params=tuple(param_vals),
        user_token=user_token,
    )


def upload_dataframe(
    catalog: str,
    schema: str,
    table: str,
    df: pd.DataFrame,
    mode: str,
    user: str,
    user_token: str | None = None,
    filename: str = "",
) -> dict[str, Any]:
    """Insert DataFrame rows with audit columns on each row and one audit-log summary."""
    if mode not in ("append", "overwrite"):
        raise ValueError("mode must be 'append' or 'overwrite'")

    full_name = f"{catalog}.{schema}.{table}"
    exists = db_client.table_exists(catalog, schema, table, user_token=user_token)

    if not exists:
        dtype_map = {
            "int64": "BIGINT", "int32": "INT", "float64": "DOUBLE",
            "float32": "FLOAT", "object": "STRING", "bool": "BOOLEAN",
            "datetime64[ns]": "TIMESTAMP",
        }
        col_defs = ", ".join(
            f"`{c}` {dtype_map.get(str(dt), 'STRING')}" for c, dt in df.dtypes.items()
        )
        db_client.execute(
            f"CREATE TABLE {full_name} ({col_defs}) USING DELTA",
            user_token=user_token,
        )
        logger.info("Created new table: %s", full_name)
        mode = "append"

    table_cols = db_client.get_table_columns(catalog, schema, table, user_token=user_token)
    if not table_cols:
        raise ValueError(f"Could not read column list for {full_name}.")

    file_columns = [
        table_cols[str(c).strip().lower()]
        for c in df.columns
        if str(c).strip().lower() in table_cols
    ]

    if mode == "overwrite":
        db_client.execute(f"TRUNCATE TABLE {full_name}", user_token=user_token)
        logger.info("Upload overwrite: truncated %s", full_name)

    count = 0
    for _, row in df.iterrows():
        cols: list[str] = []
        vals: list[Any] = []
        for c in df.columns:
            lk = str(c).strip().lower()
            if lk not in table_cols:
                continue
            raw = row[c]
            val = None if pd.isna(raw) or str(raw).strip() == "" else str(raw).strip()
            cols.append(table_cols[lk])
            vals.append(val)

        audit_cols.append_insert_audit_cols(cols, vals, table_cols, user)
        _run_insert(full_name, cols, vals, user_token)
        count += 1

    if count > 0:
        db_client.log_upload_summary(
            user, schema, table, mode, count,
            filename=filename,
            column_names=file_columns,
            user_token=user_token,
        )

    logger.info("Upload complete: %d rows -> %s (mode=%s)", count, full_name, mode)
    return {
        "rows_inserted": count,
        "table": full_name,
        "mode": mode,
        "created": not exists,
        "audit_columns": True,
        "audit_log_entries": 1 if count > 0 else 0,
        "filename": filename or "upload.csv",
        "columns": file_columns,
    }
