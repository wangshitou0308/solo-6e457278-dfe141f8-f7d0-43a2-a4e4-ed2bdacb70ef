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

# Version of the musicality-scoring model (rule normalization, hit semantics
# and score aggregation).  Two scores are only comparable when their stage and
# scheme version - scored under the same SCORING_VERSION - both match.
SCORING_VERSION = "1.0"

# Musicality rule kinds.
RULE_RUN = "run"            # consecutive ascending/descending bell runs
RULE_SEQUENCE = "sequence"  # a specified ordered bell sequence
RULE_ROW = "row"            # an exact whole row
RULE_KINDS = (RULE_RUN, RULE_SEQUENCE, RULE_ROW)
# Where inside a row a run/sequence may be found.
POSITION_FRONT = "front"    # the leading positions (treble end)
POSITION_BACK = "back"      # the trailing positions (tenor end)
POSITION_ANY = "any"        # anywhere in the row
RUN_POSITIONS = (POSITION_FRONT, POSITION_BACK, POSITION_ANY)
# Stroke convention: row index 0 (the starting row) is ALWAYS handstroke;
# even indexes ring handstroke, odd indexes backstroke.
STROKE_HAND = "hand"
STROKE_BACK = "back"
STROKES = (STROKE_HAND, STROKE_BACK)


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


class MultipartError(ValueError):
    """A multi-part replay error; nothing is expanded or stored.

    Attributes:
        message: human readable description
        code:    machine readable code - "bad_replay" (generic),
                 "bad_parts" (part count missing/illegal) or "too_large"
                 (the full replay would exceed the row cap)
        part:    1-based part the error is located to (when applicable)
        extra:   additional structured details
    """

    def __init__(self, message, code="bad_replay", part=None, extra=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.part = part
        self.extra = extra or {}

    def to_dict(self):
        out = {"error": self.message}
        if self.part is not None:
            out["part"] = self.part
        out.update(self.extra)
        return out


class SchemeError(ValueError):
    """A musicality-scheme rule error, located to the rule index (0-based).

    Attributes:
        message: human readable description
        rule:    0-based index of the offending rule in the scheme
        field:   offending field name (when applicable)
        token:   offending row/sequence token (row rules reuse NotationError
                 positioning, compact symbols only)
        offset:  0-based offset of the token
    """

    def __init__(self, message, rule=None, field=None, token=None, offset=None):
        super().__init__(message)
        self.message = message
        self.rule = rule
        self.field = field
        self.token = token
        self.offset = offset

    def to_dict(self):
        out = {"error": self.message}
        if self.rule is not None:
            out["rule"] = self.rule
        if self.field is not None:
            out["field"] = self.field
        if self.token is not None:
            out["token"] = self.token
        if self.offset is not None:
            out["offset"] = self.offset
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
        # separators are replaced one-for-one, so normalized has the same
        # length as value; positions inside `text` map back to value via the
        # stripped leading prefix (leading spaces, commas or dots).
        normalized = value.replace(",", " ").replace(".", " ")
        text = normalized.strip()
        if not text:
            raise NotationError("start_row must not be empty",
                                token=value, offset=0)
        token_text = value
        base = normalized.find(text)  # offset of the first token in value
        compact = not any(c.isspace() for c in text)
        if compact:
            # compact row: each single character is one bell symbol
            for i, ch in enumerate(text):
                bell = SYMBOL_TO_BELL.get(ch)
                if bell is None or bell > stage:
                    raise NotationError(
                        f"invalid bell symbol {ch!r} for {stage} bells "
                        f"(bells above 9 are written 0=10, E=11, T=12)",
                        token=ch, offset=base + i)
                pieces.append((bell, ch, base + i))
            # position just past the last symbol in the source string
            end_offset = base + len(text)
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
            end_offset = pieces[-1][2] + len(pieces[-1][1])
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
        # strings: offset just past the last source token; arrays: element
        # index one past the last element
        length_offset = end_offset if isinstance(value, str) else len(pieces)
        raise NotationError(
            f"row has {len(pieces)} bells, expected {stage} "
            f"(each bell 1..{stage} exactly once){detail}",
            token=token_text, offset=length_offset)
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
            token=token_text,
            offset=(end_offset if isinstance(value, str) else len(pieces)))
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


