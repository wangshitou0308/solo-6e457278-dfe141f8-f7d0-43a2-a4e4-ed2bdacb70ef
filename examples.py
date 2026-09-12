#!/usr/bin/env python3
"""End-to-end demo of the change-ringing validation API.

Start the server first:   python3 server.py --port 8000
Then run:                 python3 examples.py [base_url]

Creates method versions, runs analyses (including a bob override), compares
two versions, composes a spliced touch across methods and downloads reports
as JSON.
"""

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

PB_MINOR = "x.16.x.16.x.16.x.16.x.16.x.12"          # 12 lead end (plain)
PB_MINOR_COMMA = "x16x16x16,12"                     # same lead, abbreviated
PB_MINOR_14 = "x.16.x.16.x.16.x.16.x.16.x.14"       # 14 at every lead end
# 10 and 12 bells: "10" = places 1 AND 10, "1T" = places 1 AND 12
PB_ROYAL = "x10x10x10x10x10,12"      # Plain Bob Royal: 20-change lead
PB_MAXIMUS = "x1Tx1Tx1Tx1Tx1Tx1T,12"  # Plain Bob Maximus: 24-change lead
ROUNDS_10 = "1234567890"
ROUNDS_12 = "1234567890ET"


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

    # 9. spliced touch: 2 leads of v1 -> 2 leads of v2 (with a bob in the
    #    segment) -> 1 lead of v1; each segment continues from the previous
    #    segment's last row, switches happen only at lead boundaries
    t1 = call("POST", "/api/touches", {"segments": [
        {"method_id": m1["id"], "leads": 2},
        {"method_id": m2["id"], "leads": 2,
         "overrides": [{"lead": 1, "change": 12, "notation": "12"}]},
        {"method_id": m1["id"], "leads": 1}]})
    show("spliced touch: PB Minor -> PB Minor (14 lead end) -> PB Minor",
         t1, ["id", "status", "closed", "total_rows", "total_leads",
              "segment_count", "problems", "methods_used"])

    # 10. switch points and per-segment rows
    full = call("GET", f"/api/touches/{t1['id']}")
    print("\n=== switch points (before/after rows) ===")
    for sw in full["report"]["switches"]:
        print(f"  row {sw['at_index']:>2}: segment {sw['from_segment']} "
              f"({sw['from_method']} v{sw['from_version']}) -> "
              f"segment {sw['to_segment']} ({sw['to_method']} v{sw['to_version']})"
              f"  {sw['before_row']} -> {sw['after_row']}")
    rows = call("GET", f"/api/touches/{t1['id']}/rows?segment=2&from=25&to=30")
    print("\n=== segment 2, rows 25-30 (override source marked) ===")
    for r in rows["rows"]:
        src = f"  override {r['override']['notation']}" if r["override"] else ""
        print(f"  row {r['index']:>2}  seg {r['segment']}  "
              f"{r['method']} v{r['version']}  lead {r['lead']}  "
              f"change {r['change']:>2}  {r['row']}  {r['token']}{src}")

    # 11. a second touch (plain course as one segment) and compare
    t2 = call("POST", "/api/touches",
              {"segments": [{"method_id": m1["id"], "leads": 5}]})
    cmp = call("GET", f"/api/touches/compare?a={t1['id']}&b={t2['id']}")
    show("compare spliced touch vs plain-course touch", cmp["comparison"],
         ["same_stage", "total_rows_equal", "both_closed", "both_true",
          "methods_overlap"])

    # 12. validation errors are located to the segment and nothing is stored
    gd = call("POST", "/api/methods",
              {"name": "Grandsire Doubles", "stage": 5,
               "notation": "3.1.5.1.5.1.5.1.5.125"})
    try:
        call("POST", "/api/touches", {"segments": [
            {"method_id": m1["id"], "leads": 1},
            {"method_id": gd["id"], "leads": 1}]})   # 5 bells vs 6
    except urllib.error.HTTPError as e:
        show("stage mismatch reported against segment 2",
             json.loads(e.read().decode()))

    # 13. download the touch report as JSON
    req = urllib.request.Request(BASE + f"/api/touches/{t1['id']}/download")
    with urllib.request.urlopen(req) as resp:
        payload = resp.read()
        fname = resp.headers["Content-Disposition"].split("filename=")[-1].strip('"')
    with open(fname, "wb") as fh:
        fh.write(payload)
    print(f"\ndownloaded touch report -> {fname} ({len(payload)} bytes)")

    # 14. 10 bells (Royal): "10" in place notation is places 1 AND 10; rows
    #     write bell 10 as '0', e.g. rounds "1234567890"
    royal = call("POST", "/api/methods",
                 {"name": "Plain Bob Royal", "stage": 10,
                  "notation": PB_ROYAL, "start_row": ROUNDS_10})
    show("method: Plain Bob Royal (10 bells)", royal,
         ["id", "stage", "version", "lead_length"])
    print("first places token completed:", royal["changes"][1]["completed"],
          "= places", royal["changes"][1]["places"], "(1 AND 10, never 'ten')")
    # 10! exceeds the hard cap: creating an analysis without max_rows is refused
    try:
        call("POST", "/api/analyses", {"method_id": royal["id"]})
    except urllib.error.HTTPError as e:
        show("refused: 10! > hard limit, explicit max_rows required",
             json.loads(e.read().decode()))
    # an explicit cap within 1,000,000 runs the whole 180-row true course
    ar = call("POST", "/api/analyses",
              {"method_id": royal["id"], "max_rows": 100000})
    show("analysis: Plain Bob Royal plain course", ar,
         ["status", "lead_length", "lead_head", "period_rows", "truth"])

    # 15. 12 bells (Maximus): capped to 240 checked rows -> not closed and no
    #     full truth verdict ("truth.true" is null, only the range is claimed)
    maximus = call("POST", "/api/methods",
                   {"name": "Plain Bob Maximus", "stage": 12,
                    "notation": PB_MAXIMUS, "start_row": ROUNDS_12})
    ac = call("POST", "/api/analyses",
              {"method_id": maximus["id"], "max_rows": 240})
    show("analysis: Plain Bob Maximus, first 240 rows checked", ac,
         ["status", "closed", "rows_generated", "truth", "problems"])
    # cap high enough: the 264-row plain course closes and is true
    af = call("POST", "/api/analyses",
              {"method_id": maximus["id"], "max_rows": 1000})
    show("analysis: Plain Bob Maximus plain course", af,
         ["status", "lead_head", "period_rows", "truth"])

    # 16. located errors at 12 bells: a compact row with bell E repeated
    try:
        call("POST", "/api/methods",
             {"name": "Bad 12-bell row", "stage": 12, "notation": "x",
              "start_row": "1234567890EE"})
    except urllib.error.HTTPError as e:
        show("duplicate bell in a compact row is located",
             json.loads(e.read().decode()))

    # 17. spliced touch on 12 bells (two leads, explicit cap)
    t3 = call("POST", "/api/touches", {"max_rows": 1000, "segments": [
        {"method_id": maximus["id"], "leads": 1},
        {"method_id": maximus["id"], "leads": 1}]})
    full = call("GET", f"/api/touches/{t3['id']}")
    print("\n=== 12-bell spliced touch: switch point in 0/E/T symbols ===")
    for sw in full["report"]["switches"]:
        print(f"  row {sw['at_index']}: {sw['before_row']} -> {sw['after_row']}")


if __name__ == "__main__":
    main()
