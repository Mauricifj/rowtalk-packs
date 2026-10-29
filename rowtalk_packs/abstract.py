"""Abstract trimming: the lead's opening text -> a short plain-text abstract.

Contract (SCHEMA.md "abstract"):
  1. C0/C1 control characters that are not whitespace (NUL, U+0001..U+0008,
     U+000E..U+001B, U+007F..U+0084, U+0086..U+009F) are removed. Format
     characters (Cf: ZWJ, ZWNJ, bidi marks, soft hyphen) are KEPT — emoji
     ZWJ sequences, Persian/Indic shaping and RTL text need them.
  2. Whitespace is normalized: every run of whitespace becomes one space,
     leading/trailing whitespace is removed.
  3. Stripped-template leftovers are scrubbed: empty or punctuation-only
     parentheses ("()", "( )", "(; )", "(, ;)"), a separator directly after
     "(" or before the ")" of a group that "(" opened ("(; born 1950)" ->
     "(born 1950)"; a ":)" with no open group is text and stays), a space
     before a comma/semicolon, a space before a period that ENDS a token
     (followed by a space, a closer or the end — ".NET", ".45", ".com" keep
     their space), and double spaces.
  4. If the text fits in max_chars (code points), it is kept whole.
  5. Otherwise whole sentences are kept: the longest prefix that ends at a
     TRUSTED sentence end (see INVARIANT), fits in max_chars and is BALANCED (every "(", "[", "“",
     "«" closed by a later partner — checked with a running depth, so a
     stray closer such as the ")" of "a) b)" can never hide an open group —
     and an even number of '"'). Any length qualifies: a clean one-sentence
     abstract is the spec's answer, however short.
  6. Otherwise (no trusted sentence end fits) the text is cut at the last
     word boundary that leaves room for the ellipsis, trailing separators
     are dropped, and "…" is appended. A single token longer than the
     budget is cut at a grapheme boundary (never before a combining mark,
     variation selector, emoji modifier or tag, never next to a ZWJ, never
     inside a regional-indicator pair). The result is never longer than
     max_chars CODE POINTS (Dart: `runes.length`; `String.length` counts
     UTF-16 units and can reach 2x).

INVARIANT (DC2-01/DC2-02/DC3-01/DC4-01): an abstract that does not end in
"…" ends at a boundary of a TRUSTED KIND. The rule that serves it is
ASYMMETRIC on purpose: rejecting a real boundary only costs content (the
abstract ends at an earlier sentence, or honestly with "…"); accepting a
false one publishes a fragment. So every boundary kind that is ambiguous in
encyclopedic text is NOT a boundary, and the kinds are decided by the SHAPE
of the text, not by how complete a word list is. The shape rules judge the
token before the "." — or, for a JOINED token whose last segment after a
hyphen, dash or slash starts with an uppercase letter, that segment
("Lt-Col." is judged as "Col.", "Vice-Adm." as "Adm.", "LT-COL." as "COL.";
DC4-01). Not trusted:
  * "!" and "?": never boundaries. In encyclopedic leads they are almost
    always part of a name or title, and no token shape tells the two apart:
    "Yahoo!", "Jeopardy!", "yahoo!", "!!!", "O que é isso, companheiro?
    (1997)", "Ai, que saudade! Montado em 1980" all read like sentence ends.
  * "." after an empty token, a single character ("J. Smith", "c. 1500"), or
    a dotted token ("U.S.", "n.º");
  * "." after a SHORT CAPITALISED token — at most SHORT_TITLE_MAX letters,
    an uppercase first letter, the rest lowercase ("Mr.", "Pte.", "Msgr.",
    "Pvt.", "Bros.", "Hob.", "Vols.") — UNLESS the next token is exactly one
    of the language's configured `sentence_starters` ("The", "It", "O",
    "Em", "Located", "Localizado", ...). This is the kind that covers
    abbreviations no list names: an abbreviation of that shape introduces a
    NAME ("Pte. Ltd.", "Msgr. John", "Hob. XVI"), never an article, a
    pronoun or a lead's participle — so only a starter re-trusts it, and
    the starter list can only ever be incomplete in the SAFE direction (a
    missing starter costs content). The starter must be the WHOLE next
    token ("A" re-trusts, "A." — an initial — does not);
  * "." after a configured abbreviation (per language, plus the shared list
    `abstract.shared_abbreviations` that both languages use, since names
    such as "Warner Bros." and "Kramer vs. Kramer" are quoted verbatim in
    every language), or its plural: a LOWERCASE plural ("arts.", "eds.") or
    a CAPITALISED plural of exactly SHORT_TITLE_MAX + 1 letters ("Profs.",
    "Capts."; a shorter one is already a short capitalised token, and
    "Mars." stays re-trustable as one). A lowercase or capitalised listed
    form is never re-trusted. An ALL-CAPS listed form ("CIA.", "PTE.",
    "CO.", "JR.") is ambiguous — the agency, a state code, or a company
    name in capitals ("CIA. HERING", "PTE. LTD.") — so it is trusted only
    before a starter, like a short capitalised token ("CIA. Durante",
    "Denver, CO. The"). An all-caps token NO list names is an acronym and
    trusted ("NASA.");
  * "." followed by a digit when the token before it is a short (<=
    NUMBERED_REFERENCE_MAX characters) non-numeric word that does NOT follow
    a number — the signature of a numbered reference ("Op. 27", "art. 5",
    "pt. 2"). After a number the short word is a unit and the "." is a real
    end ("12 km. 18 villages", "2 285 km. 50 bairros") (DC3-02).
WORDS, NOT ABBREVIATIONS (DC4-02): the configured `not_abbreviations` (per
language plus `abstract.shared_not_abbreviations`) are words the rules above
would misread — short place names ("Sul", "Pará", "Ohio", "Peru") and
plurals that are words ("press", "corps", "figs"). They end a sentence like
any word. This list RE-TRUSTS, so it must only ever name words that are
never abbreviations (a config test keeps it disjoint from every abbreviation
list); its incompleteness costs content only.
RESIDUAL (not closable by shape, stated so nobody reads "never" here): an
abbreviation that no list names and that is followed by an uppercase word,
a digit or an opener is still trusted when it is
  - LOWERCASE ("assoc. Professor" if "assoc" were unlisted), including a
    joined token whose last segment is lowercase ("vice-adm.");
  - capitalised and longer than SHORT_TITLE_MAX letters ("Comdr."), unless
    it is a listed form's plural of SHORT_TITLE_MAX + 1 letters;
  - ALL-CAPS and named by no list THIS language's rules hold ("PTY. LTD."
    — "pty" is unlisted; "STO." in an English lead — "sto" is pt-only);
and a short capitalised or listed all-caps abbreviation is re-trusted when a
NAME after it happens to be spelled like a starter. Each one found is closed
by adding it to a list; the shape rules above close every short capitalised
one in advance.
The length of the prefix plays no part (the round-1 "40% share" rule threw
away correct short sentences and still accepted false boundaries past 40%).

A sentence end is "." (optionally followed by closing quotes or brackets)
that is followed by a space and then an uppercase letter, a digit or an
opening quote/bracket, or that ends the text — minus the exceptions above.
"""

