#!/usr/bin/env python3
"""Test suite for the change-ringing validator: core engine + HTTP API.

Run:  python3 tests.py  (or: python3 -m unittest tests -v)
"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from db import Store
from ringing import (NotationError, SplicedError, analyze, analyze_spliced,
                     compare_reports, compare_touch_reports, parse_notation,
                     parse_row, row_str)
from server import make_server

# Plain Bob Minor: 12-change lead, lead head 135264, 5 leads = 60 rows, true.
PB_MINOR = "x.16.x.16.x.16.x.16.x.16.x.12"
# 14 at every lead end: 3 leads = 36 rows, lead head 123564, true.
PB_MINOR_14 = "x.16.x.16.x.16.x.16.x.16.x.14"


def _seg(method_id, notation, leads, stage=6, name=None, version=1,
         overrides=None):
    return {"method_id": method_id, "name": name or f"M{method_id}",
            "version": version, "stage": stage, "notation": notation,
            "leads": leads, "overrides": overrides or []}


def completed_sequence(notation, stage):
    return [c.completed() for c in parse_notation(notation, stage)]


class ParseTests(unittest.TestCase):
    def test_cross_and_places(self):
        self.assertEqual(completed_sequence("x.16", 6), ["x", "16"])
        self.assertEqual(completed_sequence("-16-14", 6), ["x", "16", "x", "14"])
        self.assertEqual(completed_sequence("x16x14", 6), ["x", "16", "x", "14"])

    def test_dots_and_whitespace(self):
        self.assertEqual(completed_sequence(" x . 16 . ", 6), ["x", "16"])
        self.assertEqual(completed_sequence("..x..16..", 6), ["x", "16"])

    def test_comma_symmetric_expansion(self):
        # a,b -> a + reverse(a[:-1]) + b: the last change of a is the
        # half-lead pivot (mirrored, not repeated); b is the lead end.
        self.assertEqual(completed_sequence("x16x16x16,12", 6),
                         ["x", "16", "x", "16", "x", "16",
                          "x", "16", "x", "16", "x", "12"])
        self.assertEqual(completed_sequence("x.14,x.12", 4),
                         ["x", "14", "x", "x", "12"])
        self.assertEqual(completed_sequence("x,12", 4), ["x", "12"])

    def test_comma_form_of_plain_bob_minor(self):
        # the standard abbreviation x16x16x16,12 is exactly Plain Bob Minor
        rep = analyze(6, "x16x16x16,12")
        self.assertEqual(rep["lead_length"], 12)
        self.assertEqual(rep["lead_head"], "135264")
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["period_leads"], 5)
        self.assertEqual(rep["period_rows"], 60)
        self.assertTrue(rep["truth"]["true"])
        self.assertEqual(rep["hunt_bells"], [1])

    def test_place_completion(self):
        # inferable lead/lie places are completed from the stage
        self.assertEqual(completed_sequence("3", 6), ["36"])
        self.assertEqual(completed_sequence("1", 6), ["16"])
        self.assertEqual(completed_sequence("2", 6), ["12"])
        self.assertEqual(completed_sequence("5", 6), ["56"])
        self.assertEqual(completed_sequence("4", 6), ["14"])
        self.assertEqual(completed_sequence("23", 6), ["1236"])
        self.assertEqual(completed_sequence("3", 5), ["3"])   # odd stage, no fill
        self.assertEqual(completed_sequence("125", 5), ["125"])

    def test_swap_pairs(self):
        (change,) = parse_notation("14", 6)
        self.assertEqual(change.swaps, ((2, 3), (5, 6)))
        (change,) = parse_notation("x", 6)
        self.assertEqual(change.swaps, ((1, 2), (3, 4), (5, 6)))
        self.assertTrue(change.cross)

    def test_error_illegal_character_located(self):
        with self.assertRaises(NotationError) as ctx:
            parse_notation("x.1a.16", 6)
        err = ctx.exception
        self.assertEqual(err.token, "a")
        self.assertEqual(err.offset, 3)
        self.assertIn("illegal character", err.message)

    def test_error_place_out_of_range_located(self):
        with self.assertRaises(NotationError) as ctx:
            parse_notation("x.17", 6)
        self.assertEqual(ctx.exception.token, "17")
        self.assertEqual(ctx.exception.offset, 2)
        with self.assertRaises(NotationError):
            parse_notation("9", 8)
        with self.assertRaises(NotationError):
            parse_notation("0", 6)

    def test_error_duplicate_place_located(self):
        with self.assertRaises(NotationError) as ctx:
            parse_notation("x.11.16", 6)
        self.assertEqual(ctx.exception.token, "11")
        self.assertEqual(ctx.exception.offset, 2)

    def test_error_unpairable_located(self):
        # gap of one position between places 1 and 3 cannot pair up
        with self.assertRaises(NotationError) as ctx:
            parse_notation("13", 6)
        self.assertEqual(ctx.exception.token, "13")
        self.assertIn("adjacent swaps", ctx.exception.message)
        # cross on an odd stage cannot pair either
        with self.assertRaises(NotationError) as ctx:
            parse_notation("x", 5)
        self.assertEqual(ctx.exception.token, "x")

    def test_error_stage_and_empty(self):
        with self.assertRaises(NotationError):
            parse_notation("x", 3)
        with self.assertRaises(NotationError):
            parse_notation("x", 9)
        with self.assertRaises(NotationError):
            parse_notation("", 6)
        with self.assertRaises(NotationError):
            parse_notation("...", 6)

    def test_error_multiple_commas_located(self):
        with self.assertRaises(NotationError) as ctx:
            parse_notation("x.16,x.12,x.14", 6)
        self.assertEqual(ctx.exception.token, ",")
        self.assertEqual(ctx.exception.offset, 9)

    def test_parse_row_forms(self):
        self.assertEqual(parse_row(None, 6), (1, 2, 3, 4, 5, 6))
        self.assertEqual(parse_row("123456", 6), (1, 2, 3, 4, 5, 6))
        self.assertEqual(parse_row("2,1,4,3,6,5", 6), (2, 1, 4, 3, 6, 5))
        self.assertEqual(parse_row([2, 1, 4, 3, 6, 5], 6), (2, 1, 4, 3, 6, 5))
        with self.assertRaises(ValueError):
            parse_row("112345", 6)
        with self.assertRaises(ValueError):
            parse_row("12345", 6)


class AnalyzeTests(unittest.TestCase):
    def test_plain_bob_minor(self):
        rep = analyze(6, PB_MINOR)
        self.assertEqual(rep["status"], "ok")
        self.assertTrue(rep["closed"])
        self.assertEqual(rep["lead_length"], 12)
        self.assertEqual(rep["lead_head"], "135264")
        self.assertEqual(rep["period_leads"], 5)
        self.assertEqual(rep["period_rows"], 60)
        self.assertEqual(rep["hunt_bells"], [1])
        self.assertEqual(rep["working_bells"], [2, 3, 4, 5, 6])
        self.assertTrue(rep["truth"]["true"])
        self.assertIsNone(rep["truth"]["first_repeat"])
        self.assertEqual(rep["problems"], [])
        # lead heads of the plain course
        heads = [rep["rows"][i]["row"] for i in (0, 12, 24, 36, 48, 60)]
        self.assertEqual(heads, ["123456", "135264", "156342",
                                 "164523", "142635", "123456"])

    def test_untrue_marks_both_positions(self):
        rep = analyze(4, "x.14.x")
        self.assertEqual(rep["status"], "untrue")
        self.assertFalse(rep["truth"]["true"])
        repeat = rep["truth"]["first_repeat"]
        self.assertEqual(repeat["row"], "2413")
        self.assertEqual(repeat["first"], {"index": 2, "lead": 1, "change": 2})
        self.assertEqual(repeat["second"], {"index": 4, "lead": 2, "change": 1})
        self.assertIn("untrue", rep["problems"])
        # expansion continues past the first repeat to closure
        self.assertTrue(rep["closed"])
        self.assertEqual(rep["period_leads"], 2)
        self.assertEqual(rep["period_rows"], 6)
        self.assertEqual(len(rep["rows"]), 7)
        self.assertEqual(rep["rows"][-1]["row"], "1234")
        self.assertTrue(rep["rows"][4]["repeat"])
        self.assertTrue(rep["rows"][5]["repeat"])
        self.assertFalse(rep["rows"][6]["repeat"])  # the closing row itself

    def test_premature_rounds(self):
        rep = analyze(4, "x.x.x.x")
        self.assertEqual(rep["status"], "premature_rounds")
        self.assertEqual(rep["premature_rounds"],
                         {"index": 2, "lead": 1, "change": 2})
        # premature rounds is a truth violation too: rows 0 and 2 are marked
        self.assertFalse(rep["truth"]["true"])
        repeat = rep["truth"]["first_repeat"]
        self.assertEqual(repeat["row"], "1234")
        self.assertEqual(repeat["first"], {"index": 0, "lead": 0, "change": 0})
        self.assertEqual(repeat["second"], {"index": 2, "lead": 1, "change": 2})
        self.assertTrue(rep["rows"][2]["repeat"])
        # expansion continues to closure at the lead boundary
        self.assertTrue(rep["closed"])
        self.assertEqual(rep["period_leads"], 1)
        self.assertEqual(rep["period_rows"], 4)
        self.assertIn("premature_rounds", rep["problems"])
        self.assertIn("untrue", rep["problems"])

    def test_exceeded_limit_not_closed(self):
        rep = analyze(6, PB_MINOR, max_rows=10)
        self.assertEqual(rep["status"], "exceeded_limit")
        self.assertFalse(rep["closed"])
        self.assertIsNone(rep["period_rows"])
        self.assertEqual(rep["rows_generated"], 10)
        self.assertIn("exceeded_limit", rep["problems"])
        self.assertIn("not_closed", rep["problems"])

    def test_custom_start_row(self):
        rep = analyze(4, "x.x", start_row="2143")
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["period_rows"], 2)
        self.assertEqual(rep["start_row"], "2143")

    def test_untrue_and_never_closes(self):
        # one bob in Plain Bob Minor: rows repeat and the course never
        # returns to rounds within the extent
        rep = analyze(6, PB_MINOR,
                      overrides=[{"lead": 1, "change": 12, "notation": "14"}])
        self.assertEqual(rep["status"], "exceeded_limit")
        self.assertFalse(rep["closed"])
        self.assertIsNone(rep["period_rows"])
        repeat = rep["truth"]["first_repeat"]
        self.assertEqual(repeat["row"], "123564")
        self.assertEqual(repeat["first"]["index"], 12)
        self.assertEqual(repeat["second"]["index"], 72)
        self.assertEqual(rep["problems"],
                         ["untrue", "exceeded_limit", "not_closed"])
        self.assertEqual(rep["rows_generated"], 720)  # one full extent

    def test_override_keeps_surrounding_trajectory(self):
        rep = analyze(6, PB_MINOR,
                      overrides=[{"lead": 1, "change": 12, "notation": "14"}])
        (ovr,) = rep["overrides"]
        self.assertTrue(ovr["applied"])
        self.assertEqual(ovr["replaces_token"], "12")
        self.assertEqual(ovr["before_row"], "132546")  # row before the call
        self.assertEqual(ovr["after_row"], "123564")   # bob 14 instead of 12
        self.assertEqual(ovr["row_index"], 12)
        # the row entry is flagged and the trajectory around it is intact
        entry = rep["rows"][12]
        self.assertTrue(entry["override"])
        self.assertEqual(entry["token"], "14")
        self.assertEqual(rep["rows"][11]["row"], "132546")
        self.assertEqual(rep["rows"][12]["row"], "123564")
        self.assertEqual(rep["rows"][13]["row"], "215346")  # x after the bob

    def test_override_not_applied_when_lead_never_reached(self):
        rep = analyze(6, PB_MINOR,
                      overrides=[{"lead": 99, "change": 1, "notation": "14"}])
        self.assertFalse(rep["overrides"][0]["applied"])
        self.assertEqual(rep["period_rows"], 60)  # course unaffected

    def test_override_validation(self):
        with self.assertRaises(ValueError):
            analyze(6, PB_MINOR, overrides=[{"lead": 1, "change": 99, "notation": "14"}])
        with self.assertRaises(ValueError):
            analyze(6, PB_MINOR, overrides=[{"lead": 1, "change": 1, "notation": "x.16"}])
        with self.assertRaises(NotationError):
            analyze(6, PB_MINOR, overrides=[{"lead": 1, "change": 1, "notation": "1a"}])

    def test_grandsire_doubles_odd_stage(self):
        rep = analyze(5, "3.1.5.1.5.1.5.1.5.125")
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["lead_length"], 10)
        self.assertEqual(rep["lead_head"], "15423")
        self.assertEqual(rep["period_rows"], 40)  # 4 leads of 10
        self.assertTrue(rep["truth"]["true"])
        self.assertEqual(rep["hunt_bells"], [1])

    def test_compare_reports(self):
        a = analyze(6, PB_MINOR)
        b = analyze(6, "x.16.x.16.x.16.x.16.x.16.x.14")  # 14 lead end variant
        cmp = compare_reports(a, b)
        self.assertIn("period_rows_equal", cmp)
        self.assertEqual(cmp["a"]["period_rows"], 60)
        self.assertIn("first_repeat_same_position", cmp)


class SplicedTests(unittest.TestCase):
    def test_continuation_matches_plain_course(self):
        # PB Minor 2 leads + 3 leads: the second segment continues from the
        # first segment's last row (never resets to the method start_row),
        # so the splice is exactly the plain course.
        rep = analyze_spliced([_seg(1, PB_MINOR, 2), _seg(1, PB_MINOR, 3)])
        plain = analyze(6, PB_MINOR)
        self.assertEqual([r["row"] for r in rep["rows"]],
                         [r["row"] for r in plain["rows"]])
        self.assertEqual(rep["status"], "ok")
        self.assertTrue(rep["closed"])
        self.assertEqual(rep["total_rows"], 60)
        self.assertEqual(rep["total_leads"], 5)
        self.assertEqual(rep["problems"], [])
        # switch point keeps the rows around the lead boundary
        (sw,) = rep["switches"]
        self.assertEqual(sw["at_index"], 24)
        self.assertEqual(sw["before_row"], "156342")   # lead head after 2 leads
        self.assertEqual(sw["after_row"], "513624")    # plain-course row 25
        # method usage aggregated across both segments
        self.assertEqual(rep["methods_used"],
                         [{"method_id": 1, "name": "M1", "version": 1,
                           "leads": 5, "rows": 60, "segments": [1, 2]}])
        self.assertEqual(rep["segments"][0]["start_row"], "123456")
        self.assertEqual(rep["segments"][0]["end_row"], "156342")
        self.assertEqual(rep["segments"][1]["end_row"], "123456")

    def test_cross_method_splice_rows_tagged(self):
        rep = analyze_spliced([_seg(1, PB_MINOR, 2, name="PB"),
                               _seg(2, PB_MINOR_14, 2, name="PB14"),
                               _seg(1, PB_MINOR, 1, name="PB")])
        self.assertEqual(rep["total_rows"], 60)
        self.assertEqual(rep["status"], "not_closed")
        self.assertFalse(rep["closed"])
        self.assertTrue(rep["truth"]["true"])
        # every generated row is tagged with segment + method version
        r25 = rep["rows"][25]
        self.assertEqual((r25["segment"], r25["method_id"], r25["method"],
                          r25["version"], r25["lead"], r25["change"]),
                         (2, 2, "PB14", 1, 1, 1))
        self.assertIsNone(r25["override"])
        r49 = rep["rows"][49]
        self.assertEqual((r49["segment"], r49["method_id"]), (3, 1))
        # segment boundaries continue from the previous segment's last row
        self.assertEqual([(s["start_row"], s["end_row"]) for s in rep["segments"]],
                         [("123456", "156342"), ("156342", "156234"),
                          ("156234", "163542")])
        self.assertEqual([(s["at_index"], s["before_row"], s["after_row"])
                          for s in rep["switches"]],
                         [(24, "156342", "513624"), (48, "156234", "512643")])
        self.assertEqual(rep["methods_used"],
                         [{"method_id": 1, "name": "PB", "version": 1,
                           "leads": 3, "rows": 36, "segments": [1, 3]},
                          {"method_id": 2, "name": "PB14", "version": 1,
                           "leads": 2, "rows": 24, "segments": [2]}])

    def test_unified_truth_across_segments(self):
        # two one-lead segments: the repeat's second occurrence lies in the
        # next segment - truth is judged on the whole touch, not per method
        rep = analyze_spliced([_seg(1, "x.14.x", 1, stage=4),
                               _seg(2, "x.14.x", 1, stage=4)])
        self.assertEqual(rep["status"], "untrue")
        self.assertTrue(rep["closed"])
        self.assertIsNone(rep["premature_rounds"])
        repeat = rep["truth"]["first_repeat"]
        self.assertEqual(repeat["row"], "2413")
        self.assertEqual(repeat["first"],
                         {"index": 2, "segment": 1, "lead": 1, "change": 2})
        self.assertEqual(repeat["second"],
                         {"index": 4, "segment": 2, "lead": 1, "change": 1})
        self.assertTrue(rep["rows"][4]["repeat"])
        self.assertEqual(rep["rows"][4]["segment"], 2)
        self.assertEqual(rep["problems"], ["untrue"])

    def test_premature_rounds_and_not_closed(self):
        # the plain course comes round at row 60 but the touch goes on
        rep = analyze_spliced([_seg(1, PB_MINOR, 5), _seg(1, PB_MINOR, 1)])
        self.assertEqual(rep["status"], "not_closed")
        self.assertEqual(rep["problems"],
                         ["untrue", "premature_rounds", "not_closed"])
        self.assertEqual(rep["premature_rounds"],
                         {"index": 60, "segment": 1, "lead": 5, "change": 12})
        repeat = rep["truth"]["first_repeat"]
        self.assertEqual(repeat["row"], "123456")
        self.assertEqual(repeat["first"],
                         {"index": 0, "segment": 0, "lead": 0, "change": 0})
        self.assertEqual(repeat["second"],
                         {"index": 60, "segment": 1, "lead": 5, "change": 12})

    def test_override_within_segment(self):
        rep = analyze_spliced([_seg(1, PB_MINOR, 1, overrides=[
            {"lead": 1, "change": 12, "notation": "14"}])])
        self.assertEqual(rep["total_rows"], 12)
        (ovr,) = rep["overrides"]
        self.assertTrue(ovr["applied"])
        self.assertEqual(ovr["segment"], 1)
        self.assertEqual(ovr["replaces_token"], "12")
        self.assertEqual(ovr["before_row"], "132546")
        self.assertEqual(ovr["after_row"], "123564")
        self.assertEqual(ovr["row_index"], 12)
        # the row entry carries the override source
        entry = rep["rows"][12]
        self.assertEqual(entry["override"],
                         {"segment": 1, "lead": 1, "change": 12,
                          "notation": "14", "replaces_token": "12"})
        self.assertEqual(entry["token"], "14")
        self.assertEqual(entry["row"], "123564")
        self.assertEqual(rep["unapplied_overrides"], [])

    def test_superseded_override_reported_unapplied(self):
        rep = analyze_spliced([_seg(1, PB_MINOR, 1, overrides=[
            {"lead": 1, "change": 12, "notation": "14"},
            {"lead": 1, "change": 12, "notation": "16"}])])
        first, second = rep["overrides"]
        self.assertFalse(first["applied"])
        self.assertTrue(first["superseded"])
        self.assertTrue(second["applied"])
        self.assertEqual(rep["rows"][12]["token"], "16")  # last spec wins
        self.assertEqual(rep["unapplied_overrides"],
                         [{"segment": 1, "lead": 1, "change": 12,
                           "notation": "14", "replaces_token": "12",
                           "reason": "superseded by a later override for the"
                                     " same change"}])

    def test_stage_mismatch_located(self):
        with self.assertRaises(SplicedError) as ctx:
            analyze_spliced([_seg(1, PB_MINOR, 1),
                             _seg(2, "3.1.5.1.5.1.5.1.5.125", 1, stage=5)])
        err = ctx.exception
        self.assertEqual(err.segment, 2)
        self.assertEqual(err.extra["stage"], 5)
        self.assertEqual(err.extra["expected"], 6)

    def test_override_out_of_range_located(self):
        with self.assertRaises(SplicedError) as ctx:
            analyze_spliced([_seg(1, PB_MINOR, 1, overrides=[
                {"lead": 2, "change": 1, "notation": "14"}])])
        self.assertEqual((ctx.exception.segment, ctx.exception.override), (1, 0))
        with self.assertRaises(SplicedError) as ctx:
            analyze_spliced([_seg(1, PB_MINOR, 2, overrides=[
                {"lead": 1, "change": 99, "notation": "14"}])])
        self.assertEqual((ctx.exception.segment, ctx.exception.override), (1, 0))
        with self.assertRaises(SplicedError):
            analyze_spliced([_seg(1, PB_MINOR, 1, overrides=[
                {"lead": 1, "change": 1, "notation": "1a"}])])

    def test_total_rows_limit_located(self):
        with self.assertRaises(SplicedError) as ctx:
            analyze_spliced([_seg(1, PB_MINOR, 2), _seg(1, PB_MINOR, 2)],
                            max_rows=30)
        err = ctx.exception
        self.assertEqual(err.segment, 2)  # the segment that crosses the limit
        self.assertEqual(err.extra["total_rows"], 48)
        self.assertEqual(err.extra["max_rows"], 30)
        # default limit is one extent: 61 leads of 12 rows > 720
        with self.assertRaises(SplicedError):
            analyze_spliced([_seg(1, PB_MINOR, 61)])

    def test_bad_segment_specs(self):
        with self.assertRaises(SplicedError):
            analyze_spliced([])
        with self.assertRaises(SplicedError):
            analyze_spliced([_seg(1, PB_MINOR, 0)])
        with self.assertRaises(SplicedError):
            analyze_spliced(["not a dict"])
        with self.assertRaises(ValueError):
            analyze_spliced([_seg(1, PB_MINOR, 1)], max_rows=0)

    def test_compare_touch_reports(self):
        ta = analyze_spliced([_seg(1, PB_MINOR, 2), _seg(1, PB_MINOR, 3)])
        tb = analyze_spliced([_seg(1, PB_MINOR, 2), _seg(2, PB_MINOR_14, 2),
                              _seg(1, PB_MINOR, 1)])
        cmp = compare_touch_reports(ta, tb)
        self.assertTrue(cmp["same_stage"])
        self.assertTrue(cmp["total_rows_equal"])
        self.assertEqual(cmp["total_rows_delta"], 0)
        self.assertFalse(cmp["both_closed"])   # tb does not come round
        self.assertTrue(cmp["both_true"])
        self.assertEqual(cmp["methods_overlap"], [1])
        self.assertEqual(cmp["a"]["status"], "ok")
        self.assertEqual(cmp["b"]["status"], "not_closed")


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.store.close()

    def call(self, method, path, body=None, expect=200):
        req = urllib.request.Request(self.base + path, method=method)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data=data) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            payload = json.loads(e.read().decode())
            if e.code != expect:
                raise AssertionError(f"{method} {path}: got {e.code} {payload}")
            return e.code, payload

    def test_01_docs_and_index(self):
        with urllib.request.urlopen(self.base + "/") as resp:
            html = resp.read().decode()
            self.assertEqual(resp.status, 200)
            self.assertIn("Place notation", html)
        status, idx = self.call("GET", "/api")
        self.assertEqual(status, 200)
        self.assertIn("endpoints", idx)

    def test_02_parse_endpoint(self):
        status, out = self.call("POST", "/api/parse",
                                {"stage": 6, "notation": "x16x16x16,12"})
        self.assertEqual(status, 200)
        self.assertEqual(out["lead_length"], 12)
        self.assertEqual([c["completed"] for c in out["changes"]],
                         ["x", "16", "x", "16", "x", "16",
                          "x", "16", "x", "16", "x", "12"])

    def test_03_parse_error_located(self):
        status, out = self.call("POST", "/api/parse",
                                {"stage": 6, "notation": "x.1a.16"}, expect=400)
        self.assertEqual(status, 400)
        self.assertEqual(out["code"], "notation_error")
        self.assertEqual(out["token"], "a")
        self.assertEqual(out["offset"], 3)

    def test_04_method_versions_and_analysis_flow(self):
        # version 1
        status, m1 = self.call("POST", "/api/methods",
                               {"name": "Plain Bob Minor", "stage": 6,
                                "notation": PB_MINOR})
        self.assertEqual(status, 201)
        self.assertEqual(m1["version"], 1)
        self.assertEqual(m1["lead_length"], 12)
        # version 2: same name, 14 lead end
        status, m2 = self.call("POST", "/api/methods",
                               {"name": "Plain Bob Minor", "stage": 6,
                                "notation": "x.16.x.16.x.16.x.16.x.16.x.14"})
        self.assertEqual(status, 201)
        self.assertEqual(m2["version"], 2)
        # list + detail
        status, lst = self.call("GET", "/api/methods")
        self.assertEqual(len(lst["methods"]), 2)
        status, detail = self.call("GET", f"/api/methods/{m1['id']}")
        self.assertEqual(detail["changes"][0]["completed"], "x")
        # analyze v1
        status, a1 = self.call("POST", "/api/analyses", {"method_id": m1["id"]})
        self.assertEqual(status, 201)
        self.assertEqual(a1["status"], "ok")
        self.assertEqual(a1["period_rows"], 60)
        self.assertEqual(a1["hunt_bells"], [1])
        # analyze v2
        status, a2 = self.call("POST", "/api/analyses", {"method_id": m2["id"]})
        self.assertEqual(status, 201)
        # compare the two versions
        status, cmp = self.call("GET", f"/api/compare?a={a1['id']}&b={a2['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(cmp["a"]["analysis_id"], a1["id"])
        self.assertEqual(cmp["b"]["analysis_id"], a2["id"])
        self.assertIn("period_rows_equal", cmp["comparison"])
        self.assertIn("first_repeat_same_position", cmp["comparison"])
        # rows endpoint with slicing
        status, rows = self.call("GET", f"/api/analyses/{a1['id']}/rows?from=0&to=12")
        self.assertEqual(rows["total"], 61)
        self.assertEqual(len(rows["rows"]), 13)
        self.assertEqual(rows["rows"][0]["row"], "123456")
        self.assertEqual(rows["rows"][12]["row"], "135264")
        # full report
        status, full = self.call("GET", f"/api/analyses/{a1['id']}")
        self.assertEqual(full["report"]["lead_head"], "135264")
        # download
        req = urllib.request.Request(self.base + f"/api/analyses/{a1['id']}/download")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("attachment", resp.headers["Content-Disposition"])
            payload = json.loads(resp.read().decode())
            self.assertEqual(payload["report"]["period_rows"], 60)

    def test_05_analysis_with_override(self):
        status, m = self.call("POST", "/api/methods",
                              {"name": "PB Minor (override demo)", "stage": 6,
                               "notation": PB_MINOR})
        status, a = self.call("POST", "/api/analyses",
                              {"method_id": m["id"],
                               "overrides": [{"lead": 1, "change": 12,
                                              "notation": "14"}]})
        self.assertEqual(status, 201)
        status, full = self.call("GET", f"/api/analyses/{a['id']}")
        ovr = full["report"]["overrides"][0]
        self.assertTrue(ovr["applied"])
        self.assertEqual(ovr["before_row"], "132546")
        self.assertEqual(ovr["after_row"], "123564")

    def test_06_compare_by_method_ids(self):
        status, lst = self.call("GET", "/api/methods")
        ids = [m["id"] for m in lst["methods"] if m["name"] == "Plain Bob Minor"]
        status, cmp = self.call("POST", "/api/compare",
                                {"method_a": ids[0], "method_b": ids[1]})
        self.assertEqual(status, 200)
        self.assertIn("comparison", cmp)

    def test_07_errors(self):
        status, out = self.call("POST", "/api/methods",
                                {"name": "Bad", "stage": 6, "notation": "x.19"},
                                expect=400)
        self.assertEqual(out["code"], "notation_error")
        self.assertEqual(out["token"], "19")
        status, out = self.call("POST", "/api/methods",
                                {"name": "Bad", "stage": 9, "notation": "x"},
                                expect=400)
        self.assertIn("stage", out["error"])
        status, out = self.call("GET", "/api/methods/9999", expect=404)
        self.assertEqual(out["code"], "not_found")
        status, out = self.call("GET", "/api/analyses/9999", expect=404)
        status, out = self.call("POST", "/api/analyses", {"method_id": 9999},
                                expect=404)
        status, out = self.call("GET", "/api/nope", expect=404)
        status, out = self.call("POST", "/api/analyses",
                                {"method_id": 1, "max_rows": 0}, expect=400)

    def test_08_touch_flow(self):
        # two 6-bell method versions to splice
        status, m1 = self.call("POST", "/api/methods",
                               {"name": "Splice Demo PB", "stage": 6,
                                "notation": PB_MINOR})
        status, m2 = self.call("POST", "/api/methods",
                               {"name": "Splice Demo PB14", "stage": 6,
                                "notation": PB_MINOR_14})
        # three segments with a bob inside segment 2
        body = {"segments": [
            {"method_id": m1["id"], "leads": 2},
            {"method_id": m2["id"], "leads": 2,
             "overrides": [{"lead": 1, "change": 12, "notation": "12"}]},
            {"method_id": m1["id"], "leads": 1}]}
        status, t = self.call("POST", "/api/touches", body)
        self.assertEqual(status, 201)
        self.assertEqual(t["segment_count"], 3)
        self.assertEqual(t["total_rows"], 60)
        self.assertEqual(t["total_leads"], 5)
        self.assertEqual(t["status"], "not_closed")
        self.assertIn("report", t["links"])
        tid = t["id"]
        # list
        status, lst = self.call("GET", "/api/touches")
        self.assertTrue(any(x["id"] == tid for x in lst["touches"]))
        # full report
        status, full = self.call("GET", f"/api/touches/{tid}")
        rep = full["report"]
        self.assertEqual(len(rep["switches"]), 2)
        self.assertEqual(rep["switches"][0]["before_row"], "156342")
        self.assertEqual(len(rep["segments"]), 3)
        self.assertEqual(rep["segments"][1]["method_id"], m2["id"])
        self.assertTrue(rep["overrides"][0]["applied"])
        self.assertEqual(rep["unapplied_overrides"], [])
        self.assertEqual(full["segments"], body["segments"])  # spec kept
        # rows by segment: segment 2 covers indexes 25..48
        status, rows = self.call("GET", f"/api/touches/{tid}/rows?segment=2")
        self.assertEqual(rows["matched"], 24)
        self.assertTrue(all(r["segment"] == 2 for r in rows["rows"]))
        self.assertEqual(rows["rows"][0]["index"], 25)
        self.assertEqual(rows["rows"][0]["method_id"], m2["id"])
        # the override row carries its source
        ovr_row = [r for r in rows["rows"] if r["override"]][0]
        self.assertEqual(ovr_row["override"]["segment"], 2)
        self.assertEqual(ovr_row["override"]["notation"], "12")
        # global slicing still works
        status, rows = self.call("GET", f"/api/touches/{tid}/rows?from=0&to=12")
        self.assertEqual(len(rows["rows"]), 13)
        self.assertEqual(rows["rows"][0]["segment"], 0)
        # unknown segment
        status, out = self.call("GET", f"/api/touches/{tid}/rows?segment=9",
                                expect=404)
        # download
        req = urllib.request.Request(self.base + f"/api/touches/{tid}/download")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("attachment", resp.headers["Content-Disposition"])
            payload = json.loads(resp.read().decode())
            self.assertEqual(payload["report"]["segment_count"], 3)
        # a second touch (the plain course as one segment) and compare
        status, t2 = self.call("POST", "/api/touches",
                               {"segments": [{"method_id": m1["id"], "leads": 5}]})
        self.assertEqual(t2["status"], "ok")
        self.assertTrue(t2["closed"])
        status, cmp = self.call("GET",
                                f"/api/touches/compare?a={tid}&b={t2['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(cmp["a"]["touch_id"], tid)
        c = cmp["comparison"]
        self.assertTrue(c["same_stage"])
        self.assertTrue(c["total_rows_equal"])
        self.assertFalse(c["both_closed"])
        self.assertEqual(c["methods_overlap"], [m1["id"]])
        status, cmp2 = self.call("POST", "/api/touches/compare",
                                 {"a": tid, "b": t2["id"]})
        self.assertEqual(status, 200)

    def test_09_touch_errors_not_stored(self):
        status, before = self.call("GET", "/api/touches")
        n_before = len(before["touches"])
        status, m1 = self.call("GET", "/api/methods")
        pb = next(m for m in m1["methods"] if m["name"] == "Splice Demo PB")
        # 5-bell method for the stage mismatch
        status, gd = self.call("POST", "/api/methods",
                               {"name": "Grandsire Doubles", "stage": 5,
                                "notation": "3.1.5.1.5.1.5.1.5.125"})
        # method not found: 404 located to the segment
        status, out = self.call("POST", "/api/touches",
                                {"segments": [{"method_id": pb["id"], "leads": 1},
                                              {"method_id": 9999, "leads": 1}]},
                                expect=404)
        self.assertEqual(out["code"], "not_found")
        self.assertEqual(out["segment"], 2)
        self.assertEqual(out["method_id"], 9999)
        # stage mismatch
        status, out = self.call("POST", "/api/touches",
                                {"segments": [{"method_id": pb["id"], "leads": 1},
                                              {"method_id": gd["id"], "leads": 1}]},
                                expect=400)
        self.assertEqual(out["code"], "bad_segment")
        self.assertEqual(out["segment"], 2)
        self.assertEqual(out["stage"], 5)
        self.assertEqual(out["expected"], 6)
        # override out of range
        status, out = self.call("POST", "/api/touches",
                                {"segments": [{"method_id": pb["id"], "leads": 1,
                                               "overrides": [{"lead": 1,
                                                              "change": 99,
                                                              "notation": "14"}]}]},
                                expect=400)
        self.assertEqual(out["code"], "bad_segment")
        self.assertEqual(out["segment"], 1)
        self.assertEqual(out["override"], 0)
        # total rows beyond one extent (61 * 12 = 732 > 720)
        status, out = self.call("POST", "/api/touches",
                                {"segments": [{"method_id": pb["id"], "leads": 61}]},
                                expect=400)
        self.assertEqual(out["code"], "bad_segment")
        self.assertEqual(out["total_rows"], 732)
        self.assertEqual(out["max_rows"], 720)
        # malformed requests
        status, out = self.call("POST", "/api/touches", {"segments": []},
                                expect=400)
        status, out = self.call("POST", "/api/touches",
                                {"segments": [{"method_id": pb["id"],
                                               "leads": 0}]}, expect=400)
        self.assertEqual(out["segment"], 1)
        status, out = self.call("POST", "/api/touches",
                                {"segments": [{"leads": 1}]}, expect=400)
        status, out = self.call("GET", "/api/touches/9999", expect=404)
        status, out = self.call("GET", "/api/touches/compare", expect=400)
        # none of the failed touches was stored
        status, after = self.call("GET", "/api/touches")
        self.assertEqual(len(after["touches"]), n_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
