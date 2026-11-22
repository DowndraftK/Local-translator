"""The sole dictionary input/direction rule. No retrieval or task policy."""
import unicodedata
from dataclasses import asdict, dataclass

from ..text_rules import WHITESPACE, analyze_text

RULE_VERSION = "dictionary-direction-v1"
MAX_GRAPHEMES = 128
MAX_QUERY_BYTES = 2048
DIRECTIONS = ("zh-en", "en-zh")
MODES = ("auto", *DIRECTIONS)
# Assigned ideographs in Unicode 15.0, plus the two Chinese iteration/zero
# characters below. No unassigned holes or future blocks are inferred.
HAN_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFA6D),
              (0xFA70, 0xFAD9), (0x20000, 0x2A6DF), (0x2A700, 0x2B739),
              (0x2B740, 0x2B81D), (0x2B820, 0x2CEA1), (0x2CEB0, 0x2EBE0),
              (0x2F800, 0x2FA1D), (0x30000, 0x3134A), (0x31350, 0x323AF))


def identity():
    return dict(rule=RULE_VERSION, unicode="15.0.0", han="assigned-ranges-v1",
                latin="UCD15-L-category-name-contains-LATIN-v1",
                marks="attached-UCD15-M-category-v1", other="UCD15-NPS-and-fixed-whitespace-v1",
                counting="qt6102-grapheme-v1", view="fixed-trim-CRLF-statistics-only-v1")


@dataclass(frozen=True)
class DictionaryAnalysis:
    detected: str
    direction_mode: str
    directions: tuple
    status: str
    graphemes: int
    raw_utf8_bytes: int
    han: int
    latin: int

    def wire(self):
        value = asdict(self)
        value["directions"] = list(self.directions)
        value["rule_identity"] = identity()
        return value


def analyze_query(raw, direction_mode="auto"):
    """Preserve raw input; manual selection never bypasses admission.

    A mixed string is queried in full in each index. This selects an index by
    script shape, not by language identification or semantic understanding.
    """
    if not isinstance(raw, str):
        raise ValueError("invalid_query")
    try:
        byte_count = len(raw.encode("utf-8"))
    except UnicodeError:
        return DictionaryAnalysis("unsupported", direction_mode, (), "unsupported_input", 0, 0, 0, 0)
    facts = analyze_text(raw, "en-zh")  # Only statistical facts; old target is ignored.
    def result(detected, status, directions=(), han=0, latin=0):
        return DictionaryAnalysis(detected, direction_mode, tuple(directions), status,
                                  facts.graphemes, byte_count, han, latin)
    if direction_mode not in MODES:
        return result("invalid", "unsupported_direction")
    # Raw bytes win even when trimming would make a huge input empty.
    if byte_count > MAX_QUERY_BYTES or facts.graphemes > MAX_GRAPHEMES:
        return result("too_long", "input_too_long")
    if facts.is_empty:
        return result("empty", "empty")
    han = latin = 0
    attached = False
    for ch in raw.strip(WHITESPACE).replace("\r\n", "\n"):
        cp, category = ord(ch), unicodedata.category(ch)
        is_han = cp in (0x3005, 0x3007) or any(a <= cp <= b for a, b in HAN_RANGES)
        is_latin = category.startswith("L") and "LATIN" in unicodedata.name(ch, "")
        if is_han or is_latin:
            han += is_han; latin += is_latin; attached = True
        elif category.startswith("M") and attached:
            continue
        elif ch in WHITESPACE or category[0] in "NPS":
            attached = False
        else:
            return result("unsupported", "unsupported_input", han=han, latin=latin)
    if not (han or latin):
        return result("invalid", "unsupported_input")
    detected = "ambiguous" if han and latin else "zh-en" if han else "en-zh"
    directions = DIRECTIONS if detected == "ambiguous" else (detected,)
    if direction_mode != "auto":
        directions = (direction_mode,)
    return result(detected, "direction_ambiguous" if detected == "ambiguous" and direction_mode == "auto" else "ready",
                  directions, han, latin)
