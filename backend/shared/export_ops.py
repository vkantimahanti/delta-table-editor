"""Server-side filtered export to UC Volume (CSV / XLSX)."""
from __future__ import annotations

import csv
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from . import change_request, config_store, db_client, filter_sql, staging_ops

logger = logging.getLogger("delta_editor.export_ops")

CATALOG = db_client.CATALOG
EXPORT_MAX_ROWS = int(os.environ.get("EXPORT_MAX_ROWS", os.environ.get("BULK_UPDATE_MAX_ROWS", "10000")))
EXPORT_BATCH_SIZE = int(os.environ.get("EXPORT_BATCH_SIZE", "500"))
VERSION_COL = "version"
LOCAL_EXPORT_ROOT = Path(os.environ.get("LOCAL_EXPORT_ROOT", "tmp/exports"))


def _safe_ident(name: str) -> str:
    if not re.match(r"^[\w]+$", name):
        raise ValueError(f"Invalid identifier: {name}")
    return name


def _format_ext(fmt: str) -> str:
    normalized = (fmt or "csv").strip().lower()
    if normalized in ("xlsx", "excel"):
        return "xlsx"
    if normalized in ("tsv", "txt"):
        return "tsv"
    return "csv"


def volume_export_path(change_request_id: str, fmt: str) -> str:
    ext = _format_ext(fmt)
    suffix = "csv" if ext == "tsv" else ext
    return f"{staging_ops.EXPORT_VOLUME_PATH.rstrip('/')}/{change_request_id}/export.{suffix}"


def _resolve_write_path(change_request_id: str, fmt: str) -> str:
    """Prefer UC Volume; fall back to local tmp when volume is unavailable."""
    vol_path = volume_export_path(change_request_id, fmt)
    try:
        os.makedirs(os.path.dirname(vol_path), exist_ok=True)
        return vol_path
    except Exception as exc:
        logger.warning("Volume path unavailable (%s): %s", vol_path, exc)

    ext = _format_ext(fmt)
    suffix = "csv" if ext == "tsv" else ext
    local_path = LOCAL_EXPORT_ROOT / change_request_id / f"export.{suffix}"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    return str(local_path)


def _resolve_export_columns(
    *,
    schema: str,
    table: str,
    requested: list[str],
    table_cols: dict[str, str],
    user_token: str | None,
) -> list[str]:
    """Physical column names for SELECT — always include PK + version for re-upload."""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(col: str) -> None:
        key = col.lower()
        if key in seen:
            return
        actual = table_cols.get(key)
        if not actual:
            return
        seen.add(key)
        ordered.append(actual)

    for col in requested:
        add(col)

    pk_set = config_store.get_pk_cols(schema, table, user_token=user_token)
    for pk in sorted(pk_set):
        add(pk)

    if VERSION_COL in table_cols:
        add(VERSION_COL)

    if not ordered:
        return list(table_cols.values())
    return ordered


def count_filtered_rows(
    *,
    catalog: str,
    schema: str,
    table: str,
    filters: list[Any],
    user_token: str | None = None,
) -> int:
    from_table = f"{catalog}.{schema}.{table}"
    where_clause, params = filter_sql.build_filter_where(filters, safe_column=_safe_ident)
    sql = f"SELECT COUNT(*) AS total FROM {from_table}"
    if where_clause:
        sql += f" WHERE {where_clause}"
    df = db_client.query(sql, params=params, user_token=user_token)
    return int(df.iloc[0]["total"]) if not df.empty else 0


def _iter_batches(
    *,
    catalog: str,
    schema: str,
    table: str,
    columns: list[str],
    filters: list[Any],
    user_token: str | None,
) -> Iterator[pd.DataFrame]:
    from_table = f"{catalog}.{schema}.{table}"
    cols_sql = ", ".join(f"`{_safe_ident(c)}`" for c in columns)
    where_clause, params = filter_sql.build_filter_where(filters, safe_column=_safe_ident)
    base_sql = f"SELECT {cols_sql} FROM {from_table}"
    if where_clause:
        base_sql += f" WHERE {where_clause}"

    offset = 0
    while True:
        sql = f"{base_sql} LIMIT {EXPORT_BATCH_SIZE} OFFSET {offset}"
        df = db_client.query(sql, params=params, user_token=user_token)
        if df.empty:
            break
        yield df
        offset += len(df)
        if len(df) < EXPORT_BATCH_SIZE:
            break


def _cell_value(val: Any) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val)


def _write_csv(path: str, columns: list[str], batches: Iterator[pd.DataFrame], *, delimiter: str) -> int:
    total = 0
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter=delimiter)
        writer.writerow(columns)
        for df in batches:
            for _, row in df.iterrows():
                writer.writerow([_cell_value(row.get(c)) for c in columns])
                total += 1
    return total


