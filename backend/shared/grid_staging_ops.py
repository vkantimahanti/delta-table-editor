"""Grid edit staging: validate → stage → optional approval → merge apply."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from . import (
    bulk_update_ops,
    change_request,
    config_rules,
    config_store,
    db_client,
    revision_ops,
    staging_ops,
)
from . import bulk
from .bulk_update_ops import (
    VERSION_COL,
    _fetch_target_row,
    _pk_dict,
    _pk_key,
    _row_dict,
    load_target_rows_by_pk,
    _validation_error,
    _write_bulk_audit_from_summary,
)

logger = logging.getLogger("delta_editor.grid_staging_ops")

CATALOG = db_client.CATALOG
GRID_MAX_ROWS = int(os.environ.get("GRID_EDIT_MAX_ROWS", "500"))
CHANGE_SOURCE = "GRID_EDIT"


def validate_grid_edits(
    *,
    catalog: str,
    schema: str,
    table: str,
    updates: list[dict[str, Any]],
    inserts: list[dict[str, Any]],
    submitted_by: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    """Stage grid edits, validate all-or-nothing, compute diffs for approver UI."""
    total = len(updates) + len(inserts)
    if total == 0:
        return _validation_error([{
            "row": 0, "column": "",
            "reason": "No changes to stage.",
            "fix": "Edit at least one cell before saving.",
        }])
    if total > GRID_MAX_ROWS:
        return _validation_error([{
            "row": 0, "column": "",
            "reason": f"Too many rows ({total}); maximum is {GRID_MAX_ROWS}.",
            "fix": "Save in smaller batches.",
        }])

    cr_id = change_request.new_change_request_id()
    change_request.insert_request(
        change_request_id=cr_id,
        request_type="grid_edit",
        mode="grid",
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

        unique_cols = config_store.get_upload_unique_columns(schema, table)
        target_bk_index: dict[tuple[str, ...], dict[str, str]] = {}
        if unique_cols:
            target_by_pk = load_target_rows_by_pk(
                catalog, schema, table, table_cols, pk_cols, user_token
            )
            target_bk_index = bulk.build_business_key_index(
                target_by_pk, unique_cols, table_cols, pk_cols,
            )

        next_auto_pk: int | None = None

        def _assign_auto_pk(values: dict[str, str]) -> None:
            nonlocal next_auto_pk
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

        def _pk_info(original: dict[str, str], edits_in: dict[str, str]) -> dict[str, str]:
            return {
                pk: db_client.cell_to_str(original.get(pk, edits_in.get(pk, "")))
                for pk in pk_cols
            }

        reclassified_inserts: list[dict[str, Any]] = []
        actual_updates: list[dict[str, Any]] = []
        for item in updates:
            original = {
                str(k): db_client.cell_to_str(v)
                for k, v in (item.get("original") or {}).items()
            }
            edits_in = {
                str(k): db_client.cell_to_str(v)
                for k, v in (item.get("edits") or {}).items()
            }
            if bulk._is_auto_pk_new_row(_pk_info(original, edits_in), pk_cols, pk_editable):
                reclassified_inserts.append({"values": {**original, **edits_in}})
                continue
            actual_updates.append(item)
        updates = actual_updates
        inserts = reclassified_inserts + list(inserts)

        errors: list[dict[str, Any]] = []
        diffs: list[dict[str, Any]] = []
        staging_rows: list[dict[str, str]] = []
        insert_rows: list[dict[str, str]] = []
        columns_changing: set[str] = set()
        rows_with_changes = 0

        for row_num, item in enumerate(updates, start=1):
            original = {str(k): db_client.cell_to_str(v) for k, v in (item.get("original") or {}).items()}
            edits_in = {str(k): db_client.cell_to_str(v) for k, v in (item.get("edits") or {}).items()}
            pk_info = _pk_info(original, edits_in)

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
                else:
                    errors.append({
                        "row": row_num, "pk": pk_info, "column": ", ".join(pk_cols),
                        "reason": "Missing primary key value.",
                        "fix": "Refresh the grid and use Add row for new records.",
                    })
                continue

            target = _fetch_target_row(
                catalog, schema, table, table_cols, pk_cols, pk_info, user_token
            )
            if target is None:
                errors.append({
                    "row": row_num, "pk": pk_info, "column": "",
                    "reason": "No matching row in table for this primary key.",
                    "fix": "Refresh the grid and try again.",
                })
                continue

            # Version is taken from fresh DB row (target) for staging/merge.
            # Concurrency is enforced at apply via MERGE ... ON t.version = s.version.

            orig_norm = {
                str(k): db_client.cell_to_str(v) for k, v in target.items()
            }
            edits: dict[str, str] = {}
            for col, new_val in edits_in.items():
                col_l = str(col).lower()
                if col_l in db_client.AUDIT_COLUMN_NAMES or col_l in {p.lower() for p in pk_cols}:
                    continue
                if col_l not in table_cols:
                    continue
                if editable_lower and col_l not in editable_lower:
                    continue
                actual = table_cols[col_l]
                old_val = db_client.cell_to_str(orig_norm.get(actual, orig_norm.get(col, "")))
                if str(new_val) != old_val:
                    edits[col] = str(new_val)

            if not edits:
                continue

            merged = {**orig_norm, **{table_cols.get(k.lower(), k): v for k, v in edits.items()}}
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
                schema, table, orig_norm, edits, user_token=user_token
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

            before_json = json.dumps(
                {k: orig_norm.get(table_cols.get(k.lower(), k), "") for k in edits},
                default=str,
            )
            after_json = json.dumps(edits, default=str)
            for col, new_val in edits.items():
                actual = table_cols.get(str(col).lower(), col)
                diffs.append({
                    "row": row_num,
                    "operation": "update",
                    "pk": pk_info,
                    "column": col,
                    "old": db_client.cell_to_str(orig_norm.get(actual, "")),
                    "new": new_val,
                    "before_row_json": before_json,
                    "after_row_json": after_json,
                    "row_version_before": orig_norm.get(table_cols.get(VERSION_COL, VERSION_COL)),
                })

        insert_start = len(updates) + 1
        for i, item in enumerate(inserts):
            row_num = insert_start + i
            values = {str(k): db_client.cell_to_str(v) for k, v in (item.get("values") or {}).items()}
            _assign_auto_pk(values)
            pk_info = {
                pk: db_client.cell_to_str(
                    values.get(pk, values.get(table_cols.get(pk.lower(), pk), ""))
                )
                for pk in pk_cols
            }

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
                else:
                    errors.append({
                        "row": row_num, "pk": pk_info, "column": ", ".join(pk_cols),
                        "reason": "Could not assign primary key for new row.",
                        "fix": (
                            f"Leave {', '.join(pk_cols)} blank for new rows (auto-generated), "
                            "or refresh and try again."
                        ),
                    })
                continue

            if unique_cols:
                norm_key, combo_display = bulk.business_key_from_row(
                    values, unique_cols, table_cols,
                )
                if bulk.business_key_complete(norm_key):
                    existing_bk = target_bk_index.get(norm_key)
                    if existing_bk is not None:
                        errors.append(bulk.business_key_conflict_error(
                            row_num=row_num,
                            unique_cols=unique_cols,
                            combo_display=combo_display,
                            existing_pk=existing_bk,
                        ))
                        continue

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

            for col, new_val in values.items():
                col_l = str(col).lower()
                if col_l in db_client.AUDIT_COLUMN_NAMES or col_l in {p.lower() for p in pk_cols}:
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
                    "before_row_json": "{}",
                    "after_row_json": json.dumps(values, default=str),
                })

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

        if not diffs:
            staging_ops.drop_staging_table(
                cr_id, user_token=user_token, schema=schema, table_name=table, catalog=catalog,
            )
            raise ValueError("No changes detected.")

        staging_full = ""
        if staging_rows:
            df = pd.DataFrame(staging_rows)
            staging_full = staging_ops.create_staging_from_dataframe(
                cr_id, df, user_token=user_token,
                schema=schema, table_name=table, catalog=catalog,
                submitted_by=submitted_by, operation="update",
            )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        summary = {
            "total_rows": total,
            "rows_with_changes": rows_with_changes,
            "insert_count": len(insert_rows),
            "columns_changing": sorted(columns_changing),
            "sample_diffs": diffs[:10],
            "all_diffs": diffs,
            "insert_rows": insert_rows,
        }
        change_request.update_request(
            cr_id,
            staging_table_name=staging_full or None,
            row_count=total,
            status=change_request.STATUS_VALIDATED,
            validated_at=now,
            validation_summary={"passed": True, "updates": len(updates), "inserts": len(inserts)},
            change_summary=summary,
            user_token=user_token,
        )
        return {
            "change_request_id": cr_id,
            "status": change_request.STATUS_VALIDATED,
            "request_type": "grid_edit",
            "mode": "grid",
            "can_apply": True,
            "summary": summary,
            "errors": [],
            "error_count": 0,
        }

    except Exception as exc:
        logger.error("Grid validate failed: %s", exc, exc_info=True)
        staging_ops.drop_staging_table(
            cr_id, user_token=user_token, schema=schema, table_name=table, catalog=catalog,
        )
        err = [{"row": 0, "column": "", "reason": str(exc), "fix": "Fix errors and retry."}]
        change_request.update_request(
            cr_id,
            status=change_request.STATUS_FAILED,
            errors_json=err,
            failure_reason=str(exc),
            user_token=user_token,
        )
        return _validation_error(err, cr_id=cr_id)


def apply_grid_change_request(
    change_request_id: str,
    *,
    applied_by: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    rec = change_request.get_request(change_request_id, user_token=user_token)
    if not rec:
        raise ValueError("Change request not found.")
    if str(rec.get("request_type")) != "grid_edit":
        raise ValueError("Not a grid edit request.")
    if str(rec.get("status")) not in change_request.APPLYABLE_STATUSES:
        raise ValueError(f"Cannot apply request in status '{rec.get('status')}'.")

    summary = json.loads(rec.get("change_summary") or "{}")
    schema = str(rec["schema_name"])
    table = str(rec["table_name"])
    catalog = str(rec.get("catalog") or CATALOG)

    revision_id = revision_ops.create_revision(
        change_request_id=change_request_id,
        schema=schema,
        table=table,
        applied_by=applied_by,
        change_source=CHANGE_SOURCE,
        row_count=int(summary.get("rows_with_changes", 0)) + int(summary.get("insert_count", 0)),
        column_count=len(summary.get("columns_changing") or []),
        summary=summary,
        user_token=user_token,
    )

    audit_count = 0
    staging_name = str(rec.get("staging_table_name") or "").strip()

    if staging_name:
        merge_result = bulk_update_ops.apply_update_change_request(
            change_request_id, applied_by=applied_by, user_token=user_token
        )
        audit_count = int(merge_result.get("audit_entries") or 0)

    insert_rows = summary.get("insert_rows") or []
    insert_count = 0
    if insert_rows:
        insert_count = _apply_inserts(
            catalog, schema, table, insert_rows, applied_by, user_token
        )
        pk_cols = sorted(config_store.get_pk_cols(schema, table, user_token=user_token))
        for row in insert_rows:
            pk_info = {pk: row.get(pk, "") for pk in pk_cols}
            for col, val in row.items():
                if str(col).lower() in db_client.AUDIT_COLUMN_NAMES:
                    continue
                db_client.log_audit(
                    applied_by, schema, table, pk_info, col, "", val,
                    source=CHANGE_SOURCE,
                    change_request_id=change_request_id,
                    revision_id=revision_id,
                    user_token=user_token,
                )
                audit_count += 1

    if not staging_name and insert_rows:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        change_request.update_request(
            change_request_id,
            status=change_request.STATUS_APPLIED,
            applied_at=now,
            revision_id=revision_id,
            user_token=user_token,
        )
    else:
        change_request.update_request(
            change_request_id,
            revision_id=revision_id,
            user_token=user_token,
        )

    return {
        "change_request_id": change_request_id,
        "status": change_request.STATUS_APPLIED,
        "revision_id": revision_id,
        "rows_updated": summary.get("rows_with_changes", 0),
        "rows_inserted": insert_count,
        "audit_entries": audit_count,
    }


def _apply_inserts(
    catalog: str,
    schema: str,
    table: str,
    rows: list[dict[str, str]],
    user: str,
    user_token: str | None,
) -> int:
    from . import audit_cols

    table_cols = db_client.get_table_columns(catalog, schema, table, user_token=user_token)
    col_types = db_client.resolve_column_types(
        catalog,
        schema,
        table,
        config_store.get_column_storage_types(schema, table, user_token=user_token),
        user_token=user_token,
    )
    full_table = f"{catalog}.{schema}.{table}"
    grouped: dict[tuple[str, ...], list[tuple[Any, ...]]] = {}

    for row in rows:
        cols: list[str] = []
        vals: list[Any] = []
        for key, val in row.items():
            lk = str(key).lower()
            if lk in db_client.AUDIT_COLUMN_NAMES or lk not in table_cols:
                continue
            if db_client.is_empty_cell_value(val):
                continue
            normalized = db_client.normalize_cell_for_storage(
                val, col_types.get(lk, "string")
            )
            if normalized is None:
                continue
            cols.append(table_cols[lk])
            vals.append(normalized)
        if not cols:
            continue

        audit_cols.append_insert_audit_cols(cols, vals, table_cols, user)

        param_vals: list[Any] = []
        for v in vals:
            if v == "current_timestamp()":
                param_vals.append(None)
            else:
                param_vals.append(v)

        key = tuple(cols)
        grouped.setdefault(key, []).append(tuple(param_vals))

    count = 0
    for cols, batch_rows in grouped.items():
        has_ts = any(v is None for v in batch_rows[0])
        if has_ts:
            for row_vals in batch_rows:
                placeholders: list[str] = []
                param_vals: list[Any] = []
                for v in row_vals:
                    if v is None:
                        placeholders.append("current_timestamp()")
                    else:
                        placeholders.append("?")
                        param_vals.append(v)
                col_sql = ", ".join(f"`{c}`" for c in cols)
                db_client.execute(
                    f"INSERT INTO {full_table} ({col_sql}) "
                    f"VALUES ({', '.join(placeholders)})",
                    params=tuple(param_vals),
                    user_token=user_token,
                )
                count += 1
        else:
            db_client.execute_multi_insert(
                full_table, list(cols), batch_rows, user_token=user_token,
            )
            count += len(batch_rows)
    return count
