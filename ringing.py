"""Core change-ringing engine: place-notation parsing, row expansion, truth
analysis and spliced-touch composition.

Only Python's standard library is used so the whole API works offline.

Stages: 4-12 bells.  Above bell 9 the single-character royal/maximus symbols
are used everywhere a row or place is rendered compactly:
    bell 10 -> '0',  bell 11 -> 'E',  bell 12 -> 'T'
so rounds on 12 bells is "1234567890ET".  These are *single* symbols: in place
notation "10" means places 1 AND 10, never the two-digit number ten; to name
bell 10 on its own write "0".  Array input still uses plain integers
([1, ..., 10, 11, 12]); separated string input ("1,2,...,10,11,12") accepts
both the integers 10/11/12 and the symbols 0/E/T.

Place-notation syntax (4-12 bells):
    x / X / -   a cross change (all adjacent pairs swap), no places made
    1..9 0 E T  places made; each character is one place, e.g. "16", "1256",
                "10" (places 1 and 10), "1T" (places 1 and 12)
    .           separates changes (optional around x/-); whitespace ignored
    ,           symmetric expansion: "a,b" -> a + reverse(a[:-1]) + b, i.e.
                the last change of a is the half-lead pivot (mirrored, not
                repeated) and b is the lead-end change(s); e.g. "x16x16x16,12"
                on 6 bells gives the 12 changes of Plain Bob Minor

For every change, lead/lie places that can be inferred from the stage are
completed first (e.g. "3" on 6 bells becomes "36"); every remaining position
must then pair up as an adjacent swap.  Errors (illegal character, place out
of range, duplicate place, unpairable places; a row that is not a permutation)
are reported against the original token and offset, never silently corrected.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

MIN_STAGE = 4
MAX_STAGE = 12
HARD_MAX_ROWS = 1_000_000
CROSS_CHARS = "xX-"
# index i is the canonical symbol for bell i+1: 1..9, 0 (10), E (11), T (12)
BELL_SYMBOLS = "1234567890ET"
PLACE_CHARS = BELL_SYMBOLS
SYMBOL_TO_BELL = {ch: i + 1 for i, ch in enumerate(BELL_SYMBOLS)}
# 10! = 3,628,800 already exceeds the hard cap, so stages 10-12 need an
# explicit max_rows before any job can be created.
EXTENT_LIMIT_STAGE = next(n for n in range(MIN_STAGE, MAX_STAGE + 1)
                          if math.factorial(n) > HARD_MAX_ROWS)


class NotationError(ValueError):
    """A place-notation or row error, located to the original token.

    Attributes:
        message: human readable description
        token:   the offending token copied verbatim from the input
        offset:  0-based character offset (notation/row strings) or element
                 index (array rows / token lists) of the token
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


class LimitRequiredError(ValueError):
    """The default cap (stage! rows) exceeds the hard cap, so an explicit
    max_rows is required before the job can be created."""

    def __init__(self, stage, limit):
        message = (
            f"stage {stage}: one extent is {limit:,} rows which exceeds the "
            f"hard limit of {HARD_MAX_ROWS:,}; pass an explicit max_rows "
            f"(1..{HARD_MAX_ROWS:,}) to create the job anyway - truth can then "
            f"only be judged over the rows actually checked")
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.extent_rows = limit
        self.hard_max_rows = HARD_MAX_ROWS

    def to_dict(self):
        return {"error": self.message, "stage": self.stage,
                "extent_rows": self.extent_rows, "hard_max_rows": HARD_MAX_ROWS}


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
    """Validate the number of bells (4-12)."""
    if isinstance(stage, bool) or not isinstance(stage, int):
        raise NotationError("stage must be an integer")
    if not MIN_STAGE <= stage <= MAX_STAGE:
        raise NotationError(
            f"stage must be between {MIN_STAGE} and {MAX_STAGE} bells, got {stage}")


def bell_symbol(bell):
    """Canonical compact symbol for one bell number: 1..9, 0, E, T."""
    return BELL_SYMBOLS[bell - 1]


def validate_max_rows(stage, max_rows):
    """Resolve a requested row cap against the stage.

    No cap given and the extent (stage!) within the hard limit -> the extent.
    No cap given above the hard limit -> LimitRequiredError (no job).
    An explicit cap must be an int in 1..HARD_MAX_ROWS.
    """
    extent = math.factorial(stage)
    if max_rows is None:
        if extent > HARD_MAX_ROWS:
            raise LimitRequiredError(stage, extent)
        return extent
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
        raise ValueError("max_rows must be a positive integer")
    if max_rows > HARD_MAX_ROWS:
        raise ValueError(f"max_rows may not exceed {HARD_MAX_ROWS}")
    return max_rows


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
        """Canonical notation after place completion, e.g. 'x' or '10'."""
        if self.cross:
            return "x"
        return "".join(bell_symbol(p) for p in sorted(self.places))

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
            place = SYMBOL_TO_BELL[ch]
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


