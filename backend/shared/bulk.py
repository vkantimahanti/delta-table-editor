"""
Bulk CSV upload handler.
Supports append (INSERT) and overwrite (TRUNCATE + INSERT) modes.
Validates before writing — no partial inserts.
"""
import base64
import csv
import io
import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from . import audit_cols
from . import db_client

logger = logging.getLogger("delta_editor.bulk")
CATALOG = db_client.CATALOG


@dataclass(frozen=True)
class BulkTableConfig:
    label: str
    schema: str
    table: str
    required_cols: tuple[str, ...]
    insert_cols: tuple[str, ...]
    bool_cols: tuple[str, ...]
    number_cols: tuple[str, ...]
    defaults: dict[str, Any]
    sample_rows: tuple[dict[str, Any], ...]
    notes: str


def _safe(value: Any) -> str:
    return str(value or "").replace("'", "''")


def _parse_bool(value: Any, default: bool = False) -> tuple[bool | None, str]:
    if value is None or str(value).strip() == "":
        return default, ""
    text = str(value).strip().lower()
    if text in ("true", "t", "yes", "y", "1"):
        return True, ""
    if text in ("false", "f", "no", "n", "0"):
        return False, ""
    return None, f"Invalid boolean: '{value}'"


def _simple(table: str, label: str, samples: tuple[str, str]) -> BulkTableConfig:
    return BulkTableConfig(
        label=label, schema="dmz", table=table,
        required_cols=("value",),
        insert_cols=("value", "display_label", "sort_order", "is_active"),
        bool_cols=("is_active",), number_cols=("sort_order",),
        defaults={"display_label": "{value}", "sort_order": 10, "is_active": True},
        sample_rows=(
            {"value": samples[0], "display_label": samples[0], "sort_order": 10, "is_active": True},
            {"value": samples[1], "display_label": samples[1], "sort_order": 20, "is_active": True},
        ),
        notes=f"Bulk load values for {label}.",
    )


# Registry — extend as needed
BULK_CONFIGS: dict[tuple[str, str], BulkTableConfig] = {
    ("dmz", "ref_isreviewed"): _simple("ref_isreviewed", "Is Reviewed", ("Yes", "No")),
}


def get_config(schema: str, table: str) -> BulkTableConfig | None:
    return BULK_CONFIGS.get((schema, table))


def list_configs() -> list[dict]:
    return [
        {"label": c.label, "schema": c.schema, "table": c.table,
         "insert_cols": list(c.insert_cols), "notes": c.notes}
        for c in BULK_CONFIGS.values()
    ]


def template_csv(config: BulkTableConfig) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(config.insert_cols), lineterminator="\n")
    w.writeheader()
    for row in config.sample_rows:
        w.writerow(row)
    return out.getvalue()


def _existing_values(config: BulkTableConfig, user_token: str | None) -> set[str]:
    try:
        df = db_client.query(
            f"SELECT lower(trim(value)) AS vk FROM {CATALOG}.{config.schema}.{config.table} "
            "WHERE value IS NOT NULL",
            user_token=user_token,
        )
        return set(df["vk"].astype(str).tolist()) if not df.empty else set()
    except Exception:
        return set()


def validate_csv(
    csv_text: str, config: BulkTableConfig, user_token: str | None = None
) -> dict[str, Any]:
    if not csv_text.strip():
        return {"input_rows": 0, "valid_rows": [], "invalid_rows": [], "sql_preview": []}

    input_df = pd.read_csv(io.StringIO(csv_text), dtype=str).fillna("")
    input_df.columns = [c.strip() for c in input_df.columns]

    existing = _existing_values(config, user_token)
    seen: set[str] = set()
    valid_rows, invalid_rows = [], []

    for col in config.insert_cols:
        if col not in input_df.columns:
            input_df[col] = ""

    for _, row in input_df.iterrows():
        norm: dict[str, Any] = {}
        errors: list[str] = []

        for col in config.insert_cols:
            raw = str(row.get(col, "")).strip()
            default = config.defaults.get(col, "")
            if not raw and default != "":
                raw = str(default).format(value=str(row.get("value", "")).strip())
            norm[col] = raw

        for col in config.required_cols:
            if not str(norm.get(col, "")).strip():
                errors.append(f"'{col}' is required")

        vk = str(norm.get("value", "")).strip().lower()
        if vk:
            if vk in existing:
                errors.append(f"Value already exists: '{norm['value']}'")
            if vk in seen:
                errors.append(f"Duplicate in file: '{norm['value']}'")
            seen.add(vk)

        for col in config.bool_cols:
            parsed, err = _parse_bool(norm.get(col), bool(config.defaults.get(col, False)))
            if err:
                errors.append(f"{col}: {err}")
            norm[col] = parsed

        for col in config.number_cols:
            try:
                norm[col] = int(float(norm.get(col, 0) or 0))
            except Exception:
                errors.append(f"{col}: invalid number '{norm.get(col)}'")

        if errors:
            norm["validation_errors"] = "; ".join(errors)
            invalid_rows.append(norm)
        else:
            valid_rows.append(norm)

    sql_preview = [_build_sql(config, r, "<user>") for r in valid_rows[:10]]
    return {
        "input_rows": len(input_df),
        "valid_rows": valid_rows,
        "invalid_rows": invalid_rows,
        "sql_preview": sql_preview,
    }


