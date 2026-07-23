"""End-to-end API test for insert, update, delete, bulk upload, export, approval, overview."""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("TEST_API_BASE", "http://127.0.0.1:8000")
TIMEOUT = 600
CATALOG = "your_catalog"
APPROVER = "approver@example.com"
SUBMITTER = "submitter@example.com"

FI_SCHEMA = "your_schema"
FI_TABLE = "sample_entity"
MC_SCHEMA = "your_schema"
MC_TABLE = "sample_entity"


def req(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    email: str = SUBMITTER,
) -> tuple[int, object]:
    headers = {
        "Content-Type": "application/json",
        "X-Forwarded-Email": email,
    }
    data = json.dumps(body).encode() if body is not None else None
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


def step(report: dict, name: str, code: int, detail: object, *, ok: bool | None = None) -> bool:
    entry = {"name": name, "code": code, "ok": ok if ok is not None else (200 <= code < 300)}
    if isinstance(detail, dict):
        entry.update({k: detail[k] for k in ("change_request_id", "status", "requires_approval", "review_url", "mode") if k in detail})
        entry["detail"] = detail
    else:
        entry["detail"] = detail
    report["steps"].append(entry)
    return bool(entry["ok"])


def get_dropdown_pair() -> tuple[str, str]:
    code, dd = req("GET", f"/api/tables/{FI_SCHEMA}/{FI_TABLE}/dropdowns")
    if code != 200:
        raise RuntimeError(f"dropdowns failed: {dd}")
    carriers = (dd or {}).get("dropdowns", {}).get("Carrier") or []
    sub_meta = (dd or {}).get("dependent", {}).get("SubCarrier") or {}
    options_by_parent = sub_meta.get("options_by_parent") or {}
    for carrier in carriers:
        subs = options_by_parent.get(carrier) or []
        if subs:
            return carrier, subs[0]
    raise RuntimeError("No carrier/subcarrier pair found in dropdowns")


def next_carrier_id() -> int:
    code, data = req("GET", f"/api/tables/{FI_SCHEMA}/{FI_TABLE}/data?page=1&page_size=5")
    if code != 200:
        raise RuntimeError(data)
    rows = (data or {}).get("rows") or []
    if not rows:
        return 900001
    return max(int(r["CarrierID"]) for r in rows if r.get("CarrierID") is not None) + 1


