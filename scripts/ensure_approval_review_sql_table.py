"""Create dataeditor_approval_review_sql if missing (local dev helper)."""
from dotenv import load_dotenv

load_dotenv()

from backend.shared import db_client

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS your_catalog.dmz.dataeditor_approval_review_sql (
    change_request_id       STRING    NOT NULL,
    catalog                 STRING,
    schema_name             STRING    NOT NULL,
    table_name              STRING    NOT NULL,
    staging_table           STRING,
    target_table            STRING,
    compare_sql             STRING    NOT NULL,
    compare_columns_json      STRING,
    sql_version               INT,
    generated_at              TIMESTAMP,
    updated_at                TIMESTAMP
) USING DELTA
"""


def main() -> None:
    try:
        db_client.execute(CREATE_SQL)
        print("create: OK")
    except Exception as exc:
        print(f"create: FAIL - {exc}")

    df = db_client.query(
        "SELECT table_name FROM your_catalog.information_schema.tables "
        "WHERE table_schema = 'dmz' AND table_name = 'dataeditor_approval_review_sql'"
    )
    print("table exists:", not df.empty)


if __name__ == "__main__":
    main()