def _sql_literal(value: Any, config: BulkTableConfig, col: str) -> str:
    if col in config.bool_cols:
        return "TRUE" if bool(value) else "FALSE"
    if col in config.number_cols:
        return str(int(float(value))) if value not in (None, "") else "NULL"
    return f"'{_safe(value)}'"


def _build_sql(config: BulkTableConfig, row: dict, user: str, table_cols: dict[str, str] | None = None) -> str:
    cols = list(config.insert_cols)
    vals: list[Any] = []
    for c in config.insert_cols:
        raw = row.get(c)
        if c in config.bool_cols:
            parsed, _ = _parse_bool(raw, bool(config.defaults.get(c, False)))
            vals.append(parsed)
        elif c in config.number_cols:
            try:
                vals.append(int(float(raw or 0)))
            except Exception:
                vals.append(0)
        else:
            vals.append(raw)

    if table_cols:
        audit_cols.append_insert_audit_cols(cols, vals, table_cols, user)

    sql_vals = [audit_cols.sql_literal(v) for v in vals]
    return f"INSERT INTO {CATALOG}.{config.schema}.{config.table} ({', '.join(cols)}) VALUES ({', '.join(sql_vals)})"


def insert_rows(
    config: BulkTableConfig,
    rows: list[dict],
    user: str,
    mode: str = "append",           # "append" | "overwrite"
    user_token: str | None = None,
) -> int:
    """
    Insert valid rows.
    mode='append'    → INSERT only
    mode='overwrite' → TRUNCATE then INSERT (atomic within one connection)
    """
    if not rows:
        return 0

    table_cols = db_client.get_table_columns(CATALOG, config.schema, config.table, user_token=user_token)
    stmts = []
    if mode == "overwrite":
        stmts.append(f"TRUNCATE TABLE {CATALOG}.{config.schema}.{config.table}")
        logger.info("Bulk overwrite: truncating %s.%s", config.schema, config.table)

    stmts.extend([_build_sql(config, r, user, table_cols) for r in rows])
    db_client.execute_many(stmts, user_token=user_token)
    logger.info("Bulk insert: %d rows into %s.%s (mode=%s)", len(rows), config.schema, config.table, mode)
    return len(rows)


# ── Generic file upload (non-ref tables) ─────────────────────────────────────

def parse_upload_csv(
    csv_text: str,
    delimiter: str = ",",
    has_header: bool = True,
) -> pd.DataFrame:
    """Parse uploaded CSV text. First row is treated as column names when has_header=True."""
    sep = "\t" if delimiter in ("\\t", "\t") else delimiter
    df = pd.read_csv(
        io.StringIO(csv_text),
        dtype=str,
        sep=sep,
        header=0 if has_header else None,
        keep_default_na=False,
    ).fillna("")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def normalize_upload_format(filename: str, file_format: str = "") -> str:
    """Return csv | tsv | xlsx based on explicit format or file extension."""
    fmt = str(file_format or "").strip().lower()
    if fmt in ("xlsx", "excel"):
        return "xlsx"
    if fmt in ("tsv", "txt", "text"):
        return "tsv"
    if fmt == "csv":
        return "csv"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "xlsx":
        return "xlsx"
    if ext == "xls":
        raise ValueError("Legacy .xls is not supported. Save the workbook as .xlsx and retry.")
    if ext in ("txt", "tsv"):
        return "tsv"
    return "csv"


