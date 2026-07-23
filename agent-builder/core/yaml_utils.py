"""Extract and validate YAML from LLM output."""
from __future__ import annotations

import re

import yaml


def extract_yaml_block(text: str) -> str:
    """Strip markdown fences and leading commentary."""
    raw = text.strip()
    fence = re.search(r"```(?:yaml)?\s*\n(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()
    # Drop lines before first top-level key
    lines = raw.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.strip().startswith(("table:", "columns:")):
            start = i
            break
    return "\n".join(lines[start:]).strip()


def parse_yaml(text: str) -> dict:
    cleaned = extract_yaml_block(text)
    data = yaml.safe_load(cleaned)
    if not isinstance(data, dict):
        raise ValueError("LLM output is not a YAML mapping")
    return data


def audit_columns_present(data: dict) -> list[str]:
    """Return audit column names wrongly included in columns list."""
    audit = {"version", "inserted_by", "inserted_at", "updated_by", "updated_at"}
    found = []
    for col in data.get("columns") or []:
        name = str(col.get("column_name") or "")
        if name in audit:
            found.append(name)
    return found
