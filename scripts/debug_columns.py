from dotenv import load_dotenv
load_dotenv()
from backend.shared import config_store, db_client

CATALOG = db_client.CATALOG
schema, table = "dmz", "dash_test_carrier"

df = db_client.query(
    f"SELECT column_name, visible, is_active, typeof(visible) AS vt "
    f"FROM {CATALOG}.dmz.dataeditor_column_config "
    f"WHERE schema_name = '{schema}' AND table_name = '{table}' "
    f"ORDER BY col_order LIMIT 5"
)
print("sample rows:")
print(df)

df2 = db_client.query(
    f"SELECT COUNT(1) AS c FROM {CATALOG}.dmz.dataeditor_column_config "
    f"WHERE schema_name = '{schema}' AND table_name = '{table}' "
    f"AND visible = TRUE AND is_active = TRUE"
)
print("filtered count:", df2.iloc[0]["c"])

cols = config_store.get_columns(schema, table)
print("config_store.get_columns count:", len(cols))
if cols:
    print("first:", cols[0])