def parse_upload_dataframe(
    *,
    csv_text: str = "",
    file_base64: str = "",
    filename: str = "upload.csv",
    file_format: str = "",
    delimiter: str = ",",
    has_header: bool = True,
) -> pd.DataFrame:
    """Parse CSV/TSV text or Excel (base64) upload into a string-typed DataFrame."""
    fmt = normalize_upload_format(filename, file_format)

    if fmt == "xlsx":
        if not str(file_base64 or "").strip():
            raise ValueError("Excel upload is missing file content.")
        try:
            raw = base64.b64decode(file_base64, validate=True)
        except Exception as exc:
            raise ValueError(f"Invalid Excel file encoding: {exc}") from exc
        if not raw:
            raise ValueError("Excel upload is empty.")
        try:
            df = pd.read_excel(
                io.BytesIO(raw),
                dtype=str,
                header=0 if has_header else None,
                engine="openpyxl",
            ).fillna("")
        except Exception as exc:
            raise ValueError(f"Could not read Excel file: {exc}") from exc
        if not has_header:
            df.columns = [f"col_{i + 1}" for i in range(len(df.columns))]
        else:
            df.columns = [str(c).strip() for c in df.columns]
        return df

    if not str(csv_text or "").strip():
        raise ValueError("Upload file is empty.")
    sep = "\t" if fmt == "tsv" else delimiter
    return parse_upload_csv(csv_text, delimiter=sep, has_header=has_header)


def _format_pk_display(pk_info: dict[str, str]) -> str:
    parts: list[str] = []
    for pk, val in pk_info.items():
        text = str(val or "").strip()
        parts.append(f"{pk}=(blank)" if not text else f"{pk}={text}")
    return ", ".join(parts)


def _is_auto_pk_new_row(
    pk_info: dict[str, str],
    pk_cols: list[str],
    pk_editable: dict[str, bool],
) -> bool:
    """
    True when the row has blank PK value(s) that will be auto-generated on insert.

    Multiple such rows must not be treated as duplicate keys (each gets MAX+1, MAX+2, …).
    """
    if any(str(pk_info.get(pk, "") or "").strip() for pk in pk_cols):
        return False
    for pk in pk_cols:
        if pk_editable.get(pk, True):
            return False
    return True