def _prepare_segments(segments, max_rows=None):
    """Validate spliced/replay segments and parse their changes.

    Shared by analyze_spliced() (one round of the segments) and
    analyze_multipart() (several parts replaying the same segments).  Each
    spec is {"method_id", "name", "version", "stage", "notation", "leads",
    "overrides"?}; every segment must use the same number of bells and its
    leads/overrides must be in range.  The cumulative row count must stay at
    or below the resolved cap.

    Returns (stage, effective_max, infos, total_rows, total_leads) where each
    info carries the parsed changes and prepared override maps.  Raises
    SplicedError located to a segment; with no cap on stages 10-12 it raises
    LimitRequiredError before anything is expanded.
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
    return stage, effective_max, infos, total_rows, total_leads


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
    stage, effective_max, infos, total_rows, total_leads = \
        _prepare_segments(segments, max_rows)
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


# ============================================================ multi-part replay
# A "touch" in the store is one round of a list of method segments (see
# analyze_spliced).  A multi-part replay rings the SAME touch "parts" times:
# part 2 continues from part 1's last row, part 3 from part 2's, and so on -
# a part is never reset to the start row, so the row shared by two adjacent
# parts is stored/counted exactly once (the global row list is
# 1 + parts * touch_rows entries).
#
# Truth (repeats, a premature return to the start row, closure at the very
# end) is judged across the whole replay, never per part.  Ringing the touch
# once from rounds applies a fixed permutation P to the bells (the "part-end
# permutation"); after k parts the permutation is P**k, so the replay closes
# exactly when parts is a multiple of P's order.  The order is exported from
# part 1's start/end rows and any parts/order disagreement is reported.
def _permutation_order(mapping):
    """Order of a permutation given as a 1-based mapping dict (p -> image)."""
    order = 1
    seen = set()
    for p in mapping:
        if p in seen:
            continue
        cycle_len = 0
        q = p
        while q not in seen:
            seen.add(q)
            q = mapping[q]
            cycle_len += 1
        order = order * cycle_len // math.gcd(order, cycle_len)
    return order


def _part_end_permutation(start, end):
    """The fixed permutation one part applies, exported from any part's
    start/end rows (both bell tuples of the same stage).

    A fixed sequence of changes applies one fixed permutation of POSITIONS:
    the bell at start position p ends at end position q(p).  Labeling
    positions by the bells of rounds (position p "is" bell p), the part
    permutation is p -> q(p); its cycle structure/order is the same for every
    start row.  Applied to rounds, position j of the end row holds the bell
    Q^{-1}(j), i.e. canonical_end is the inverse row of Q (same cycle
    structure/order) - it is exactly the end row rung from rounds.

    Returns (position_map, canonical_end_row): position_map maps the 1-based
    start position p to its destination position q(p); canonical_end_row is
    the part-end row rung from rounds (tuple of bells by position).
    """
    stage = len(start)
    end_position = {bell: pos for pos, bell in enumerate(end)}
    position_map = {}
    for pos, bell in enumerate(start):
        position_map[pos + 1] = end_position[bell] + 1
    canonical_end = [None] * stage
    for p, q in position_map.items():
        canonical_end[q - 1] = p
    return position_map, tuple(canonical_end)


def _multipart_layout(infos):
    """Global (from_index, to_index] layout of one part from its segments."""
    layout = []
    cum = 0
    for info in infos:
        n = info["leads"] * info["lead_len"]
        layout.append({"from_index": cum, "to_index": cum + n,
                       "lead_length": info["lead_len"]})
        cum += n
    return layout


def _locate_multipart(index, part, part_offset, layout):
    """Map a global replay index to part/segment/lead/change (1-based)."""
    if index <= 0:
        return {"index": 0, "part": 0, "segment": 0, "lead": 0, "change": 0}
    for seg_i, lay in enumerate(layout, start=1):
        if part_offset <= lay["to_index"]:
            off = part_offset - lay["from_index"]
            return {"index": index, "part": part, "segment": seg_i,
                    "lead": (off - 1) // lay["lead_length"] + 1,
                    "change": (off - 1) % lay["lead_length"] + 1}
    return {"index": index, "part": part, "segment": None,
            "lead": None, "change": None}


def analyze_multipart(segments, parts, start_row=None, max_rows=None):
    """Replay one validated segment list `parts` times and check the result.

    segments: the same segment specs analyze_spliced() takes (resolved method
        versions with stage/notation/leads/overrides); the list describes ONE
        part ("the touch").  The whole planned replay must fit in the row
        cap: its length is 1 + parts * touch_rows and every adjacent pair of
        parts shares its boundary row, counted once.
    parts: number of parts, a positive integer.  Each part continues from the
        previous part's last row; no part is reset to start_row.

    Truth is judged across the entire replay: a non-closing repeated row is
    "untrue", a return to start_row before the final global row is
    "premature_rounds", and the final row is expected back at start_row
    ("closed").  The part-end permutation is exported from part 1's start/end
    rows together with its order; the replay only closes for a multiple of
    that order, and a parts/order disagreement is reported explicitly even
    when the status is otherwise fine.  A result that is not closed is never
    reported as a success.

    Raises MultipartError on a bad part count or an over-size replay
    (nothing expanded), LimitRequiredError without a cap on stages 10-12 and
    SplicedError located to a segment on bad segment specs.
    """
    if isinstance(parts, bool) or not isinstance(parts, int) or parts < 1:
        raise MultipartError(
            "parts must be a positive integer", code="bad_parts",
            extra={"parts": parts})
    stage, effective_max, infos, part_rows, total_leads_one = \
        _prepare_segments(segments, max_rows)
    # the planned replay: one shared starting row plus `parts` rounds of the
    # touch, with each inter-part boundary row counted exactly once
    total_rows = parts * part_rows
    if total_rows > effective_max:
        raise MultipartError(
            f"total rows {total_rows} ({parts} parts x {part_rows}) exceed "
            f"the limit of {effective_max}", code="too_large",
            extra={"parts": parts, "part_rows": part_rows,
                   "total_rows": total_rows, "max_rows": effective_max,
                   "hard_max_rows": HARD_MAX_ROWS})
    start = parse_row(start_row, stage)
    extent = math.factorial(stage)
    layout = _multipart_layout(infos)

    rows = [start]
    entries = [{"index": 0, "part": 0, "segment": 0, "method_id": None,
                "method": None, "version": None, "lead": 0, "change": 0,
                "row": row_str(start), "token": None, "override": None,
                "repeat": False}]
    seen = {start: 0}
    first_repeat = None
    premature = None
    step = 0
    part_summaries = []
    boundaries = []
    # methods aggregated over every part: {(method_id, name, version): usage}
    used = {}

    for part in range(1, parts + 1):
        part_start_index = step
        part_start_row = rows[-1]
        part_overrides = []
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
                    part_offset = step - part_start_index
                    if is_repeat and first_repeat is None:
                        first_index = seen[nxt]
                        first_part = ((first_index - 1) // part_rows + 1
                                      if first_index > 0 else 0)
                        first_offset = (first_index
                                        - (first_part - 1) * part_rows)
                        first_repeat = {
                            "row": row_str(nxt),
                            "first": _locate_multipart(
                                first_index, first_part, first_offset, layout),
                            "second": _locate_multipart(
                                step, part, part_offset, layout)}
                    if nxt == start and not closing and premature is None:
                        premature = _locate_multipart(
                            step, part, part_offset, layout)
                    override_src = None
                    if is_override:
                        rep = info["override_owner"][key]
                        if not rep["applied"]:
                            rep["applied"] = True
                            rep["row_index"] = step
                            rep["part"] = part
                            rep["before_row"] = row_str(rows[step - 1])
                            rep["after_row"] = row_str(nxt)
                        part_overrides.append({
                            "segment": i, "lead": lead, "change": pos,
                            "notation": rep["notation"],
                            "replaces_token": rep["replaces_token"]})
                        override_src = {"part": part, "segment": i,
                                        "lead": lead, "change": pos,
                                        "notation": rep["notation"],
                                        "replaces_token": rep["replaces_token"]}
                    entries.append({"index": step, "part": part, "segment": i,
                                    "method_id": info["method_id"],
                                    "method": info["name"],
                                    "version": info["version"],
                                    "lead": lead, "change": pos,
                                    "row": row_str(nxt), "token": change.token,
                                    "override": override_src,
                                    "repeat": is_repeat})
                    if not is_repeat:
                        seen[nxt] = step
            mid = info["method_id"]
            entry = used.setdefault(
                mid, {"method_id": mid, "name": info["name"],
                      "version": info["version"], "leads": 0, "rows": 0,
                      "parts": [], "segments_in_part": []})
            entry["leads"] += info["leads"]
            entry["rows"] += info["leads"] * info["lead_len"]
            if part not in entry["parts"]:
                entry["parts"].append(part)
            if i not in entry["segments_in_part"]:
                entry["segments_in_part"].append(i)
        part_end_row = rows[-1]
        seg_summaries = []
        for i, info in enumerate(infos, start=1):
            lay = layout[i - 1]
            g_from = part_start_index + lay["from_index"]
            g_to = part_start_index + lay["to_index"]
            seg_summaries.append({
                "index": i, "method_id": info["method_id"],
                "name": info["name"], "version": info["version"],
                "leads": info["leads"], "lead_length": info["lead_len"],
                "rows": lay["to_index"] - lay["from_index"],
                "from_index": g_from, "to_index": g_to,
                "start_row": row_str(rows[g_from]),
                "end_row": row_str(rows[g_to]),
                "overrides": [o for o in part_overrides
                              if o["segment"] == i]})
        part_summaries.append({
            "part": part,
            "from_index": part_start_index, "to_index": step,
            "start_row": row_str(part_start_row),
            "end_row": row_str(part_end_row),
            "rows": step - part_start_index,
            "segments": seg_summaries})
        if part > 1:
            boundaries.append({
                "between_parts": [part - 1, part], "at_index": part_start_index,
                "row": row_str(part_start_row),
                "from_method_id": infos[-1]["method_id"],
                "from_method": infos[-1]["name"],
                "from_version": infos[-1]["version"],
                "to_method_id": infos[0]["method_id"],
                "to_method": infos[0]["name"],
                "to_version": infos[0]["version"],
                "counted_once": True})

    closed = rows[-1] == start
    # part-end permutation exported from part 1: the fixed position
    # permutation one part applies; its canonical row is the end row ringing
    # the part FROM ROUNDS would give, and the cycle structure (order) is the
    # same for any start row.  P**k sends rounds back to rounds iff k is a
    # multiple of order(P).
    p1_start, p1_end = start, rows[part_rows]
    position_map, canonical_end = _part_end_permutation(p1_start, p1_end)
    # the position permutation and its inverse row share cycle structure,
    # so this order is also the order of the rounds-based part-end row
    order = _permutation_order(position_map)
    parts_match_order = parts == order
    order_divides_parts = parts % order == 0
    if parts_match_order:
        order_note = "parts equals the part-end permutation order"
    elif order_divides_parts:
        order_note = (f"parts ({parts}) is a multiple of the permutation "
                      f"order ({order}); the replay closes but rings the "
                      f"cycle {parts // order} time(s)")
    else:
        order_note = (f"parts ({parts}) is not a multiple of the part-end "
                      f"permutation order ({order}); the replay cannot close")

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
    if not parts_match_order:
        problems.append("parts_order_mismatch")

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

    methods_used = []
    for entry in used.values():
        methods_used.append({
            "method_id": entry["method_id"], "name": entry["name"],
            "version": entry["version"], "leads": entry["leads"],
            "rows": entry["rows"], "parts": entry["parts"],
            "segments": entry["segments_in_part"]})

    # switches inside a part (segment-to-segment method changes), reported
    # once per part with global indexes; inter-part changes are "boundaries"
    switches = []
    for part in range(1, parts + 1):
        base = (part - 1) * part_rows
        for i in range(1, len(infos)):
            boundary = base + layout[i]["from_index"]
            prev, cur = infos[i - 1], infos[i]
            switches.append({
                "at_index": boundary, "part": part,
                "from_segment": i, "to_segment": i + 1,
                "from_method_id": prev["method_id"],
                "from_method": prev["name"], "from_version": prev["version"],
                "to_method_id": cur["method_id"],
                "to_method": cur["name"], "to_version": cur["version"],
                "before_row": row_str(rows[boundary]),
                "after_row": row_str(rows[boundary + 1])})

    return {
        "stage": stage,
        "start_row": row_str(start),
        "parts": parts,
        "part_count": parts,
        "touch_rows": part_rows,           # changes rung per part
        "total_leads_per_part": total_leads_one,
        "segment_count": len(infos),
        "total_leads": parts * total_leads_one,
        "total_rows": total_rows,          # changes rung over all parts
        "rows_generated": step,
        "distinct_rows": len(entries),     # 1 + total_rows (boundaries once)
        "max_rows": effective_max,
        "extent_rows": extent,
        "status": status,                  # ok | untrue | premature_rounds | not_closed
        "closed": closed,
        "success": status == "ok",         # never success unless closed&true
        "truth": {"true": first_repeat is None and closed,
                  "conclusive": True,
                  "checked_rows": total_rows,
                  "first_repeat": first_repeat},
        "premature_rounds": premature,
        "part_end_permutation": {
            "row": row_str(p1_end),        # part 1 end row (from start_row)
            "canonical_row": row_str(canonical_end),  # end row from rounds
            # position_mapping: the bell at start position p ends at position
            # q(p) after one part (fixed for any start row).  Positions are
            # labeled by the rounds bells (canonical 0/E/T symbols).
            "mapping": {bell_symbol(p): bell_symbol(q)
                        for p, q in sorted(position_map.items())},
            "position_mapping": {p: position_map[p]
                                 for p in sorted(position_map)},
            "order": order,
            "parts": parts,
            "parts_equal_order": parts_match_order,
            "order_divides_parts": order_divides_parts,
            "consistent": closed == order_divides_parts,
            "note": order_note},
        "boundaries": boundaries,
        "boundary_rows_counted_once": len(boundaries),
        "switches": switches,
        "methods_used": methods_used,
        "part_summaries": part_summaries,
        "overrides": all_overrides,
        "unapplied_overrides": unapplied,
        "problems": problems,
        "rows": entries,
    }


def _multipart_summary(report):
    keys = ("status", "success", "closed", "stage", "parts", "part_count",
            "touch_rows", "total_rows", "total_leads", "segment_count",
            "problems")
    out = {k: report[k] for k in keys}
    out["truth"] = report["truth"]
    out["part_end_permutation"] = {
        k: report["part_end_permutation"][k] for k in
        ("row", "canonical_row", "order", "parts", "parts_equal_order",
         "order_divides_parts", "consistent", "note")}
    out["methods_used"] = [{k: m[k] for k in
                           ("method_id", "name", "version", "leads", "rows",
                            "parts")} for m in report["methods_used"]]
    return out


def compare_multipart_reports(report_a, report_b):
    """Compare two multi-part replay reports: size, closure, truth, order."""
    fa = report_a["truth"]["first_repeat"]
    fb = report_b["truth"]["first_repeat"]
    same_repeat = None
    if fa and fb:
        same_repeat = ((fa["first"]["index"], fa["second"]["index"])
                       == (fb["first"]["index"], fb["second"]["index"]))
    ids_a = {m["method_id"] for m in report_a["methods_used"]}
    ids_b = {m["method_id"] for m in report_b["methods_used"]}
    oa, ob = (report_a["part_end_permutation"],
              report_b["part_end_permutation"])
    return {
        "a": _multipart_summary(report_a),
        "b": _multipart_summary(report_b),
        "same_stage": report_a["stage"] == report_b["stage"],
        "parts_equal": report_a["parts"] == report_b["parts"],
        "parts_delta": report_a["parts"] - report_b["parts"],
        "total_rows_equal": report_a["total_rows"] == report_b["total_rows"],
        "total_rows_delta": report_a["total_rows"] - report_b["total_rows"],
        "both_closed": report_a["closed"] and report_b["closed"],
        "both_success": (report_a["success"] and report_b["success"]),
        "both_true": (report_a["truth"]["true"] and report_b["truth"]["true"]),
        "first_repeat_same_position": same_repeat,
        "part_end_permutation_equal":
            oa["position_mapping"] == ob["position_mapping"],
        "part_end_row_a": oa["canonical_row"],
        "part_end_row_b": ob["canonical_row"],
        "part_end_order_equal": oa["order"] == ob["order"],
        "order_a": oa["order"], "order_b": ob["order"],
        "parts_order_mismatch_a": not oa["parts_equal_order"],
        "parts_order_mismatch_b": not ob["parts_equal_order"],
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


# ====================================================================== music
# Musicality ("music") scoring: weighted rules spot the rows ringers actually
# listen for - consecutive ascending/descending bell runs at the front, the
# back or anywhere, named bell sequences and whole rows - and score them with
# the stroke, lead-end and segment context of every hit.
#
# Stroke convention (fixed for every scheme): row index 0, the starting row,
# is ALWAYS handstroke; parity then alternates, so even indexes ring handstroke
# and odd indexes backstroke.  Bells keep the compact symbols everywhere:
# 10 -> '0', 11 -> 'E', 12 -> 'T'.

_DIRECTION_UP = "up"
_DIRECTION_DOWN = "down"


def stroke_for_index(index):
    """Stroke rung at a row index: index 0 is always handstroke."""
    return STROKE_HAND if index % 2 == 0 else STROKE_BACK


def _scheme_err(message, rule_idx=None, field=None, token=None, offset=None):
    return SchemeError(message, rule=rule_idx, field=field,
                       token=token, offset=offset)


def _bells_from_string(text, stage, rule_idx, field):
    """Parse a compact/separated bell sequence ('4321' or '4 3 2 1' /
    '4,3,2,1'); symbols 0/E/T are accepted for bells 10/11/12."""
    cleaned = text.replace(",", " ").replace(".", " ").strip()
    if not cleaned:
        raise _scheme_err(f"{field} must not be empty", rule_idx, field)
    bells = []
    if not any(c.isspace() for c in cleaned):
        # compact form: one symbol per bell, e.g. "4321" or "90ET"
        try:
            return list(compact_bells(cleaned, stage))
        except NotationError as err:
            raise _scheme_err(err.message, rule_idx, field,
                              token=err.token, offset=err.offset)
    else:
        for tok in cleaned.split():
            bell = _decode_bell_token(tok)
            if bell is None or bell > stage:
                raise _scheme_err(
                    f"invalid bell token {tok!r} for {stage} bells",
                    rule_idx, field, token=tok)
            bells.append(bell)
    return bells


def _parse_rule_bells(value, stage, rule_idx, field):
    """Normalize a rule's bell list from a string or an integer array."""
    if isinstance(value, str):
        bells = _bells_from_string(value, stage, rule_idx, field)
    elif isinstance(value, (list, tuple)):
        bells = []
        for i, v in enumerate(value):
            if isinstance(v, bool) or not isinstance(v, int) \
                    or not 1 <= v <= stage:
                raise _scheme_err(
                    f"{field} must contain integer bells 1..{stage}",
                    rule_idx, field, token=v, offset=i)
            bells.append(v)
    else:
        raise _scheme_err(f"{field} must be a string or a list of bells",
                          rule_idx, field)
    if not bells:
        raise _scheme_err(f"{field} must contain at least one bell",
                          rule_idx, field)
    seen = set()
    for bell in bells:
        if bell in seen:
            raise _scheme_err(
                f"bell {bell} appears more than once in {field}",
                rule_idx, field, token=bell_symbol(bell))
        seen.add(bell)
    return tuple(bells)


