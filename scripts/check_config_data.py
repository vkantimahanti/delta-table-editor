"""Quick check of deployed config data in Delta tables."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from databricks import sql as dbsql

load_dotenv(Path(__file__).parent.parent / ".env")

CATALOG = os.environ.get("TARGET_CATALOG", "your_catalog")
SCHEMA = sys.argv[1] if len(sys.argv) > 1 else "your_schema"
TABLES = [
    "sample_entity",
    "sample_entity_detail",
    "sample_entity",
]
IN_LIST = ", ".join(f"'{t}'" for t in TABLES)


def get_connection():
    hostname = (
        os.environ.get("DATABRICKS_SERVER_HOSTNAME")
        or os.environ.get("DATABRICKS_HOST", "").replace("https://", "").rstrip("/")
    )
    http_path = (
        os.environ.get("DATABRICKS_HTTP_PATH")
        or f"/sql/1.0/warehouses/{os.environ.get('DATABRICKS_WAREHOUSE_ID', '')}"
    )
    token = os.environ.get("DATABRICKS_TOKEN", "")
    return dbsql.connect(
        server_hostname=hostname,
        http_path=http_path,
        access_token=token,
    )


def fetch(cur, sql):
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def main():
    with get_connection() as conn:
        with conn.cursor() as cur:
            print("=== dataeditor_table_registry ===")
            rows = fetch(
                cur,
                f"""
                SELECT schema_name, table_name, primary_keys, requires_approval,
                       approver_emails, is_active, row_limit
                FROM {CATALOG}.dmz.dataeditor_table_registry
                WHERE schema_name = '{SCHEMA}' AND table_name IN ({IN_LIST})
                ORDER BY table_name
                """,
            )
            print(f"Rows: {len(rows)}")
            for r in rows:
                print(r)

            print("\n=== dataeditor_column_config (counts) ===")
            rows = fetch(
                cur,
                f"""
                SELECT table_name, COUNT(1) AS col_count
                FROM {CATALOG}.dmz.dataeditor_column_config
                WHERE schema_name = '{SCHEMA}' AND table_name IN ({IN_LIST})
                  AND is_active = TRUE
                GROUP BY table_name
                ORDER BY table_name
                """,
            )
            for r in rows:
                print(r)

            print("\n=== dataeditor_column_config (detail) ===")
            rows = fetch(
                cur,
                f"""
                SELECT table_name, column_name, col_order, column_type,
                       mandatory, is_primary_key, visible, editable, dropdown_source
                FROM {CATALOG}.dmz.dataeditor_column_config
                WHERE schema_name = '{SCHEMA}' AND table_name IN ({IN_LIST})
                  AND is_active = TRUE
                ORDER BY table_name, col_order, column_name
                """,
            )
            print(f"Rows: {len(rows)}")
            for r in rows:
                ds = r.get("dropdown_source")
                if ds and len(str(ds)) > 120:
                    r["dropdown_source"] = str(ds)[:120] + "..."
                print(r)

            print("\n=== dataeditor_business_rules ===")
            rows = fetch(
                cur,
                f"""
                SELECT table_name, column_name, condition_type, on_fail, reason,
                       col_is_active, cond_is_active
                FROM {CATALOG}.dmz.dataeditor_business_rules
                WHERE schema_name = '{SCHEMA}' AND table_name IN ({IN_LIST})
                  AND col_is_active = TRUE AND cond_is_active = TRUE
                ORDER BY table_name, column_name, condition_type
                """,
            )
            print(f"Rows: {len(rows)}")
            for r in rows:
                print(r)


if __name__ == "__main__":
    main()