def _write_xlsx(path: str, columns: list[str], batches: Iterator[pd.DataFrame]) -> int:
    from openpyxl import Workbook

    total = 0
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title="export")
    ws.append(columns)
    for df in batches:
        for _, row in df.iterrows():
            ws.append([_cell_value(row.get(c)) for c in columns])
            total += 1
    wb.save(path)
    return total


def run_filtered_export(
    *,
    catalog: str,
    schema: str,
    table: str,
    filters: list[Any],
    columns: list[str],
    fmt: str,
    submitted_by: str,
    filter_snapshot: dict[str, Any] | None = None,
    user_token: str | None = None,
) -> dict[str, Any]:
    """Count → reject if over limit → stream batches → write file → change_request."""
    t0 = time.perf_counter()
    phase: dict[str, float] = {}

    row_count = count_filtered_rows(
        catalog=catalog,
        schema=schema,
        table=table,
        filters=filters,
        user_token=user_token,
    )
    phase["count"] = time.perf_counter() - t0

    if row_count == 0:
        raise ValueError("No rows match the current filters.")
    if row_count > EXPORT_MAX_ROWS:
        raise ValueError(
            f"Export has {row_count:,} rows; maximum is {EXPORT_MAX_ROWS:,}. "
            "Narrow your filters or raise EXPORT_MAX_ROWS."
        )

    table_cols = db_client.get_table_columns(catalog, schema, table, user_token=user_token)
    if not table_cols:
        raise ValueError(f"Could not read columns for {catalog}.{schema}.{table}.")

    export_cols = _resolve_export_columns(
        schema=schema,
        table=table,
        requested=columns,
        table_cols=table_cols,
        user_token=user_token,
    )

    cr_id = change_request.new_change_request_id()
    change_request.insert_request(
        change_request_id=cr_id,
        request_type="export",
        mode=None,
        schema_name=schema,
        table_name=table,
        submitted_by=submitted_by,
        catalog=catalog,
        user_token=user_token,
    )

    try:
        ext = _format_ext(fmt)
        path = _resolve_write_path(cr_id, fmt)
        batches = _iter_batches(
            catalog=catalog,
            schema=schema,
            table=table,
            columns=export_cols,
            filters=filters,
            user_token=user_token,
        )

        t_write = time.perf_counter()
        if ext == "xlsx":
            written = _write_xlsx(path, export_cols, batches)
        else:
            delimiter = "\t" if ext == "tsv" else ","
            written = _write_csv(path, export_cols, batches, delimiter=delimiter)
        phase["write"] = time.perf_counter() - t_write
        phase["total"] = time.perf_counter() - t0
        logger.info(
            "Export %s.%s rows=%d format=%s timing_sec=%s",
            schema, table, written, ext,
            {k: round(v, 2) for k, v in phase.items()},
        )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        change_request.update_request(
            cr_id,
            status=change_request.STATUS_APPLIED,
            export_volume_path=path,
            row_count=written,
            filter_snapshot=filter_snapshot,
            applied_at=now,
            change_summary={
                "format": ext,
                "columns": export_cols,
                "filtered": bool(filters),
            },
            user_token=user_token,
        )
        return {
            "change_request_id": cr_id,
            "status": change_request.STATUS_APPLIED,
            "row_count": written,
            "format": ext,
            "export_path": path,
            "download_url": f"/api/exports/{cr_id}/download",
        }
    except Exception as exc:
        logger.error("Export failed for %s: %s", cr_id, exc, exc_info=True)
        change_request.update_request(
            cr_id,
            status=change_request.STATUS_FAILED,
            failure_reason=str(exc),
            errors_json=[{"row": 0, "column": "", "reason": str(exc), "fix": "Retry export."}],
            user_token=user_token,
        )
        raise


def read_export_file(change_request_id: str, user_token: str | None = None) -> tuple[str, str, str]:
    """Return (path, filename, media_type) for a completed export job."""
    rec = change_request.get_request(change_request_id, user_token=user_token)
    if not rec:
        raise ValueError("Export not found.")
    if str(rec.get("request_type")) != "export":
        raise ValueError("Not an export job.")
    if str(rec.get("status")) != change_request.STATUS_APPLIED:
        raise ValueError(f"Export is not ready (status={rec.get('status')}).")

    path = str(rec.get("export_volume_path") or "")
    if not path or not os.path.isfile(path):
        raise ValueError("Export file is missing.")

    ext = Path(path).suffix.lower()
    if ext == ".xlsx":
        return path, f"{rec.get('table_name', 'export')}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if ext == ".csv":
        return path, f"{rec.get('table_name', 'export')}.csv", "text/csv"
    return path, f"{rec.get('table_name', 'export')}.txt", "text/plain"