def duplicate_pk_validation_errors(
    df: pd.DataFrame,
    pk_cols_in_df: list[str],
    pk_cols: list[str],
    *,
    has_header: bool,
    pk_editable: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """
    Build structured validation errors for duplicate primary keys in an upload file.

    Each error lists every file row number that shares the same PK value(s).
    Rows with blank auto-generated PKs are excluded — they receive unique IDs at insert.
    """
    if df.empty or not pk_cols_in_df:
        return []

    row_offset = 2 if has_header else 1
    pk_by_lower = {str(p).lower(): p for p in pk_cols}
    col_to_pk = {
        str(c): pk_by_lower.get(str(c).lower(), str(c))
        for c in pk_cols_in_df
    }
    editable = pk_editable or {pk: True for pk in pk_cols}

    groups: dict[tuple[str, ...], list[int]] = {}
    pk_info_by_key: dict[tuple[str, ...], dict[str, str]] = {}

    for idx, row in df.iterrows():
        pk_vals = tuple(
            "" if pd.isna(row[c]) else str(row[c]).strip()
            for c in pk_cols_in_df
        )
        pk_info = {
            col_to_pk[str(c)]: pk_vals[i]
            for i, c in enumerate(pk_cols_in_df)
        }
        if _is_auto_pk_new_row(pk_info, pk_cols, editable):
            continue

        row_num = int(idx) + row_offset
        groups.setdefault(pk_vals, []).append(row_num)
        if pk_vals not in pk_info_by_key:
            pk_info_by_key[pk_vals] = pk_info

    errors: list[dict[str, Any]] = []
    for pk_vals, row_nums in sorted(groups.items(), key=lambda item: min(item[1])):
        if len(row_nums) < 2:
            continue
        sorted_rows = sorted(row_nums)
        pk_info = pk_info_by_key[pk_vals]
        pk_display = _format_pk_display(pk_info)
        errors.append({
            "row": sorted_rows[0],
            "duplicate_rows": sorted_rows,
            "pk": pk_info,
            "column": ", ".join(pk_cols),
            "reason": f"Duplicate primary key ({pk_display}) appears on file rows {', '.join(map(str, sorted_rows))}.",
            "fix": (
                "Each primary key value must appear only once in the file. "
                "For new rows, leave the auto-generated key blank — only existing keys "
                "should be repeated when updating the same row."
            ),
        })
    return errors


def normalize_business_key_value(val: Any) -> str:
    """Case-insensitive, trimmed comparison for upload business-key columns."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val).strip().lower()


def _cell_display_value(val: Any) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val).strip()


def business_key_from_row(
    row: dict[str, Any],
    unique_cols: list[str],
    table_cols: dict[str, str],
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Return (normalized_key, display_values) for configured business-key columns."""
    display: dict[str, str] = {}
    norm_parts: list[str] = []
    for col in unique_cols:
        actual = table_cols.get(col.lower(), col)
        raw = row.get(actual, row.get(col, row.get(col.lower(), "")))
        text = _cell_display_value(raw)
        display[col] = text
        norm_parts.append(normalize_business_key_value(text))
    return tuple(norm_parts), display


def business_key_complete(norm_key: tuple[str, ...]) -> bool:
    return bool(norm_key) and all(part for part in norm_key)


def format_business_key_display(combo_info: dict[str, str]) -> str:
    parts: list[str] = []
    for col, val in combo_info.items():
        parts.append(f"{col}=(blank)" if not val else f"{col}={val}")
    return ", ".join(parts)


def duplicate_business_key_validation_errors(
    df: pd.DataFrame,
    unique_cols: list[str],
    table_cols: dict[str, str],
    *,
    has_header: bool,
) -> list[dict[str, Any]]:
    """Duplicate Carrier + SubCarrier + Market (etc.) groups within the upload file."""
    if df.empty or not unique_cols:
        return []

    missing = [c for c in unique_cols if c.lower() not in table_cols]
    if missing:
        return [{
            "row": 0,
            "column": ", ".join(missing),
            "reason": f"File missing business-key column(s): {', '.join(missing)}.",
            "fix": "Include all business-key columns in the export/upload file.",
        }]

    row_offset = 2 if has_header else 1
    groups: dict[tuple[str, ...], list[int]] = {}
    display_by_key: dict[tuple[str, ...], dict[str, str]] = {}

    for idx, row in df.iterrows():
        row_dict = {str(c): row[c] for c in df.columns}
        norm_key, display = business_key_from_row(row_dict, unique_cols, table_cols)
        if not business_key_complete(norm_key):
            continue
        row_num = int(idx) + row_offset
        groups.setdefault(norm_key, []).append(row_num)
        display_by_key.setdefault(norm_key, display)

    errors: list[dict[str, Any]] = []
    cols_label = ", ".join(unique_cols)
    for norm_key, row_nums in sorted(groups.items(), key=lambda item: min(item[1])):
        if len(row_nums) < 2:
            continue
        sorted_rows = sorted(row_nums)
        combo_display = format_business_key_display(display_by_key[norm_key])
        errors.append({
            "row": sorted_rows[0],
            "duplicate_rows": sorted_rows,
            "column": cols_label,
            "reason": (
                f"Duplicate business key ({combo_display}) appears on file rows "
                f"{', '.join(map(str, sorted_rows))}."
            ),
            "fix": (
                f"Each {cols_label} combination must appear only once in the file."
            ),
        })
    return errors


def build_business_key_index(
    rows_by_pk: dict[tuple[str, ...], dict[str, Any]],
    unique_cols: list[str],
    table_cols: dict[str, str],
    pk_cols: list[str],
) -> dict[tuple[str, ...], dict[str, str]]:
    """Map normalized business key → existing row PK values."""
    index: dict[tuple[str, ...], dict[str, str]] = {}
    for row_dict in rows_by_pk.values():
        norm_key, _ = business_key_from_row(row_dict, unique_cols, table_cols)
        if not business_key_complete(norm_key):
            continue
        pk_info = {
            pk: db_client.cell_to_str(
                row_dict.get(table_cols.get(pk.lower(), pk), row_dict.get(pk, ""))
            ).strip()
            for pk in pk_cols
        }
        index[norm_key] = pk_info
    return index


def business_key_conflict_error(
    *,
    row_num: int,
    unique_cols: list[str],
    combo_display: dict[str, str],
    existing_pk: dict[str, str],
) -> dict[str, Any]:
    pk_label = ", ".join(f"{k}={v}" for k, v in existing_pk.items() if str(v).strip())
    combo_text = format_business_key_display(combo_display)
    return {
        "row": row_num,
        "pk": existing_pk,
        "column": ", ".join(unique_cols),
        "reason": (
            f"Business key ({combo_text}) already exists in the table"
            + (f" as {pk_label}." if pk_label else ".")
        ),
        "fix": (
            "Export the table and update that existing row (keep its CarrierID), "
            "or use a unique combination of business-key values."
        ),
    }


def business_key_update_conflict_error(
    *,
    row_num: int,
    unique_cols: list[str],
    combo_display: dict[str, str],
    existing_pk: dict[str, str],
) -> dict[str, Any]:
    pk_label = ", ".join(f"{k}={v}" for k, v in existing_pk.items() if str(v).strip())
    combo_text = format_business_key_display(combo_display)
    return {
        "row": row_num,
        "pk": existing_pk,
        "column": ", ".join(unique_cols),
        "reason": (
            f"Update would duplicate business key ({combo_text}) "
            f"already used by {pk_label}."
        ),
        "fix": "Change the business-key values or update the other row instead.",
    }


def persist_upload_copy(
    cr_id: str,
    filename: str,
    *,
    csv_text: str = "",
    file_base64: str = "",
) -> str | None:
    """Store original upload on UC Volume when configured."""
    from . import staging_ops

    path = staging_ops.volume_upload_path(cr_id, filename)
    if str(file_base64 or "").strip():
        return staging_ops.try_write_volume_bytes(path, base64.b64decode(file_base64))
    return staging_ops.try_write_volume_file(path, csv_text)


def generic_upload(
    catalog: str,
    schema: str,
    table: str,
    df: pd.DataFrame,
    mode: str,               # "append" | "overwrite"
    user: str,
    user_token: str | None = None,
) -> dict[str, Any]:
    """
    Upload any DataFrame to any allowed table.
    Checks existence first — if table exists and mode is not set, raises ValueError.
    """
    exists = db_client.table_exists(catalog, schema, table, user_token=user_token)
    full_name = f"{catalog}.{schema}.{table}"

    if not exists:
        # Create table from DataFrame schema
        dtype_map = {
            "int64": "BIGINT", "int32": "INT", "float64": "DOUBLE",
            "float32": "FLOAT", "object": "STRING", "bool": "BOOLEAN",
            "datetime64[ns]": "TIMESTAMP",
        }
        col_defs = ", ".join(
            f"`{c}` {dtype_map.get(str(dt), 'STRING')}"
            for c, dt in df.dtypes.items()
        )
        db_client.execute(f"CREATE TABLE {full_name} ({col_defs}) USING DELTA", user_token=user_token)
        logger.info("Created new table: %s", full_name)
        mode = "append"

    stmts = []
    if mode == "overwrite":
        stmts.append(f"TRUNCATE TABLE {full_name}")

    table_cols = db_client.get_table_columns(catalog, schema, table, user_token=user_token)
    if exists and not table_cols:
        raise ValueError(
            f"Could not read column list for {full_name}. "
            "Upload aborted to avoid rows without audit columns."
        )

    for _, row in df.iterrows():
        cols: list[str] = []
        vals: list[Any] = []
        for c in df.columns:
            lk = str(c).strip().lower()
            if table_cols and lk not in table_cols:
                continue
            col_name = table_cols[lk] if table_cols else str(c)
            raw = row[c]
            if pd.isna(raw) or str(raw).strip() == "":
                vals.append(None)
            else:
                vals.append(str(raw).strip())
            cols.append(col_name)

        if table_cols:
            audit_cols.append_insert_audit_cols(cols, vals, table_cols, user)

        sql_vals = [audit_cols.sql_literal(v) for v in vals]
        stmt = f"INSERT INTO {full_name} ({', '.join(cols)}) VALUES ({', '.join(sql_vals)})"
        if table_cols and "version" in table_cols and "version" not in {c.lower() for c in cols}:
            logger.error("Audit columns missing from INSERT for %s — cols=%s", full_name, cols)
        stmts.append(stmt)

    if stmts:
        sample_idx = 1 if mode == "overwrite" and len(stmts) > 1 else 0
        logger.info("Upload sample INSERT: %s", stmts[sample_idx][:500])
    db_client.execute_many(stmts, user_token=user_token)
    logger.info("Generic upload: %d rows to %s (mode=%s)", len(df), full_name, mode)
    return {"rows_inserted": len(df), "table": full_name, "mode": mode, "created": not exists}
