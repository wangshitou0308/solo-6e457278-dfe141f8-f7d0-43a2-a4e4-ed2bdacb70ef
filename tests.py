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
from ringing import (HARD_MAX_ROWS, LimitRequiredError, NotationError,
                     SchemeError, SplicedError, analyze, analyze_spliced,
                     compare_music, compare_reports, compare_touch_reports,
                     parse_notation, parse_row, parse_scheme, row_str,
                     score_analysis, score_touch, stroke_for_index)
from server import make_server

# Plain Bob Minor: 12-change lead, lead head 135264, 5 leads = 60 rows, true.
PB_MINOR = "x.16.x.16.x.16.x.16.x.16.x.12"
# 14 at every lead end: 3 leads = 36 rows, lead head 123564, true.
PB_MINOR_14 = "x.16.x.16.x.16.x.16.x.16.x.14"
# Plain Bob Royal (10 bells): "10" means places 1 AND 10, 20-change lead,
# lead head 1352749608, 9 leads = 180 rows, true.
PB_ROYAL = "x10x10x10x10x10,12"
# Plain Bob Maximus (12 bells): 24-change lead, 11 leads = 264 rows, true.
PB_MAXIMUS = "x1Tx1Tx1Tx1Tx1Tx1T,12"
ROUNDS_10 = "1234567890"
ROUNDS_12 = "1234567890ET"


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
            parse_notation("x", 13)
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


class StageTenTwelveParseTests(unittest.TestCase):
    def test_royal_maximus_lead_notation(self):
        ch = parse_notation(PB_ROYAL, 10)
        self.assertEqual(len(ch), 20)
        self.assertEqual([c.completed() for c in ch[:3]], ["x", "10", "x"])
        self.assertEqual(ch[-1].completed(), "12")
        ch = parse_notation(PB_MAXIMUS, 12)
        self.assertEqual(len(ch), 24)
        self.assertEqual(ch[1].completed(), "1T")
        # completion fills inferable end places with the right symbols
        self.assertEqual(completed_sequence("3", 12), ["3T"])
        self.assertEqual(completed_sequence("E", 12), ["ET"])
        self.assertEqual(completed_sequence("12E", 12), ["12ET"])
        self.assertEqual(completed_sequence("3", 10), ["30"])
        (cx,) = parse_notation("x", 12)
        self.assertEqual(cx.swaps, tuple((i, i + 1) for i in range(1, 12, 2)))

    def test_token_10_is_places_1_and_10(self):
        # the crucial rule: "10" is two single-symbol places, never ten
        (c,) = parse_notation("10", 12)
        self.assertEqual(sorted(c.places), [1, 10])
        self.assertEqual(c.completed(), "10")
        self.assertEqual(c.swaps, ((2, 3), (4, 5), (6, 7), (8, 9), (11, 12)))
        # the bare symbol 0 alone means place 10, which completes to 10 too
        (c0,) = parse_notation("0", 12)
        self.assertEqual(sorted(c0.places), [1, 10])
        # E alone on 12 completes to ET; on 11 "12E" leaves places 1,2,11
        (ce,) = parse_notation("12E", 11)
        self.assertEqual(sorted(ce.places), [1, 2, 11])
        self.assertEqual(ce.completed(), "12E")
        # bell-12 symbol T out of range on 10 bells is located
        with self.assertRaises(NotationError) as ctx:
            parse_notation("x.1T", 10)
        self.assertEqual(ctx.exception.token, "1T")
        self.assertEqual(ctx.exception.offset, 2)
        with self.assertRaises(NotationError):
            parse_notation("E", 10)

    def test_symbol_rendering(self):
        self.assertEqual(row_str(tuple(range(1, 13))), "1234567890ET")
        self.assertEqual(row_str(tuple(range(1, 11))), "1234567890")
        self.assertEqual(row_str((1, 3, 5, 2, 7, 4, 9, 6, 10, 8)),
                         "1352749608")
        self.assertEqual(row_str((1, 3, 5, 2, 7, 4, 9, 6, 11, 8, 12, 10)),
                         "13527496E8T0")

    def test_row_input_forms(self):
        rounds = tuple(range(1, 13))
        # compact symbols
        self.assertEqual(parse_row(ROUNDS_12, 12), rounds)
        # separated symbols AND plain multi-digit integers
        self.assertEqual(parse_row("1,2,3,4,5,6,7,8,9,0,E,T", 12), rounds)
        self.assertEqual(parse_row("1 2 3 4 5 6 7 8 9 10 11 12", 12), rounds)
        # arrays stay integer-only
        self.assertEqual(parse_row(list(range(1, 13)), 12), rounds)
        self.assertEqual(parse_row(None, 10), tuple(range(1, 11)))
        self.assertEqual(parse_row(ROUNDS_10, 10), tuple(range(1, 11)))

    def test_compact_row_duplicate_located(self):
        with self.assertRaises(NotationError) as ctx:
            parse_row("1234567890EE", 12)
        self.assertEqual(ctx.exception.token, "E")
        self.assertEqual(ctx.exception.offset, 11)
        with self.assertRaises(NotationError) as ctx:
            parse_row("12345678900E", 12)  # bell 10 repeated, T missing
        self.assertEqual((ctx.exception.token, ctx.exception.offset), ("0", 10))
        self.assertIn("appears more than once", ctx.exception.message)

    def test_compact_row_missing_located(self):
        with self.assertRaises(NotationError) as ctx:
            parse_row("1234567890E", 12)  # 11 bells: T missing
        self.assertEqual(ctx.exception.token, "1234567890E")
        self.assertEqual(ctx.exception.offset, 11)
        self.assertIn("missing", ctx.exception.message)
        with self.assertRaises(NotationError) as ctx:
            parse_row("123456789EET", 12)  # 13 chars: bell E repeated, 0 missing
        # duplicate check runs before the length check, pointing at 2nd E
        self.assertEqual((ctx.exception.token, ctx.exception.offset), ("E", 10))
        # the separated form accepts 10/11/12 as integers; a 13-token row
        # trips the length check before the duplicate check, pointing just
        # past the last source token (index 27 + length 1 = 28)
        with self.assertRaises(NotationError) as ctx:
            parse_row("1 2 3 4 5 6 7 8 9 10 11 12 1", 12)
        self.assertEqual(ctx.exception.offset, 28)
        # a 12-token permutation with a duplicate gets the symbol location
        with self.assertRaises(NotationError) as ctx:
            parse_row("1 2 3 4 5 6 7 8 9 10 11 1", 12)
        self.assertEqual((ctx.exception.token, ctx.exception.offset), ("1", 24))
        with self.assertRaises(NotationError) as ctx:
            parse_row([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 5], 12)
        self.assertEqual((ctx.exception.token, ctx.exception.offset), (5, 11))

    def test_row_offsets_with_leading_whitespace(self):
        # leading whitespace/separators must be counted in the source offset:
        # the duplicate E sits at index 12 in " 1234567890EE"
        with self.assertRaises(NotationError) as ctx:
            parse_row(" 1234567890EE", 12)
        self.assertEqual((ctx.exception.token, ctx.exception.offset), ("E", 12))
        with self.assertRaises(NotationError) as ctx:
            parse_row("\t1234567890EE", 12)
        self.assertEqual((ctx.exception.token, ctx.exception.offset), ("E", 12))
        with self.assertRaises(NotationError) as ctx:
            parse_row("..1234567890EE", 12)
        self.assertEqual((ctx.exception.token, ctx.exception.offset), ("E", 13))
        # trailing whitespace must not shift an earlier symbol
        with self.assertRaises(NotationError) as ctx:
            parse_row("1234567890EE ", 12)
        self.assertEqual((ctx.exception.token, ctx.exception.offset), ("E", 11))
        # length error points just past the last source symbol
        with self.assertRaises(NotationError) as ctx:
            parse_row("   1234567890E", 12)
        self.assertEqual(ctx.exception.token, "   1234567890E")
        self.assertEqual(ctx.exception.offset, 14)
        self.assertIn("missing", ctx.exception.message)
        # separated form with leading padding keeps the source offset too
        with self.assertRaises(NotationError) as ctx:
            parse_row(" 2, 1, 1, 4, 5, 6", 6)
        self.assertEqual((ctx.exception.token, ctx.exception.offset), ("1", 7))
        # leading padding is accepted on valid rows
        self.assertEqual(parse_row(" 1234567890ET", 12), tuple(range(1, 13)))
        self.assertEqual(parse_row(" 1 2 3 4 5 6 ", 6), tuple(range(1, 7)))

    def test_row_illegal_symbol_located(self):
        with self.assertRaises(NotationError) as ctx:
            parse_row("1234567890T", 11)  # T (bell 12) out of stage
        self.assertEqual((ctx.exception.token, ctx.exception.offset), ("T", 10))
        with self.assertRaises(NotationError) as ctx:
            parse_row("1234567890EX", 12)
        self.assertEqual((ctx.exception.token, ctx.exception.offset), ("X", 11))
        # separated token form keeps the source offset
        with self.assertRaises(NotationError) as ctx:
            parse_row("1, 2, z, 4", 4)
        self.assertEqual((ctx.exception.token, ctx.exception.offset), ("z", 6))

    def test_array_row_errors_located(self):
        with self.assertRaises(NotationError) as ctx:
            parse_row([1, 2, 2, 4], 4)  # duplicate
        self.assertEqual((ctx.exception.token, ctx.exception.offset), (2, 2))
        with self.assertRaises(NotationError) as ctx:
            parse_row([1, 2, 3, 13], 12)  # out of stage
        self.assertEqual((ctx.exception.token, ctx.exception.offset), (13, 3))
        with self.assertRaises(NotationError) as ctx:
            parse_row([1, 2, "3", 4], 4)  # strings rejected: integer-only
        self.assertEqual(ctx.exception.offset, 2)
        with self.assertRaises(NotationError):
            parse_row([1, 2, 3], 4)  # missing bell 4