import re
import unicodedata

_CONTROL = re.compile("[\x00-\x08\x0e-\x1b\x7f-\x84\x86-\x9f]")
_WS = re.compile(r"\s+")
_EMPTY_PARENS = re.compile(r"\(\s*[,;:]*\s*\)")
_OPEN_SEP = re.compile(r"\(\s*[,;:]+\s*")
_CLOSE_SEP = re.compile(r"(\([^()]*?)\s*[,;:]+\s*\)")
_SPACE_BEFORE_SEP = re.compile(r" +([,;])")
_SPACE_BEFORE_DOT = re.compile(r" +\.(?=[ \"'”’»)\]]|$)")
_DOUBLE_SPACE = re.compile(r" {2,}")
_CLOSERS = "\"'”’»)]"
_OPENERS = "\"'“‘«(["
_ZWJ = "\u200d"
_PAIRS = {"(": ")", "[": "]", "\u201c": "\u201d", "\u00ab": "\u00bb"}
_OPENER_OF = {closer: opener for opener, closer in _PAIRS.items()}
NUMBERED_REFERENCE_MAX = 3  # "Op. 27", "art. 5": short word + "." + digit
SHORT_TITLE_MAX = 4  # "Mr.", "Pte.", "Msgr.", "Bros.": capitalised, <= 4 letters