def _normalize_filters(rule, rule_idx):
    """strokes / lead_end / segments filters shared by every rule kind."""
    strokes = rule.get("strokes", list(STROKES))
    if not isinstance(strokes, list) or not strokes:
        raise _scheme_err("strokes must be a non-empty list", rule_idx, "strokes")
    norm_strokes = []
    for stroke in strokes:
        if stroke not in STROKES:
            raise _scheme_err(
                f"stroke must be one of {', '.join(STROKES)}, got {stroke!r}",
                rule_idx, "strokes")
        if stroke not in norm_strokes:
            norm_strokes.append(stroke)
    lead_end = rule.get("lead_end", "any")
    if lead_end not in ("any", "lead_end", "not_lead_end"):
        raise _scheme_err(
            "lead_end must be 'any', 'lead_end' or 'not_lead_end'",
            rule_idx, "lead_end")
    segments = rule.get("segments", [])
    if segments is None:
        segments = []
    if not isinstance(segments, list) or any(
            isinstance(s, bool) or not isinstance(s, int) or s < 1
            for s in segments):
        raise _scheme_err("segments must be a list of positive integers",
                          rule_idx, "segments")
    if len(set(segments)) != len(segments):
        raise _scheme_err("segments must not contain duplicates",
                          rule_idx, "segments")
    return norm_strokes, lead_end, sorted(segments)