class StageTenTwelveAnalyzeTests(unittest.TestCase):
    def test_plain_bob_royal_course(self):
        rep = analyze(10, PB_ROYAL, max_rows=100000)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["lead_length"], 20)
        self.assertEqual(rep["lead_head"], "1352749608")
        self.assertEqual(rep["period_leads"], 9)
        self.assertEqual(rep["period_rows"], 180)
        self.assertEqual(rep["hunt_bells"], [1])
        self.assertTrue(rep["truth"]["true"])
        self.assertTrue(rep["truth"]["conclusive"])
        # symbols flow through every row entry and the closing row
        self.assertEqual(rep["rows"][0]["row"], ROUNDS_10)
        self.assertTrue(all(set(r["row"]) <= set("1234567890")
                            for r in rep["rows"]))

    def test_plain_bob_maximus_course(self):
        rep = analyze(12, PB_MAXIMUS, max_rows=100000)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["lead_length"], 24)
        self.assertEqual(rep["lead_head"], "13527496E8T0")
        self.assertEqual(rep["period_rows"], 264)
        self.assertEqual(rep["hunt_bells"], [1])
        self.assertTrue(rep["truth"]["true"])
        self.assertEqual(rep["rows"][0]["row"], ROUNDS_12)
        self.assertEqual(rep["rows"][-1]["row"], ROUNDS_12)

    def test_extent_above_hard_cap_requires_explicit_max_rows(self):
        for stage in (10, 11, 12):
            with self.assertRaises(LimitRequiredError) as ctx:
                analyze(stage, "x" if stage % 2 == 0
                        else "3.1.5.1.7.1.9.1.E.1.01")
            err = ctx.exception
            self.assertEqual(err.stage, stage)
            self.assertGreater(err.extent_rows, HARD_MAX_ROWS)
            self.assertIn("explicit max_rows", err.message)
        # stages 4-8 still default to the extent without a cap
        rep = analyze(6, PB_MINOR)
        self.assertEqual(rep["max_rows"], 720)

    def test_explicit_cap_over_hard_limit_rejected(self):
        with self.assertRaises(ValueError):
            analyze(10, PB_ROYAL, max_rows=HARD_MAX_ROWS + 1)
        with self.assertRaises(ValueError):
            analyze(12, PB_MAXIMUS, max_rows=10**9)

    def test_capped_without_repeat_is_inconclusive(self):
        # stop after 240 checked rows (10 Maximus leads), before any repeat
        rep = analyze(12, PB_MAXIMUS, max_rows=240)
        self.assertEqual(rep["status"], "exceeded_limit")
        self.assertFalse(rep["closed"])
        self.assertIsNone(rep["truth"]["true"])        # NOT a true verdict
        self.assertFalse(rep["truth"]["conclusive"])
        self.assertIsNone(rep["truth"]["first_repeat"])
        self.assertEqual(rep["truth"]["checked_rows"], 240)
        self.assertEqual(rep["rows_generated"], 240)
        self.assertEqual(rep["problems"],
                         ["exceeded_limit", "not_closed", "truth_inconclusive"])
        self.assertIsNone(rep["period_rows"])

    def test_capped_but_repeat_found_is_untrue_conclusive(self):
        # Plain Bob Maximus closes at 264 rows; with a 300-row cap the full
        # true course is reached normally - sanity for the conclusive branch
        rep = analyze(12, PB_MAXIMUS, max_rows=300)
        self.assertEqual(rep["status"], "ok")
        self.assertTrue(rep["truth"]["conclusive"])
        self.assertTrue(rep["truth"]["true"])

    def test_spliced_requires_cap_at_stage_12(self):
        seg = {"method_id": 1, "name": "M1", "version": 1, "stage": 12,
               "notation": PB_MAXIMUS, "leads": 1, "overrides": []}
        with self.assertRaises(LimitRequiredError):
            analyze_spliced([seg])
        rep = analyze_spliced([seg], max_rows=1000)
        self.assertEqual(rep["total_rows"], 24)
        self.assertTrue(rep["truth"]["conclusive"])
        self.assertEqual(rep["rows"][0]["row"], ROUNDS_12)

    def test_spliced_switch_trajectory_in_symbols(self):
        # two Maximus-style methods spliced lead-to-lead: switch rows and
        # every row entry stay in canonical 0/E/T symbols
        alt = "x12x12x12x12x12x12,1T"
        segs = [
            {"method_id": 1, "name": "Maximus", "version": 1, "stage": 12,
             "notation": PB_MAXIMUS, "leads": 1, "overrides": []},
            {"method_id": 2, "name": "MaxAlt", "version": 1, "stage": 12,
             "notation": alt, "leads": 1, "overrides": []},
        ]
        rep = analyze_spliced(segs, max_rows=1000)
        self.assertEqual(rep["total_rows"], 48)
        (sw,) = rep["switches"]
        self.assertEqual(sw["at_index"], 24)
        # lead head after one Maximus lead, rendered with E/T
        self.assertEqual(sw["before_row"], "13527496E8T0")
        self.assertEqual(sw["from_method"], "Maximus")
        self.assertEqual(sw["to_method"], "MaxAlt")
        self.assertEqual(sw["after_row"], rep["rows"][25]["row"])
        self.assertEqual(rep["rows"][25]["segment"], 2)
        alphabet = set("1234567890ET")
        self.assertTrue(all(set(r["row"]) <= alphabet for r in rep["rows"]))
        self.assertTrue(rep["truth"]["conclusive"])

    def test_compare_inconclusive_truth_is_null(self):
        capped = analyze(12, PB_MAXIMUS, max_rows=240)   # inconclusive
        closed = analyze(12, PB_MAXIMUS, max_rows=1000)  # ok, true
        cmp = compare_reports(closed, capped)
        self.assertTrue(cmp["a"]["truth"]["true"])
        self.assertIsNone(cmp["b"]["truth"]["true"])
        self.assertFalse(cmp["b"]["truth"]["conclusive"])
        self.assertIsNone(cmp["both_true"])


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


