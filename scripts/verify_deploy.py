"""Quick check: config tables vs physical table columns."""
from dotenv import load_dotenv

load_dotenv()

from backend.shared import db_client

CATALOG = db_client.CATALOG
SCHEMA, TABLE = "dmz", "dash_test_carrier"


def show(title: str, sql: str) -> None:
    print(f"\n=== {title} ===")
    df = db_client.query(sql)
    print(df.to_string(index=False) if not df.empty else "(empty)")


show(
    "registry",
    f"SELECT schema_name, table_name, display_name, primary_keys, is_active "
    f"FROM {CATALOG}.dmz.dataeditor_table_registry "
    f"WHERE schema_name = '{SCHEMA}' AND table_name = '{TABLE}'",
)
show(
    "column config",
    f"SELECT column_name, col_order, visible, editable, column_type "
    f"FROM {CATALOG}.dmz.dataeditor_column_config "
    f"WHERE schema_name = '{SCHEMA}' AND table_name = '{TABLE}' "
    f"ORDER BY col_order",
)
show(
    "rules count",
    f"SELECT COUNT(1) AS rule_count FROM {CATALOG}.dmz.dataeditor_business_rules "
    f"WHERE schema_name = '{SCHEMA}' AND table_name = '{TABLE}'",
)
show(
    "physical table columns",
    f"SELECT column_name, data_type FROM {CATALOG}.information_schema.columns "
    f"WHERE table_catalog = '{CATALOG}' AND table_schema = '{SCHEMA}' "
    f"AND table_name = '{TABLE}' ORDER BY ordinal_position",
)
