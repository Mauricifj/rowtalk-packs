#!/usr/bin/env python3
"""Generate fold/fold_table.json — the DATA half of fold_v1.

fold_v1 is deliberately defined as a lookup table plus three trivial rules
(see SCHEMA.md, "fold_v1"), because the app (Dart) has no Unicode
normalization and its case mapping is not guaranteed to match Python's
across Unicode versions. Every Unicode-aware decision (case, diacritics,
ligatures, width, punctuation, whitespace) is therefore made ONCE, here,
from Python's `unicodedata`, and then FROZEN in the committed table.

THE TABLE IS FROZEN. Running this script again on another Python (another
Unicode version) may produce a different table; that is a NEW fold version
(fold_v2), never an edit of v1. `tests/test_fold.py` pins the committed
table's SHA-256 and fails if the file changes.

Usage (only when deliberately minting a new fold version):

    python3 tools/gen_fold_table.py --out fold/fold_table.json --check
    python3 tools/gen_fold_table.py --out fold/fold_table_v2.json --version 2

`--check` regenerates in memory and compares with the file at `--out`
(meaningful only on the Unicode version the file records).

Per code point c (surrogates excluded), the first matching rule wins:

  R1  noncharacter (U+FDD0..U+FDEF, U+xxFFFE, U+xxFFFF)      -> ""   (drop)
  R2  whitespace (TAB LF VT FF CR, U+0085, Zs, Zl, Zp)        -> " "
  R3  other control or format character (Cc, Cf)             -> ""   (drop)
  R4  stripped mark (combining diacritics, Hebrew points,
      Arabic harakat, variation selectors; see MARK_RANGES)  -> ""   (drop)
  R5  joiner punctuation (apostrophes, periods, middle dots;
      see JOINERS) and Arabic tatweel                        -> ""   (drop)
  R6  kept ASCII symbols # & @ %                             -> itself
  R7  any other punctuation (P*)                             -> " "
  R8  modifier symbol (Sk)                                   -> ""   (drop)
  R9  decimal digit (Nd)                                     -> its ASCII digit
  R10 Hangul syllable (U+AC00..U+D7A3)                       -> itself
  R11 everything else: NFKD, casefold, NFKD again, strip R4
      marks, apply LETTER_EXTRA (ae, oe, o-slash, ...), then
      fold every resulting code point again with R1..R11. If a
      mark that R4 does not strip survives (e.g. kana voicing
      marks), the code point keeps only its casefold, and if
      even that carries such a mark, it maps to itself.

Only code points whose result differs from themselves are written. A code
point absent from the table maps to itself. Every value in the table is a
string of FIXED POINTS (code points that are absent from the table or map
to themselves), which is what makes fold_v1 idempotent — the tests assert
it. No value ever contains U+10FFFF (a noncharacter, R1), which is what
makes the prefix range query in SCHEMA.md exact.
"""

import argparse
import hashlib
import json
import sys
import unicodedata

FOLD_VERSION = 1

# R4: marks that fold_v1 removes. Ranges are inclusive. Only code points of
# general category Mn/Me inside these ranges are stripped.
MARK_RANGES = (
    (0x0300, 0x036F),  # Combining Diacritical Marks
    (0x0591, 0x05C7),  # Hebrew points and cantillation (Mn only)
    (0x0610, 0x061A),  # Arabic marks
    (0x064B, 0x065F),  # Arabic harakat
    (0x0670, 0x0670),  # Arabic superscript alef
    (0x06D6, 0x06ED),  # Arabic Quranic marks (Mn only)
    (0x1AB0, 0x1AFF),  # Combining Diacritical Marks Extended
    (0x1DC0, 0x1DFF),  # Combining Diacritical Marks Supplement
    (0x20D0, 0x20FF),  # Combining Diacritical Marks for Symbols
    (0xFE00, 0xFE0F),  # Variation Selectors
    (0xFE20, 0xFE2F),  # Combining Half Marks
    (0xE0100, 0xE01EF),  # Variation Selectors Supplement
)

# R5: punctuation that JOINS (vanishes) instead of separating, so that
# "O'Brien" ~ "obrien", "E.U.A." ~ "eua", "l·l" ~ "ll".
JOINERS = frozenset((
    0x0027,  # APOSTROPHE
    0x0060,  # GRAVE ACCENT
    0x00B4,  # ACUTE ACCENT
    0x2018,  # LEFT SINGLE QUOTATION MARK
    0x2019,  # RIGHT SINGLE QUOTATION MARK
    0x201B,  # SINGLE HIGH-REVERSED-9 QUOTATION MARK
    0x2032,  # PRIME
    0x2035,  # REVERSED PRIME
    0x02B9,  # MODIFIER LETTER PRIME
    0x02BB,  # MODIFIER LETTER TURNED COMMA (okina)
    0x02BC,  # MODIFIER LETTER APOSTROPHE
    0x02BE,  # MODIFIER LETTER RIGHT HALF RING
    0x02BF,  # MODIFIER LETTER LEFT HALF RING
    0xFF07,  # FULLWIDTH APOSTROPHE
    0x002E,  # FULL STOP
    0xFF0E,  # FULLWIDTH FULL STOP
    0x00B7,  # MIDDLE DOT
    0x0387,  # GREEK ANO TELEIA
    0x2027,  # HYPHENATION POINT
    0x0640,  # ARABIC TATWEEL (a letter-extender, not punctuation)
))

# R6: ASCII symbols that carry meaning in titles ("C#", "AT&T", "100%").
KEPT = frozenset((0x0023, 0x0026, 0x0040, 0x0025))