def _decode_bell_token(token):
    """Decode one row token: '1'..'9','0','E','T' or '10','11','12'.

    Returns the bell number or None for an illegal token.
    """
    if len(token) == 1:
        return SYMBOL_TO_BELL.get(token)
    # separated input only: multi-character tokens must be the plain
    # integers 10/11/12 (compact rows never contain a multi-char token)
    if token.isdigit():
        value = int(token)
        if 10 <= value <= 12:
            return value
    return None


def parse_row(value, stage):
    """Normalize a row to a tuple of bell integers 1..stage.

    Accepted forms (all describe the same permutation):
      None                 -> rounds
      [1, 2, ..., 12]      -> array of integers (the only form for bells > 9
                             that does not use symbols)
      "1234567890ET"       -> compact row: one symbol per bell, bells 10/11/12
                             written 0/E/T
      "1,2,...,10,11,12"   -> separated tokens; integers 10/11/12 and the
                             symbols 0/E/T are both accepted

    Must be a permutation of 1..stage.  Illegal symbols, duplicates, bells
    out of stage and missing bells are reported as NotationError against the
    original token and its offset (character offset in strings, element index
    in arrays); nothing is silently corrected.
    """
    check_stage(stage)
    if value is None:
        return tuple(range(1, stage + 1))
    # pieces: parallel list of (bell, original_token, offset) for error reports
    pieces = []
    token_text = None
    if isinstance(value, (list, tuple)):
        # array input stays integer-only: each element is one bell number
        for i, v in enumerate(value):
            if isinstance(v, bool) or not isinstance(v, int) \
                    or not 1 <= v <= stage:
                raise NotationError(
                    f"invalid bell {v!r} for {stage} bells "
                    f"(array rows use integer bell numbers 1..{stage})",
                    token=v, offset=i)
            pieces.append((v, v, i))
        token_text = "[" + ",".join(str(p[0]) for p in pieces) + "]"
    elif isinstance(value, str):
        text = value.replace(",", " ").replace(".", " ").strip()
        if not text:
            raise NotationError("start_row must not be empty",
                                token=value, offset=0)
        token_text = value
        compact = not any(c.isspace() for c in text)
        if compact:
            # compact row: each single character is one bell symbol
            for i, ch in enumerate(text):
                bell = SYMBOL_TO_BELL.get(ch)
                if bell is None or bell > stage:
                    raise NotationError(
                        f"invalid bell symbol {ch!r} for {stage} bells "
                        f"(bells above 9 are written 0=10, E=11, T=12)",
                        token=ch, offset=i)
                pieces.append((bell, ch, i))
        else:
            # separated tokens: plain integers 10/11/12 or single symbols
            search_from = 0
            for tok in text.split():
                pos = value.find(tok, search_from)
                search_from = pos + len(tok)
                bell = _decode_bell_token(tok)
                if bell is None or bell > stage:
                    raise NotationError(
                        f"invalid bell token {tok!r} for {stage} bells",
                        token=tok, offset=pos)
                pieces.append((bell, tok, pos))
    else:
        raise ValueError("start_row must be a string or a list of bells")

    if len(pieces) != stage:
        present = {bell for bell, _, _ in pieces}
        missing = [b for b in range(1, stage + 1) if b not in present]
        extras = sorted(bell for bell in present if bell > stage)
        detail = ""
        if missing:
            detail += ": missing bell(s) " + "".join(bell_symbol(b) for b in missing)
        if extras:
            detail += "; bell(s) out of stage: " + ",".join(map(str, extras))
        raise NotationError(
            f"row has {len(pieces)} bells, expected {stage} "
            f"(each bell 1..{stage} exactly once){detail}",
            token=token_text, offset=len(pieces))
    seen = set()
    for bell, token, offset in pieces:
        if bell in seen:
            raise NotationError(
                f"bell {bell} appears more than once in the row",
                token=token, offset=offset)
        seen.add(bell)
    missing = [b for b in range(1, stage + 1) if b not in seen]
    if missing:
        raise NotationError(
            f"row is not a permutation of 1..{stage}: missing bell(s) "
            + "".join(bell_symbol(b) for b in missing),
            token=token_text, offset=len(pieces))
    return tuple(bell for bell, _, _ in pieces)


