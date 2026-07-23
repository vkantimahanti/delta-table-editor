"""Bulk file update: staging → validate (all-or-nothing) → MERGE apply."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from . import bulk, change_request, config_rules, config_store, db_client, staging_ops

logger = logging.getLogger("delta_editor.bulk_update_ops")

CATALOG = db_client.CATALOG
BULK_UPDATE_MAX_ROWS = int(os.environ.get("BULK_UPDATE_MAX_ROWS", "10000"))
VERSION_COL = "version"
CHANGE_SOURCE = "FILE_UPDATE"


def _validation_error(
    errors: list[dict[str, Any]],
    *,
    cr_id: str | None = None,
    mode: str = "update",
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


def _fetch_target_row(
    catalog: str,
    schema: str,
    table: str,
    table_cols: dict[str, str],
    pk_cols: list[str],
    pk_vals: dict[str, str],
    user_token: str | None,
) -> dict[str, Any] | None:
    where_parts: list[str] = []
    params: list[str] = []
    for pk in pk_cols:
        col = table_cols.get(pk.lower())
        if not col:
            return None
        where_parts.append(f"`{col}` = ?")
        params.append(pk_vals.get(pk, ""))
    sql = (
        f"SELECT * FROM {catalog}.{schema}.{table} "
        f"WHERE {' AND '.join(where_parts)} LIMIT 1"
    )
    df = db_client.query(sql, params=tuple(params), user_token=user_token)
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def _pk_key(pk_vals: dict[str, str], pk_cols: list[str]) -> tuple[str, ...]:
    """Normalized PK tuple for in-memory lookups."""
    return tuple(str(pk_vals.get(pk, "") or "").strip() for pk in pk_cols)


def _target_row_pk_key(
    row: dict[str, Any],
    pk_cols: list[str],
    table_cols: dict[str, str],
) -> tuple[str, ...]:
    pk_info = {
        pk: db_client.cell_to_str(row.get(table_cols.get(pk.lower(), pk), row.get(pk, "")))
        for pk in pk_cols
    }
    return _pk_key(pk_info, pk_cols)


def load_target_rows_by_pk(
    catalog: str,
    schema: str,
    table: str,
    table_cols: dict[str, str],
    pk_cols: list[str],
    user_token: str | None,
) -> dict[tuple[str, ...], dict[str, Any]]:
    """Load target table rows keyed by primary key (one query instead of per-row lookups)."""
    df = db_client.query(
        f"SELECT * FROM {catalog}.{schema}.{table}",
        user_token=user_token,
    )
    index: dict[tuple[str, ...], dict[str, Any]] = {}
    if df.empty:
        return index
    for _, row in df.iterrows():
        row_dict = row.to_dict()
        key = _target_row_pk_key(row_dict, pk_cols, table_cols)
        index[key] = row_dict
    return index


def validate_update_upload(
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
    """Parse file, load staging, validate all rows. All-or-nothing."""
    try:
        df = bulk.parse_upload_dataframe(
            csv_text=csv_text,
            file_base64=file_base64,
            filename=filename,
            file_format=file_format,
            delimiter=delimiter,
            has_header=has_header,
        )
    except Exception as exc:
        return _validation_error([{
            "row": 0, "column": "",
            "reason": f"Invalid file: {exc}",
            "fix": "Fix file format and retry.",
        }])

    if len(df) > BULK_UPDATE_MAX_ROWS:
        return _validation_error([{
            "row": 0, "column": "",
            "reason": f"File has {len(df)} rows; maximum is {BULK_UPDATE_MAX_ROWS}.",
            "fix": "Split the file or narrow your export.",
        }])

    cr_id = change_request.new_change_request_id()
    change_request.insert_request(
        change_request_id=cr_id,
        request_type="upload",
        mode="update",
        schema_name=schema,
        table_name=table,
        submitted_by=submitted_by,
        catalog=catalog,
        user_token=user_token,
    )

    try:
        vol_path = bulk.persist_upload_copy(
            cr_id,
            filename,
            csv_text=csv_text,
            file_base64=file_base64,
        )
        staging_full = staging_ops.create_staging_from_dataframe(
            cr_id, df, user_token=user_token,
            schema=schema, table_name=table, catalog=catalog,
            submitted_by=submitted_by, operation="update",
        )
        change_request.update_request(
            cr_id,
            staging_table_name=staging_full,
            source_file_volume_path=vol_path,
            row_count=len(df),
            user_token=user_token,
        )

        table_cols = db_client.get_table_columns(catalog, schema, table, user_token=user_token)
        if not table_cols:
            raise ValueError(f"Could not read columns for {catalog}.{schema}.{table}.")

        pk_set = config_store.get_pk_cols(schema, table, user_token=user_token)
        if not pk_set:
            raise ValueError("No primary key configured for this table.")
        pk_cols = sorted(pk_set)

        col_meta = config_store.get_columns(schema, table, user_token=user_token)
        mandatory_cols = [c["column_name"] for c in col_meta if c.get("is_mandatory")]
        editable_lower = {
            str(c["column_name"]).lower() for c in col_meta if c.get("is_editable", True)
        }
        pk_editable = {
            pk: next(
                (bool(c.get("is_editable", True)) for c in col_meta if c["column_name"] == pk),
                True,
            )
            for pk in pk_cols
        }

        file_cols_lower = {str(c).lower() for c in df.columns}
        missing_pk = [pk for pk in pk_cols if pk.lower() not in file_cols_lower]
        if missing_pk:
            raise ValueError(f"File missing primary key column(s): {', '.join(missing_pk)}")

        unknown = [
            str(c) for c in df.columns
            if str(c).lower() not in table_cols
            and str(c).lower() not in db_client.AUDIT_COLUMN_NAMES
        ]
        if unknown:
            raise ValueError(f"Unknown columns in file: {', '.join(unknown)}")

        pk_cols_in_df = [c for c in df.columns if str(c).lower() in {p.lower() for p in pk_cols}]
        dup_errors = bulk.duplicate_pk_validation_errors(
            df, pk_cols_in_df, pk_cols, has_header=has_header, pk_editable=pk_editable,
        )
        if dup_errors:
            staging_ops.drop_staging_table(
                cr_id, user_token=user_token, schema=schema, table_name=table, catalog=catalog,
            )
            change_request.update_request(
                cr_id,
                status=change_request.STATUS_FAILED,
                errors_json=dup_errors,
                failure_reason=f"{len(dup_errors)} duplicate primary key group(s).",
                user_token=user_token,
            )
            return _validation_error(dup_errors, cr_id=cr_id, mode="update")

        unique_cols = config_store.get_upload_unique_columns(schema, table)
        if unique_cols:
            bk_errors = bulk.duplicate_business_key_validation_errors(
                df, unique_cols, table_cols, has_header=has_header,
            )
            if bk_errors:
                staging_ops.drop_staging_table(
                    cr_id, user_token=user_token, schema=schema, table_name=table, catalog=catalog,
                )
                change_request.update_request(
                    cr_id,
                    status=change_request.STATUS_FAILED,
                    errors_json=bk_errors,
                    failure_reason=f"{len(bk_errors)} duplicate business key group(s).",
                    user_token=user_token,
                )
                return _validation_error(bk_errors, cr_id=cr_id, mode="update")

        has_version = VERSION_COL in table_cols
        errors: list[dict[str, Any]] = []
        diffs: list[dict[str, Any]] = []
        rows_with_changes = 0
        rows_unchanged = 0
        columns_changing: set[str] = set()
        readonly_columns_changed: set[str] = set()

        target_bk_index: dict[tuple[str, ...], dict[str, str]] = {}
        if unique_cols:
            target_by_pk = load_target_rows_by_pk(
                catalog, schema, table, table_cols, pk_cols, user_token
            )
            target_bk_index = bulk.build_business_key_index(
                target_by_pk, unique_cols, table_cols, pk_cols,
            )

        for idx, row in df.iterrows():
            row_num = int(idx) + (2 if has_header else 1)
            file_row = _row_dict(row, list(df.columns))
            pk_info = _pk_dict(file_row, pk_cols, table_cols)

            if any(not str(v).strip() for v in pk_info.values()):
                errors.append({
                    "row": row_num, "pk": pk_info, "column": ", ".join(pk_cols),
                    "reason": "Missing primary key value.",
                    "fix": "Fill in all PK columns.",
                })
                continue

            target = _fetch_target_row(
                catalog, schema, table, table_cols, pk_cols, pk_info, user_token
            )
            if target is None:
                errors.append({
                    "row": row_num, "pk": pk_info, "column": "",
                    "reason": "No matching row in table for this primary key.",
                    "fix": "Confirm PK values match existing data.",
                })
                continue

            if has_version:
                vcol = table_cols[VERSION_COL]
                file_has_version = VERSION_COL in file_cols_lower or any(
                    str(c).lower() == VERSION_COL for c in df.columns
                )
                if file_has_version:
                    file_ver = file_row.get(vcol, file_row.get(VERSION_COL, ""))
                    db_ver = target.get(vcol)
                    if not db_client.versions_match(file_ver, db_ver):
                        errors.append({
                            "row": row_num, "pk": pk_info, "column": VERSION_COL,
                            "reason": "Row was modified by another user (version mismatch).",
                            "fix": "Re-export and re-apply changes for this row.",
                        })
                        continue

            original = {
                str(k): "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)
                for k, v in target.items()
            }
            edits: dict[str, str] = {}
            for col in df.columns:
                col_l = str(col).lower()
                if col_l in db_client.AUDIT_COLUMN_NAMES or col_l in {p.lower() for p in pk_cols}:
                    continue
                if col_l not in table_cols:
                    continue
                new_val = file_row.get(str(col), "")
                actual = table_cols[col_l]
                old_val = db_client.cell_to_str(original.get(actual, original.get(col, "")))
                if editable_lower and col_l not in editable_lower:
                    if new_val != old_val:
                        readonly_columns_changed.add(str(col))
                    continue
                if new_val != old_val:
                    edits[str(col)] = new_val

            merged = {**original, **{table_cols.get(k.lower(), k): v for k, v in edits.items()}}
            if unique_cols and edits:
                merged_norm, merged_display = bulk.business_key_from_row(
                    merged, unique_cols, table_cols,
                )
                if bulk.business_key_complete(merged_norm):
                    other_bk = target_bk_index.get(merged_norm)
                    if other_bk and _pk_key(other_bk, pk_cols) != _pk_key(pk_info, pk_cols):
                        errors.append(bulk.business_key_update_conflict_error(
                            row_num=row_num,
                            unique_cols=unique_cols,
                            combo_display=merged_display,
                            existing_pk=other_bk,
                        ))
                        continue

            for col in mandatory_cols:
                actual = table_cols.get(col.lower(), col)
                val = str(merged.get(actual, merged.get(col, "")) or "")
                if not val.strip() or val.strip() in ("None", "nan"):
                    errors.append({
                        "row": row_num, "pk": pk_info, "column": col,
                        "reason": f"'{col}' is required and cannot be empty.",
                        "fix": f"Enter a value for '{col}'.",
                    })

            if edits:
                blocking, _ = config_rules.run_all_rules(
                    schema, table, original, edits, user_token=user_token
                )
                for b in blocking:
                    errors.append({
                        "row": row_num, "pk": pk_info,
                        "column": b.get("column", ""),
                        "reason": b.get("reason", ""),
                        "fix": b.get("fix", ""),
                    })
                rows_with_changes += 1
                columns_changing.update(edits.keys())
                for col, new_val in edits.items():
                    actual = table_cols.get(str(col).lower(), col)
                    diffs.append({
                        "row": row_num, "pk": pk_info, "column": col,
                        "old": str(original.get(actual, "") or ""), "new": new_val,
                    })
            else:
                rows_unchanged += 1

        if errors:
            staging_ops.drop_staging_table(
                cr_id, user_token=user_token, schema=schema, table_name=table, catalog=catalog,
            )
            change_request.update_request(
                cr_id,
                status=change_request.STATUS_FAILED,
                errors_json=errors,
                failure_reason=f"{len(errors)} validation error(s).",
                user_token=user_token,
            )
            return _validation_error(errors, cr_id=cr_id)

        if rows_with_changes == 0:
            if readonly_columns_changed:
                cols = ", ".join(sorted(readonly_columns_changed))
                raise ValueError(
                    "No applicable changes detected. "
                    f"Changes in read-only columns were ignored: {cols}."
                )
            raise ValueError("No changes detected in file compared to current table data.")

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        summary = {
            "total_rows": len(df),
            "rows_with_changes": rows_with_changes,
            "rows_unchanged": rows_unchanged,
            "columns_changing": sorted(columns_changing),
            "sample_diffs": diffs[:10],
            "all_diffs": diffs,
        }
        change_request.update_request(
            cr_id,
            status=change_request.STATUS_VALIDATED,
            validated_at=now,
            validation_summary={"passed": True, "total_rows": len(df)},
            change_summary=summary,
            user_token=user_token,
        )
        return {
            "change_request_id": cr_id,
            "status": change_request.STATUS_VALIDATED,
            "mode": "update",
            "can_apply": True,
            "summary": {
                k: v for k, v in summary.items() if k != "all_diffs"
            },
        }

    except Exception as exc:
        logger.error("Validate upload failed: %s", exc, exc_info=True)
        staging_ops.drop_staging_table(
            cr_id, user_token=user_token, schema=schema, table_name=table, catalog=catalog,
        )
        err = [{
            "row": 0, "column": "",
            "reason": str(exc),
            "fix": "Fix the file and try again.",
        }]
        change_request.update_request(
            cr_id,
            status=change_request.STATUS_FAILED,
            errors_json=err,
            failure_reason=str(exc),
            user_token=user_token,
        )
        return _validation_error(err, cr_id=cr_id)


def apply_update_change_request(
    change_request_id: str,
    *,
    applied_by: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    """MERGE staging into target and write audit rows. All-or-nothing."""
    rec = change_request.get_request(change_request_id, user_token=user_token)
    if not rec:
        raise ValueError("Change request not found.")
    if str(rec.get("status")) not in change_request.APPLYABLE_STATUSES:
        raise ValueError(f"Cannot apply request in status '{rec.get('status')}'.")
    if str(rec.get("mode")) not in ("update", "grid"):
        raise ValueError("Only update/grid mode is supported for merge apply.")

    catalog = str(rec.get("catalog") or CATALOG)
    schema = str(rec["schema_name"])
    table = str(rec["table_name"])
    staging_full = str(rec.get("staging_table_name") or "")
    if not staging_full:
        raise ValueError("Staging table missing for this request.")

    summary = json.loads(rec.get("change_summary") or "{}")
    table_cols = db_client.get_table_columns(catalog, schema, table, user_token=user_token)
    pk_cols = sorted(config_store.get_pk_cols(schema, table, user_token=user_token))
    has_version = VERSION_COL in table_cols

    version_errors = _recheck_versions(
        catalog, schema, table, staging_full, change_request_id,
        table_cols, pk_cols, has_version, user_token
    )
    if version_errors:
        change_request.update_request(
            change_request_id,
            status=change_request.STATUS_FAILED,
            errors_json=version_errors,
            failure_reason="Concurrency check failed at apply time.",
            user_token=user_token,
        )
        raise ValueError("Apply blocked: data changed since validation. Re-export and retry.")

    audit_count = _write_bulk_audit_from_summary(
        change_request_id, schema, table, summary, applied_by, user_token
    )

    staging_cols = staging_ops.business_staging_columns(staging_full, user_token=user_token)
    columns_changing = summary.get("columns_changing") or []
    staging_source = staging_ops.staging_merge_source(staging_full, change_request_id)
    merge_sql = _build_merge_sql(
        catalog, schema, table, staging_source, staging_cols,
        table_cols, pk_cols, has_version, applied_by, columns_changing,
    )
    db_client.execute(merge_sql, user_token=user_token)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    change_request.update_request(
        change_request_id,
        status=change_request.STATUS_APPLIED,
        applied_at=now,
        user_token=user_token,
    )
    staging_ops.drop_staging_table(
        change_request_id, user_token=user_token,
        schema=schema, table_name=table, catalog=catalog,
    )

    return {
        "change_request_id": change_request_id,
        "status": change_request.STATUS_APPLIED,
        "rows_updated": summary.get("rows_with_changes", 0),
        "audit_entries": audit_count,
    }


def _build_merge_sql(
    catalog: str,
    schema: str,
    table: str,
    staging_full: str,
    staging_cols: list[str],
    table_cols: dict[str, str],
    pk_cols: list[str],
    has_version: bool,
    user: str,
    columns_changing: list[str],
) -> str:
    target = f"{catalog}.{schema}.{table}"
    on_parts = [
        f"t.`{table_cols[pk.lower()]}` = s.`{table_cols[pk.lower()]}`"
        for pk in pk_cols
    ]
    if has_version:
        vcol = table_cols[VERSION_COL]
        on_parts.append(f"t.`{vcol}` = s.`{vcol}`")

    staging_lower = {c.lower(): c for c in staging_cols}
    set_parts: list[str] = []
    for lk, actual in table_cols.items():
        if lk in {p.lower() for p in pk_cols}:
            continue
        if lk in db_client.AUDIT_COLUMN_NAMES and lk != VERSION_COL:
            continue
        if actual not in staging_cols and lk not in staging_lower:
            continue
        s_col = staging_lower.get(lk, actual)
        if lk == VERSION_COL:
            set_parts.append(f"t.`{actual}` = t.`{actual}` + 1")
        else:
            set_parts.append(f"t.`{actual}` = s.`{s_col}`")

    safe_user = str(user).replace("'", "''")
    if "updated_by" in table_cols:
        set_parts.append(f"t.`{table_cols['updated_by']}` = '{safe_user}'")
    elif "modified_by" in table_cols:
        set_parts.append(f"t.`{table_cols['modified_by']}` = '{safe_user}'")
    if "updated_at" in table_cols:
        set_parts.append(f"t.`{table_cols['updated_at']}` = current_timestamp()")
    elif "modified_date" in table_cols:
        set_parts.append(f"t.`{table_cols['modified_date']}` = current_timestamp()")

    change_checks = [
        f"s.`{table_cols[c.lower()]}` IS DISTINCT FROM t.`{table_cols[c.lower()]}`"
        for c in columns_changing
        if c.lower() in table_cols
    ]
    when_clause = f"WHEN MATCHED AND ({' OR '.join(change_checks)})" if change_checks else "WHEN MATCHED"

    return (
        f"MERGE INTO {target} AS t USING {staging_full} AS s "
        f"ON {' AND '.join(on_parts)} "
        f"{when_clause} THEN UPDATE SET {', '.join(set_parts)}"
    )


def _recheck_versions(
    catalog: str,
    schema: str,
    table: str,
    staging_full: str,
    change_request_id: str,
    table_cols: dict[str, str],
    pk_cols: list[str],
    has_version: bool,
    user_token: str | None,
) -> list[dict[str, Any]]:
    if not has_version:
        return []
    vcol = table_cols[VERSION_COL]
    staging_source = staging_ops.staging_merge_source(staging_full, change_request_id)
    on_parts = [
        f"t.`{table_cols[pk.lower()]}` = s.`{table_cols[pk.lower()]}`"
        for pk in pk_cols
    ]
    sql = (
        f"SELECT 1 FROM {staging_source} s "
        f"INNER JOIN {catalog}.{schema}.{table} t ON {' AND '.join(on_parts)} "
        f"WHERE COALESCE(CAST(s.`{vcol}` AS BIGINT), -1) "
        f"IS DISTINCT FROM COALESCE(CAST(t.`{vcol}` AS BIGINT), -1)"
    )
    df = db_client.query(sql, user_token=user_token)
    if df.empty:
        return []
    return [{
        "row": 0, "column": VERSION_COL,
        "reason": "Version mismatch at apply time.",
        "fix": "Re-export, re-validate, and apply again.",
    }]


def _write_bulk_audit_from_summary(
    change_request_id: str,
    schema: str,
    table: str,
    summary: dict[str, Any],
    user: str,
    user_token: str | None,
    *,
    source: str = CHANGE_SOURCE,
) -> int:
    entries = []
    for d in summary.get("all_diffs") or []:
        col = str(d.get("column") or "")
        if col.lower() in db_client.AUDIT_COLUMN_NAMES:
            continue
        entries.append({
            "changed_by": user,
            "table_schema": schema,
            "table_name": table,
            "record_key": d.get("pk") or {},
            "column_name": col,
            "old_value": d.get("old"),
            "new_value": d.get("new"),
            "source": source,
            "change_request_id": change_request_id,
        })
    return db_client.log_audit_batch(entries, user_token=user_token)
