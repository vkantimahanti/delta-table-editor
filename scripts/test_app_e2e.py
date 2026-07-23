"""Quick local end-to-end API test for Data Canvas."""
from __future__ import annotations

import csv
import io
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
TIMEOUT = 300
SCHEMA, TABLE = "dmz", "dash_test_carrier"


def req(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw


def row_to_csv(row: dict, *, notes_override: str | None = None) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(row.keys()), lineterminator="\n")
    writer.writeheader()
    out = dict(row)
    if notes_override is not None:
        for key in out:
            if key.lower() == "notes":
                out[key] = notes_override
                break
    writer.writerow({k: ("" if v is None else v) for k, v in out.items()})
    return buf.getvalue()


def main() -> int:
    report: dict[str, object] = {"steps": []}

    code, health = req("GET", "/api/health")
    report["steps"].append({"name": "health", "code": code, "status": health})
    if code != 200:
        print(json.dumps(report, indent=2, default=str))
        return 1

    code, tables = req("GET", "/api/tables")
    names = []
    if isinstance(tables, list):
        names = [t.get("table_name") for t in tables]
    report["steps"].append({"name": "tables", "code": code, "count": len(names), "has_test_table": TABLE in names})

    code, data = req("GET", f"/api/tables/{SCHEMA}/{TABLE}/data?page=1&page_size=5")
    rows = data.get("rows", []) if isinstance(data, dict) else []
    report["steps"].append({
        "name": "data",
        "code": code,
        "total_rows": data.get("total_rows") if isinstance(data, dict) else None,
        "page_rows": len(rows),
    })
    if not rows:
        print(json.dumps(report, indent=2, default=str))
        return 1

    row = rows[0]
    pk = row.get("carrierid")
    notes_old = row.get("notes") or ""
    notes_new = notes_old if notes_old.endswith(" [e2e-test]") else (notes_old + " [e2e-test]").strip()

    validate_payload = {
        "catalog": "your_catalog",
        "csv_text": row_to_csv(row, notes_override=notes_new),
        "delimiter": ",",
        "has_header": True,
        "filename": "e2e_test_update.csv",
        "mode": "update",
    }
    code, validate = req("POST", f"/api/tables/{SCHEMA}/{TABLE}/upload/validate", validate_payload)
    report["steps"].append({"name": "validate", "code": code, "result": validate})
    if code != 200 or not isinstance(validate, dict) or not validate.get("can_apply"):
        print(json.dumps(report, indent=2, default=str))
        return 1

    cr_id = validate["change_request_id"]
    code, apply_res = req(
        "POST",
        f"/api/tables/{SCHEMA}/{TABLE}/upload/apply",
        {"change_request_id": cr_id},
    )
    report["steps"].append({"name": "apply", "code": code, "result": apply_res})

    code, cr = req("GET", f"/api/change-requests/{cr_id}")
    report["steps"].append({"name": "change_request", "code": code, "status": cr.get("status") if isinstance(cr, dict) else cr})

    code, data2 = req("GET", f"/api/tables/{SCHEMA}/{TABLE}/data?page=1&page_size=50")
    rows2 = data2.get("rows", []) if isinstance(data2, dict) else []
    matched = next((r for r in rows2 if str(r.get("carrierid")) == str(pk)), None)
    notes_after = matched.get("notes") if matched else None
    report["steps"].append({
        "name": "verify_notes",
        "code": code,
        "pk": pk,
        "ok": notes_after == notes_new,
        "expected": notes_new,
        "actual": notes_after,
    })

    code, root = req("GET", "/")
    report["steps"].append({"name": "frontend_root", "code": code, "has_html": isinstance(root, str) and "<html" in root.lower()})

    ok = all(
        step.get("ok", True)
        for step in report["steps"]
        if step["name"] in ("verify_notes",)
    ) and report["steps"][0]["code"] == 200
    report["passed"] = ok
    print(json.dumps(report, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
