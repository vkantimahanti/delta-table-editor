"""Shared structured column-filter SQL builders."""
from __future__ import annotations

from typing import Any, Protocol


class _FilterLike(Protocol):
    column: str
    value: str | None


def build_filter_where(
    filters: list[Any],
    *,
    safe_column,
) -> tuple[str, tuple[str, ...]]:
    """Build WHERE clause + params for substring (LIKE) filters."""
    if not filters:
        return "", ()

    conditions: list[str] = []
    params: list[str] = []
    for filt in filters:
        column = filt.column if hasattr(filt, "column") else filt["column"]
        value = filt.value if hasattr(filt, "value") else filt.get("value")
        safe_col = safe_column(column)
        if value is None:
            conditions.append(f"{safe_col} IS NULL")
        else:
            conditions.append(f"CAST({safe_col} AS STRING) LIKE ?")
            params.append(f"%{value}%")
    return " AND ".join(conditions), tuple(params)
