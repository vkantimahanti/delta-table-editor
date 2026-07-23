"""Ensure standard audit columns exist on physical Delta tables (prod-safe ALTER)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .databricks import _connection

logger = logging.getLogger("agent_builder.audit_columns")

# Standard audit columns expected by Data Manager (see config/defaults.yaml)
AUDIT_COLUMNS: list[tuple[str, str]] = [
    ("version", "DOUBLE"),
    ("inserted_by", "STRING"),
    ("inserted_at", "TIMESTAMP"),
    ("updated_by", "STRING"),
    ("updated_at", "TIMESTAMP"),
]

# Legacy aliases — treated as satisfying the standard name if present
ALIASES: dict[str, tuple[str, ...]] = {
    "inserted_by": ("created_by",),
    "inserted_at": ("created_at",),
    "updated_by": ("modified_by",),
    "updated_at": ("modified_date",),
}


@dataclass
class AuditColumnPlan:
    catalog: str
    schema: str
    table: str
    existing: set[str]
    to_add: list[tuple[str, str]]
    satisfied_by_alias: dict[str, str]

    @property
    def full_name(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.table}"

    def needs_changes(self) -> bool:
        return bool(self.to_add)


def _describe_columns(catalog: str, schema: str, table: str) -> set[str]:
    full = f"{catalog}.{schema}.{table}"
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DESCRIBE TABLE {full}")
            rows = cur.fetchall()
    names: set[str] = set()
    for row in rows:
        if not row:
            continue
        name = str(row[0]).strip()
        if name and not name.startswith("#"):
            names.add(name.lower())
    return names


def plan_audit_columns(
    schema: str,
    table: str,
    *,
    catalog: str,
) -> AuditColumnPlan:
    existing = _describe_columns(catalog, schema, table)
    to_add: list[tuple[str, str]] = []
    satisfied: dict[str, str] = {}

    for col, dtype in AUDIT_COLUMNS:
        if col in existing:
            continue
        alias_hit = next((a for a in ALIASES.get(col, ()) if a in existing), None)
        if alias_hit:
            satisfied[col] = alias_hit
            continue
        to_add.append((col, dtype))

    return AuditColumnPlan(
        catalog=catalog,
        schema=schema,
        table=table,
        existing=existing,
        to_add=to_add,
        satisfied_by_alias=satisfied,
    )


def build_sql(plan: AuditColumnPlan, *, backfill_version: bool = True) -> list[str]:
    if not plan.needs_changes():
        return []

    stmts: list[str] = []
    for col, dtype in plan.to_add:
        stmts.append(f"ALTER TABLE {plan.full_name} ADD COLUMN `{col}` {dtype}")
    if backfill_version and any(c == "version" for c, _ in plan.to_add):
        stmts.append(
            f"UPDATE {plan.full_name} SET version = 0 WHERE version IS NULL"
        )
    return stmts


def apply_sql(statements: list[str]) -> None:
    with _connection() as conn:
        with conn.cursor() as cur:
            for sql in statements:
                logger.info("Executing: %s", sql)
                cur.execute(sql)


def format_report(plan: AuditColumnPlan) -> str:
    lines = [f"Table: {plan.full_name}", f"Existing columns: {len(plan.existing)}"]
    if plan.satisfied_by_alias:
        for std, alias in plan.satisfied_by_alias.items():
            lines.append(f"  OK (alias): {std} → {alias}")
    present = [
        c for c, _ in AUDIT_COLUMNS
        if c in plan.existing or c in plan.satisfied_by_alias
    ]
    if present:
        lines.append(f"Already present: {', '.join(present)}")
    if plan.to_add:
        lines.append("To add:")
        for col, dtype in plan.to_add:
            lines.append(f"  - {col} ({dtype})")
    else:
        lines.append("No audit columns to add.")
    return "\n".join(lines)