class SchemeParseTests(unittest.TestCase):
    def scheme(self, rules, stage=6, name="s"):
        return parse_scheme({"name": name, "rules": rules}, stage=stage)

    def test_rule_defaults(self):
        s = self.scheme([{"name": "r", "kind": "run", "min_length": 3}])
        (r,) = s["rules"]
        self.assertEqual(r["direction"], "both")
        self.assertEqual(r["position"], "any")
        self.assertEqual(r["weight"], 1)
        self.assertEqual(r["strokes"], ["hand", "back"])
        self.assertEqual(r["lead_end"], "any")
        self.assertEqual(r["segments"], [])
        self.assertEqual(r["id"], "rule-1")

    def test_sequence_forms_and_symbols(self):
        s = self.scheme([{"name": "a", "kind": "sequence", "bells": "4321"},
                         {"name": "b", "kind": "sequence",
                          "bells": [6, 5, 4, 3]},
                         {"name": "c", "kind": "sequence",
                          "bells": "0 E T", "position": "back"}], stage=12)
        self.assertEqual([r["bells"] for r in s["rules"]],
                         [(4, 3, 2, 1), (6, 5, 4, 3), (10, 11, 12)])
        self.assertEqual([r["pattern"] for r in s["rules"]],
                         ["4321", "6543", "0ET"])

    def test_row_rule_must_be_a_permutation(self):
        with self.assertRaises(SchemeError) as ctx:
            self.scheme([{"name": "r", "kind": "row", "row": "123455"}])
        err = ctx.exception
        self.assertEqual(err.rule, 0)
        self.assertEqual(err.field, "row")
        self.assertEqual(err.token, "5")
        self.assertEqual(err.offset, 5)

    def test_errors_located_at_rule_index(self):
        with self.assertRaises(SchemeError) as ctx:
            self.scheme([{"name": "ok", "kind": "row", "row": "123456"},
                         {"name": "bad", "kind": "run",
                          "direction": "sideways"}])
        self.assertEqual(ctx.exception.rule, 1)
        self.assertEqual(ctx.exception.field, "direction")
        with self.assertRaises(SchemeError) as ctx:
            self.scheme([{"name": "ok", "kind": "row", "row": "123456"},
                         {"name": "bad", "kind": "run", "min_length": 99}])
        self.assertEqual((ctx.exception.rule, ctx.exception.field),
                         (1, "min_length"))
        with self.assertRaises(SchemeError) as ctx:
            self.scheme([{"name": "bad", "kind": "sequence", "bells": "18"}])
        self.assertEqual(ctx.exception.field, "bells")
        self.assertEqual(ctx.exception.token, "8")
        for rules in ([], [{"name": "x", "kind": "row", "row": "12345"}],):
            with self.assertRaises((SchemeError, NotationError)):
                self.scheme(rules)
        with self.assertRaises(SchemeError):
            self.scheme([{"name": "same", "kind": "row", "row": "123456"},
                         {"name": "same", "kind": "run", "min_length": 3}])

    def test_filter_validation(self):
        with self.assertRaises(SchemeError) as ctx:
            self.scheme([{"name": "r", "kind": "run", "min_length": 3,
                          "strokes": ["treble"]}])
        self.assertEqual(ctx.exception.field, "strokes")
        with self.assertRaises(SchemeError) as ctx:
            self.scheme([{"name": "r", "kind": "run", "min_length": 3,
                          "lead_end": "half-lead"}])
        self.assertEqual(ctx.exception.field, "lead_end")
        with self.assertRaises(SchemeError) as ctx:
            self.scheme([{"name": "r", "kind": "run", "min_length": 3,
                          "segments": [1, 1]}])
        self.assertEqual(ctx.exception.field, "segments")
        with self.assertRaises(SchemeError):
            self.scheme([{"name": "r", "kind": "run", "min_length": 3,
                          "weight": 0}])

    def test_stage_mismatch(self):
        with self.assertRaises(SchemeError):
            parse_scheme({"name": "s", "stage": 5,
                          "rules": [{"name": "r", "kind": "row",
                                     "row": "123456"}]})