def parse_scheme(scheme, stage=None):
    """Validate and normalize a musicality scoring scheme for one stage.

    Scheme: {"name", "stage"?, "rules": [rule, ...]}; the stage may be given
    here or taken from the scored report/touch (it must agree).  Each rule is
    one of:
      run:      {"name", "kind":"run", "direction":"up"|"down"|"both",
                 "position":"front"|"back"|"any", "min_length":4, "weight"}
      sequence: {"name", "kind":"sequence", "bells":"4321"|[4,3,2,1],
                 "position":"front"|"back"|"any", "weight"}
      row:      {"name", "kind":"row", "row":"123456", "weight"}
    plus optional shared filters: "strokes" (["hand","back"]), "lead_end"
    ("any"|"lead_end"|"not_lead_end") and "segments" (touch only, 1-based).

    Weights must be positive numbers; a run hit scores weight * run length,
    sequence/row hits score weight.  Rule errors raise SchemeError located to
    the 0-based rule index (row rules additionally carry token/offset);
    stage/row permutation errors raise NotationError.
    """
    if not isinstance(scheme, dict):
        raise SchemeError("scheme must be an object")
    name = scheme.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SchemeError("scheme name must be a non-empty string", field="name")
    if stage is None:
        stage = scheme.get("stage")
    check_stage(stage)  # NotationError for missing/bad stage
    raw_rules = scheme.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise SchemeError("scheme must contain at least one rule", field="rules")

    rules = []
    names = set()
    ids = set()
    for rule_idx, rule in enumerate(raw_rules):
        if not isinstance(rule, dict):
            raise _scheme_err("rule must be an object", rule_idx)
        rule_name = rule.get("name")
        if not isinstance(rule_name, str) or not rule_name.strip():
            raise _scheme_err("rule name must be a non-empty string",
                              rule_idx, "name")
        if rule_name in names:
            raise _scheme_err(f"duplicate rule name {rule_name!r}",
                              rule_idx, "name")
        names.add(rule_name)
        rule_id = rule.get("id", f"rule-{rule_idx + 1}")
        if not isinstance(rule_id, (str, int)) or isinstance(rule_id, bool) \
                or (isinstance(rule_id, str) and not rule_id.strip()):
            raise _scheme_err("rule id must be a non-empty string or integer",
                              rule_idx, "id")
        if rule_id in ids:
            raise _scheme_err(f"duplicate rule id {rule_id!r}", rule_idx, "id")
        ids.add(rule_id)
        kind = rule.get("kind")
        if kind not in RULE_KINDS:
            raise _scheme_err(
                f"kind must be one of {', '.join(RULE_KINDS)}, got {kind!r}",
                rule_idx, "kind")
        weight = rule.get("weight", 1)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) \
                or weight <= 0:
            raise _scheme_err("weight must be a positive number",
                              rule_idx, "weight")
        strokes, lead_end, segments = _normalize_filters(rule, rule_idx)
        norm = {"id": rule_id, "name": rule_name, "kind": kind,
                "weight": weight, "strokes": strokes, "lead_end": lead_end,
                "segments": segments}
        if kind == RULE_RUN:
            direction = rule.get("direction", "both")
            if direction not in ("up", "down", "both"):
                raise _scheme_err(
                    "direction must be 'up', 'down' or 'both'",
                    rule_idx, "direction")
            position = rule.get("position", POSITION_ANY)
            if position not in RUN_POSITIONS:
                raise _scheme_err(
                    f"position must be one of {', '.join(RUN_POSITIONS)}",
                    rule_idx, "position")
            min_length = rule.get("min_length", 4)
            if isinstance(min_length, bool) or not isinstance(min_length, int) \
                    or not 2 <= min_length <= stage:
                raise _scheme_err(
                    f"min_length must be an integer in 2..{stage}",
                    rule_idx, "min_length")
            norm.update({"direction": direction, "position": position,
                         "min_length": min_length})
        elif kind == RULE_SEQUENCE:
            if "bells" not in rule:
                raise _scheme_err("sequence rule needs 'bells'",
                                  rule_idx, "bells")
            bells = _parse_rule_bells(rule["bells"], stage, rule_idx, "bells")
            if not 2 <= len(bells) <= stage:
                raise _scheme_err(
                    f"sequence must contain between 2 and {stage} bells, "
                    f"got {len(bells)}", rule_idx, "bells")
            position = rule.get("position", POSITION_ANY)
            if position not in RUN_POSITIONS:
                raise _scheme_err(
                    f"position must be one of {', '.join(RUN_POSITIONS)}",
                    rule_idx, "position")
            norm.update({"bells": bells, "pattern": row_str(bells),
                         "position": position})
        else:  # RULE_ROW
            if "row" not in rule:
                raise _scheme_err("row rule needs 'row'", rule_idx, "row")
            try:
                bells = parse_row(rule["row"], stage)
            except NotationError as err:
                # keep the permutation error's token/offset, add the rule index
                raise _scheme_err(err.message, rule_idx, "row",
                                  token=err.token, offset=err.offset)
            norm.update({"bells": bells, "pattern": row_str(bells)})
        rules.append(norm)
    return {"name": name.strip(), "stage": stage, "rules": rules}


def is_normalized_scheme(scheme):
    """Whether scheme already comes from parse_scheme() (rules normalized)."""
    return (isinstance(scheme, dict) and bool(scheme.get("rules"))
            and isinstance(scheme["rules"][0], dict)
            and "strokes" in scheme["rules"][0]
            and isinstance(scheme["rules"][0]["strokes"], list))


def compact_bells(text, stage):
    """Decode a compact bell string ('135', '90ET') to a bell tuple.

    Unlike parse_row this accepts sub-sequences and repeated bells are not
    rejected here; only the symbol range is checked against the stage.
    """
    bells = []
    for i, ch in enumerate(str(text)):
        bell = SYMBOL_TO_BELL.get(ch)
        if bell is None or bell > stage:
            raise NotationError(
                f"invalid bell symbol {ch!r} for {stage} bells "
                f"(bells above 9 are written 0=10, E=11, T=12)",
                token=ch, offset=i)
        bells.append(bell)
    return tuple(bells)


def rule_to_dict(rule):
    """JSON form of a normalized rule (bells render as 0/E/T symbols)."""
    out = {k: rule[k] for k in
           ("id", "name", "kind", "weight", "strokes", "lead_end", "segments")}
    if rule["kind"] == RULE_RUN:
        out.update({"direction": rule["direction"],
                    "position": rule["position"],
                    "min_length": rule["min_length"]})
    elif rule["kind"] == RULE_SEQUENCE:
        out.update({"bells": rule["pattern"], "position": rule["position"]})
    else:
        out["row"] = rule["pattern"]
    return out


# ------------------------------------------------------------- run matching
def _prefix_run_length(bells, delta):
    """Length of the monotonic run starting at the front (delta +1 up / -1)."""
    length = 1
    while length < len(bells) and bells[length] - bells[length - 1] == delta:
        length += 1
    return length


