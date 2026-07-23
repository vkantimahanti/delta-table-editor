"""Bulk upsert upload: existing PK → update, new PK → insert; overwrite stays separate."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from . import bulk, change_request, config_rules, config_store, db_client, staging_ops
from .bulk_update_ops import (
    VERSION_COL,
    _build_merge_sql,
    _pk_dict,
    _pk_key,
    _recheck_versions,
    _row_dict,
    _validation_error,
    _write_bulk_audit_from_summary,
    load_target_rows_by_pk,
)
from .grid_staging_ops import _apply_inserts

logger = logging.getLogger("delta_editor.bulk_upsert_ops")

CATALOG = db_client.CATALOG
BULK_UPSERT_MAX_ROWS = int(os.environ.get("BULK_UPDATE_MAX_ROWS", "10000"))
CHANGE_SOURCE = "FILE_UPSERT"


def validate_upsert_upload(
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
    """Parse file; update rows with existing PK; insert rows with new PK."""
    t0 = time.perf_counter()
    phase: dict[str, float] = {}

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
        }], mode="upsert")

    phase["parse"] = time.perf_counter() - t0

    if len(df) > BULK_UPSERT_MAX_ROWS:
        return _validation_error([{
            "row": 0, "column": "",
            "reason": f"File has {len(df)} rows; maximum is {BULK_UPSERT_MAX_ROWS}.",
            "fix": "Split the file or narrow your export.",
        }], mode="upsert")

    cr_id = change_request.new_change_request_id()
    change_request.insert_request(
        change_request_id=cr_id,
        request_type="upload",
        mode="upsert",
        schema_name=schema,
        table_name=table,
        submitted_by=submitted_by,
        catalog=catalog,
        user_token=user_token,
    )

    try:
        table_cols = db_client.get_table_columns(catalog, schema, table, user_token=user_token)
        if not table_cols:
            raise ValueError(f"Could not read columns for {catalog}.{schema}.{table}.")

        col_types = db_client.resolve_column_types(
            catalog,
            schema,
            table,
            config_store.get_column_storage_types(schema, table, user_token=user_token),
            user_token=user_token,
        )

        pk_set = config_store.get_pk_cols(schema, table, user_token=user_token)
        if not pk_set:
            raise ValueError("No primary key configured for this table.")
        pk_cols = sorted(pk_set)

        col_meta = config_store.get_columns(schema, table, user_token=user_token)
        mandatory_cols = [
            c["column_name"] for c in col_meta
            if c.get("is_mandatory")
            and str(c["column_name"]).lower() not in db_client.AUDIT_COLUMN_NAMES
        ]
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
            return _validation_error(dup_errors, cr_id=cr_id, mode="upsert")

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
                return _validation_error(bk_errors, cr_id=cr_id, mode="upsert")

        has_version = VERSION_COL in table_cols
        errors: list[dict[str, Any]] = []
        diffs: list[dict[str, Any]] = []
        staging_rows: list[dict[str, str]] = []
        insert_rows: list[dict[str, str]] = []
        columns_changing: set[str] = set()
        rows_with_changes = 0
        rows_unchanged = 0
        insert_count = 0
        readonly_columns_changed: set[str] = set()

        t_load = time.perf_counter()
        target_by_pk = load_target_rows_by_pk(
            catalog, schema, table, table_cols, pk_cols, user_token
        )
        phase["load_target"] = time.perf_counter() - t_load
        target_bk_index = (
            bulk.build_business_key_index(
                target_by_pk, unique_cols, table_cols, pk_cols,
            )
            if unique_cols else {}
        )

        next_auto_pk: int | None = None
        auto_pk_col: str | None = None
        for pk in pk_cols:
            if pk_editable.get(pk, True):
                continue
            auto_pk_col = table_cols.get(pk.lower(), pk)
            break

        def _assign_auto_pk(values: dict[str, str]) -> None:
            nonlocal next_auto_pk
            if not auto_pk_col:
                return
            for pk in pk_cols:
                actual = table_cols.get(pk.lower(), pk)
                current = db_client.cell_to_str(values.get(actual, values.get(pk, "")))
                if current.strip():
                    continue
                if pk_editable.get(pk, True):
                    continue
                if next_auto_pk is None:
                    df_pk = db_client.query(
                        f"SELECT COALESCE(MAX(`{actual}`), 0) + 1 AS next_pk "
                        f"FROM {catalog}.{schema}.{table}",
                        user_token=user_token,
                    )
                    next_auto_pk = int(df_pk.iloc[0]["next_pk"]) if not df_pk.empty else 1
                else:
                    next_auto_pk += 1
                values[pk] = str(next_auto_pk)
                values[actual] = str(next_auto_pk)

        def _pk_values(row: dict[str, str]) -> dict[str, str]:
            out = _pk_dict(row, pk_cols, table_cols)
            return {
                pk: "" if db_client.is_empty_cell_value(val) else str(val).strip()
                for pk, val in out.items()
            }

        t_rows = time.perf_counter()
        for idx, row in df.iterrows():
            row_num = int(idx) + (2 if has_header else 1)
            file_row = _row_dict(row, list(df.columns))
            pk_info = _pk_values(file_row)
            was_new_row = bulk._is_auto_pk_new_row(
                {pk: pk_info.get(pk, "") for pk in pk_cols},
                pk_cols,
                pk_editable,
            )

            if unique_cols:
                norm_key, combo_display = bulk.business_key_from_row(
                    file_row, unique_cols, table_cols,
                )
                if bulk.business_key_complete(norm_key):
                    existing_bk = target_bk_index.get(norm_key)
                    if existing_bk is not None and (
                        was_new_row or _pk_key(pk_info, pk_cols) not in target_by_pk
                    ):
                        errors.append(bulk.business_key_conflict_error(
                            row_num=row_num,
                            unique_cols=unique_cols,
                            combo_display=combo_display,
                            existing_pk=existing_bk,
                        ))
                        continue

            if was_new_row:
                _assign_auto_pk(file_row)
                pk_info = _pk_values(file_row)

            if any(not str(v).strip() for v in pk_info.values()):
                missing_editable = [
                    pk for pk in pk_cols
                    if pk_editable.get(pk, True) and not str(pk_info.get(pk, "") or "").strip()
                ]
                if missing_editable:
                    errors.append({
                        "row": row_num, "pk": pk_info, "column": ", ".join(missing_editable),
                        "reason": "Missing primary key value.",
                        "fix": "Enter the primary key for this row.",
                    })
                    continue
                errors.append({
                    "row": row_num, "pk": pk_info, "column": ", ".join(pk_cols),
                    "reason": "Could not assign primary key for new row.",
                    "fix": (
                        f"Leave {', '.join(pk_cols)} blank for new rows (auto-generated), "
                        "or include it when updating an existing row."
                    ),
                })
                continue

            exists = _pk_key(pk_info, pk_cols) in target_by_pk

            if exists:
                target = target_by_pk[_pk_key(pk_info, pk_cols)]

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
                    str(k): db_client.cell_to_str(v) if not isinstance(v, float) or not pd.isna(v) else ""
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

                if not edits:
                    rows_unchanged += 1
                    continue

                merged = {**original, **{table_cols.get(k.lower(), k): v for k, v in edits.items()}}
                if unique_cols:
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
                    val = db_client.cell_to_str(merged.get(actual, merged.get(col, "")))
                    if not val.strip() or val.strip() in ("None", "nan"):
                        errors.append({
                            "row": row_num, "pk": pk_info, "column": col,
                            "reason": f"'{col}' is required and cannot be empty.",
                            "fix": f"Enter a value for '{col}'.",
                        })

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
                row_out: dict[str, str] = {}
                for lk, actual in table_cols.items():
                    if lk in db_client.AUDIT_COLUMN_NAMES and lk != VERSION_COL:
                        continue
                    row_out[actual] = db_client.cell_to_str(merged.get(actual, merged.get(lk, "")))
                staging_rows.append(row_out)

                for col, new_val in edits.items():
                    actual = table_cols.get(str(col).lower(), col)
                    diffs.append({
                        "row": row_num,
                        "operation": "update",
                        "pk": pk_info,
                        "column": col,
                        "old": db_client.cell_to_str(original.get(actual, "")),
                        "new": new_val,
                    })
                continue

            values = {str(c): file_row.get(str(c), "") for c in df.columns}
            pk_info = _pk_values(values)

            for col in mandatory_cols:
                val = db_client.cell_to_str(values.get(col, ""))
                if not val.strip():
                    errors.append({
                        "row": row_num, "pk": pk_info, "column": col,
                        "reason": f"'{col}' is required for new rows.",
                        "fix": f"Enter a value for '{col}'.",
                    })

            blocking, _ = config_rules.run_all_rules(
                schema, table, {}, values, user_token=user_token
            )
            for b in blocking:
                errors.append({
                    "row": row_num, "pk": pk_info,
                    "column": b.get("column", ""),
                    "reason": b.get("reason", ""),
                    "fix": b.get("fix", ""),
                })

            row_out = {}
            pk_lower = {p.lower() for p in pk_cols}
            for lk, actual in table_cols.items():
                if lk in db_client.AUDIT_COLUMN_NAMES:
                    continue
                raw = values.get(actual, values.get(lk, ""))
                if db_client.is_empty_cell_value(raw) and lk not in pk_lower:
                    continue
                normalized = db_client.normalize_cell_for_storage(
                    raw, col_types.get(lk, "string")
                )
                if normalized is None:
                    continue
                row_out[actual] = normalized
            insert_rows.append(row_out)
            insert_count += 1

            for col, new_val in values.items():
                col_l = str(col).lower()
                if col_l in db_client.AUDIT_COLUMN_NAMES or col_l in pk_lower:
                    continue
                if col_l not in table_cols:
                    continue
                if not str(new_val).strip():
                    continue
                diffs.append({
                    "row": row_num,
                    "operation": "insert",
                    "pk": pk_info,
                    "column": col,
                    "old": "",
                    "new": str(new_val),
                })

        phase["validate_rows"] = time.perf_counter() - t_rows

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
            return _validation_error(errors, cr_id=cr_id, mode="upsert")

        if rows_with_changes == 0 and insert_count == 0:
            if readonly_columns_changed:
                cols = ", ".join(sorted(readonly_columns_changed))
                raise ValueError(
                    "No applicable changes detected. "
                    f"Changes in read-only columns were ignored: {cols}."
                )
            raise ValueError("No changes detected in file compared to current table data.")

        t_persist = time.perf_counter()
        vol_path = bulk.persist_upload_copy(
            cr_id, filename, csv_text=csv_text, file_base64=file_base64,
        )
        phase["persist_file"] = time.perf_counter() - t_persist

        staging_full = ""
        if staging_rows:
            t_stage = time.perf_counter()
            stage_df = pd.DataFrame(staging_rows)
            staging_full = staging_ops.create_staging_from_dataframe(
                cr_id, stage_df, user_token=user_token,
                schema=schema, table_name=table, catalog=catalog,
                submitted_by=submitted_by, operation="update",
            )
            phase["stage_updates"] = time.perf_counter() - t_stage

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        summary = {
            "total_rows": len(df),
            "rows_with_changes": rows_with_changes,
            "rows_unchanged": rows_unchanged,
            "insert_count": insert_count,
            "columns_changing": sorted(columns_changing),
            "sample_diffs": diffs[:10],
            "all_diffs": diffs,
            "insert_rows": insert_rows,
        }
        change_request.update_request(
            cr_id,
            staging_table_name=staging_full or None,
            row_count=len(df),
            status=change_request.STATUS_VALIDATED,
            validated_at=now,
            validation_summary={
                "passed": True,
                "updates": rows_with_changes,
                "inserts": insert_count,
            },
            change_summary=summary,
            source_file_volume_path=vol_path,
            user_token=user_token,
        )
        phase["total"] = time.perf_counter() - t0
        logger.info(
            "Upsert validate %s.%s rows=%d updates=%d inserts=%d timing_sec=%s",
            schema, table, len(df), rows_with_changes, insert_count,
            {k: round(v, 2) for k, v in phase.items()},
        )
        return {
            "change_request_id": cr_id,
            "status": change_request.STATUS_VALIDATED,
            "mode": "upsert",
            "can_apply": True,
            "summary": {
                k: v for k, v in summary.items() if k != "all_diffs"
            },
        }

    except Exception as exc:
        logger.error("Validate upsert failed: %s", exc, exc_info=True)
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
        return _validation_error(err, cr_id=cr_id, mode="upsert")


def apply_upsert_change_request(
    change_request_id: str,
    *,
    applied_by: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    """MERGE updates for existing PKs, INSERT rows for new PKs."""
    t0 = time.perf_counter()
    phase: dict[str, float] = {}

    rec = change_request.get_request(change_request_id, user_token=user_token)
    if not rec:
        raise ValueError("Change request not found.")
    if str(rec.get("mode")) != "upsert":
        raise ValueError("Not an upsert request.")
    if str(rec.get("status")) not in change_request.APPLYABLE_STATUSES:
        raise ValueError(f"Cannot apply request in status '{rec.get('status')}'.")

    catalog = str(rec.get("catalog") or CATALOG)
    schema = str(rec["schema_name"])
    table = str(rec["table_name"])
    summary = json.loads(rec.get("change_summary") or "{}")
    staging_full = str(rec.get("staging_table_name") or "").strip()

    rows_updated = 0
    audit_count = 0

    if staging_full:
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

        update_diffs = [
            d for d in (summary.get("all_diffs") or [])
            if str(d.get("operation") or "update") == "update"
        ]
        audit_count += _write_bulk_audit_from_summary(
            change_request_id,
            schema,
            table,
            {"all_diffs": update_diffs},
            applied_by,
            user_token,
            source=CHANGE_SOURCE,
        )
        phase["audit_updates"] = time.perf_counter() - t0

        staging_cols = staging_ops.business_staging_columns(staging_full, user_token=user_token)
        columns_changing = summary.get("columns_changing") or []
        staging_source = staging_ops.staging_merge_source(staging_full, change_request_id)
        merge_sql = _build_merge_sql(
            catalog, schema, table, staging_source, staging_cols,
            table_cols, pk_cols, has_version, applied_by, columns_changing,
        )
        t_merge = time.perf_counter()
        db_client.execute(merge_sql, user_token=user_token)
        phase["merge"] = time.perf_counter() - t_merge
        rows_updated = int(summary.get("rows_with_changes") or 0)

        staging_ops.drop_staging_table(
            change_request_id, user_token=user_token,
            schema=schema, table_name=table, catalog=catalog,
        )

    insert_rows = summary.get("insert_rows") or []
    insert_count = 0
    if insert_rows:
        t_ins = time.perf_counter()
        insert_count = _apply_inserts(
            catalog, schema, table, insert_rows, applied_by, user_token
        )
        phase["inserts"] = time.perf_counter() - t_ins
        insert_diffs = [
            d for d in (summary.get("all_diffs") or [])
            if str(d.get("operation") or "") == "insert"
        ]
        audit_count += _write_bulk_audit_from_summary(
            change_request_id,
            schema,
            table,
            {"all_diffs": insert_diffs},
            applied_by,
            user_token,
            source=CHANGE_SOURCE,
        )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    change_request.update_request(
        change_request_id,
        status=change_request.STATUS_APPLIED,
        applied_at=now,
        user_token=user_token,
    )

    phase["total"] = time.perf_counter() - t0
    logger.info(
        "Upsert apply %s id=%s updated=%d inserted=%d audit=%d timing_sec=%s",
        table, change_request_id, rows_updated, insert_count, audit_count,
        {k: round(v, 2) for k, v in phase.items()},
    )

    return {
        "change_request_id": change_request_id,
        "status": change_request.STATUS_APPLIED,
        "mode": "upsert",
        "rows_updated": rows_updated,
        "rows_inserted": insert_count,
        "audit_entries": audit_count,
    }
