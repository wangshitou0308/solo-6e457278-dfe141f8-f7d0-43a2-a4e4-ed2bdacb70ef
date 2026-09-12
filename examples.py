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

    # 18. musicality scoring: a weighted scheme version for the 6-bell
    #     analyses created above (runs, a bell sequence and a whole row)
    scheme = call("POST", "/api/schemes", {
        "name": "minor-music", "stage": 6, "rules": [
            {"id": "front-up", "name": "front run up >=4", "kind": "run",
             "direction": "up", "position": "front", "min_length": 4,
             "weight": 2},
            {"id": "back-down", "name": "back run down >=4", "kind": "run",
             "direction": "down", "position": "back", "min_length": 4},
            {"id": "queens", "name": "135 at the front", "kind": "sequence",
             "bells": "135", "position": "front"},
            {"id": "rounds-hand", "name": "rounds (handstroke)", "kind": "row",
             "row": "123456", "strokes": ["hand"]},
            {"id": "le-runs", "name": "lead-end runs", "kind": "run",
             "direction": "both", "position": "any", "min_length": 4,
             "lead_end": "lead_end", "weight": 3}]})
    show("scheme version (same name posted again bumps 'version')", scheme,
         ["id", "name", "version", "rule_count", "scoring_version"])
    # score the plain-course analysis (row index 0 is always handstroke)
    mu = call("POST", "/api/music",
              {"analysis_id": a1["id"], "scheme_id": scheme["id"]})
    print("\n=== music: Plain Bob Minor plain course ===")
    print(f"  total {mu['total_hits']} hits, score {mu['total_score']}, "
          f"partial={mu['partial']}, checked_rows={mu['checked_rows']}")
    for r in mu["rule_scores"]:
        print(f"  {r['name']:<26} {r['hits']:>2} hits  {r['score']:>3} pts")
    full = call("GET", f"/api/music/{mu['id']}")
    print("  handstroke lead-end hits:")
    for h in full["result"]["hits"]:
        if h["stroke"] == "hand" and h["lead_end"]:
            print(f"    row {h['index']:>2} ({h['rule']}) {h['row']} "
                  f"matched {h['matched']} +{h['score']}")

    # 19. capped analysis -> partial score over the checked rows only
    acap = call("POST", "/api/music",
                {"analysis_id": a2["id"], "scheme_id": scheme["id"]})
    show("music of the bob analysis (may be partial under a row cap)", acap,
         ["partial", "rows_analyzed", "checked_rows", "total_hits",
          "total_score", "truth"])

    # 20. hit filtering: one rule / one stroke / an index slice
    hits = call("GET", f"/api/music/{mu['id']}/hits"
                       "?rule_id=front-up&stroke=back")
    print("\n=== backstroke front-up run hits ===")
    for h in hits["hits"]:
        print(f"  row {h['index']:>2} {h['row']} run={h['matched']} "
              f"lead {h['lead']} change {h['change']} +{h['score']}")

    # 21. compare two music results (same stage, same scheme version)
    cmp = call("GET", f"/api/music/compare?a={mu['id']}&b={acap['id']}")
    print("\n=== compare plain-course music vs bob analysis ===")
    c = cmp["comparison"]
    print(f"  comparable={c['comparable']}  score delta "
          f"{c['total_score_delta']:+}  hits delta {c['total_hits_delta']:+}")
    for r in c["rules"]:
        print(f"  {r['name']:<26} {r['a']['score']:>3} -> {r['b']['score']:>3}"
              f"  ({r['score_delta']:+})")

    # 22. touch scoring with a segment-restricted rule
    tscheme = call("POST", "/api/schemes", {
        "name": "touch-music", "stage": 6, "rules": [
            {"id": "runs", "name": "four-runs anywhere", "kind": "run",
             "direction": "both", "position": "any", "min_length": 4},
            {"id": "seg2", "name": "segment 2 only", "kind": "run",
             "direction": "both", "position": "any", "min_length": 4,
             "segments": [2]}]})
    tmu = call("POST", "/api/music",
               {"touch_id": t1["id"], "scheme_id": tscheme["id"]})
    seg_hits = call("GET", f"/api/music/{tmu['id']}/hits?segment=2")
    show("touch music (segment-restricted rules stay zero elsewhere)",
         {"total_hits": tmu["total_hits"], "total_score": tmu["total_score"],
          "rule_scores": tmu["rule_scores"],
          "segment_2_hits": seg_hits["matched"]})

    # 23. a scheme-version mismatch is refused as incomparable
    scheme2 = call("POST", "/api/schemes", {
        "name": "minor-music", "stage": 6, "rules": [
            {"name": "rounds only", "kind": "row", "row": "123456"}]})
    mu_v2 = call("POST", "/api/music",
                 {"analysis_id": a1["id"], "scheme_id": scheme2["id"]})
    try:
        call("GET", f"/api/music/compare?a={mu['id']}&b={mu_v2['id']}")
    except urllib.error.HTTPError as e:
        show("compare across scheme versions refused (incomparable)",
             json.loads(e.read().decode()), ["code", "reasons"])

    # 24. download the music result as a self-contained JSON
    req = urllib.request.Request(BASE + f"/api/music/{mu['id']}/download")
    with urllib.request.urlopen(req) as resp:
        payload = resp.read()
        fname = resp.headers["Content-Disposition"].split("filename=")[-1].strip('"')
    with open(fname, "wb") as fh:
        fh.write(payload)
    print(f"\ndownloaded music result -> {fname} ({len(payload)} bytes)")


if __name__ == "__main__":
    main()
