"""Core change-ringing engine: place-notation parsing, row expansion, truth
analysis and spliced-touch composition.

Only Python's standard library is used so the whole API works offline.

Place-notation syntax (4-8 bells):
    x / X / -   a cross change (all adjacent pairs swap), no places made
    1..8        places made; each digit is one place, e.g. "16" or "1256"
    .           separates changes (optional around x/-); whitespace ignored
    ,           symmetric expansion: "a,b" -> a + reverse(a[:-1]) + b, i.e.
                the last change of a is the half-lead pivot (mirrored, not
                repeated) and b is the lead-end change(s); e.g. "x16x16x16,12"
                on 6 bells gives the 12 changes of Plain Bob Minor

For every change, lead/lie places that can be inferred from the stage are
completed first (e.g. "3" on 6 bells becomes "36"); every remaining position
must then pair up as an adjacent swap.  Errors (illegal character, place out
of range, duplicate place, unpairable places) are reported against the
original token and are never silently corrected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MIN_STAGE = 4
MAX_STAGE = 8
CROSS_CHARS = "xX-"
PLACE_CHARS = "123456789"
HARD_MAX_ROWS = 1_000_000


class NotationError(ValueError):
    """A place-notation error, located to the original token.

    Attributes:
        message: human readable description
        token:   the offending token copied verbatim from the notation
        offset:  0-based character offset of the token in the notation string
    """

    def __init__(self, message, token=None, offset=None):
        super().__init__(message)
        self.message = message
        self.token = token
        self.offset = offset

    def to_dict(self):
        out = {"error": self.message}
        if self.token is not None:
            out["token"] = self.token
        if self.offset is not None:
            out["offset"] = self.offset
        return out


class SplicedError(ValueError):
    """A spliced-touch error, located to a segment (1-based).

    Attributes:
        message:  human readable description
        segment:  1-based index of the offending segment (if applicable)
        override: 0-based index of the offending override within the segment
        extra:    additional structured details (token, offset, totals...)
    """

    def __init__(self, message, segment=None, override=None, extra=None):
        super().__init__(message)
        self.message = message
        self.segment = segment
        self.override = override
        self.extra = extra or {}

    def to_dict(self):
        out = {"error": self.message}
        if self.segment is not None:
            out["segment"] = self.segment
        if self.override is not None:
            out["override"] = self.override
        out.update(self.extra)
        return out


def check_stage(stage):
    """Validate the number of bells (4-8)."""
    if isinstance(stage, bool) or not isinstance(stage, int):
        raise NotationError("stage must be an integer")
    if not MIN_STAGE <= stage <= MAX_STAGE:
        raise NotationError(
            f"stage must be between {MIN_STAGE} and {MAX_STAGE} bells, got {stage}")


@dataclass(frozen=True)
class Change:
    """One change: a set of places made plus adjacent swap pairs (1-based)."""

    places: frozenset  # places that stay
    swaps: tuple       # tuple of (a, b) adjacent pairs that swap
    token: str         # original token text from the notation
    offset: int        # 0-based offset in the source notation
    cross: bool

    def apply(self, row):
        row = list(row)
        for a, b in self.swaps:
            row[a - 1], row[b - 1] = row[b - 1], row[a - 1]
        return tuple(row)

    def completed(self):
        """Canonical notation after place completion, e.g. 'x' or '16'."""
        if self.cross:
            return "x"
        return "".join(str(p) for p in sorted(self.places))

    def to_dict(self):
        return {
            "token": self.token,
            "offset": self.offset,
            "completed": self.completed(),
            "places": sorted(self.places),
            "swaps": [list(s) for s in self.swaps],
            "cross": self.cross,
        }


def _tokenize(part, base_offset):
    """Split one notation part into (token, offset) pairs."""
    tokens = []
    i = 0
    while i < len(part):
        ch = part[i]
        if ch == "." or ch.isspace():
            i += 1
        elif ch in CROSS_CHARS:
            tokens.append((ch, base_offset + i))
            i += 1
        elif ch in PLACE_CHARS:
            j = i
            while j < len(part) and part[j] in PLACE_CHARS:
                j += 1
            tokens.append((part[i:j], base_offset + i))
            i = j
        else:
            raise NotationError(f"illegal character {ch!r} in notation",
                                token=ch, offset=base_offset + i)
    return tokens


def _build_change(token, offset, stage):
    """Validate one token and build its Change, completing inferable places."""
    if len(token) == 1 and token in CROSS_CHARS:
        places = set()
    else:
        places = set()
        for ch in token:
            place = int(ch)
            if place < 1 or place > stage:
                raise NotationError(
                    f"place {place} out of range for {stage} bells",
                    token=token, offset=offset)
            if place in places:
                raise NotationError(
                    f"duplicate place {place} in change",
                    token=token, offset=offset)
            places.add(place)
    # Complete inferable lead (place 1) / lie (place `stage`) places: an odd
    # number of unplaced positions below the lowest / above the highest
    # explicit place forces an implied place at that end.
    if places:
        if (min(places) - 1) % 2 == 1:
            places.add(1)
        if (stage - max(places)) % 2 == 1:
            places.add(stage)
    # Whatever remains unplaced must pair up as adjacent swaps.
    swaps = []
    pos = 1
    while pos <= stage:
        if pos in places:
            pos += 1
            continue
        if pos + 1 > stage or (pos + 1) in places:
            raise NotationError(
                "places cannot be paired as adjacent swaps",
                token=token, offset=offset)
        swaps.append((pos, pos + 1))
        pos += 2
    return Change(frozenset(places), tuple(swaps), token, offset, cross=not places)


def parse_notation(notation, stage):
    """Parse place notation into a list of Change objects (one lead).

    Syntax: 'x'/'-' = cross (all change); digits = places made; '.' separates
    changes; a single comma mirrors the first part: 'a,b' expands to
    a + reverse(a[:-1]) + b (the last change of a is the half-lead pivot,
    mirrored but not repeated; b is the lead-end change appended at the end).

    Raises NotationError locating the original token on any invalid input.
    """
    check_stage(stage)
    if not isinstance(notation, str) or not notation.strip():
        raise NotationError("notation must be a non-empty string")
    commas = [i for i, c in enumerate(notation) if c == ","]
    if len(commas) > 1:
        raise NotationError("at most one ',' is allowed (symmetric expansion)",
                            token=",", offset=commas[1])
    if commas:
        cut = commas[0]
        part_a = _tokenize(notation[:cut], 0)
        part_b = _tokenize(notation[cut + 1:], cut + 1)
        if not part_a or not part_b:
            raise NotationError("both sides of ',' must contain at least one change",
                                token=",", offset=cut)
        # Symmetric expansion: the last change of part A is the half-lead
        # pivot; mirror A without repeating the pivot, then append B (the
        # lead-end change).  "x16x16x16,12" -> the 12 changes of PB Minor.
        raw = part_a + list(reversed(part_a[:-1])) + part_b
    else:
        raw = _tokenize(notation, 0)
    if not raw:
        raise NotationError("notation contains no changes")
    return [_build_change(token, offset, stage) for token, offset in raw]


def parse_row(value, stage):
    """Normalize a row given as '123456', '1,2,3,4,5,6' or [1,2,3,4,5,6].

    Defaults to rounds.  Must be a permutation of 1..stage.
    """
    if value is None:
        return tuple(range(1, stage + 1))
    if isinstance(value, (list, tuple)):
        try:
            row = tuple(int(v) for v in value)
        except (TypeError, ValueError):
            raise ValueError("start_row must contain bell numbers")
    elif isinstance(value, str):
        text = value.replace(",", " ").replace(".", " ").strip()
        if " " in text:
            try:
                row = tuple(int(p) for p in text.split())
            except ValueError:
                raise ValueError(f"invalid start_row {value!r}")
        else:
            if not text or any(c not in PLACE_CHARS for c in text):
                raise ValueError(f"invalid start_row {value!r}")
            row = tuple(int(c) for c in text)
    else:
        raise ValueError("start_row must be a string or a list of bells")
    if sorted(row) != list(range(1, stage + 1)):
        raise ValueError(f"start_row must be a permutation of 1..{stage}")
    return row


def row_str(row):
    """Render a row tuple as a string, e.g. (1,3,5,2,6,4) -> '135264'."""
    return "".join(str(b) for b in row)


def _locate(index, lead_len):
    """Map a row index to its {'index', 'lead', 'change'} position (1-based)."""
    if index <= 0:
        return {"index": 0, "lead": 0, "change": 0}
    return {"index": index,
            "lead": (index - 1) // lead_len + 1,
            "change": (index - 1) % lead_len + 1}


def analyze(stage, notation, start_row=None, overrides=None, max_rows=None):
    """Expand a method lead by lead and check its truth within one extent.

    The first repeated row (a premature return to the start row mid-lead
    counts as a repeat of row 0) is recorded with both positions, but the
    expansion keeps going so the closure period and the full trajectory are
    always reported; it stops when the start row returns at a lead boundary
    or when max_rows rows have been generated.

    overrides: list of {"lead": L, "change": K, "notation": "14"} replacing
        change K of lead L by a parsed single-change notation (a "call").
    max_rows:  safety cap on generated rows; defaults to one extent (stage!).

    Returns a report dict.  Raises NotationError / ValueError on bad input.
    """
    check_stage(stage)
    changes = parse_notation(notation, stage)
    start = parse_row(start_row, stage)
    lead_len = len(changes)
    extent = math.factorial(stage)
    if max_rows is None:
        max_rows = extent
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
        raise ValueError("max_rows must be a positive integer")
    if max_rows > HARD_MAX_ROWS:
        raise ValueError(f"max_rows may not exceed {HARD_MAX_ROWS}")

    # Composition overrides: {(lead, change_index): Change}
    override_map = {}
    override_reports = []
    for idx, spec in enumerate(overrides or []):
        if not isinstance(spec, dict):
            raise ValueError(f"override #{idx}: must be an object")
        try:
            lead = int(spec["lead"])
            pos = int(spec["change"])
            ovr_notation = spec["notation"]
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                f"override #{idx}: needs integer 'lead' and 'change' plus 'notation'")
        if lead < 1:
            raise ValueError(f"override #{idx}: lead must be >= 1")
        if not 1 <= pos <= lead_len:
            raise ValueError(
                f"override #{idx}: change must be between 1 and {lead_len}")
        ovr_changes = parse_notation(ovr_notation, stage)  # may raise NotationError
        if len(ovr_changes) != 1:
            raise ValueError(
                f"override #{idx}: notation must be exactly one change, "
                f"got {len(ovr_changes)}")
        override_map[(lead, pos)] = ovr_changes[0]
        override_reports.append({
            "lead": lead,
            "change": pos,
            "notation": ovr_notation,
            "parsed": ovr_changes[0].to_dict(),
            "replaces_token": changes[pos - 1].token,
            "applied": False,
        })

    rows = [start]
    entries = [{"index": 0, "lead": 0, "change": 0, "row": row_str(start),
                "token": None, "override": False, "repeat": False}]
    seen = {start: 0}
    first_repeat = None
    premature = None
    closed = False
    step = 0
    # Expand until the start row comes back at a lead boundary (closure) or
    # the row limit is hit.  Repeats - including a premature return to the
    # start row mid-lead - are recorded but do NOT stop the expansion, so
    # the closure period and the full trajectory are always reported.
    while step < max_rows:
        lead = step // lead_len + 1
        pos = step % lead_len + 1
        is_override = (lead, pos) in override_map
        change = override_map[(lead, pos)] if is_override else changes[pos - 1]
        nxt = change.apply(rows[-1])
        step += 1
        rows.append(nxt)
        closing = nxt == start and step % lead_len == 0
        is_repeat = nxt in seen and not closing
        if is_repeat and first_repeat is None:
            first_repeat = {"row": row_str(nxt),
                            "first": _locate(seen[nxt], lead_len),
                            "second": _locate(step, lead_len)}
        if nxt == start and not closing and premature is None:
            premature = _locate(step, lead_len)
        entries.append({"index": step, "lead": lead, "change": pos,
                        "row": row_str(nxt), "token": change.token,
                        "override": is_override, "repeat": is_repeat})
        if is_override:
            for rep in override_reports:
                if rep["lead"] == lead and rep["change"] == pos:
                    rep["applied"] = True
                    rep["row_index"] = step
                    rep["before_row"] = row_str(rows[step - 1])
                    rep["after_row"] = row_str(nxt)
        if closing:
            closed = True
            break
        if not is_repeat:
            seen[nxt] = step

    lead_head = rows[lead_len] if len(rows) > lead_len else None
    hunt_bells = []
    if lead_head is not None:
        hunt_bells = [b for b in range(1, stage + 1)
                      if lead_head.index(b) == start.index(b)]
    if not closed:
        status = "exceeded_limit"
    elif premature is not None:
        status = "premature_rounds"
    elif first_repeat is not None:
        status = "untrue"
    else:
        status = "ok"
    problems = []
    if first_repeat is not None:
        problems.append("untrue")
    if premature is not None:
        problems.append("premature_rounds")
    if not closed:
        problems.extend(["exceeded_limit", "not_closed"])

    return {
        "stage": stage,
        "notation": notation,
        "start_row": row_str(start),
        "changes": [c.to_dict() for c in changes],
        "lead_length": lead_len,
        "lead_head": row_str(lead_head) if lead_head else None,
        "status": status,          # ok | untrue | premature_rounds | exceeded_limit
        "closed": closed,
        "period_leads": step // lead_len if closed else None,
        "period_rows": step if closed else None,
        "rows_generated": step,
        "max_rows": max_rows,
        "extent_rows": extent,
        "hunt_bells": hunt_bells,
        "working_bells": [b for b in range(1, stage + 1) if b not in hunt_bells],
        "truth": {"true": first_repeat is None, "first_repeat": first_repeat},
        "premature_rounds": premature,
        "overrides": override_reports,
        "problems": problems,
        "rows": entries,
    }


def _locate_spliced(index, layout):
    """Map a touch row index to {'index', 'segment', 'lead', 'change'} (1-based)."""
    if index <= 0:
        return {"index": 0, "segment": 0, "lead": 0, "change": 0}
    for seg_i, lay in enumerate(layout, start=1):
        if index <= lay["to_index"]:
            offset = index - lay["from_index"]
            return {"index": index, "segment": seg_i,
                    "lead": (offset - 1) // lay["lead_length"] + 1,
                    "change": (offset - 1) % lay["lead_length"] + 1}
    return {"index": index, "segment": None, "lead": None, "change": None}


def _notation_details(err):
    out = {}
    if err.token is not None:
        out["token"] = err.token
    if err.offset is not None:
        out["offset"] = err.offset
    return out


def analyze_spliced(segments, start_row=None, max_rows=None):
    """Expand a spliced touch across method segments and check its truth.

    segments: list of {"method_id", "name", "version", "stage", "notation",
        "leads", "overrides"?}.  Every segment must use the same number of
        bells; "leads" gives the number of whole leads to ring, so method
        switches only ever happen at lead boundaries.  "overrides" replace
        single changes inside the segment: {"lead": L, "change": K,
        "notation": "14"} with L counted from 1 within the segment.

    The touch starts at start_row (default rounds); each following segment
    continues from the previous segment's last row - the methods' own
    start_rows are NOT re-applied.  Repeated rows, a premature return to
    the start row, closure at the end and the row limit are judged across
    the whole touch, never from the individual methods' own truth.

    Raises SplicedError (located to a segment) on inconsistent stages, bad
    leads, out-of-range overrides or a cumulative row count above max_rows;
    nothing is expanded in that case.
    """
    if not isinstance(segments, (list, tuple)) or not segments:
        raise SplicedError("segments must be a non-empty list")
    if max_rows is not None:
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) \
                or max_rows < 1:
            raise ValueError("max_rows must be a positive integer")
        if max_rows > HARD_MAX_ROWS:
            raise ValueError(f"max_rows may not exceed {HARD_MAX_ROWS}")

    infos = []
    stage = None
    effective_max = max_rows
    total_rows = 0
    total_leads = 0
    for i, spec in enumerate(segments, start=1):
        if not isinstance(spec, dict):
            raise SplicedError("segment must be an object", segment=i)
        seg_stage = spec.get("stage")
        check_stage(seg_stage)  # NotationError on a bad/missing stage
        if stage is None:
            stage = seg_stage
            if effective_max is None:
                effective_max = math.factorial(stage)
        elif seg_stage != stage:
            raise SplicedError(
                f"stage mismatch: segment has {seg_stage} bells, "
                f"the touch is on {stage}", segment=i,
                extra={"stage": seg_stage, "expected": stage})
        leads = spec.get("leads")
        if isinstance(leads, bool) or not isinstance(leads, int) or leads < 1:
            raise SplicedError("leads must be a positive integer", segment=i,
                               extra={"leads": leads})
        try:
            changes = parse_notation(spec.get("notation"), stage)
        except NotationError as err:
            raise SplicedError(err.message, segment=i,
                               extra=_notation_details(err))
        lead_len = len(changes)
        # Per-segment overrides: {(lead, change): Change}; when two specs
        # target the same change the last one wins and the earlier one is
        # reported as superseded (never applied).
        override_map = {}
        override_owner = {}
        override_reports = []
        for j, ospec in enumerate(spec.get("overrides") or []):
            if not isinstance(ospec, dict):
                raise SplicedError("override must be an object",
                                   segment=i, override=j)
            try:
                olead = int(ospec["lead"])
                opos = int(ospec["change"])
                onotation = ospec["notation"]
            except (KeyError, TypeError, ValueError):
                raise SplicedError(
                    "override needs integer 'lead' and 'change' plus 'notation'",
                    segment=i, override=j)
            if not 1 <= olead <= leads:
                raise SplicedError(
                    f"override lead must be between 1 and {leads} "
                    f"(the segment's lead count)", segment=i, override=j,
                    extra={"lead": olead, "leads": leads})
            if not 1 <= opos <= lead_len:
                raise SplicedError(
                    f"override change must be between 1 and {lead_len} "
                    f"(the lead length)", segment=i, override=j,
                    extra={"change": opos, "lead_length": lead_len})
            try:
                ochanges = parse_notation(onotation, stage)
            except NotationError as err:
                raise SplicedError(err.message, segment=i, override=j,
                                   extra=_notation_details(err))
            if len(ochanges) != 1:
                raise SplicedError(
                    f"override notation must be exactly one change, "
                    f"got {len(ochanges)}", segment=i, override=j)
            key = (olead, opos)
            if key in override_owner:
                override_owner[key]["superseded"] = True
            rep = {"segment": i, "lead": olead, "change": opos,
                   "notation": onotation, "parsed": ochanges[0].to_dict(),
                   "replaces_token": changes[opos - 1].token, "applied": False}
            override_map[key] = ochanges[0]
            override_owner[key] = rep
            override_reports.append(rep)
        total_rows += leads * lead_len
        total_leads += leads
        if total_rows > effective_max:
            raise SplicedError(
                f"total rows {total_rows} exceed the limit of {effective_max}",
                segment=i, extra={"total_rows": total_rows,
                                  "max_rows": effective_max})
        infos.append({"method_id": spec.get("method_id"),
                      "name": spec.get("name"),
                      "version": spec.get("version"),
                      "leads": leads, "lead_len": lead_len, "changes": changes,
                      "override_map": override_map,
                      "override_owner": override_owner,
                      "override_reports": override_reports})

    start = parse_row(start_row, stage)
    extent = math.factorial(stage)

    # Row layout: segment i covers the global indexes (from_index, to_index].
    layout = []
    cum = 0
    for info in infos:
        n = info["leads"] * info["lead_len"]
        layout.append({"from_index": cum, "to_index": cum + n,
                       "lead_length": info["lead_len"]})
        cum += n

    rows = [start]
    entries = [{"index": 0, "segment": 0, "method_id": None, "method": None,
                "version": None, "lead": 0, "change": 0, "row": row_str(start),
                "token": None, "override": None, "repeat": False}]
    seen = {start: 0}
    first_repeat = None
    premature = None
    step = 0
    # Expand the whole composition: every segment rings all of its leads,
    # continuing from the previous segment's last row.  Repeats and a
    # premature return to the start row are recorded against the whole
    # touch; the expansion never resets to a method's own start row.
    for i, info in enumerate(infos, start=1):
        changes = info["changes"]
        lead_len = info["lead_len"]
        for lead in range(1, info["leads"] + 1):
            for pos in range(1, lead_len + 1):
                key = (lead, pos)
                is_override = key in info["override_map"]
                change = (info["override_map"][key] if is_override
                          else changes[pos - 1])
                nxt = change.apply(rows[-1])
                step += 1
                rows.append(nxt)
                closing = nxt == start and step == total_rows
                is_repeat = nxt in seen and not closing
                if is_repeat and first_repeat is None:
                    first_repeat = {"row": row_str(nxt),
                                    "first": _locate_spliced(seen[nxt], layout),
                                    "second": _locate_spliced(step, layout)}
                if nxt == start and not closing and premature is None:
                    premature = _locate_spliced(step, layout)
                override_src = None
                if is_override:
                    rep = info["override_owner"][key]
                    rep["applied"] = True
                    rep["row_index"] = step
                    rep["before_row"] = row_str(rows[step - 1])
                    rep["after_row"] = row_str(nxt)
                    override_src = {"segment": i, "lead": lead, "change": pos,
                                    "notation": rep["notation"],
                                    "replaces_token": rep["replaces_token"]}
                entries.append({"index": step, "segment": i,
                                "method_id": info["method_id"],
                                "method": info["name"],
                                "version": info["version"],
                                "lead": lead, "change": pos,
                                "row": row_str(nxt), "token": change.token,
                                "override": override_src, "repeat": is_repeat})
                if not is_repeat:
                    seen[nxt] = step

    closed = rows[-1] == start
    if not closed:
        status = "not_closed"
    elif premature is not None:
        status = "premature_rounds"
    elif first_repeat is not None:
        status = "untrue"
    else:
        status = "ok"
    problems = []
    if first_repeat is not None:
        problems.append("untrue")
    if premature is not None:
        problems.append("premature_rounds")
    if not closed:
        problems.append("not_closed")

    switches = []
    for i in range(1, len(infos)):
        boundary = layout[i]["from_index"]
        prev, cur = infos[i - 1], infos[i]
        switches.append({
            "at_index": boundary,
            "from_segment": i, "to_segment": i + 1,
            "from_method_id": prev["method_id"], "from_method": prev["name"],
            "from_version": prev["version"],
            "to_method_id": cur["method_id"], "to_method": cur["name"],
            "to_version": cur["version"],
            "before_row": row_str(rows[boundary]),
            "after_row": row_str(rows[boundary + 1]),
        })

    used = {}
    for i, info in enumerate(infos, start=1):
        mid = info["method_id"]
        entry = used.setdefault(mid, {"method_id": mid, "name": info["name"],
                                      "version": info["version"],
                                      "leads": 0, "rows": 0, "segments": []})
        entry["leads"] += info["leads"]
        entry["rows"] += info["leads"] * info["lead_len"]
        entry["segments"].append(i)

    seg_summaries = []
    for i, info in enumerate(infos, start=1):
        lay = layout[i - 1]
        seg_summaries.append({
            "index": i, "method_id": info["method_id"], "name": info["name"],
            "version": info["version"], "leads": info["leads"],
            "lead_length": info["lead_len"],
            "rows": lay["to_index"] - lay["from_index"],
            "from_index": lay["from_index"], "to_index": lay["to_index"],
            "start_row": row_str(rows[lay["from_index"]]),
            "end_row": row_str(rows[lay["to_index"]]),
            "overrides": info["override_reports"],
        })

    all_overrides = [rep for info in infos for rep in info["override_reports"]]
    unapplied = []
    for rep in all_overrides:
        if rep["applied"]:
            continue
        brief = {k: rep[k] for k in
                 ("segment", "lead", "change", "notation", "replaces_token")}
        if rep.get("superseded"):
            brief["reason"] = "superseded by a later override for the same change"
        unapplied.append(brief)

    return {
        "stage": stage,
        "start_row": row_str(start),
        "segments": seg_summaries,
        "segment_count": len(infos),
        "total_leads": total_leads,
        "total_rows": total_rows,
        "rows_generated": step,
        "max_rows": effective_max,
        "extent_rows": extent,
        "status": status,          # ok | untrue | premature_rounds | not_closed
        "closed": closed,
        "truth": {"true": first_repeat is None, "first_repeat": first_repeat},
        "premature_rounds": premature,
        "switches": switches,
        "methods_used": list(used.values()),
        "overrides": all_overrides,
        "unapplied_overrides": unapplied,
        "problems": problems,
        "rows": entries,
    }


def _touch_summary(report):
    keys = ("status", "closed", "stage", "total_rows", "total_leads",
            "segment_count", "problems")
    out = {k: report[k] for k in keys}
    out["truth"] = report["truth"]
    out["methods_used"] = [{k: m[k] for k in
                            ("method_id", "name", "version", "leads", "rows")}
                           for m in report["methods_used"]]
    return out


def compare_touch_reports(report_a, report_b):
    """Compare two spliced-touch reports: size, closure, truth, methods."""
    fa = report_a["truth"]["first_repeat"]
    fb = report_b["truth"]["first_repeat"]
    same_repeat = None
    if fa and fb:
        same_repeat = ((fa["first"]["index"], fa["second"]["index"])
                       == (fb["first"]["index"], fb["second"]["index"]))
    ids_a = {m["method_id"] for m in report_a["methods_used"]}
    ids_b = {m["method_id"] for m in report_b["methods_used"]}
    return {
        "a": _touch_summary(report_a),
        "b": _touch_summary(report_b),
        "same_stage": report_a["stage"] == report_b["stage"],
        "total_rows_equal": report_a["total_rows"] == report_b["total_rows"],
        "total_rows_delta": report_a["total_rows"] - report_b["total_rows"],
        "both_closed": report_a["closed"] and report_b["closed"],
        "both_true": report_a["truth"]["true"] and report_b["truth"]["true"],
        "first_repeat_same_position": same_repeat,
        "methods_overlap": sorted(ids_a & ids_b),
    }


def _summary(report):
    keys = ("status", "closed", "period_leads", "period_rows", "lead_length",
            "lead_head", "hunt_bells", "problems")
    out = {k: report[k] for k in keys}
    out["truth"] = report["truth"]
    return out


def compare_reports(report_a, report_b):
    """Compare two analysis reports: periods, truth and repeat positions."""
    fa = report_a["truth"]["first_repeat"]
    fb = report_b["truth"]["first_repeat"]
    pa, pb = report_a["period_rows"], report_b["period_rows"]
    same_repeat = None
    if fa and fb:
        same_repeat = ((fa["first"]["index"], fa["second"]["index"])
                       == (fb["first"]["index"], fb["second"]["index"]))
    return {
        "a": _summary(report_a),
        "b": _summary(report_b),
        "period_rows_equal": pa is not None and pa == pb,
        "period_rows_delta": (pa - pb) if pa is not None and pb is not None else None,
        "both_closed": report_a["closed"] and report_b["closed"],
        "both_true": report_a["truth"]["true"] and report_b["truth"]["true"],
        "first_repeat_same_position": same_repeat,
    }