def normalize_ws(text):
    return _WS.sub(" ", text).strip()


def scrub(text):
    text = normalize_ws(_CONTROL.sub("", text))
    previous = None
    # Iterate: removing "( )" can expose another empty pair "(( ))". Every
    # rule only deletes characters, so this terminates.
    while previous != text:
        previous = text
        text = _EMPTY_PARENS.sub("", text)
        text = _OPEN_SEP.sub("(", text)
        text = _CLOSE_SEP.sub(r"\1)", text)
        text = _SPACE_BEFORE_SEP.sub(r"\1", text)
        text = _SPACE_BEFORE_DOT.sub(".", text)
        text = _DOUBLE_SPACE.sub(" ", text)
    return text.strip()


def _token_before(text, index):
    start = text.rfind(" ", 0, index) + 1
    return text[start:index].lstrip(_OPENERS)


def _previous_token(text, index):
    """The token before the one that ends at `index` ("" at the start)."""
    start = text.rfind(" ", 0, index)
    if start <= 0:
        return ""
    return _token_before(text, start)


_JOINERS = "-\u2010\u2011\u2013\u2014/"


def _core(token):
    """The part of `token` the shape rules judge: the last segment of a
    joined token when that segment starts with an uppercase letter ("Lt-Col"
    -> "Col", "Vice/Adm" -> "Adm"), else the whole token (DC4-01)."""
    cut = max(token.rfind(joiner) for joiner in _JOINERS)
    if cut >= 0 and token[cut + 1:cut + 2].isupper():
        return token[cut + 1:]
    return token


def _is_acronym(token):
    return len(token) >= 2 and token.isalpha() and token.isupper()


def _is_short_title(token):
    """'Mr', 'Pte', 'Msgr', 'Bros': an abbreviation's shape, listed or not."""
    return (token.isalpha() and len(token) <= SHORT_TITLE_MAX and token[0].isupper()
            and (len(token) == 1 or token[1:].islower()))


def _is_listed(token, abbreviations):
    """The token, in any case, or its plural (see the module INVARIANT for
    which plurals), is a configured abbreviation."""
    key = token.casefold()
    if key in abbreviations:
        return True
    if len(key) <= 2 or not key.endswith("s") or key[:-1] not in abbreviations:
        return False
    return token.islower() or (len(token) == SHORT_TITLE_MAX + 1 and token[0].isupper()
                               and token[1:].islower())


def _is_abbreviation(text, period_index, abbreviations, words=frozenset()):
    token = _token_before(text, period_index)
    if not token:
        return True
    if len(token) == 1:
        return True  # initials: "J. Smith", "c. 1500"
    if "." in token:
        return True  # dotted acronyms: "U.S.", "e.g."
    core = _core(token)
    if core.casefold() in words:
        return False  # "Sul", "Ohio", "press": words the shapes would misread (DC4-02)
    if _is_short_title(core):
        return True  # "Pte.", "Msgr.", "Pvt.", "Lt-Col.": no list needs to name them
    return _is_listed(core, abbreviations)


def _starter_retrusts(core, abbreviations):
    """True for the two AMBIGUOUS kinds a following starter re-trusts: an
    unlisted short capitalised token of 2+ letters, and a listed all-caps
    one ("CIA.", "CO.")."""
    if _is_acronym(core):
        return True  # only reached when listed: an unlisted acronym is trusted
    return len(core) > 1 and _is_short_title(core) and not _is_listed(core, abbreviations)


def _has_digit(token):
    return any(ch.isdigit() for ch in token)


