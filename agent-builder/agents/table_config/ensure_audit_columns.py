#!/usr/bin/env python3
"""Check and optionally ADD missing audit columns on a prod Delta table."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENT_ROOT))

from core.audit_columns import apply_sql, build_sql, format_report, plan_audit_columns  # noqa: E402
from core.config import databricks_catalog, load_env  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ensure_audit_columns")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ensure Data Manager audit columns exist on a physical table"
    )
    parser.add_argument("--schema", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--catalog", default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute ALTER/UPDATE (default: show SQL only)",
    )
    args = parser.parse_args()

    load_env()
    cat = args.catalog or databricks_catalog()
    plan = plan_audit_columns(args.schema, args.table, catalog=cat)

    print(format_report(plan))
    print()

    sqls = build_sql(plan)
    if not sqls:
        logger.info("Nothing to do.")
        return

    print("SQL to run:")
    for s in sqls:
        print(s)
    print()

    if args.apply:
        logger.info("Applying %d statement(s) to %s", len(sqls), plan.full_name)
        apply_sql(sqls)
        logger.info("Done.")
    else:
        print("Dry run only — re-run with --apply to execute on Databricks.")


if __name__ == "__main__":
    main()