def row_str(row):
    """Render a row tuple in canonical compact symbols.

    Bells 1..9 are themselves, bell 10 is '0', bell 11 'E', bell 12 'T',
    e.g. (1..12) -> '1234567890ET'.
    """
    return "".join(bell_symbol(b) for b in row)


def _locate(index, lead_len):
    """Map a row index to its {'index', 'lead', 'change'} position (1-based)."""
    if index <= 0:
        return {"index": 0, "lead": 0, "change": 0}
    return {"index": index,
            "lead": (index - 1) // lead_len + 1,
            "change": (index - 1) % lead_len + 1}


def analyze(stage, notation, start_row=None, overrides=None, max_rows=None):
    """Expand a method lead by lead and check its truth up to the row cap.

    The first repeated row (a premature return to the start row mid-lead
    counts as a repeat of row 0) is recorded with both positions, but the
    expansion keeps going so the closure period and the full trajectory are
    always reported; it stops when the start row returns at a lead boundary
    or when max_rows rows have been generated.

    overrides: list of {"lead": L, "change": K, "notation": "14"} replacing
        change K of lead L by a parsed single-change notation (a "call").
    max_rows:  safety cap on generated rows; defaults to one extent (stage!).
        On stages 10-12 the extent exceeds the hard limit, so omitting the cap
        raises LimitRequiredError and an explicit value <= the hard limit is
        mandatory.

    If the cap stops an expansion that had not repeated and had not closed,
    truth is reported as inconclusive ("truth.true" is null) rather than
    "true": only the checked range is claimed.

    Returns a report dict.  Raises NotationError / ValueError on bad input.
"""
    check_stage(stage)
    changes = parse_notation(notation, stage)
    start = parse_row(start_row, stage)
    lead_len = len(changes)
    extent = math.factorial(stage)
    # No cap given: default to one extent; when the extent is above the hard
    # limit (stages 10-12) an explicit cap is required to create the job.
    max_rows = validate_max_rows(stage, max_rows)

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
    # Truth can be decided over the rows actually checked only when either a
    # repeat was found (untrue) or the course closed (every row of the cycle
    # was seen).  If the cap stopped an as-yet-unrepeated expansion, no full
    # truth conclusion is possible: we report what was checked, not "true".
    truth_conclusive = closed or first_repeat is not None
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
        if not truth_conclusive:
            problems.append("truth_inconclusive")
    truth = {"true": (first_repeat is None) if truth_conclusive else None,
             "conclusive": truth_conclusive,
             "checked_rows": step,
             "first_repeat": first_repeat}

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
        "truth": truth,
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
    nothing is expanded in that case.  With no max_rows given the cap is one
    extent; on stages 10-12 that is above the hard limit and LimitRequiredError
    is raised before any segment is expanded.
    """
    if not isinstance(segments, (list, tuple)) or not segments:
        raise SplicedError("segments must be a non-empty list")

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
            # resolve the cap now that the stage is known: no cap given on
            # 10+ bells (stage! above the hard limit) refuses the job
            effective_max = validate_max_rows(stage, effective_max)
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
    # The whole planned touch is expanded (total_rows <= the cap is enforced
    # up front), so truth is always judged over every planned row.
    truth_conclusive = True
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
        "truth": {"true": first_repeat is None,
                  "conclusive": truth_conclusive,
                  "checked_rows": total_rows,
                  "first_repeat": first_repeat},
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


def _truth_value(report):
    """truth.true for a report, tolerating pre-1.1 stored reports and the
    inconclusive form {"true": null, "conclusive": false}."""
    truth = report["truth"]
    if not truth.get("conclusive", True):
        return None
    return truth["true"]


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
    ta, tb = _truth_value(report_a), _truth_value(report_b)
    return {
        "a": _touch_summary(report_a),
        "b": _touch_summary(report_b),
        "same_stage": report_a["stage"] == report_b["stage"],
        "total_rows_equal": report_a["total_rows"] == report_b["total_rows"],
        "total_rows_delta": report_a["total_rows"] - report_b["total_rows"],
        "both_closed": report_a["closed"] and report_b["closed"],
        # null when either side's truth is inconclusive (checked under a cap)
        "both_true": (ta is True and tb is True) if (ta is not None and tb is not None) else None,
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
    ta, tb = _truth_value(report_a), _truth_value(report_b)
    return {
        "a": _summary(report_a),
        "b": _summary(report_b),
        "period_rows_equal": pa is not None and pa == pb,
        "period_rows_delta": (pa - pb) if pa is not None and pb is not None else None,
        "both_closed": report_a["closed"] and report_b["closed"],
        # null when either side's truth is inconclusive (checked under a cap)
        "both_true": (ta is True and tb is True) if (ta is not None and tb is not None) else None,
        "first_repeat_same_position": same_repeat,
    }