def main() -> int:
    report: dict[str, object] = {"steps": [], "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    all_ok = True

    code, health = req("GET", "/api/health")
    all_ok &= step(report, "1_health", code, health)

    code, overview_before = req("GET", "/api/overview")
    all_ok &= step(report, "2_overview_before", code, {
        "pending_approvals": (overview_before or {}).get("metrics", {}).get("pending_approvals"),
    })

    carrier, subcarrier = get_dropdown_pair()
    new_id = next_carrier_id()

    # 1) Insert via grid stage/apply
    insert_vals = {
        "Carrier": carrier,
        "SubCarrier": subcarrier,
        "Market": f"E2E_INSERT_{new_id}",
    }
    code, stage_ins = req(
        "POST",
        f"/api/tables/{FI_SCHEMA}/{FI_TABLE}/edits/stage",
        {"catalog": CATALOG, "updates": [], "inserts": [{"values": insert_vals}]},
    )
    all_ok &= step(report, "3_insert_stage", code, stage_ins)
    cr_ins = (stage_ins or {}).get("change_request_id") if isinstance(stage_ins, dict) else None
    if cr_ins and (stage_ins or {}).get("can_apply"):
        code, apply_ins = req(
            "POST",
            f"/api/tables/{FI_SCHEMA}/{FI_TABLE}/edits/apply",
            {"change_request_id": cr_ins},
        )
        all_ok &= step(report, "4_insert_apply", code, apply_ins)

    code, verify_ins = req("GET", f"/api/tables/{FI_SCHEMA}/{FI_TABLE}/data?page=1&page_size=500")
    inserted = None
    if isinstance(verify_ins, dict):
        inserted = next(
            (r for r in verify_ins.get("rows", []) if str(r.get("Market")) == insert_vals["Market"]),
            None,
        )
    all_ok &= step(report, "5_insert_verify", code, {
        "found": inserted is not None,
        "carrier_id": (inserted or {}).get("CarrierID"),
    }, ok=inserted is not None)

    if not inserted:
        print(json.dumps(report, indent=2, default=str))
        return 1

    pk_id = inserted["CarrierID"]
    market_new = f"E2E_UPDATE_{pk_id}"

    # 2) Update
    code, stage_upd = req(
        "POST",
        f"/api/tables/{FI_SCHEMA}/{FI_TABLE}/edits/stage",
        {
            "catalog": CATALOG,
            "updates": [{"original": inserted, "edits": {"Market": market_new}}],
            "inserts": [],
        },
    )
    all_ok &= step(report, "6_update_stage", code, stage_upd)
    cr_upd = (stage_upd or {}).get("change_request_id") if isinstance(stage_upd, dict) else None
    if cr_upd and (stage_upd or {}).get("can_apply"):
        code, apply_upd = req(
            "POST",
            f"/api/tables/{FI_SCHEMA}/{FI_TABLE}/edits/apply",
            {"change_request_id": cr_upd},
        )
        all_ok &= step(report, "7_update_apply", code, apply_upd)

    code, verify_upd = req("GET", f"/api/tables/{FI_SCHEMA}/{FI_TABLE}/data?page=1&page_size=500")
    updated = None
    if isinstance(verify_upd, dict):
        updated = next((r for r in verify_upd.get("rows", []) if str(r.get("CarrierID")) == str(pk_id)), None)
    all_ok &= step(report, "8_update_verify", code, {
        "market": (updated or {}).get("Market"),
        "expected": market_new,
    }, ok=(updated or {}).get("Market") == market_new)

    # 3) Delete
    code, del_res = req(
        "DELETE",
        f"/api/tables/{FI_SCHEMA}/{FI_TABLE}/row?soft=false",
        {"CarrierID": pk_id},
    )
    all_ok &= step(report, "9_delete", code, del_res)
    code, verify_del = req("GET", f"/api/tables/{FI_SCHEMA}/{FI_TABLE}/data?page=1&page_size=500")
    still_there = False
    if isinstance(verify_del, dict):
        still_there = any(str(r.get("CarrierID")) == str(pk_id) for r in verify_del.get("rows", []))
    all_ok &= step(report, "10_delete_verify", code, {"still_present": still_there}, ok=not still_there)

    # 4) Upload 1000 rows (append)
    bulk_start = next_carrier_id()
    fieldnames = ["CarrierID", "Carrier", "SubCarrier", "Market"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for i in range(1000):
        writer.writerow({
            "CarrierID": bulk_start + i,
            "Carrier": carrier,
            "SubCarrier": subcarrier,
            "Market": f"E2E_BULK_{bulk_start + i}",
        })
    csv_text = buf.getvalue()

    code, val_bulk = req(
        "POST",
        f"/api/tables/{FI_SCHEMA}/{FI_TABLE}/upload/validate",
        {
            "catalog": CATALOG,
            "csv_text": csv_text,
            "delimiter": ",",
            "has_header": True,
            "filename": "e2e_bulk_1000.csv",
            "mode": "append",
        },
    )
    all_ok &= step(report, "11_upload_validate_1000", code, val_bulk)
    cr_bulk = (val_bulk or {}).get("change_request_id") if isinstance(val_bulk, dict) else None
    if cr_bulk and (val_bulk or {}).get("can_apply"):
        code, apply_bulk = req(
            "POST",
            f"/api/tables/{FI_SCHEMA}/{FI_TABLE}/upload/apply",
            {"change_request_id": cr_bulk},
        )
        all_ok &= step(report, "12_upload_apply_1000", code, apply_bulk)

    # 5) Export
    code, export_res = req(
        "POST",
        f"/api/tables/{FI_SCHEMA}/{FI_TABLE}/export",
        {
            "catalog": CATALOG,
            "format": "csv",
            "columns": ["CarrierID", "Carrier", "SubCarrier", "Market"],
            "filters": [{"column": "Market", "op": "starts_with", "value": "E2E_BULK_"}],
            "filter_snapshot": {},
        },
    )
    all_ok &= step(report, "13_export", code, export_res)

    # 6) Approval workflow on sample entity
    code, mc_data = req("GET", f"/api/tables/{MC_SCHEMA}/{MC_TABLE}/data?page=1&page_size=5")
    mc_rows = (mc_data or {}).get("rows", []) if isinstance(mc_data, dict) else []
    if not mc_rows:
        step(report, "14_approval_skip", 0, "No sample entity rows to update", ok=False)
        all_ok = False
    else:
        mc_row = mc_rows[0]
        pk_col = "id"
        old_abbr = str(mc_row.get("clientabbreviation") or "")
        new_abbr = old_abbr if old_abbr.endswith("_E2E") else (old_abbr + "_E2E")[:50]

        code, stage_mc = req(
            "POST",
            f"/api/tables/{MC_SCHEMA}/{MC_TABLE}/edits/stage",
            {
                "catalog": CATALOG,
                "updates": [{"original": mc_row, "edits": {"clientabbreviation": new_abbr}}],
                "inserts": [],
            },
            email=SUBMITTER,
        )
        all_ok &= step(report, "14_approval_stage", code, stage_mc)
        cr_mc = (stage_mc or {}).get("change_request_id") if isinstance(stage_mc, dict) else None
        needs_approval = bool((stage_mc or {}).get("requires_approval"))
        review_url = (stage_mc or {}).get("review_url")
        step(report, "15_approval_queued", 200 if needs_approval else 422, {
            "requires_approval": needs_approval,
            "review_url": review_url,
            "note": "Email is log-only locally — copy review_url from backend logs or Approvals tab",
        }, ok=needs_approval)

        if cr_mc and needs_approval:
            code, approve_res = req(
                "POST",
                f"/api/change-requests/{cr_mc}/approve",
                {},
                email=APPROVER,
            )
            all_ok &= step(report, "16_approval_approve", code, approve_res)
            code, apply_mc = req(
                "POST",
                f"/api/tables/{MC_SCHEMA}/{MC_TABLE}/edits/apply",
                {"change_request_id": cr_mc},
                email=APPROVER,
            )
            all_ok &= step(report, "17_approval_apply", code, apply_mc)

            # Revert abbreviation (no approval needed if we restore original? still needs approval)
            code, stage_revert = req(
                "POST",
                f"/api/tables/{MC_SCHEMA}/{MC_TABLE}/edits/stage",
                {
                    "catalog": CATALOG,
                    "updates": [{
                        "original": {**mc_row, "clientabbreviation": new_abbr},
                        "edits": {"clientabbreviation": old_abbr},
                    }],
                    "inserts": [],
                },
                email=SUBMITTER,
            )
            cr_rev = (stage_revert or {}).get("change_request_id") if isinstance(stage_revert, dict) else None
            if cr_rev and (stage_revert or {}).get("requires_approval"):
                req("POST", f"/api/change-requests/{cr_rev}/approve", {}, email=APPROVER)
                req(
                    "POST",
                    f"/api/tables/{MC_SCHEMA}/{MC_TABLE}/edits/apply",
                    {"change_request_id": cr_rev},
                    email=APPROVER,
                )

    # 7) Overview after changes
    code, overview_after = req("GET", "/api/overview?refresh=true")
    metrics = (overview_after or {}).get("metrics", {}) if isinstance(overview_after, dict) else {}
    all_ok &= step(report, "18_overview_after", code, {
        "pending_approvals": metrics.get("pending_approvals"),
        "recent_edits_count": len((overview_after or {}).get("recent_edits") or []),
        "recent_requests_count": len((overview_after or {}).get("recent_requests") or []),
    })

    report["passed"] = all_ok
    print(json.dumps(report, indent=2, default=str))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