def _suffix_run_length(bells, delta):
    """Length of the monotonic run ending at the back."""
    length = 1
    i = len(bells) - 1
    while i > 0 and bells[i] - bells[i - 1] == delta:
        length += 1
        i -= 1
    return length


def _all_runs(bells, delta):
    """All maximal monotonic runs (start 0-based, length) inside a row."""
    runs = []
    start = 0
    while start < len(bells) - 1:
        if bells[start + 1] - bells[start] != delta:
            start += 1
            continue
        end = start + 1
        while end < len(bells) - 1 and bells[end + 1] - bells[end] == delta:
            end += 1
        runs.append((start, end - start + 1))
        start = end + 1
    return runs


def _match_run(bells, rule):
    """Longest qualifying run per accepted direction in the rule's range.

    For the same row, direction and position range only the single longest run
    counts (ties resolved at the earliest position); direction 'both' may thus
    yield one up and one down hit.  Returns a list of {start, length}.
    """
    directions = ((_DIRECTION_UP, 1), (_DIRECTION_DOWN, -1))
    wanted = rule["direction"]
    hits = []
    for name, delta in directions:
        if wanted != "both" and wanted != name:
            continue
        candidates = []
        position = rule["position"]
        if position == POSITION_FRONT:
            length = _prefix_run_length(bells, delta)
            if length >= rule["min_length"]:
                candidates.append((0, length))
        elif position == POSITION_BACK:
            length = _suffix_run_length(bells, delta)
            if length >= rule["min_length"]:
                candidates.append((len(bells) - length, length))
        else:  # POSITION_ANY: keep only the longest run in the whole row
            candidates = [(s, l) for s, l in _all_runs(bells, delta)
                          if l >= rule["min_length"]]
        if candidates:
            # longest, ties broken by the earliest starting position
            start, length = max(candidates, key=lambda c: (c[1], -c[0]))
            hits.append({"direction": name, "start": start, "length": length})
    return hits


def _match_sequence(bells, rule):
    """Find the (at most one, rows are permutations) contiguous placement."""
    wanted = rule["bells"]
    n, m = len(bells), len(wanted)
    position = rule["position"]
    starts = ()
    if position == POSITION_FRONT:
        starts = (0,)
    elif position == POSITION_BACK:
        starts = (n - m,)
    else:
        starts = range(0, n - m + 1)
    for start in starts:
        if tuple(bells[start:start + m]) == wanted:
            return [{"start": start, "length": m}]
    return []


def _match_row(bells, rule):
    return [{"start": 0, "length": len(bells)}] if bells == rule["bells"] else []


def _rule_passes_filters(rule, entry, stroke, lead_end_indexes, is_touch):
    if stroke not in rule["strokes"]:
        return False
    is_lead_end = entry["index"] in lead_end_indexes
    if rule["lead_end"] == "lead_end" and not is_lead_end:
        return False
    if rule["lead_end"] == "not_lead_end" and is_lead_end:
        return False
    if rule["segments"]:
        # segment filters only make sense for touches (1-based segments);
        # analyses have no segments and the starting row is segment 0, so
        # neither can satisfy a segment-restricted rule (zero hits kept).
        if not is_touch or entry.get("segment", 0) not in rule["segments"]:
            return False
    return True


def _empty_group():
    return {"hits": 0, "score": 0}


def _add_group(bucket, key, score):
    group = bucket.get(key)
    if group is None:
        group = bucket[key] = _empty_group()
    group["hits"] += 1
    group["score"] += score


def _score_entries(entries, stage, rules, *, is_touch, method_seed,
                   segment_seed, lead_end_indexes):
    """Score normalized row entries against normalized scheme rules."""
    # method seed: list of (method_id, name, version); segment seed: indexes
    method_bucket_order = list(method_seed)
    segment_bucket_order = list(segment_seed)
    summaries = []
    for rule in rules:
        summaries.append({
            "id": rule["id"], "name": rule["name"], "kind": rule["kind"],
            "rule": rule_to_dict(rule),
            "hits": 0, "score": 0,
            "by_stroke": {STROKE_HAND: _empty_group(), STROKE_BACK: _empty_group()},
            "by_method": {}, "by_segment": {},
        })
    rule_by_id = {rule["id"]: summaries[i] for i, rule in enumerate(rules)}
    method_buckets = {}
    segment_buckets = {}
    hits = []
    total_score = 0
    for entry in entries:
        bells = tuple(SYMBOL_TO_BELL[ch] for ch in entry["row"])
        stroke = stroke_for_index(entry["index"])
        method_key = (entry.get("method_id"), entry.get("method"),
                      entry.get("version"))
        segment_key = entry.get("segment", 0) if is_touch else None
        for rule in rules:
            if not _rule_passes_filters(rule, entry, stroke,
                                        lead_end_indexes, is_touch):
                continue
            if rule["kind"] == RULE_RUN:
                matches = _match_run(bells, rule)
            elif rule["kind"] == RULE_SEQUENCE:
                matches = _match_sequence(bells, rule)
            else:
                matches = _match_row(bells, rule)
            for match in matches:
                start, length = match["start"], match["length"]
                score = rule["weight"] * length if rule["kind"] == RULE_RUN \
                    else rule["weight"]
                hit = {
                    "rule_id": rule["id"], "rule": rule["name"],
                    "kind": rule["kind"], "row": entry["row"],
                    "index": entry["index"], "stroke": stroke,
                    "lead_end": entry["index"] in lead_end_indexes,
                    "position": (rule["position"] if rule["kind"] != RULE_ROW
                                 else "row"),
                    "start": start + 1,  # 1-based place
                    "length": length,
                    "matched": entry["row"][start:start + length],
                    "score": score,
                    "lead": entry["lead"], "change": entry["change"],
                    "method_id": entry.get("method_id"),
                    "method": entry.get("method"),
                    "version": entry.get("version"),
                    "segment": segment_key,
                }
                if rule["kind"] == RULE_RUN:
                    hit["direction"] = match["direction"]
                if rule["kind"] == RULE_ROW:
                    hit["pattern"] = rule["pattern"]
                elif rule["kind"] == RULE_SEQUENCE:
                    hit["pattern"] = rule["pattern"]
                hits.append(hit)
                total_score += score
                summary = rule_by_id[rule["id"]]
                summary["hits"] += 1
                summary["score"] += score
                grp = summary["by_stroke"][stroke]
                grp["hits"] += 1
                grp["score"] += score
                mb = method_buckets.setdefault(rule["id"], {})
                _add_group(mb, method_key, score)
                if method_key not in method_bucket_order:
                    method_bucket_order.append(method_key)
                if is_touch:
                    sb = segment_buckets.setdefault(rule["id"], {})
                    _add_group(sb, segment_key, score)
                    if segment_key not in segment_bucket_order:
                        segment_bucket_order.append(segment_key)

    def _method_sort_key(key):
        return (1, key[0]) if key[0] is not None else (0, 0)

    sorted_methods = sorted(dict.fromkeys(method_bucket_order),
                            key=_method_sort_key)
    sorted_segments = sorted(dict.fromkeys(segment_bucket_order))

    for summary in summaries:
        rid = summary["id"]
        buckets = method_buckets.get(rid, {})
        summary["by_method"] = [{
            "method_id": key[0], "name": key[1], "version": key[2],
            "hits": buckets[key]["hits"], "score": buckets[key]["score"]}
            for key in sorted_methods if key in buckets]
        if is_touch:
            buckets = segment_buckets.get(rid, {})
            summary["by_segment"] = [{
                "segment": key,
                "hits": buckets[key]["hits"], "score": buckets[key]["score"]}
                for key in sorted_segments if key in buckets]
        else:
            summary["by_segment"] = None

    return {"total_hits": len(hits), "total_score": total_score,
            "rules": summaries, "hits": hits}


def _scheme_ref(scheme):
    return {"id": scheme.get("id"), "name": scheme["name"],
            "version": scheme.get("version")}