# R11: letters that neither NFKD nor casefold decompose.
LETTER_EXTRA = {
    "æ": "ae", "œ": "oe", "ø": "o", "đ": "d", "ð": "d", "þ": "th",
    "ł": "l", "ħ": "h", "ŧ": "t", "ı": "i", "ȷ": "j",
}

WHITESPACE_CC = frozenset((0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x85))


def is_noncharacter(cp):
    return 0xFDD0 <= cp <= 0xFDEF or (cp & 0xFFFE) == 0xFFFE


def is_stripped_mark(cp):
    if unicodedata.category(chr(cp)) not in ("Mn", "Me"):
        return False
    return any(lo <= cp <= hi for lo, hi in MARK_RANGES)


def has_unstripped_mark(text):
    return any(
        unicodedata.category(ch) in ("Mn", "Mc", "Me") and not is_stripped_mark(ord(ch))
        for ch in text
    )


class Folder:
    def __init__(self):
        self.memo = {}

    def fold_cp(self, cp, depth=0):
        if cp in self.memo:
            return self.memo[cp]
        if depth > 8:
            raise RuntimeError("fold recursion too deep at U+%04X" % cp)
        result = self._rule(cp, depth)
        self.memo[cp] = result
        return result

    def _rule(self, cp, depth):
        ch = chr(cp)
        cat = unicodedata.category(ch)
        if is_noncharacter(cp):  # R1
            return ""
        if cp in WHITESPACE_CC or cat in ("Zs", "Zl", "Zp"):  # R2
            return " "
        if cat in ("Cc", "Cf"):  # R3
            return ""
        if is_stripped_mark(cp):  # R4
            return ""
        # Compatibility twins (fullwidth / small-form variants) of a joiner or
        # a kept symbol behave like the ASCII original: "＆" -> "&", "．" -> "".
        compat = unicodedata.normalize("NFKD", ch)
        twin = ord(compat) if len(compat) == 1 else None
        if cp in JOINERS or twin in JOINERS:  # R5
            return ""
        if cp in KEPT:  # R6
            return ch
        if twin in KEPT:  # R6 (twin)
            return compat
        if cat.startswith("P"):  # R7
            return " "
        if cat == "Sk":  # R8
            return ""
        if cat == "Nd":  # R9
            return str(unicodedata.decimal(ch))
        if 0xAC00 <= cp <= 0xD7A3:  # R10
            return ch
        # R11
        text = unicodedata.normalize("NFKD", ch).casefold()
        text = unicodedata.normalize("NFKD", text)
        text = "".join(c for c in text if not is_stripped_mark(ord(c)))
        if has_unstripped_mark(text):
            text = ch.casefold()
            if has_unstripped_mark(text):
                return ch
        text = "".join(LETTER_EXTRA.get(c, c) for c in text)
        if text == ch:
            return ch
        out = []
        for c in text:
            sub = ord(c)
            out.append(c if sub == cp else self.fold_cp(sub, depth + 1))
        return "".join(out)


def generate(version=FOLD_VERSION):
    folder = Folder()
    entries = {}
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        ch = chr(cp)
        # Unassigned code points (Cn) other than noncharacters pass through:
        # their future meaning is unknown, and a fold version is frozen.
        if unicodedata.category(ch) == "Cn" and not is_noncharacter(cp):
            continue
        folded = folder.fold_cp(cp)
        if folded != ch:
            entries["%04X" % cp] = folded
    # Self-check: every value is made of fixed points (idempotence) and never
    # contains U+10FFFF.
    for key, value in entries.items():
        for c in value:
            hexkey = "%04X" % ord(c)
            if hexkey in entries and entries[hexkey] != c:
                raise RuntimeError("U+%s folds to non-fixed point U+%s" % (key, hexkey))
            if ord(c) == 0x10FFFF:
                raise RuntimeError("U+%s folds to U+10FFFF" % key)
    return {
        "fold_version": version,
        "unicode_version": unicodedata.unidata_version,
        "python_version": "%d.%d.%d" % sys.version_info[:3],
        "generator": "tools/gen_fold_table.py",
        "note": "FROZEN. Code points absent from 'entries' map to themselves. "
                "Keys are uppercase hex code points. See SCHEMA.md fold_v1.",
        "entry_count": len(entries),
        "entries": dict(sorted(entries.items(), key=lambda kv: int(kv[0], 16))),
    }


def serialize(table):
    """One entry per line so the frozen file diffs and reviews well."""
    head = {k: v for k, v in table.items() if k != "entries"}
    lines = ["{"]
    for key, value in head.items():
        lines.append("  %s: %s," % (json.dumps(key), json.dumps(value, ensure_ascii=False)))
    lines.append('  "entries": {')
    items = list(table["entries"].items())
    for index, (key, value) in enumerate(items):
        comma = "," if index < len(items) - 1 else ""
        lines.append("    %s: %s%s" % (json.dumps(key), json.dumps(value, ensure_ascii=True), comma))
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Generate the frozen fold table.")
    parser.add_argument("--out", required=True)
    parser.add_argument("--version", type=int, default=FOLD_VERSION)
    parser.add_argument("--check", action="store_true",
                        help="compare a fresh generation with --out instead of writing")
    args = parser.parse_args()
    text = serialize(generate(args.version))
    if args.check:
        with open(args.out, "r", encoding="utf-8") as handle:
            current = handle.read()
        same = current == text
        print("fold table %s: %s (sha256 %s)" % (
            args.out, "identical" if same else "DIFFERENT",
            hashlib.sha256(current.encode("utf-8")).hexdigest()))
        return 0 if same else 1
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(text)
    print("wrote %s (%d bytes, sha256 %s)" % (
        args.out, len(text.encode("utf-8")), hashlib.sha256(text.encode("utf-8")).hexdigest()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
