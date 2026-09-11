#!/usr/bin/env python3
"""End-to-end demo of the change-ringing validation API.

Start the server first:   python3 server.py --port 8000
Then run:                 python3 examples.py [base_url]

Creates method versions, runs analyses (including a bob override), compares
two versions and downloads a report as JSON.
"""

import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

PB_MINOR = "x.16.x.16.x.16.x.16.x.16.x.12"          # 12 lead end (plain)
PB_MINOR_COMMA = "x16x16x16,12"                     # same lead, abbreviated
PB_MINOR_14 = "x.16.x.16.x.16.x.16.x.16.x.14"       # 14 at every lead end


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data) as resp:
        return json.loads(resp.read().decode())


def show(title, obj, keys=None):
    print(f"\n=== {title} ===")
    if keys:
        obj = {k: obj.get(k) for k in keys}
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main():
    print(f"API base: {BASE}")

    # 1. parse only (no storage): the standard comma abbreviation expands
    #    to the 12 changes of Plain Bob Minor
    parsed = call("POST", "/api/parse", {"stage": 6, "notation": PB_MINOR_COMMA})
    show("parse: 'x16x16x16,12' (comma symmetric expansion)",
         parsed, ["lead_length"])
    print("changes:", " ".join(c["completed"] for c in parsed["changes"]))

    # 2. a located notation error (never auto-corrected)
    try:
        call("POST", "/api/parse", {"stage": 6, "notation": "x.13.16"})
    except urllib.error.HTTPError as e:
        show("error report for 'x.13.16' on 6 bells",
             json.loads(e.read().decode()))

    # 3. create two versions of Plain Bob Minor
    m1 = call("POST", "/api/methods",
              {"name": "Plain Bob Minor", "stage": 6, "notation": PB_MINOR})
    m2 = call("POST", "/api/methods",
              {"name": "Plain Bob Minor", "stage": 6, "notation": PB_MINOR_14})
    show("method versions", [{"id": m1["id"], "version": m1["version"]},
                             {"id": m2["id"], "version": m2["version"]}])

    # 4. plain-course analysis of version 1
    a1 = call("POST", "/api/analyses", {"method_id": m1["id"]})
    show("analysis: Plain Bob Minor plain course", a1,
         ["id", "status", "lead_length", "lead_head", "period_leads",
          "period_rows", "hunt_bells", "truth", "problems"])

    # 5. composition: bob 14 at lead 1, change 12 (before/after kept)
    a2 = call("POST", "/api/analyses",
              {"method_id": m1["id"],
               "overrides": [{"lead": 1, "change": 12, "notation": "14"}]})
    full = call("GET", f"/api/analyses/{a2['id']}")
    show("analysis with bob 14 at lead 1 change 12",
         full["report"]["overrides"][0],
         ["lead", "change", "notation", "replaces_token", "applied",
          "before_row", "after_row", "row_index"])

    # 6. row-by-row view of the first lead
    rows = call("GET", f"/api/analyses/{a1['id']}/rows?from=0&to=12")
    print("\n=== first lead, row by row ===")
    for r in rows["rows"]:
        mark = " (override)" if r["override"] else ""
        print(f"  row {r['index']:>2}  lead {r['lead']}  change {r['change']:>2}"
              f"  {r['row']}  {r['token'] or ''}{mark}")

    # 7. compare the two versions (periods + repeat positions)
    a3 = call("POST", "/api/analyses", {"method_id": m2["id"]})
    cmp = call("GET", f"/api/compare?a={a1['id']}&b={a3['id']}")
    show("compare v1 (12 lead end) vs v2 (14 lead end)", cmp)

    # 8. download a report as JSON
    req = urllib.request.Request(BASE + f"/api/analyses/{a1['id']}/download")
    with urllib.request.urlopen(req) as resp:
        payload = resp.read()
        fname = resp.headers["Content-Disposition"].split("filename=")[-1].strip('"')
    with open(fname, "wb") as fh:
        fh.write(payload)
    print(f"\ndownloaded report -> {fname} ({len(payload)} bytes)")


if __name__ == "__main__":
    main()