def score_analysis(report, scheme, method=None):
    """Score an analyze() report against a normalized/parsed scheme.

    method: optional {"id","name","version"} of the analyzed method version;
    every hit is attributed to it (the index-0 row carries lead/change 0).
    When the expansion was stopped by the row cap the result is marked
    "partial": the score covers only the rows actually checked and must never
    be read as a full-extent result.
    """
    if not is_normalized_scheme(scheme):
        scheme = parse_scheme(scheme, report["stage"])
    if scheme["stage"] != report["stage"]:
        raise SchemeError(
            f"stage mismatch: scheme is on {scheme['stage']} bells, "
            f"the analysis is on {report['stage']}")
    lead_len = report["lead_length"]
    lead_end_indexes = {e["index"] for e in report["rows"]
                        if e["change"] == lead_len and e["index"] > 0}
    entries = [dict(e, method_id=method.get("id") if method else None,
                    method=method.get("name") if method else None,
                    version=method.get("version") if method else None)
               for e in report["rows"]]
    method_seed = []
    if method is not None:
        method_seed = [(method.get("id"), method.get("name"),
                        method.get("version"))]
    scored = _score_entries(entries, report["stage"], scheme["rules"],
                            is_touch=False, method_seed=method_seed,
                            segment_seed=[], lead_end_indexes=lead_end_indexes)
    partial = report["status"] == "exceeded_limit"
    result = {
        "kind": "analysis",
        "scoring_version": SCORING_VERSION,
        "stage": report["stage"],
        "scheme": _scheme_ref(scheme),
        "index0_stroke": STROKE_HAND,
        "strokes": list(STROKES),
        "lead_length": lead_len,
        "rows_analyzed": len(report["rows"]),
        "rows_generated": report["rows_generated"],
        "checked_rows": report["truth"]["checked_rows"],
        "partial": partial,
        "truncated": partial,
        "status": report["status"],
        "closed": report["closed"],
        "truth": {"true": report["truth"]["true"],
                  "conclusive": report["truth"]["conclusive"],
                  "checked_rows": report["truth"]["checked_rows"]},
        "problems": report["problems"],
        "total_hits": scored["total_hits"],
        "total_score": scored["total_score"],
        "rules": scored["rules"],
        "hits": scored["hits"],
    }
    return result


def score_touch(report, scheme):
    """Score an analyze_spliced() report against a normalized/parsed scheme.

    Hits keep their segment/method-version/lead/change context; the starting
    row (index 0) is segment 0 with no method.  Segment-restricted rules only
    score inside their listed 1-based segments.
    """
    if not is_normalized_scheme(scheme):
        scheme = parse_scheme(scheme, report["stage"])
    if scheme["stage"] != report["stage"]:
        raise SchemeError(
            f"stage mismatch: scheme is on {scheme['stage']} bells, "
            f"the touch is on {report['stage']}")
    lead_len_by_segment = {s["index"]: s["lead_length"]
                           for s in report["segments"]}
    lead_end_indexes = {e["index"] for e in report["rows"]
                        if e["index"] > 0
                        and e["change"] == lead_len_by_segment.get(e["segment"])}
    method_seed = [(m["method_id"], m["name"], m["version"])
                   for m in sorted(report["methods_used"],
                                   key=lambda m: (m["method_id"] is None,
                                                  m["method_id"]))]
    scored = _score_entries(report["rows"], report["stage"], scheme["rules"],
                            is_touch=True, method_seed=method_seed,
                            segment_seed=list(
                                range(1, report["segment_count"] + 1)),
                            lead_end_indexes=lead_end_indexes)
    # The whole planned touch is always expanded (its size is validated up
    # front), so a music score is never "partial under a cap"; an open touch
    # still covers exactly the rows that were rung.
    result = {
        "kind": "touch",
        "scoring_version": SCORING_VERSION,
        "stage": report["stage"],
        "scheme": _scheme_ref(scheme),
        "index0_stroke": STROKE_HAND,
        "strokes": list(STROKES),
        "total_leads": report["total_leads"],
        "rows_analyzed": len(report["rows"]),
        "rows_generated": report["rows_generated"],
        "checked_rows": report["truth"]["checked_rows"],
        "partial": False,
        "truncated": False,
        "status": report["status"],
        "closed": report["closed"],
        "truth": {"true": report["truth"]["true"],
                  "conclusive": report["truth"]["conclusive"],
                  "checked_rows": report["truth"]["checked_rows"]},
        "problems": report["problems"],
        "total_hits": scored["total_hits"],
        "total_score": scored["total_score"],
        "rules": scored["rules"],
        "hits": scored["hits"],
    }
    return result


def compare_music(result_a, result_b):
    """Compare two music results.

    Only results on the SAME stage, scored with the SAME scheme version under
    the SAME scoring version are comparable; otherwise "comparable" is false
    with the mismatched attributes listed (the HTTP layer turns that into a
    400 'incomparable').  Per-rule scores/hits and their deltas are listed by
    rule id; rules missing on one side keep zero values.
    """
    scheme_a, scheme_b = result_a["scheme"], result_b["scheme"]
    reasons = []
    if result_a["scoring_version"] != result_b["scoring_version"]:
        reasons.append("scoring_version")
    if result_a["stage"] != result_b["stage"]:
        reasons.append("stage")
    # the scheme version is the (name, version) pair; two stored ids of the
    # same name+version still compare equal, a changed version is reported
    # separately from a wholly different scheme
    if scheme_a.get("name") != scheme_b.get("name"):
        reasons.append("scheme")
    elif scheme_a.get("version") != scheme_b.get("version"):
        reasons.append("scheme_version")
    base = {
        "comparable": not reasons,
        "reasons": reasons,
        "a": {"result_id": result_a.get("id"), "kind": result_a["kind"],
              "stage": result_a["stage"], "scheme": scheme_a,
              "scoring_version": result_a["scoring_version"]},
        "b": {"result_id": result_b.get("id"), "kind": result_b["kind"],
              "stage": result_b["stage"], "scheme": scheme_b,
              "scoring_version": result_b["scoring_version"]},
    }
    if reasons:
        return base
    rules_a = {r["id"]: r for r in result_a["rules"]}
    rules_b = {r["id"]: r for r in result_b["rules"]}
    ids = list(dict.fromkeys([r["id"] for r in result_a["rules"]]
                             + [r["id"] for r in result_b["rules"]]))
    per_rule = []
    for rid in ids:
        ra, rb = rules_a.get(rid), rules_b.get(rid)
        ha = ra["hits"] if ra else 0
        hb = rb["hits"] if rb else 0
        sa = ra["score"] if ra else 0
        sb = rb["score"] if rb else 0
        per_rule.append({
            "id": rid,
            "name": (ra or rb)["name"],
            "kind": (ra or rb)["kind"],
            "present_a": ra is not None, "present_b": rb is not None,
            "a": {"hits": ha, "score": sa},
            "b": {"hits": hb, "score": sb},
            "hits_delta": hb - ha,
            "score_delta": sb - sa,
        })
    base.update({
        "rules_same": set(rules_a) == set(rules_b),
        "partial_a": result_a["partial"],
        "partial_b": result_b["partial"],
        "checked_rows_a": result_a["checked_rows"],
        "checked_rows_b": result_b["checked_rows"],
        "a_score": {"total_hits": result_a["total_hits"],
                    "total_score": result_a["total_score"]},
        "b_score": {"total_hits": result_b["total_hits"],
                    "total_score": result_b["total_score"]},
        "total_hits_delta": result_b["total_hits"] - result_a["total_hits"],
        "total_score_delta": result_b["total_score"] - result_a["total_score"],
        "rules": per_rule,
    })
    return base


# ============================================================ touch search
# Touch ("composition") search: at every lead boundary the search branches
# over a set of same-stage method versions, and for each method over "plain"
# (the notation as written) plus named calls - a call replaces one change
# inside the lead by a single-change notation (a bob/single style device).
#
# Each branch continues from the previous lead's last row and accumulates the
# rows already rung ("seen").  A lead that repeats a row without closing, or
# returns to the start row mid-lead, is pruned; a branch is a solution only
# when it comes back to the start row at a lead boundary within the requested
# lead range with every other row unique.  Exploration is bounded by a state
# cap (lead attempts) and a result cap (kept solutions); hitting either marks
# the search "truncated" and the result must never be read as "no composition
# exists".
TOUCH_SEARCH_PLAIN = "plain"
# Branch prune reasons, reported with counts:
SEARCH_REPEAT = "repeat"                # a non-closing row was rung already
SEARCH_PREMATURE = "premature_rounds"   # start row again before the lead end
SEARCH_BELOW_MIN = "below_min_leads"    # closed true but shorter than min leads
SEARCH_LEAD_LIMIT = "lead_limit"        # reached max leads without closing
SEARCH_PRUNE_REASONS = (SEARCH_REPEAT, SEARCH_PREMATURE,
                        SEARCH_BELOW_MIN, SEARCH_LEAD_LIMIT)
