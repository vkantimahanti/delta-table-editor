"""Generate a CSV for bulk append upload (default: 1000 rows)."""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
SCHEMA = "your_schema"
TABLE = "sample_entity"


def api_get(path: str) -> object:
    with urllib.request.urlopen(BASE + path, timeout=300) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else None


def api_post(path: str, body: dict) -> object:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--rows", type=int, default=1000)
    parser.add_argument("-o", "--output", default="test_upload_1000.csv")
    args = parser.parse_args()

    try:
        api_get("/api/health")
    except urllib.error.URLError as exc:
        print(f"Backend not reachable at {BASE}: {exc}", file=sys.stderr)
        return 1

    dropdowns = api_get(f"/api/tables/{SCHEMA}/{TABLE}/dropdowns")
    carriers = (dropdowns or {}).get("dropdowns", {}).get("Carrier") or []
    sub_meta = (dropdowns or {}).get("dependent", {}).get("SubCarrier") or {}
    options_by_parent = sub_meta.get("options_by_parent") or {}
    carrier = None
    subcarrier = ""
    for c in carriers:
        opts = options_by_parent.get(c) or []
        if opts:
            carrier, subcarrier = c, opts[0]
            break
    if not carrier:
        print("No Carrier/SubCarrier pair found in dropdowns.", file=sys.stderr)
        return 1

    data = api_get(f"/api/tables/{SCHEMA}/{TABLE}/data?page=1&page_size=1")
    rows = (data or {}).get("rows") or []
    start_id = 900000
    if rows and rows[0].get("CarrierID") is not None:
        try:
            max_row = api_post(
                f"/api/tables/{SCHEMA}/{TABLE}/filter?catalog=your_catalog&page=1&page_size=1",
                [{"column": "CarrierID", "op": "order_desc"}],
            )
            top = (max_row or {}).get("rows") or rows
            start_id = int(top[0]["CarrierID"]) + 1
        except Exception:
            start_id = int(rows[0]["CarrierID"]) + 10000

    fieldnames = ["CarrierID", "Carrier", "SubCarrier", "Market"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for i in range(args.rows):
        writer.writerow({
            "CarrierID": start_id + i,
            "Carrier": carrier,
            "SubCarrier": subcarrier,
            "Market": f"E2E_BULK_{start_id + i}",
        })

    with open(args.output, "w", encoding="utf-8", newline="") as fh:
        fh.write(buf.getvalue())

    print(f"Wrote {args.rows} rows to {args.output}")
    print(f"Carrier={carrier!r} SubCarrier={subcarrier!r} CarrierID range={start_id}..{start_id + args.rows - 1}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
