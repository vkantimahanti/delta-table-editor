"""Create dataeditor_change_requests if missing (local dev helper)."""
from dotenv import load_dotenv

load_dotenv()

from backend.shared import db_client

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS your_catalog.dmz.dataeditor_change_requests (
    change_request_id         STRING    NOT NULL,
    status                    STRING    NOT NULL,
    request_type              STRING    NOT NULL,
    mode                      STRING,
    catalog                   STRING,
    schema_name               STRING    NOT NULL,
    table_name                STRING    NOT NULL,
    submitted_by              STRING,
    submitted_at              TIMESTAMP,
    validated_at              TIMESTAMP,
    applied_at                TIMESTAMP,
    updated_at                TIMESTAMP,
    source_file_volume_path   STRING,
    staging_table_name        STRING,
    export_volume_path        STRING,
    row_count                 INT,
    validation_summary        STRING,
    change_summary            STRING,
    errors_json               STRING,
    filter_snapshot           STRING,
    failure_reason            STRING,
    requires_approval         BOOLEAN,
    approver_emails           STRING,
    approval_status           STRING,
    approved_by               STRING,
    approved_at               TIMESTAMP,
    rejected_by               STRING,
    rejected_at               TIMESTAMP,
    rejection_reason          STRING,
    expires_at                TIMESTAMP,
    approval_token_hash       STRING
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
        "WHERE table_schema = 'dmz' AND table_name = 'dataeditor_change_requests'"
    )
    print("table exists:", not df.empty)


if __name__ == "__main__":
    main()
