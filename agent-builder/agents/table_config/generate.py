#!/usr/bin/env python3
"""
Generate Data Manager config/tables/*.yaml using LiteLLM + optional Databricks DESCRIBE.

Usage (from agent-builder/):
  pip install -r requirements.txt
  cp .env.example .env   # fill LITELLM_* and optionally DATABRICKS_*

  python agents/table_config/generate.py \\
    --schema dmz --table dash_test_carrier --primary-keys carrierid --fetch-describe

  python agents/table_config/generate.py \\
    --schema dmz --table my_table --primary-keys id \\
    --describe-file input/describe.txt

  python agents/table_config/generate.py ... --dry-run-llm   # skip LLM, test DESCRIBE only

  # Audit columns on physical table (before or after YAML):
  python agents/table_config/ensure_audit_columns.py --schema dmz --table my_table
  python agents/table_config/ensure_audit_columns.py --schema dmz --table my_table --apply
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENT_ROOT))

from core.config import data_editor_root, databricks_catalog, load_env  # noqa: E402
from core.databricks import describe_table  # noqa: E402
from core.llm import chat_completion  # noqa: E402
from core.validate_deploy import run_deploy_dry_run  # noqa: E402
from core.yaml_utils import audit_columns_present, extract_yaml_block, parse_yaml  # noqa: E402

import yaml  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("table_config.generate")

PROMPT_FILE = Path(__file__).parent / "prompt.md"
EXAMPLE_FILE = Path(__file__).parent / "example.yaml"


def build_user_prompt(
    *,
    schema: str,
    table: str,
    primary_keys: list[str],
    describe_text: str,
    display_name: str | None,
    group: str | None,
    hints: str | None,
) -> str:
    example = EXAMPLE_FILE.read_text(encoding="utf-8")
    meta = [
        f"schema: {schema}",
        f"table: {table}",
        f"primary_keys: {primary_keys}",
        f"display_name: {display_name or table.replace('_', ' ').title()}",
        f"group: {group or 'General'}",
        f"catalog: {databricks_catalog()}",
    ]
    if hints:
        meta.append(f"user_hints: {hints}")
    return (
        "EXAMPLE (minimal enterprise style):\n"
        f"{example}\n\n"
        "METADATA:\n"
        + "\n".join(meta)
        + "\n\nDESCRIBE TABLE:\n"
        + describe_text
        + "\n\nGenerate YAML for the table above. Output YAML only."
    )


def default_output_path(schema: str, table: str) -> Path:
    return data_editor_root() / "config" / "tables" / f"{schema}.{table}.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Data Manager table YAML via LiteLLM")
    parser.add_argument("--schema", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--primary-keys", required=True, help="Comma-separated PK columns")
    parser.add_argument("--display-name", default=None)
    parser.add_argument("--group", default=None)
    parser.add_argument("--hints", default=None, help="Extra instructions (dropdowns, mandatory cols)")
    parser.add_argument("--catalog", default=None)
    parser.add_argument("--fetch-describe", action="store_true", help="DESCRIBE from Databricks")
    parser.add_argument("--describe-file", default=None, help="Path to pasted DESCRIBE text")
    parser.add_argument("--output", "-o", default=None, help="Output YAML path")
    parser.add_argument("--validate", action="store_true", help="Run deploy_config.py --dry-run")
    parser.add_argument("--dry-run-llm", action="store_true", help="Skip LLM; only show DESCRIBE input")
    args = parser.parse_args()

    load_env()
    pk_cols = [c.strip() for c in args.primary_keys.split(",") if c.strip()]
    if not pk_cols:
        logger.error("At least one primary key required")
        sys.exit(1)

    if args.describe_file:
        describe_text = Path(args.describe_file).read_text(encoding="utf-8")
    elif args.fetch_describe:
        describe_text = describe_table(
            args.schema, args.table, catalog=args.catalog
        )
    else:
        logger.error("Provide --fetch-describe or --describe-file")
        sys.exit(1)

    user_prompt = build_user_prompt(
        schema=args.schema,
        table=args.table,
        primary_keys=pk_cols,
        describe_text=describe_text,
        display_name=args.display_name,
        group=args.group,
        hints=args.hints,
    )

    if args.dry_run_llm:
        print("=== USER PROMPT (preview) ===")
        print(user_prompt)
        return

    system = PROMPT_FILE.read_text(encoding="utf-8")
    raw = chat_completion(system=system, user=user_prompt)
    cleaned = extract_yaml_block(raw)

    try:
        data = parse_yaml(cleaned)
    except Exception as exc:
        logger.error("Invalid YAML from LLM: %s", exc)
        print("=== RAW LLM OUTPUT ===")
        print(raw)
        sys.exit(1)

    bad_audit = audit_columns_present(data)
    if bad_audit:
        logger.warning("Removing audit columns from output: %s", bad_audit)
        data["columns"] = [
            c for c in (data.get("columns") or [])
            if str(c.get("column_name")) not in bad_audit
        ]

    out_path = Path(args.output) if args.output else default_output_path(args.schema, args.table)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# Generated by agent-builder/agents/table_config/generate.py\n"
        f"# Review before deploy: python scripts/deploy_config.py --file {out_path.name}\n\n"
    )
    yaml_body = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    out_path.write_text(header + yaml_body, encoding="utf-8")
    logger.info("Wrote %s (%d lines)", out_path, len(yaml_body.splitlines()))

    if args.validate:
        ok, log = run_deploy_dry_run(out_path)
        print(log)
        if not ok:
            logger.error("deploy_config --dry-run failed")
            sys.exit(1)
        logger.info("Validation passed")


if __name__ == "__main__":
    main()
