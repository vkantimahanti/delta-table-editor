"""Bulk append/overwrite: staging → validate → INSERT / TRUNCATE+INSERT."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from . import bulk, change_request, config_rules, config_store, db_client, staging_ops

logger = logging.getLogger("delta_editor.bulk_upload_ops")

CATALOG = db_client.CATALOG
BULK_UPDATE_MAX_ROWS = int(os.environ.get("BULK_UPDATE_MAX_ROWS", "10000"))
CHANGE_SOURCE_APPEND = "FILE_APPEND"
CHANGE_SOURCE_OVERWRITE = "FILE_OVERWRITE"


def _validation_error(
    errors: list[dict[str, Any]],
    *,
    mode: str,
    cr_id: str | None = None,
) -> dict[str, Any]:
    return {
        "change_request_id": cr_id,
        "status": change_request.STATUS_FAILED,
        "mode": mode,
        "can_apply": False,
        "errors": errors,
        "error_count": len(errors),
    }


def _row_dict(row: pd.Series, columns: list[str]) -> dict[str, str]:
    return {str(c): "" if pd.isna(row[c]) else str(row[c]).strip() for c in columns}


def _pk_dict(row: dict[str, str], pk_cols: list[str], table_cols: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pk in pk_cols:
        actual = table_cols.get(pk.lower(), pk)
        out[pk] = row.get(actual, row.get(pk, row.get(pk.lower(), "")))
    return out


def _pk_exists(
    catalog: str,
    schema: str,
    table: str,
    table_cols: dict[str, str],
    pk_cols: list[str],
    pk_vals: dict[str, str],
    user_token: str | None,
) -> bool:
    where_parts: list[str] = []
    params: list[str] = []
    for pk in pk_cols:
        col = table_cols.get(pk.lower())
        if not col:
            return False
        where_parts.append(f"`{col}` = ?")
        params.append(pk_vals.get(pk, ""))
    sql = (
        f"SELECT 1 FROM {catalog}.{schema}.{table} "
        f"WHERE {' AND '.join(where_parts)} LIMIT 1"
    )
    df = db_client.query(sql, params=tuple(params), user_token=user_token)
    return not df.empty


def _stage_file(
    *,
    catalog: str,
    schema: str,
    table: str,
    mode: str,
    csv_text: str = "",
    file_base64: str = "",
    file_format: str = "",
    delimiter: str,
    has_header: bool,
    filename: str,
    submitted_by: str,
    user_token: str | None,
) -> tuple[str, pd.DataFrame, str]:
    df = bulk.parse_upload_dataframe(
        csv_text=csv_text,
        file_base64=file_base64,
        filename=filename,
        file_format=file_format,
        delimiter=delimiter,
        has_header=has_header,
    )
    if len(df) > BULK_UPDATE_MAX_ROWS:
        raise ValueError(
            f"File has {len(df)} rows; maximum is {BULK_UPDATE_MAX_ROWS}."
        )
    if df.empty:
        raise ValueError("Upload file has no data rows.")

    cr_id = change_request.new_change_request_id()
    change_request.insert_request(
        change_request_id=cr_id,
        request_type="upload",
        mode=mode,
        schema_name=schema,
        table_name=table,
        submitted_by=submitted_by,
        catalog=catalog,
        user_token=user_token,
    )
    vol_path = bulk.persist_upload_copy(
        cr_id,
        filename,
        csv_text=csv_text,
        file_base64=file_base64,
    )
    staging_full = staging_ops.create_staging_from_dataframe(
        cr_id, df, user_token=user_token,
        schema=schema, table_name=table, catalog=catalog,
        submitted_by=submitted_by, operation=mode,
    )
    change_request.update_request(
        cr_id,
        staging_table_name=staging_full,
        source_file_volume_path=vol_path,
        row_count=len(df),
        user_token=user_token,
    )
    return cr_id, df, staging_full


def _validate_rows(
    *,
    catalog: str,
    schema: str,
    table: str,
    mode: str,
    df: pd.DataFrame,
    has_header: bool,
    table_exists: bool,
    user_token: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    table_cols = db_client.get_table_columns(catalog, schema, table, user_token=user_token) if table_exists else {}
    pk_set = config_store.get_pk_cols(schema, table, user_token=user_token) if table_exists else set()
    pk_cols = sorted(pk_set)
    col_meta = config_store.get_columns(schema, table, user_token=user_token) if table_exists else []
    mandatory_cols = [c["column_name"] for c in col_meta if c.get("is_mandatory")]
    pk_editable = {
        pk: next(
            (bool(c.get("is_editable", True)) for c in col_meta if c["column_name"] == pk),
            True,
        )
        for pk in pk_cols
    }

    if table_exists and table_cols:
        file_cols_lower = {str(c).lower() for c in df.columns}
        unknown = [
            str(c) for c in df.columns
            if str(c).lower() not in table_cols
            and str(c).lower() not in db_client.AUDIT_COLUMN_NAMES
        ]
        if unknown:
            raise ValueError(f"Unknown columns in file: {', '.join(unknown)}")

        if pk_cols:
            missing_pk = [pk for pk in pk_cols if pk.lower() not in file_cols_lower]
            if missing_pk:
                raise ValueError(f"File missing primary key column(s): {', '.join(missing_pk)}")
            pk_cols_in_df = [c for c in df.columns if str(c).lower() in {p.lower() for p in pk_cols}]
            dup_errors = bulk.duplicate_pk_validation_errors(
                df, pk_cols_in_df, pk_cols, has_header=has_header, pk_editable=pk_editable,
            )
            if dup_errors:
                return dup_errors, {"duplicate_pk_groups": len(dup_errors)}

            unique_cols = config_store.get_upload_unique_columns(schema, table)
            if unique_cols:
                bk_errors = bulk.duplicate_business_key_validation_errors(
                    df, unique_cols, table_cols, has_header=has_header,
                )
                if bk_errors:
                    return bk_errors, {"duplicate_business_key_groups": len(bk_errors)}

    errors: list[dict[str, Any]] = []
    sample_rows: list[dict[str, Any]] = []

    for idx, row in df.iterrows():
        row_num = int(idx) + (2 if has_header else 1)
        file_row = _row_dict(row, list(df.columns))
        pk_info = _pk_dict(file_row, pk_cols, table_cols) if pk_cols else {}

        if pk_cols and any(not str(v).strip() for v in pk_info.values()):
            errors.append({
                "row": row_num, "pk": pk_info, "column": ", ".join(pk_cols),
                "reason": "Missing primary key value.",
                "fix": "Fill in all PK columns.",
            })
            continue

        if mode == "append" and table_exists and pk_cols:
            if _pk_exists(catalog, schema, table, table_cols, pk_cols, pk_info, user_token):
                errors.append({
                    "row": row_num, "pk": pk_info, "column": "",
                    "reason": "Primary key already exists in table (append only inserts new rows).",
                    "fix": "Use update mode to change existing rows, or use new PK values.",
                })
                continue

        edits = {
            str(c): file_row.get(str(c), "")
            for c in df.columns
            if str(c).lower() not in db_client.AUDIT_COLUMN_NAMES
            and (not table_cols or str(c).lower() in table_cols)
        }
        for col in mandatory_cols:
            actual = table_cols.get(col.lower(), col) if table_cols else col
            val = str(edits.get(actual, edits.get(col, "")) or "")
            if not val.strip() or val.strip() in ("None", "nan"):
                errors.append({
                    "row": row_num, "pk": pk_info, "column": col,
                    "reason": f"'{col}' is required and cannot be empty.",
                    "fix": f"Enter a value for '{col}'.",
                })

        if table_exists:
            blocking, _ = config_rules.run_all_rules(
                schema, table, {}, edits, user_token=user_token
            )
            for b in blocking:
                errors.append({
                    "row": row_num, "pk": pk_info,
                    "column": b.get("column", ""),
                    "reason": b.get("reason", ""),
                    "fix": b.get("fix", ""),
                })

        if len(sample_rows) < 5:
            sample_rows.append({"row": row_num, "pk": pk_info, "values": edits})

    existing_count = 0
    if table_exists:
        count_df = db_client.query(
            f"SELECT COUNT(*) AS total FROM {catalog}.{schema}.{table}",
            user_token=user_token,
        )
        existing_count = int(count_df.iloc[0]["total"]) if not count_df.empty else 0

    summary = {
        "total_rows": len(df),
        "rows_to_insert": len(df),
        "existing_row_count": existing_count,
        "mode": mode,
        "sample_rows": sample_rows,
        "will_truncate": mode == "overwrite" and table_exists,
    }
    return errors, summary


def validate_append_upload(
    *,
    catalog: str,
    schema: str,
    table: str,
    csv_text: str = "",
    file_base64: str = "",
    file_format: str = "",
    delimiter: str,
    has_header: bool,
    filename: str,
    submitted_by: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    cr_id: str | None = None
    try:
        table_exists = db_client.table_exists(catalog, schema, table, user_token=user_token)
        cr_id, df, _staging = _stage_file(
            catalog=catalog, schema=schema, table=table, mode="append",
            csv_text=csv_text, file_base64=file_base64, file_format=file_format,
            delimiter=delimiter, has_header=has_header,
            filename=filename, submitted_by=submitted_by, user_token=user_token,
        )
        errors, summary = _validate_rows(
            catalog=catalog, schema=schema, table=table, mode="append",
            df=df, has_header=has_header, table_exists=table_exists,
            user_token=user_token,
        )
        if errors:
            staging_ops.drop_staging_table(
                cr_id, user_token=user_token, schema=schema, table_name=table, catalog=catalog,
            )
            change_request.update_request(
                cr_id, status=change_request.STATUS_FAILED,
                errors_json=errors, failure_reason=f"{len(errors)} validation error(s).",
                user_token=user_token,
            )
            return _validation_error(errors, mode="append", cr_id=cr_id)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        change_request.update_request(
            cr_id, status=change_request.STATUS_VALIDATED, validated_at=now,
            validation_summary={"passed": True, "total_rows": len(df)},
            change_summary=summary, user_token=user_token,
        )
        return {
            "change_request_id": cr_id,
            "status": change_request.STATUS_VALIDATED,
            "mode": "append",
            "can_apply": True,
            "summary": summary,
        }
    except Exception as exc:
        logger.error("Validate append failed: %s", exc, exc_info=True)
        if cr_id:
            staging_ops.drop_staging_table(
                cr_id, user_token=user_token, schema=schema, table_name=table, catalog=catalog,
            )
            err = [{"row": 0, "column": "", "reason": str(exc), "fix": "Fix the file and try again."}]
            change_request.update_request(
                cr_id, status=change_request.STATUS_FAILED, errors_json=err,
                failure_reason=str(exc), user_token=user_token,
            )
            return _validation_error(err, mode="append", cr_id=cr_id)
        return _validation_error([{
            "row": 0, "column": "", "reason": str(exc), "fix": "Fix the file and try again.",
        }], mode="append")


def validate_overwrite_upload(
    *,
    catalog: str,
    schema: str,
    table: str,
    csv_text: str = "",
    file_base64: str = "",
    file_format: str = "",
    delimiter: str,
    has_header: bool,
    filename: str,
    submitted_by: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    cr_id: str | None = None
    try:
        policy = config_store.get_upload_policy(schema, table, user_token=user_token)
        if not policy.get("allow_overwrite", True):
            raise ValueError("Overwrite is disabled for this table in registry config.")

        table_exists = db_client.table_exists(catalog, schema, table, user_token=user_token)
        cr_id, df, _staging = _stage_file(
            catalog=catalog, schema=schema, table=table, mode="overwrite",
            csv_text=csv_text, file_base64=file_base64, file_format=file_format,
            delimiter=delimiter, has_header=has_header,
            filename=filename, submitted_by=submitted_by, user_token=user_token,
        )
        errors, summary = _validate_rows(
            catalog=catalog, schema=schema, table=table, mode="overwrite",
            df=df, has_header=has_header, table_exists=table_exists,
            user_token=user_token,
        )
        if errors:
            staging_ops.drop_staging_table(
                cr_id, user_token=user_token, schema=schema, table_name=table, catalog=catalog,
            )
            change_request.update_request(
                cr_id, status=change_request.STATUS_FAILED,
                errors_json=errors, failure_reason=f"{len(errors)} validation error(s).",
                user_token=user_token,
            )
            return _validation_error(errors, mode="overwrite", cr_id=cr_id)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        change_request.update_request(
            cr_id, status=change_request.STATUS_VALIDATED, validated_at=now,
            validation_summary={"passed": True, "total_rows": len(df)},
            change_summary=summary, user_token=user_token,
        )
        return {
            "change_request_id": cr_id,
            "status": change_request.STATUS_VALIDATED,
            "mode": "overwrite",
            "can_apply": True,
            "summary": summary,
        }
    except Exception as exc:
        logger.error("Validate overwrite failed: %s", exc, exc_info=True)
        if cr_id:
            staging_ops.drop_staging_table(
                cr_id, user_token=user_token, schema=schema, table_name=table, catalog=catalog,
            )
            err = [{"row": 0, "column": "", "reason": str(exc), "fix": "Fix the file and try again."}]
            change_request.update_request(
                cr_id, status=change_request.STATUS_FAILED, errors_json=err,
                failure_reason=str(exc), user_token=user_token,
            )
            return _validation_error(err, mode="overwrite", cr_id=cr_id)
        return _validation_error([{
            "row": 0, "column": "", "reason": str(exc), "fix": "Fix the file and try again.",
        }], mode="overwrite")


def _build_insert_select_sql(
    *,
    catalog: str,
    schema: str,
    table: str,
    staging_source: str,
    staging_cols: list[str],
    table_cols: dict[str, str],
    applied_by: str,
) -> str:
    target = f"{catalog}.{schema}.{table}"
    staging_lower = {c.lower(): c for c in staging_cols}
    insert_cols: list[str] = []
    select_exprs: list[str] = []

    for lk, actual in table_cols.items():
        if lk in db_client.AUDIT_COLUMN_NAMES:
            continue
        s_col = staging_lower.get(lk)
        if s_col:
            insert_cols.append(f"`{actual}`")
            select_exprs.append(f"s.`{s_col}`")

    safe_user = str(applied_by).replace("'", "''")
    if "inserted_by" in table_cols and "inserted_by" not in {c.lower() for c in insert_cols}:
        insert_cols.append(f"`{table_cols['inserted_by']}`")
        select_exprs.append(f"'{safe_user}'")
    elif "created_by" in table_cols and "created_by" not in {c.lower() for c in insert_cols}:
        insert_cols.append(f"`{table_cols['created_by']}`")
        select_exprs.append(f"'{safe_user}'")

    if "updated_by" in table_cols:
        insert_cols.append(f"`{table_cols['updated_by']}`")
        select_exprs.append(f"'{safe_user}'")
    elif "modified_by" in table_cols:
        insert_cols.append(f"`{table_cols['modified_by']}`")
        select_exprs.append(f"'{safe_user}'")

    if "inserted_at" in table_cols:
        insert_cols.append(f"`{table_cols['inserted_at']}`")
        select_exprs.append("current_timestamp()")
    elif "created_at" in table_cols:
        insert_cols.append(f"`{table_cols['created_at']}`")
        select_exprs.append("current_timestamp()")

    if "updated_at" in table_cols:
        insert_cols.append(f"`{table_cols['updated_at']}`")
        select_exprs.append("current_timestamp()")
    elif "modified_date" in table_cols:
        insert_cols.append(f"`{table_cols['modified_date']}`")
        select_exprs.append("current_timestamp()")

    if "version" in table_cols:
        insert_cols.append(f"`{table_cols['version']}`")
        select_exprs.append("0")

    return (
        f"INSERT INTO {target} ({', '.join(insert_cols)}) "
        f"SELECT {', '.join(select_exprs)} FROM {staging_source} s"
    )


def _ensure_table_from_staging(
    catalog: str,
    schema: str,
    table: str,
    staging_full: str,
    user_token: str | None,
) -> dict[str, str]:
    full_name = f"{catalog}.{schema}.{table}"
    if db_client.table_exists(catalog, schema, table, user_token=user_token):
        cols = db_client.get_table_columns(catalog, schema, table, user_token=user_token)
        if cols:
            return cols

    describe = db_client.query(f"DESCRIBE TABLE {staging_full}", user_token=user_token)
    name_col = "col_name" if "col_name" in describe.columns else describe.columns[0]
    meta = set(staging_ops.STAGE_META_COLS)
    col_defs = ", ".join(
        f"`{str(r[name_col])}` STRING" for _, r in describe.iterrows()
        if str(r[name_col]) and not str(r[name_col]).startswith("#")
        and str(r[name_col]) not in meta
    )
    db_client.execute(
        f"CREATE TABLE {full_name} ({col_defs}) USING DELTA",
        user_token=user_token,
    )
    return db_client.get_table_columns(catalog, schema, table, user_token=user_token) or {}


def apply_append_change_request(
    change_request_id: str,
    *,
    applied_by: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    rec = change_request.get_request(change_request_id, user_token=user_token)
    if not rec:
        raise ValueError("Change request not found.")
    if str(rec.get("mode")) != "append":
        raise ValueError("Not an append request.")
    if str(rec.get("status")) not in change_request.APPLYABLE_STATUSES:
        raise ValueError(f"Cannot apply request in status '{rec.get('status')}'.")

    catalog = str(rec.get("catalog") or CATALOG)
    schema = str(rec["schema_name"])
    table = str(rec["table_name"])
    staging_full = str(rec.get("staging_table_name") or "")
    if not staging_full:
        raise ValueError("Staging table missing for this request.")

    summary = json.loads(rec.get("change_summary") or "{}")
    table_cols = _ensure_table_from_staging(catalog, schema, table, staging_full, user_token)
    staging_cols = staging_ops.business_staging_columns(staging_full, user_token=user_token)
    staging_source = staging_ops.staging_merge_source(staging_full, change_request_id)
    insert_sql = _build_insert_select_sql(
        catalog=catalog, schema=schema, table=table,
        staging_source=staging_source, staging_cols=staging_cols,
        table_cols=table_cols, applied_by=applied_by,
    )
    db_client.execute(insert_sql, user_token=user_token)

    file_columns = [
        table_cols.get(str(c).lower(), str(c))
        for c in staging_cols
        if str(c).lower() in table_cols
    ]
    row_count = int(summary.get("rows_to_insert") or rec.get("row_count") or 0)
    db_client.log_upload_summary(
        applied_by, schema, table, "append", row_count,
        filename=str(rec.get("source_file_volume_path") or "").split("/")[-1] or "upload.csv",
        column_names=file_columns,
        change_request_id=change_request_id,
        user_token=user_token,
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    change_request.update_request(
        change_request_id, status=change_request.STATUS_APPLIED,
        applied_at=now, user_token=user_token,
    )
    staging_ops.drop_staging_table(
        change_request_id, user_token=user_token,
        schema=schema, table_name=table, catalog=catalog,
    )
    return {
        "change_request_id": change_request_id,
        "status": change_request.STATUS_APPLIED,
        "rows_inserted": row_count,
        "mode": "append",
    }


def apply_overwrite_change_request(
    change_request_id: str,
    *,
    applied_by: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    rec = change_request.get_request(change_request_id, user_token=user_token)
    if not rec:
        raise ValueError("Change request not found.")
    if str(rec.get("mode")) != "overwrite":
        raise ValueError("Not an overwrite request.")
    if str(rec.get("status")) not in change_request.APPLYABLE_STATUSES:
        raise ValueError(f"Cannot apply request in status '{rec.get('status')}'.")

    catalog = str(rec.get("catalog") or CATALOG)
    schema = str(rec["schema_name"])
    table = str(rec["table_name"])
    staging_full = str(rec.get("staging_table_name") or "")
    if not staging_full:
        raise ValueError("Staging table missing for this request.")

    summary = json.loads(rec.get("change_summary") or "{}")
    target = f"{catalog}.{schema}.{table}"
    table_cols = _ensure_table_from_staging(catalog, schema, table, staging_full, user_token)
    staging_cols = staging_ops.business_staging_columns(staging_full, user_token=user_token)
    staging_source = staging_ops.staging_merge_source(staging_full, change_request_id)

    if db_client.table_exists(catalog, schema, table, user_token=user_token):
        db_client.execute(f"TRUNCATE TABLE {target}", user_token=user_token)

    insert_sql = _build_insert_select_sql(
        catalog=catalog, schema=schema, table=table,
        staging_source=staging_source, staging_cols=staging_cols,
        table_cols=table_cols, applied_by=applied_by,
    )
    db_client.execute(insert_sql, user_token=user_token)

    file_columns = [
        table_cols.get(str(c).lower(), str(c))
        for c in staging_cols
        if str(c).lower() in table_cols
    ]
    row_count = int(summary.get("rows_to_insert") or rec.get("row_count") or 0)
    db_client.log_upload_summary(
        applied_by, schema, table, "overwrite", row_count,
        filename=str(rec.get("source_file_volume_path") or "").split("/")[-1] or "upload.csv",
        column_names=file_columns,
        change_request_id=change_request_id,
        user_token=user_token,
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    change_request.update_request(
        change_request_id, status=change_request.STATUS_APPLIED,
        applied_at=now, user_token=user_token,
    )
    staging_ops.drop_staging_table(
        change_request_id, user_token=user_token,
        schema=schema, table_name=table, catalog=catalog,
    )
    return {
        "change_request_id": change_request_id,
        "status": change_request.STATUS_APPLIED,
        "rows_inserted": row_count,
        "mode": "overwrite",
        "truncated": bool(summary.get("will_truncate")),
    }