def _is_numbered_reference(text, period_index):
    token = _token_before(text, period_index)
    if len(token) > NUMBERED_REFERENCE_MAX or _has_digit(token):
        return False
    # "12 km. 18 villages": a short word after a number is a unit, and the
    # "." after it is a real end (DC3-02).
    return not _has_digit(_previous_token(text, period_index - len(token)))


def _next_token(text, index):
    end = text.find(" ", index)
    return text[index:] if end < 0 else text[index:end]


def sentence_ends(text, abbreviations=frozenset(), starters=frozenset(), words=frozenset()):
    """Yield indices just past each TRUSTED sentence end (exclusive slice
    bounds); see the module INVARIANT for what is not trusted. `starters`
    are exact (case-sensitive) tokens that re-trust a "." after an UNLISTED
    short capitalised token or a LISTED all-caps one; `words` (casefolded)
    are never abbreviations."""
    length = len(text)
    for index, ch in enumerate(text):
        if ch != ".":
            continue  # "!" and "?" are never trusted (module INVARIANT)
        end = index + 1
        while end < length and text[end] in _CLOSERS:
            end += 1
        if end >= length:
            yield end
            continue
        if text[end] != " " or end + 1 >= length:
            continue
        nxt = text[end + 1]
        if not (nxt.isupper() or nxt.isdigit() or nxt in _OPENERS):
            continue
        if _is_abbreviation(text, index, abbreviations, words):
            token = _token_before(text, index)
            if not (len(token) > 1 and "." not in token
                    and _starter_retrusts(_core(token), abbreviations)
                    and _next_token(text, end + 1) in starters):
                continue
        if nxt.isdigit() and _is_numbered_reference(text, index):
            continue
        yield end


def balanced(text):
    """Every "(", "[", "“", "«" is closed by a LATER partner (running depth;
    a closer with no open partner is ignored, so it cannot cancel a later
    opener), and the number of '"' is even (see contract step 5)."""
    depth = dict.fromkeys(_PAIRS, 0)
    for ch in text:
        if ch in depth:
            depth[ch] += 1
        elif ch in _OPENER_OF and depth[_OPENER_OF[ch]]:
            depth[_OPENER_OF[ch]] -= 1
    return not any(depth.values()) and text.count('"') % 2 == 0


def _extends(ch):
    """True if `ch` belongs to the grapheme cluster before it."""
    cp = ord(ch)
    return (ch == _ZWJ or unicodedata.category(ch) in ("Mn", "Me", "Mc")
            or 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF   # variation selectors
            or 0x1F3FB <= cp <= 0x1F3FF                             # emoji modifiers
            or 0xE0020 <= cp <= 0xE007F)                            # tag characters


def _regional(ch):
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def grapheme_floor(text, index):
    """The largest cut position <= index that does not split a grapheme
    cluster (approximation of UAX #29 sufficient for a hard cut)."""
    i = index
    while 0 < i < len(text) and (_extends(text[i]) or text[i - 1] == _ZWJ):
        i -= 1
    if 0 < i < len(text) and _regional(text[i]):
        run = 0
        while i - run - 1 >= 0 and _regional(text[i - run - 1]):
            run += 1
        if run % 2:
            i -= 1  # text[i-1] and text[i] are one flag
    return i if i > 0 else index


def make_abstract(opening_text, max_chars=600, ellipsis="…", abbreviations=frozenset(),
                  starters=frozenset(), words=frozenset()):
    """Return the abstract for `opening_text` ("" when nothing usable)."""
    if not isinstance(opening_text, str):
        return ""
    text = scrub(opening_text)
    if len(text) <= max_chars:
        return text
    best = 0
    for end in sentence_ends(text, abbreviations, starters, words):
        if end > max_chars:
            break
        if balanced(text[:end]):
            best = end
    if best:
        return text[:best].rstrip()
    budget = max_chars - len(ellipsis)
    cut = text.rfind(" ", 0, budget + 1)
    if cut <= 0:
        cut = grapheme_floor(text, budget)  # one giant token: hard cut
    head = text[:cut].rstrip(" ,;:-–—(")
    return head + ellipsis
