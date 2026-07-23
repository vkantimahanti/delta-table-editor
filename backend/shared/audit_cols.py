"""Shared audit column helpers for INSERT/UPDATE paths."""
from typing import Any

_INSERT_AUDIT_FIELDS: list[tuple[str, str]] = [
    ("inserted_by", "user"),
    ("inserted_at", "now"),
    ("updated_by", "user"),
    ("updated_at", "now"),
    ("created_by", "user"),
    ("created_at", "now"),
    ("version", "zero"),
]


def insert_audit_value(kind: str, user: str) -> Any:
    if kind == "user":
        return user
    if kind == "now":
        return "current_timestamp()"
    if kind == "zero":
        return 0
    return kind


def append_insert_audit_cols(
    cols: list[str], vals: list[Any], table_cols: dict[str, str], user: str
) -> None:
    present = {c.lower() for c in cols}
    for field, kind in _INSERT_AUDIT_FIELDS:
        if field not in table_cols or field in present:
            continue
        cols.append(table_cols[field])
        vals.append(insert_audit_value(kind, user))
        present.add(field)


def append_update_audit_cols(
    set_parts: list[str], set_vals: list[Any], table_cols: dict[str, str], user: str
) -> None:
    if "updated_by" in table_cols:
        set_parts.append(f"{table_cols['updated_by']} = ?")
        set_vals.append(user)
    elif "modified_by" in table_cols:
        set_parts.append(f"{table_cols['modified_by']} = ?")
        set_vals.append(user)
    if "updated_at" in table_cols:
        set_parts.append(f"{table_cols['updated_at']} = current_timestamp()")
    elif "modified_date" in table_cols:
        set_parts.append(f"{table_cols['modified_date']} = current_timestamp()")


def sql_literal(value: Any) -> str:
    """Format a value for inline SQL INSERT (non-parameterized bulk paths)."""
    if value == "current_timestamp()":
        return "current_timestamp()"
    if value is None or value == "":
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(int(value)) if isinstance(value, float) and value == int(value) else str(value)
    return f"'{str(value).replace(chr(39), chr(39) * 2)}'"