SEARCH_DEFAULT_MAX_STATES = 100_000
SEARCH_DEFAULT_MAX_RESULTS = 100


class TouchSearchError(ValueError):
    """A touch-search configuration error (nothing is searched or stored).

    Attributes:
        message: human readable description
        code:    machine readable error code - "bad_search" (generic),
                 "stage_mismatch", "bad_call", "too_large", "bad_limit"
        method:  0-based index of the offending method entry
        call:    0-based index of the offending call within that method
        extra:   additional structured details
    """

    def __init__(self, message, code="bad_search", method=None, call=None,
                 extra=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.method = method
        self.call = call
        self.extra = extra or {}

    def to_dict(self):
        out = {"error": self.message}
        if self.method is not None:
            out["method"] = self.method
        if self.call is not None:
            out["call"] = self.call
        out.update(self.extra)
        return out


def _prepare_search_methods(methods):
    """Validate and prepare the searched method versions and their calls.

    methods: list of {"method_id", "name", "version", "stage", "notation",
        "calls": [{"name", "change", "notation"}, ...]}.  Every method must be
    on the same stage; a call names the lead-internal change (1-based) it
    replaces and its notation must parse to exactly ONE change.  "plain" is
    always offered implicitly and is a reserved call name.

    Returns (stage, prepared) where each prepared method carries its parsed
    changes and an ordered option list: plain first, then the named calls.
    Raises TouchSearchError located to the method/call index.
    """
    if not isinstance(methods, (list, tuple)) or not methods:
        raise TouchSearchError("methods must be a non-empty list")
    stage = None
    seen_ids = set()
    prepared = []
    for mi, spec in enumerate(methods):
        if not isinstance(spec, dict):
            raise TouchSearchError(f"method {mi} must be an object",
                                   method=mi)
        method_stage = spec.get("stage")
        try:
            check_stage(method_stage)
        except NotationError:
            raise TouchSearchError("stage must be an integer 4..12",
                                   code="bad_search", method=mi,
                                   extra={"stage": method_stage})
        if stage is None:
            stage = method_stage
        elif method_stage != stage:
            raise TouchSearchError(
                f"stage mismatch: method {mi} is on {method_stage} bells, "
                f"the search is on {stage}", code="stage_mismatch",
                method=mi, extra={"stage": method_stage, "expected": stage})
        method_id = spec.get("method_id")
        if method_id in seen_ids:
            raise TouchSearchError(
                f"method version {method_id} is listed more than once",
                method=mi, extra={"method_id": method_id})
        seen_ids.add(method_id)
        try:
            changes = parse_notation(spec.get("notation"), stage)
        except NotationError as err:
            raise TouchSearchError(err.message, method=mi,
                                   extra=_notation_details(err))
        lead_len = len(changes)
        calls = spec.get("calls") or []
        if not isinstance(calls, list):
            raise TouchSearchError("calls must be a list", code="bad_call",
                                   method=mi)
        options = [{
            "call": TOUCH_SEARCH_PLAIN, "change": None,
            "change_obj": None, "notation": None, "replaces_token": None,
        }]
        call_names = {TOUCH_SEARCH_PLAIN}
        for ci, cspec in enumerate(calls):
            if not isinstance(cspec, dict):
                raise TouchSearchError("call must be an object",
                                       code="bad_call", method=mi, call=ci)
            name = cspec.get("name")
            if not isinstance(name, str) or not name.strip():
                raise TouchSearchError("call name must be a non-empty string",
                                       code="bad_call", method=mi, call=ci,
                                       extra={"field": "name"})
            name = name.strip()
            if name == TOUCH_SEARCH_PLAIN:
                raise TouchSearchError(
                    f"{TOUCH_SEARCH_PLAIN!r} is reserved for the unmodified lead",
                    code="bad_call", method=mi, call=ci,
                    extra={"field": "name"})
            if name in call_names:
                raise TouchSearchError(f"duplicate call name {name!r}",
                                       code="bad_call", method=mi, call=ci,
                                       extra={"field": "name"})
            call_names.add(name)
            change = cspec.get("change")
            if isinstance(change, bool) or not isinstance(change, int) \
                    or not 1 <= change <= lead_len:
                raise TouchSearchError(
                    f"call {name!r}: change must be between 1 and {lead_len} "
                    f"(the lead length)", code="bad_call", method=mi,
                    call=ci, extra={"field": "change", "change": change,
                                    "lead_length": lead_len})
            notation = cspec.get("notation")
            try:
                parsed = parse_notation(notation, stage)
            except NotationError as err:
                raise TouchSearchError(err.message, code="bad_call",
                                       method=mi, call=ci,
                                       extra={"field": "notation",
                                              **_notation_details(err)})
            if len(parsed) != 1:
                raise TouchSearchError(
                    f"call {name!r}: notation must be exactly one change, "
                    f"got {len(parsed)}", code="bad_call", method=mi, call=ci,
                    extra={"field": "notation", "change_count": len(parsed)})
            options.append({"call": name, "change": change,
                            "change_obj": parsed[0], "notation": notation,
                            "replaces_token": changes[change - 1].token})
        prepared.append({"method_id": method_id, "name": spec.get("name"),
                         "version": spec.get("version"), "stage": stage,
                         "lead_len": lead_len, "changes": changes,
                         "options": options})
    return stage, prepared


def _expand_search_lead(m, option, head, start, seen):
    """Ring one lead of method m from head with the given plain/call option.

    Returns (rows, None): rows[0] is head and rows[-1] the lead head, or
    (rows, reason) when the lead is pruned - a non-closing repeat or a return
    to the start row before the lead end.  Closing onto the start row exactly
    at the lead end is allowed and comes back as rows[-1] == start.
    """
    changes = m["changes"]
    lead_len = m["lead_len"]
    call_at = option["change"]
    call_obj = option["change_obj"]
    row = head
    rows = [head]
    local = set()  # rows already produced inside this very lead
    for pos in range(1, lead_len + 1):
        change = call_obj if call_at == pos else changes[pos - 1]
        row = change.apply(row)
        closing = pos == lead_len and row == start
        if row == start and not closing:
            return rows, SEARCH_PREMATURE
        if not closing and (row in seen or row in local):
            return rows, SEARCH_REPEAT
        local.add(row)
        rows.append(row)
    return rows, None


def _replay_candidate(stage, start, choices, prepared, rules):
    """Re-expand a solved choice path: decision records, music and counts."""
    entries = [{"index": 0, "segment": None, "method_id": None,
                "method": None, "version": None, "lead": 0, "change": 0,
                "row": row_str(start)}]
    decisions = []
    lead_end_indexes = set()
    calls = switches = index = 0
    prev_mi = None
    head = start
    for lead_no, (mi, oi) in enumerate(choices, start=1):
        m = prepared[mi]
        option = m["options"][oi]
        call_at = option["change"]
        call_obj = option["change_obj"]
        from_index = index
        for pos in range(1, m["lead_len"] + 1):
            change = call_obj if call_at == pos else m["changes"][pos - 1]
            head = change.apply(head)
            index += 1
            entries.append({"index": index, "segment": None,
                            "method_id": m["method_id"], "method": m["name"],
                            "version": m["version"], "lead": lead_no,
                            "change": pos, "row": row_str(head),
                            "token": change.token})
        lead_end_indexes.add(index)
        decisions.append({
            "lead": lead_no, "method_id": m["method_id"],
            "method": m["name"], "version": m["version"],
            "call": option["call"], "change": option["change"],
            "notation": option["notation"],
            "replaces_token": option["replaces_token"],
            "lead_length": m["lead_len"], "from_index": from_index,
            "to_index": index, "lead_head": row_str(head)})
        if option["call"] != TOUCH_SEARCH_PLAIN:
            calls += 1
        if prev_mi is not None and mi != prev_mi:
            switches += 1
        prev_mi = mi
    music = None
    if rules:
        method_seed = [(m["method_id"], m["name"], m["version"])
                       for m in prepared]
        scored = _score_entries(entries, stage, rules, is_touch=False,
                                method_seed=method_seed, segment_seed=[],
                                lead_end_indexes=lead_end_indexes)
        music = {"total_score": scored["total_score"],
                 "total_hits": scored["total_hits"]}
    return decisions, calls, switches, index, music


def _search_method_info(m):
    return {"method_id": m["method_id"], "name": m["name"],
            "version": m["version"], "stage": m["stage"],
            "lead_length": m["lead_len"],
            "calls": [{"name": o["call"], "change": o["change"],
                       "notation": o["notation"],
                       "replaces_token": o["replaces_token"]}
                      for o in m["options"][1:]]}


def search_touches(methods, start_row=None, min_leads=1, max_leads=None,
                   max_states=SEARCH_DEFAULT_MAX_STATES,
                   max_results=SEARCH_DEFAULT_MAX_RESULTS, scheme=None):
    """Search for true, closing touches by branching on methods and calls.

    At each lead boundary every (method, plain|named call) option is tried;
    the branch continues from the lead head, adding the lead's rows to the
    accumulated seen set.  A lead is pruned on a non-closing repeat
    ("repeat") or a premature return to start_row ("premature_rounds");
    closing true short of min_leads is pruned ("below_min_leads") and running
    to max_leads without closing is pruned ("lead_limit").

    max_states bounds the number of lead attempts (exploration budget) and
    max_results the number of solutions kept; reaching either stops the
    search with truncated=True (truncated_reason "max_states"/"max_results")
    and the outcome must never be reported as "no solution".  With both caps
    untouched the exploration is exhaustive.

    scheme: optional normalized parse_scheme() scheme used only to rank
    candidates (total music score; segment-restricted rules never match - a
    candidate is not segmented before export).  Candidates are ranked by
    music score descending, then fewer calls, fewer switches, fewer leads and
    finally the lexicographic decision sequence.

    The longest possible path (max_leads times the longest lead) must fit in
    HARD_MAX_ROWS rows, otherwise the search is refused (TouchSearchError,
    code "too_large"); differing stages and bad calls refuse it likewise.
    """
    stage, prepared = _prepare_search_methods(methods)
    if isinstance(max_leads, bool) or not isinstance(max_leads, int) \
            or max_leads < 1:
        raise TouchSearchError("max_leads must be a positive integer",
                               code="bad_limit", extra={"field": "max_leads"})
    if isinstance(min_leads, bool) or not isinstance(min_leads, int) \
            or not 1 <= min_leads <= max_leads:
        raise TouchSearchError(
            "min_leads must be a positive integer not greater than max_leads",
            code="bad_limit",
            extra={"field": "min_leads", "min_leads": min_leads,
                   "max_leads": max_leads})
    for field, value in (("max_states", max_states),
                         ("max_results", max_results)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise TouchSearchError(f"{field} must be a positive integer",
                                   code="bad_limit", extra={"field": field})
    max_lead_len = max(m["lead_len"] for m in prepared)
    max_possible_rows = max_leads * max_lead_len
    if max_possible_rows > HARD_MAX_ROWS:
        raise TouchSearchError(
            f"longest possible path is {max_possible_rows:,} rows "
            f"({max_leads} leads x {max_lead_len} rows), which exceeds the "
            f"hard limit of {HARD_MAX_ROWS:,}",
            code="too_large",
            extra={"max_possible_rows": max_possible_rows,
                   "hard_max_rows": HARD_MAX_ROWS, "max_leads": max_leads,
                   "max_lead_length": max_lead_len})
    start = parse_row(start_row, stage)
    rules = None
    if scheme is not None:
        if not is_normalized_scheme(scheme):
            scheme = parse_scheme(scheme, stage)
        if scheme["stage"] != stage:
            raise SchemeError(
                f"stage mismatch: scheme is on {scheme['stage']} bells, "
                f"the search is on {stage}", field="scheme")
        rules = scheme["rules"]

    # Flat option list in declared order (methods in order, plain first).
    options = [(mi, oi) for mi, m in enumerate(prepared)
               for oi in range(len(m["options"]))]
    pruned = {reason: 0 for reason in SEARCH_PRUNE_REASONS}
    results = []
    states_used = 0
    max_depth = 0
    truncated_reason = None
    # Iterative DFS (paths can be far longer than Python's recursion limit):
    # (head row, seen set, chosen (method_index, option_index) tuple, depth).
    stack = [(start, {start}, (), 0)]
    while stack and truncated_reason is None:
        head, seen, choices, depth = stack.pop()
        lead_no = depth + 1
        if lead_no > max_depth:
            max_depth = lead_no
        children = []
        for mi, oi in options:
            if states_used >= max_states:
                truncated_reason = "max_states"
                break
            states_used += 1
            m = prepared[mi]
            option = m["options"][oi]
            rows, reason = _expand_search_lead(m, option, head, start, seen)
            if reason is not None:
                pruned[reason] += 1
                continue
            lead_head = rows[-1]
            if lead_head == start:
                # closure only counts inside the requested lead range
                if lead_no < min_leads:
                    pruned[SEARCH_BELOW_MIN] += 1
                    continue
                decisions, calls, switches, total_rows, music = \
                    _replay_candidate(stage, start,
                                      choices + ((mi, oi),), prepared, rules)
                results.append({
                    "leads": lead_no, "rows": total_rows, "calls": calls,
                    "switches": switches, "closed": True, "true": True,
                    "start_row": row_str(start), "end_row": row_str(lead_head),
                    "music_score": music["total_score"] if music else None,
                    "music_hits": music["total_hits"] if music else None,
                    "decisions": decisions,
                    "_signature": ";".join(
                        f"{pj}:{prepared[pj]['options'][pj_o]['call']}"
                        for pj, pj_o in choices + ((mi, oi),))})
                if len(results) >= max_results:
                    truncated_reason = "max_results"
                    break
            elif lead_no >= max_leads:
                pruned[SEARCH_LEAD_LIMIT] += 1
            else:
                new_seen = set(seen)
                new_seen.update(rows[1:])
                children.append((lead_head, new_seen,
                                 choices + ((mi, oi),), lead_no))
        # push in reverse so the declared option order is explored first
        stack.extend(reversed(children))

    def _sort_key(candidate):
        return (-(candidate["music_score"] or 0), candidate["calls"],
                candidate["switches"], candidate["leads"],
                candidate["_signature"])

    results.sort(key=_sort_key)
    for i, candidate in enumerate(results):
        del candidate["_signature"]
        candidate["index"] = i
    if truncated_reason is not None:
        status = "truncated"
    elif results:
        status = "ok"
    else:
        # exhaustive search with no candidate: a definite "no solution here"
        status = "exhausted"
    return {
        "stage": stage,
        "start_row": row_str(start),
        "lead_range": {"min": min_leads, "max": max_leads},
        "max_states": max_states,
        "max_results": max_results,
        "max_lead_length": max_lead_len,
        "max_possible_rows": max_possible_rows,
        "branching_factor": len(options),
        "scoring_version": SCORING_VERSION if rules else None,
        "scheme": _scheme_ref(scheme) if rules else None,
        "methods": [_search_method_info(m) for m in prepared],
        "status": status,          # ok | exhausted | truncated
        "truncated": truncated_reason is not None,
        "truncated_reason": truncated_reason,
        "exhausted": truncated_reason is None,
        "result_count": len(results),
        "results": results,
        "prune_reasons": pruned,
        "stats": {
            "states_used": states_used,
            "options_tried": states_used,
            "pruned": sum(pruned.values()),
            "prune_reasons": dict(pruned),
            "max_depth_reached": max_depth,
            "branching_factor": len(options),
            "candidates_found": len(results),
        },
    }