class RunMatchTests(unittest.TestCase):
    def test_prefix_suffix_and_any(self):
        from ringing import _match_run
        front_up = self._rule("run", direction="up", position="front",
                              min_length=4)
        back_dn = self._rule("run", direction="down", position="back",
                             min_length=4)
        any_dn = self._rule("run", direction="down", position="any",
                            min_length=4)
        bells = tuple(range(6, 0, -1))  # 654321 (back rounds)
        self.assertEqual(_match_run(bells, front_up), [])
        self.assertEqual(_match_run(bells, back_dn),
                         [{"direction": "down", "start": 0, "length": 6}])
        self.assertEqual(_match_run(bells, any_dn),
                         [{"direction": "down", "start": 0, "length": 6}])

    @staticmethod
    def _rule(kind, **kw):
        base = {"id": "r", "name": "r", "kind": kind, "weight": 1,
                "strokes": ["hand", "back"], "lead_end": "any",
                "segments": []}
        base.update(kw)
        return base

    def test_longest_run_only_per_direction_and_range(self):
        from ringing import _match_run
        # permutation on 8 bells: an up/down check on a row with two down
        # runs - 8765 (length 4) at the front and 432 (length 3) later -
        # keeps only the longest run for the same direction+range
        row = (8, 7, 6, 5, 1, 4, 3, 2)
        rule = self._rule("run", direction="down", position="any",
                          min_length=3)
        self.assertEqual(_match_run(row, rule),
                         [{"direction": "down", "start": 0, "length": 4}])

    def test_both_directions_can_both_hit(self):
        from ringing import _match_run
        # 234 up at front, 987 down at back: direction 'both' yields both
        row = (2, 3, 4, 1, 5, 6, 9, 8, 7)
        rule = self._rule("run", direction="both", position="any",
                          min_length=3)
        hits = _match_run(row, rule)
        self.assertEqual(sorted((h["direction"], h["start"], h["length"])
                                for h in hits),
                         [("down", 6, 3), ("up", 0, 3)])

    def test_sequence_and_row_matching(self):
        from ringing import _match_sequence, _match_row
        seq = self._rule("sequence", bells=(1, 3, 5), pattern="135",
                         position="front")
        self.assertEqual(_match_sequence((1, 3, 5, 2, 6, 4), seq),
                         [{"start": 0, "length": 3}])
        self.assertEqual(_match_sequence((6, 1, 3, 5, 2, 4), seq), [])
        seq_any = dict(seq, position="any")
        self.assertEqual(_match_sequence((6, 1, 3, 5, 2, 4), seq_any),
                         [{"start": 1, "length": 3}])
        seq_back = dict(seq, position="back")
        self.assertEqual(_match_sequence((6, 2, 4, 1, 3, 5), seq_back),
                         [{"start": 3, "length": 3}])
        row_rule = self._rule("row", bells=(1, 2, 3, 4, 5, 6),
                              pattern="123456")
        self.assertEqual(_match_row((1, 2, 3, 4, 5, 6), row_rule),
                         [{"start": 0, "length": 6}])
        self.assertEqual(_match_row((1, 2, 3, 4, 6, 5), row_rule), [])


class ScoreAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.rep = analyze(6, PB_MINOR)
        self.method = {"id": 7, "name": "Plain Bob Minor", "version": 1}

    def score(self, rules, rep=None):
        scheme = parse_scheme({"name": "s", "rules": rules}, stage=6)
        return score_analysis(rep or self.rep, scheme, method=self.method)

    def test_run_weight_and_run_length(self):
        res = self.score([
            {"name": "front up", "kind": "run", "direction": "up",
             "position": "front", "min_length": 4, "weight": 2},
            {"name": "rounds", "kind": "row", "row": "123456"},
        ])
        runs = [r for r in res["rules"] if r["kind"] == "run"][0]
        # run score is weight * run length for every hit
        self.assertEqual(runs["score"],
                         sum(h["score"] for h in res["hits"]
                             if h["kind"] == "run"))
        self.assertTrue(all(h["score"] == 2 * h["length"]
                            for h in res["hits"] if h["kind"] == "run"))
        hit0 = next(h for h in res["hits"] if h["index"] == 0
                    and h["kind"] == "run")
        self.assertEqual((hit0["start"], hit0["length"], hit0["matched"],
                          hit0["position"], hit0["direction"]),
                         (1, 6, "123456", "front", "up"))
        # hits carry rule/row/index/position plus method/lead/change context
        self.assertEqual((hit0["rule"], hit0["row"], hit0["method_id"],
                          hit0["method"], hit0["version"], hit0["segment"]),
                         ("front up", "123456", 7, "Plain Bob Minor", 1, None))

    def test_index0_is_handstroke(self):
        self.assertEqual(stroke_for_index(0), "hand")
        self.assertEqual(stroke_for_index(1), "back")
        res = self.score([{"name": "r", "kind": "row", "row": "123456"}])
        h0 = next(h for h in res["hits"] if h["index"] == 0)
        self.assertEqual(h0["stroke"], "hand")
        self.assertFalse(h0["lead_end"])

    def test_stroke_filter(self):
        hand = self.score([{"name": "r", "kind": "row", "row": "123456",
                            "strokes": ["hand"]}])
        back = self.score([{"name": "r", "kind": "row", "row": "123456",
                            "strokes": ["back"]}])
        # rounds appears at index 0 (hand) and 60 (hand, lead end)
        self.assertEqual(hand["rules"][0]["hits"], 2)
        self.assertEqual(back["rules"][0]["hits"], 0)
        # unmatched rules keep zero values everywhere
        self.assertEqual(back["rules"][0]["score"], 0)
        self.assertEqual(back["rules"][0]["by_stroke"],
                         {"hand": {"hits": 0, "score": 0},
                          "back": {"hits": 0, "score": 0}})
        self.assertEqual(back["rules"][0]["by_method"], [])

    def test_lead_end_filter(self):
        le = self.score([{"name": "r", "kind": "row", "row": "123456",
                          "lead_end": "lead_end"}])
        nle = self.score([{"name": "r", "kind": "row", "row": "123456",
                           "lead_end": "not_lead_end"}])
        # only the closing index 60 (change 12 == lead length) is a lead end
        self.assertEqual([h["index"] for h in le["hits"]], [60])
        self.assertEqual([h["index"] for h in nle["hits"]], [0])
        self.assertTrue(all(h["lead_end"] for h in le["hits"]))
        self.assertFalse(any(h["lead_end"] for h in nle["hits"]))

    def test_aggregation_by_stroke_and_method(self):
        res = self.score([{"name": "r", "kind": "row", "row": "123456"}])
        (rule,) = res["rules"]
        self.assertEqual(rule["by_stroke"]["hand"],
                         {"hits": 2, "score": 2})
        self.assertEqual(rule["by_stroke"]["back"],
                         {"hits": 0, "score": 0})
        self.assertEqual(rule["by_method"],
                         [{"method_id": 7, "name": "Plain Bob Minor",
                           "version": 1, "hits": 2, "score": 2}])
        self.assertIsNone(rule["by_segment"])  # analyses have no segments

    def test_segment_rule_scores_zero_on_an_analysis(self):
        res = self.score([{"name": "r", "kind": "run", "direction": "both",
                           "position": "any", "min_length": 3,
                           "segments": [1]}])
        self.assertEqual(res["total_hits"], 0)
        self.assertEqual(res["rules"][0]["hits"], 0)

    def test_capped_report_is_partial_checked_rows_only(self):
        capped = analyze(6, PB_MINOR, max_rows=12)  # one lead only
        full = self.rep
        rules = [{"name": "r", "kind": "run", "direction": "both",
                  "position": "any", "min_length": 4}]
        p = self.score(rules, rep=capped)
        f = self.score(rules, rep=full)
        self.assertTrue(p["partial"])
        self.assertTrue(p["truncated"])
        self.assertEqual(p["rows_analyzed"], 13)
        self.assertEqual(p["checked_rows"], 12)
        self.assertFalse(p["truth"]["conclusive"])
        self.assertIsNone(p["truth"]["true"])
        self.assertLess(p["total_score"], f["total_score"])
        # the partial result must not be presented as a full-extent score
        self.assertIn("truth_inconclusive", p["problems"])


class ScoreTouchTests(unittest.TestCase):
    def setUp(self):
        self.touch = analyze_spliced([
            _seg(1, PB_MINOR, 2, name="PB"),
            _seg(2, PB_MINOR_14, 2, name="PB14"),
            _seg(1, PB_MINOR, 1, name="PB")])

    def score(self, rules):
        scheme = parse_scheme({"name": "s", "rules": rules}, stage=6)
        return score_touch(self.touch, scheme)

    def test_hit_context_and_segment_aggregation(self):
        res = self.score([{"name": "r", "kind": "run", "direction": "both",
                           "position": "any", "min_length": 4}])
        (rule,) = res["rules"]
        self.assertFalse(res["partial"])
        # segments include the starting row (segment 0, no method)
        seg_hits = {b["segment"]: b["hits"] for b in rule["by_segment"]}
        self.assertIn(0, seg_hits)
        methods = {b["method_id"]: b["hits"] for b in rule["by_method"]}
        self.assertIn(None, methods)  # index-0 row has no method
        self.assertTrue(set(methods) >= {1, 2})
        h_seg2 = next(h for h in res["hits"] if h["segment"] == 2)
        self.assertEqual((h_seg2["method_id"], h_seg2["version"]), (2, 1))
        self.assertEqual(h_seg2["method"], "PB14")
        # segment 2 starts after 24 rows (2 leads * 12): its lead/change
        # follow from the position inside the segment
        offset = h_seg2["index"] - 24
        self.assertEqual((h_seg2["lead"], h_seg2["change"]),
                         ((offset - 1) // 12 + 1, (offset - 1) % 12 + 1))

    def test_segment_filter(self):
        res = self.score([{"name": "r", "kind": "run", "direction": "both",
                           "position": "any", "min_length": 4,
                           "segments": [2]}])
        (rule,) = res["rules"]
        self.assertTrue(res["hits"])
        self.assertTrue(all(h["segment"] == 2 for h in res["hits"]))
        self.assertEqual(rule["hits"],
                         sum(1 for h in res["hits"] if h["segment"] == 2))
        self.assertEqual({b["segment"] for b in rule["by_segment"]}, {2})

    def test_stroke_alternation_across_segments(self):
        res = self.score([{"name": "r", "kind": "run", "direction": "both",
                           "position": "any", "min_length": 4}])
        for h in res["hits"]:
            self.assertEqual(h["stroke"],
                             "hand" if h["index"] % 2 == 0 else "back")


class CompareMusicTests(unittest.TestCase):
    def setUp(self):
        self.scheme = {"id": 1, "name": "s", "version": 1}
        rules = [{"id": "r1", "name": "rounds", "kind": "row",
                  "hits": 2, "score": 2}]
        self.a = self._result(1, stage=6, scheme=self.scheme, hits=2, score=2,
                              rules=rules)
        self.b = self._result(2, stage=6, scheme=self.scheme, hits=5, score=9,
                              rules=[{"id": "r1", "name": "rounds",
                                      "kind": "row", "hits": 5, "score": 9}])

    @staticmethod
    def _result(i, stage, scheme, hits, score, rules, partial=False,
                checked=60, kind="analysis", version="1.0"):
        return {"id": i, "kind": kind, "scoring_version": version,
                "stage": stage, "scheme": scheme, "partial": partial,
                "checked_rows": checked, "total_hits": hits,
                "total_score": score, "rules": rules}

    def test_comparable_per_rule_deltas(self):
        cmp = compare_music(self.a, self.b)
        self.assertTrue(cmp["comparable"])
        self.assertEqual(cmp["total_hits_delta"], 3)
        self.assertEqual(cmp["total_score_delta"], 7)
        (r,) = cmp["rules"]
        self.assertEqual((r["hits_delta"], r["score_delta"]), (3, 7))
        self.assertEqual((r["a"]["hits"], r["b"]["score"]), (2, 9))

    def test_missing_rule_keeps_zero(self):
        b = self._result(2, 6, self.scheme, 1, 4,
                         [{"id": "r2", "name": "other", "kind": "run",
                           "hits": 1, "score": 4}])
        cmp = compare_music(self.a, b)
        self.assertTrue(cmp["comparable"])
        self.assertFalse(cmp["rules_same"])
        by_id = {r["id"]: r for r in cmp["rules"]}
        self.assertEqual((by_id["r1"]["present_a"], by_id["r1"]["present_b"],
                          by_id["r1"]["b"]["score"]), (True, False, 0))
        self.assertEqual(by_id["r2"]["a"]["hits"], 0)

    def test_incomparable_stage_and_scheme(self):
        other_stage = self._result(3, 5, self.scheme, 1, 1, [])
        self.assertEqual(compare_music(self.a, other_stage)["reasons"],
                         ["stage"])
        other_scheme = self._result(4, 6, {"id": 9, "name": "x", "version": 1},
                                    1, 1, [])
        self.assertEqual(compare_music(self.a, other_scheme)["reasons"],
                         ["scheme"])
        v2 = self._result(5, 6, {"id": 1, "name": "s", "version": 2}, 1, 1, [])
        self.assertEqual(compare_music(self.a, v2)["reasons"],
                         ["scheme_version"])
        # same name+version through different stored ids stays comparable
        alias = self._result(6, 6, {"id": 99, "name": "s", "version": 1},
                             1, 1, [])
        self.assertTrue(compare_music(self.a, alias)["comparable"])

    def test_partial_flags_carried(self):
        partial = self._result(7, 6, self.scheme, 1, 1, [], partial=True,
                               checked=12)
        cmp = compare_music(self.a, partial)
        self.assertFalse(cmp["partial_a"])
        self.assertTrue(cmp["partial_b"])
        self.assertEqual((cmp["checked_rows_a"], cmp["checked_rows_b"]),
                         (60, 12))


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
                                {"name": "Bad", "stage": 13, "notation": "x"},
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

    def test_10_royal_and_maximus_flow(self):
        # Plain Bob Royal (10 bells): "10" is places 1 and 10
        status, m10 = self.call("POST", "/api/methods",
                                {"name": "Plain Bob Royal", "stage": 10,
                                 "notation": PB_ROYAL, "start_row": ROUNDS_10})
        self.assertEqual(status, 201)
        self.assertEqual(m10["lead_length"], 20)
        self.assertEqual(m10["changes"][1]["completed"], "10")
        self.assertEqual(m10["changes"][1]["places"], [1, 10])
        # without max_rows the job is refused: 10! > 1,000,000
        status, out = self.call("POST", "/api/analyses",
                                {"method_id": m10["id"]}, expect=400)
        self.assertEqual(out["code"], "limit_required")
        self.assertEqual(out["stage"], 10)
        self.assertEqual(out["extent_rows"], 3628800)
        self.assertEqual(out["hard_max_rows"], HARD_MAX_ROWS)
        # explicit cap within the hard limit runs the whole true course
        status, a10 = self.call("POST", "/api/analyses",
                                {"method_id": m10["id"], "max_rows": 100000})
        self.assertEqual(status, 201)
        self.assertEqual(a10["status"], "ok")
        self.assertEqual(a10["period_rows"], 180)
        self.assertEqual(a10["lead_head"], "1352749608")
        self.assertTrue(a10["truth"]["true"])
        # rows are compact symbols with 0 for bell 10
        status, rows = self.call("GET",
                                 f"/api/analyses/{a10['id']}/rows?from=0&to=1")
        self.assertEqual(rows["rows"][0]["row"], ROUNDS_10)
        # an explicit cap over the hard limit is rejected at the API
        status, out = self.call("POST", "/api/analyses",
                                {"method_id": m10["id"],
                                 "max_rows": HARD_MAX_ROWS + 1}, expect=400)
        self.assertIn(str(HARD_MAX_ROWS), out["error"])

        # Plain Bob Maximus (12 bells): T for bell 12
        status, m12 = self.call("POST", "/api/methods",
                                {"name": "Plain Bob Maximus", "stage": 12,
                                 "notation": PB_MAXIMUS})
        self.assertEqual(status, 201)
        self.assertEqual(m12["lead_length"], 24)
        self.assertEqual(m12["changes"][1]["completed"], "1T")
        # capped at 240 rows: not closed and truth is inconclusive only
        status, a12 = self.call("POST", "/api/analyses",
                                {"method_id": m12["id"], "max_rows": 240})
        self.assertEqual(status, 201)
        self.assertEqual(a12["status"], "exceeded_limit")
        self.assertFalse(a12["closed"])
        self.assertIsNone(a12["truth"]["true"])
        self.assertFalse(a12["truth"]["conclusive"])
        self.assertEqual(a12["truth"]["checked_rows"], 240)
        self.assertIn("truth_inconclusive", a12["problems"])
        self.assertEqual(a12["rows_generated"], 240)
        status, full = self.call("GET", f"/api/analyses/{a12['id']}")
        self.assertEqual(full["report"]["start_row"], ROUNDS_12)
        # full course with a cap high enough closes at 264 rows and is true
        status, a12b = self.call("POST", "/api/analyses",
                                 {"method_id": m12["id"], "max_rows": 1000})
        self.assertEqual(a12b["status"], "ok")
        self.assertEqual(a12b["period_rows"], 264)
        self.assertEqual(a12b["lead_head"], "13527496E8T0")
        self.assertTrue(a12b["truth"]["true"])

    def test_11_stage12_row_and_notation_errors(self):
        # duplicate bell in a compact row is located to the original symbol
        status, out = self.call("POST", "/api/methods",
                                {"name": "Bad Row", "stage": 12, "notation": "x",
                                 "start_row": "1234567890EE"}, expect=400)
        self.assertEqual(out["code"], "notation_error")
        self.assertEqual(out["token"], "E")
        self.assertEqual(out["offset"], 11)
        # leading whitespace is part of the source string and shifts offset
        status, out = self.call("POST", "/api/methods",
                                {"name": "Bad Row", "stage": 12, "notation": "x",
                                 "start_row": " 1234567890EE"}, expect=400)
        self.assertEqual((out["token"], out["offset"]), ("E", 12))
        status, out = self.call("POST", "/api/parse",
                                {"stage": 12, "notation": "x",
                                 "start_row": " 1234567890ET"})
        self.assertEqual(out["start_row"], ROUNDS_12)
        # missing bell (wrong length) likewise keeps token + offset
        status, out = self.call("POST", "/api/methods",
                                {"name": "Bad Row", "stage": 12, "notation": "x",
                                 "start_row": "1234567890E"}, expect=400)
        self.assertEqual(out["offset"], 11)
        self.assertIn("missing", out["error"])
        # place out of range on 10 bells (T = 12) keeps token + offset
        status, out = self.call("POST", "/api/parse",
                                {"stage": 10, "notation": "x.1T"}, expect=400)
        self.assertEqual(out["token"], "1T")
        self.assertEqual(out["offset"], 2)
        # parse endpoint normalizes every accepted start_row form
        for form in (ROUNDS_12, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
                     "1 2 3 4 5 6 7 8 9 10 11 12",
                     "1,2,3,4,5,6,7,8,9,0,E,T"):
            status, out = self.call("POST", "/api/parse",
                                    {"stage": 12, "notation": "x",
                                     "start_row": form})
            self.assertEqual(out["start_row"], ROUNDS_12, form)
        # spliced touch on 12 bells also needs the explicit cap
        status, m12 = self.call("POST", "/api/methods",
                                {"name": "Maximus for touch", "stage": 12,
                                 "notation": PB_MAXIMUS})
        status, out = self.call("POST", "/api/touches",
                                {"segments": [{"method_id": m12["id"],
                                               "leads": 1}]}, expect=400)
        self.assertEqual(out["code"], "limit_required")
        self.assertEqual(out["stage"], 12)
        status, t = self.call("POST", "/api/touches",
                              {"segments": [{"method_id": m12["id"], "leads": 1}],
                               "max_rows": 1000})
        self.assertEqual(status, 201)
        self.assertEqual(t["total_rows"], 24)
        status, full = self.call("GET", f"/api/touches/{t['id']}")
        self.assertEqual(full["report"]["rows"][0]["row"], ROUNDS_12)
        self.assertEqual(full["report"]["switches"], [])

    def test_12_api_index_advertises_stages(self):
        status, idx = self.call("GET", "/api")
        self.assertEqual(idx["max_stage"], 12)
        self.assertEqual(idx["hard_max_rows"], HARD_MAX_ROWS)
        self.assertEqual(idx["bell_symbols"], {"10": "0", "11": "E", "12": "T"})

    def test_13_scheme_and_music_flow(self):
        # a method version + its plain-course analysis
        status, m = self.call("POST", "/api/methods",
                              {"name": "Music PB Minor", "stage": 6,
                               "notation": PB_MINOR})
        status, a = self.call("POST", "/api/analyses", {"method_id": m["id"]})
        # a scoring scheme: weighted runs, a sequence and a whole row
        scheme_body = {"name": "minor-music", "stage": 6, "rules": [
            {"id": "front-up", "name": "front run up >=4", "kind": "run",
             "direction": "up", "position": "front", "min_length": 4,
             "weight": 2},
            {"id": "back-down", "name": "back run down >=4", "kind": "run",
             "direction": "down", "position": "back", "min_length": 4},
            {"id": "queens", "name": "135 at front", "kind": "sequence",
             "bells": "135", "position": "front"},
            {"id": "rounds", "name": "rounds handstroke", "kind": "row",
             "row": "123456", "strokes": ["hand"]}]}
        status, s = self.call("POST", "/api/schemes", scheme_body)
        self.assertEqual(status, 201)
        self.assertEqual(s["version"], 1)
        self.assertEqual(s["scoring_version"], "1.0")
        self.assertEqual(s["rule_count"], 4)
        # re-posting under the same name creates version 2
        status, s2 = self.call("POST", "/api/schemes",
                               {"name": "minor-music", "stage": 6,
                                "rules": scheme_body["rules"][:1]})
        self.assertEqual(s2["version"], 2)
        status, lst = self.call("GET", "/api/schemes")
        self.assertEqual([x["version"] for x in lst["schemes"]
                          if x["name"] == "minor-music"], [1, 2])
        status, detail = self.call("GET", f"/api/schemes/{s['id']}")
        self.assertEqual(detail["rules"][0]["min_length"], 4)
        self.assertEqual(detail["rules"][2]["bells"], "135")
        # score the plain course
        status, mu = self.call("POST", "/api/music",
                               {"analysis_id": a["id"], "scheme_id": s["id"]})
        self.assertEqual(status, 201)
        self.assertFalse(mu["partial"])
        self.assertEqual(mu["rows_analyzed"], 61)
        self.assertEqual(mu["checked_rows"], 60)
        self.assertEqual(mu["scheme"]["id"], s["id"])
        self.assertTrue(mu["total_hits"] >= 1)
        by_id = {r["id"]: r for r in mu["rule_scores"]}
        # rounds at index 0 and 60 are both handstroke
        self.assertEqual(by_id["rounds"]["hits"], 2)
        # full result keeps every rule with zero values preserved
        status, full = self.call("GET", f"/api/music/{mu['id']}")
        self.assertEqual(len(full["result"]["rules"]), 4)
        self.assertEqual(full["result"]["index0_stroke"], "hand")
        self.assertEqual(full["result"]["strokes"], ["hand", "back"])
        self.assertEqual(full["scheme"]["name"], "minor-music")

        # hit filtering: rule, stroke, lead end, kind and an index slice
        status, hits = self.call(
            "GET", f"/api/music/{mu['id']}/hits?rule_id=rounds")
        self.assertTrue(all(h["rule_id"] == "rounds" for h in hits["hits"]))
        status, hits = self.call(
            "GET", f"/api/music/{mu['id']}/hits?stroke=hand")
        self.assertTrue(all(h["stroke"] == "hand" for h in hits["hits"]))
        status, hits = self.call(
            "GET", f"/api/music/{mu['id']}/hits?lead_end=true")
        self.assertTrue(all(h["lead_end"] for h in hits["hits"]))
        self.assertIn(60, [h["index"] for h in hits["hits"]])
        status, hits = self.call(
            "GET", f"/api/music/{mu['id']}/hits?kind=run&from=0&to=6")
        self.assertTrue(all(h["kind"] == "run" and h["index"] <= 6
                            for h in hits["hits"]))
        status, out = self.call(
            "GET", f"/api/music/{mu['id']}/hits?stroke=bob", expect=400)
        self.assertEqual(out["code"], "bad_field")

        # download: attachment with the full scheme rules embedded
        req = urllib.request.Request(
            self.base + f"/api/music/{mu['id']}/download")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("attachment", resp.headers["Content-Disposition"])
            payload = json.loads(resp.read().decode())
        self.assertEqual(payload["scheme"]["rule_count"], 4)
        self.assertEqual(payload["result"]["total_score"],
                         mu["total_score"])

    def test_14_music_partial_under_cap_and_compare(self):
        status, m = self.call("POST", "/api/methods",
                              {"name": "Music Cap PB", "stage": 6,
                               "notation": PB_MINOR})
        # one-lead cap: partial score over the checked rows only
        status, cap = self.call("POST", "/api/analyses",
                                {"method_id": m["id"], "max_rows": 12})
        status, full_a = self.call("POST", "/api/analyses",
                                   {"method_id": m["id"]})
        status, s = self.call("POST", "/api/schemes",
                              {"name": "cap-music", "stage": 6, "rules": [
                                  {"name": "r", "kind": "run",
                                   "direction": "both", "position": "any",
                                   "min_length": 4}]})
        status, mu_full = self.call("POST", "/api/music",
                                    {"analysis_id": full_a["id"],
                                     "scheme_id": s["id"]})
        status, mu_cap = self.call("POST", "/api/music",
                                   {"analysis_id": cap["id"],
                                    "scheme_id": s["id"]})
        self.assertTrue(mu_cap["partial"])
        self.assertEqual(mu_cap["checked_rows"], 12)
        self.assertLess(mu_cap["total_score"], mu_full["total_score"])
        # comparable: same stage, same scheme version; per-rule deltas listed
        status, cmp = self.call(
            "GET", f"/api/music/compare?a={mu_full['id']}&b={mu_cap['id']}")
        self.assertEqual(status, 200)
        c = cmp["comparison"]
        self.assertTrue(c["comparable"])
        self.assertTrue(c["partial_b"])
        self.assertEqual(c["checked_rows_b"], 12)
        self.assertLess(c["total_score_delta"], 0)
        self.assertEqual(len(c["rules"]), 1)
        self.assertLess(c["rules"][0]["hits_delta"], 0)

        # scheme v2 under the same name makes the results incomparable
        status, s2 = self.call("POST", "/api/schemes",
                               {"name": "cap-music", "stage": 6, "rules": [
                                   {"name": "r", "kind": "run",
                                    "direction": "up", "position": "front",
                                    "min_length": 3}]})
        status, mu_v2 = self.call("POST", "/api/music",
                                  {"analysis_id": full_a["id"],
                                   "scheme_id": s2["id"]})
        status, out = self.call(
            "GET", f"/api/music/compare?a={mu_full['id']}&b={mu_v2['id']}",
            expect=400)
        self.assertEqual(out["code"], "incomparable")
        self.assertEqual(out["reasons"], ["scheme_version"])
        # cross-stage comparisons are refused too
        status, gd = self.call("POST", "/api/methods",
                               {"name": "Music GD", "stage": 5,
                                "notation": "3.1.5.1.5.1.5.1.5.125"})
        status, a5 = self.call("POST", "/api/analyses",
                               {"method_id": gd["id"]})
        status, s5 = self.call("POST", "/api/schemes",
                               {"name": "five", "stage": 5, "rules": [
                                   {"name": "r", "kind": "row",
                                    "row": "12345"}]})
        status, mu5 = self.call("POST", "/api/music",
                                {"analysis_id": a5["id"],
                                 "scheme_id": s5["id"]})
        status, out = self.call(
            "GET", f"/api/music/compare?a={mu_full['id']}&b={mu5['id']}",
            expect=400)
        self.assertIn("stage", out["reasons"])

    def test_15_touch_music_with_segments(self):
        status, m1 = self.call("POST", "/api/methods",
                               {"name": "Music Touch PB", "stage": 6,
                                "notation": PB_MINOR})
        status, m2 = self.call("POST", "/api/methods",
                               {"name": "Music Touch PB14", "stage": 6,
                                "notation": PB_MINOR_14})
        status, t = self.call("POST", "/api/touches", {"segments": [
            {"method_id": m1["id"], "leads": 2},
            {"method_id": m2["id"], "leads": 2},
            {"method_id": m1["id"], "leads": 1}]})
        status, s = self.call("POST", "/api/schemes",
                              {"name": "touch-music", "stage": 6, "rules": [
                                  {"id": "runs", "name": "four-runs",
                                   "kind": "run", "direction": "both",
                                   "position": "any", "min_length": 4},
                                  {"id": "seg2", "name": "segment 2 only",
                                   "kind": "run", "direction": "both",
                                   "position": "any", "min_length": 4,
                                   "segments": [2]}]})
        status, mu = self.call("POST", "/api/music",
                               {"touch_id": t["id"], "scheme_id": s["id"]})
        self.assertEqual(status, 201)
        self.assertEqual(mu["kind"], "touch")
        self.assertFalse(mu["partial"])
        status, full = self.call("GET", f"/api/music/{mu['id']}")
        rules = {r["id"]: r for r in full["result"]["rules"]}
        self.assertTrue(rules["seg2"]["hits"] <= rules["runs"]["hits"])
        self.assertTrue(all(b["segment"] == 2
                            for b in rules["seg2"]["by_segment"]))
        # method aggregation spans the two method versions
        method_ids = {b["method_id"] for b in rules["runs"]["by_method"]}
        self.assertTrue(method_ids & {m1["id"], m2["id"]})
        # hit endpoint segment filter
        status, hits = self.call(
            "GET", f"/api/music/{mu['id']}/hits?segment=2&rule_id=seg2")
        self.assertTrue(hits["hits"])
        self.assertTrue(all(h["segment"] == 2 and h["rule_id"] == "seg2"
                            for h in hits["hits"]))
        # listing with filters
        status, lst = self.call("GET", "/api/music?kind=touch")
        self.assertTrue(all(x["kind"] == "touch"
                            for x in lst["music_analyses"]))
        status, lst = self.call("GET", f"/api/music?scheme_id={s['id']}")
        self.assertTrue(all(x["scheme_id"] == s["id"]
                            for x in lst["music_analyses"]))

    def test_16_music_errors(self):
        # bad rule located by rule index/field
        status, out = self.call("POST", "/api/schemes",
                                {"name": "bad", "stage": 6, "rules": [
                                    {"name": "ok", "kind": "row",
                                     "row": "123456"},
                                    {"name": "x", "kind": "run",
                                     "direction": "sideways"}]}, expect=400)
        self.assertEqual(out["code"], "bad_rule")
        self.assertEqual((out["rule"], out["field"]), (1, "direction"))
        # row permutation error keeps token/offset plus the rule index
        status, out = self.call("POST", "/api/schemes",
                                {"name": "bad2", "stage": 6, "rules": [
                                    {"name": "r", "kind": "row",
                                     "row": "123455"}]}, expect=400)
        self.assertEqual(out["code"], "bad_rule")
        self.assertEqual((out["rule"], out["token"], out["offset"]),
                         (0, "5", 5))
        status, out = self.call("POST", "/api/schemes",
                                {"name": "", "stage": 6, "rules": [
                                    {"name": "r", "kind": "row",
                                     "row": "123456"}]}, expect=400)
        self.assertEqual(out["code"], "bad_field")
        # unknown scheme / analysis
        status, out = self.call("POST", "/api/music",
                                {"analysis_id": 1, "scheme_id": 999999},
                                expect=404)
        self.assertEqual(out["code"], "not_found")
        status, out = self.call("POST", "/api/music",
                                {"analysis_id": 999999, "scheme_id": 1},
                                expect=404)
        # must provide exactly one subject
        status, out = self.call("POST", "/api/music", {"scheme_id": 1},
                                expect=400)
        self.assertEqual(out["code"], "missing_field")
        # stage mismatch between scheme and subject
        status, m = self.call("POST", "/api/methods",
                              {"name": "Music Mismatch", "stage": 6,
                               "notation": PB_MINOR})
        status, a = self.call("POST", "/api/analyses", {"method_id": m["id"]})
        status, s5 = self.call("POST", "/api/schemes",
                               {"name": "five-mis", "stage": 5, "rules": [
                                   {"name": "r", "kind": "row",
                                    "row": "12345"}]})
        status, out = self.call("POST", "/api/music",
                                {"analysis_id": a["id"],
                                 "scheme_id": s5["id"]}, expect=400)
        self.assertEqual(out["code"], "bad_rule")
        # unknown resources on reads
        status, out = self.call("GET", "/api/schemes/9999", expect=404)
        status, out = self.call("GET", "/api/music/9999", expect=404)
        status, out = self.call("GET", "/api/music/9999/hits", expect=404)
        status, out = self.call("GET", "/api/music/compare", expect=400)
        # non-object bodies and non-integer ids are 400, never 500
        status, out = self.call("POST", "/api/schemes", [1, 2], expect=400)
        self.assertEqual(out["code"], "bad_field")
        status, out = self.call("POST", "/api/music", [], expect=400)
        self.assertEqual(out["code"], "bad_field")
        status, out = self.call("POST", "/api/music",
                                {"analysis_id": a["id"], "scheme_id": "x"},
                                expect=400)
        self.assertEqual(out["code"], "bad_field")
        status, out = self.call("GET", "/api/music?kind=bogus", expect=400)
        self.assertEqual(out["code"], "bad_field")

    def test_18_scheme_stage_inherited_from_reference(self):
        status, m = self.call("POST", "/api/methods",
                              {"name": "Inherit PB", "stage": 6,
                               "notation": PB_MINOR})
        status, a = self.call("POST", "/api/analyses", {"method_id": m["id"]})
        # no stage in the body: it is taken from the referenced analysis
        status, s = self.call("POST", "/api/schemes",
                              {"name": "inherited", "analysis_id": a["id"],
                               "rules": [{"name": "r", "kind": "row",
                                          "row": "123456"}]})
        self.assertEqual(status, 201)
        self.assertEqual(s["stage"], 6)
        # an unknown reference is a 404 located before scheme validation
        status, out = self.call("POST", "/api/schemes",
                                {"name": "inherited-x",
                                 "analysis_id": 999999,
                                 "rules": [{"name": "r", "kind": "row",
                                            "row": "123456"}]}, expect=404)
        self.assertEqual(out["code"], "not_found")

    def test_17_royal_music_symbols(self):
        # 10 bells: bell 10 is '0' in rules, rows and hits
        status, m10 = self.call("POST", "/api/methods",
                                {"name": "Music Royal", "stage": 10,
                                 "notation": PB_ROYAL,
                                 "start_row": ROUNDS_10})
        status, a10 = self.call("POST", "/api/analyses",
                                {"method_id": m10["id"], "max_rows": 100000})
        status, s10 = self.call("POST", "/api/schemes",
                                {"name": "royal-music", "stage": 10,
                                 "rules": [
                                     {"name": "lead head", "kind": "row",
                                      "row": "1352749608"},
                                     {"name": "0-front sequence",
                                      "kind": "sequence", "bells": [10, 8],
                                      "position": "back"}]})
        status, mu = self.call("POST", "/api/music",
                               {"analysis_id": a10["id"],
                                "scheme_id": s10["id"]})
        self.assertEqual(status, 201)
        by_name = {r["name"]: r for r in mu["rule_scores"]}
        self.assertGreaterEqual(by_name["lead head"]["hits"], 1)
        status, hits = self.call(
            "GET", f"/api/music/{mu['id']}/hits?rule_id=rule-1")
        self.assertTrue(any("0" in h["row"] for h in hits["hits"]))
        # T (bell 12) is out of range on 10 bells and located
        status, out = self.call("POST", "/api/schemes",
                                {"name": "royal-bad", "stage": 10, "rules": [
                                    {"name": "s", "kind": "sequence",
                                     "bells": "0T"}]}, expect=400)
        self.assertEqual((out["token"], out["offset"]), ("T", 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
