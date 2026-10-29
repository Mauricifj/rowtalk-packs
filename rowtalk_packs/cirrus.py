"""Tolerant parser for CirrusSearch content-index dump lines.

The dump is newline-delimited JSON. Per Meta "Data dumps/Misc dumps format"
it is Elasticsearch BULK format — an action line such as
  {"index":{"_type":"page","_id":"29012876"}}
followed by the document line. The new sharded dump's exact shape is
unverified, so this parser accepts BOTH shapes:
  * action line + document line (page id from the action's `_id` when the
    document has no `page_id`), and
  * document lines only (page id from the document's `page_id`).
A line is an ACTION line iff it is a JSON object with exactly one key out of
{index, create, update, delete} whose value is an object or null. A real
document always has many keys, so the two can never be confused.

Malformed lines (invalid JSON, non-object JSON) are counted and skipped; an
action line followed by a malformed line does not leak its id to the next
document. Unknown fields are ignored; missing fields degrade per field
(`classify` decides whether the page is usable).

INVARIANT (no text that cannot be stored): `json.loads` ACCEPTS lone UTF-16
surrogates (the escape "\\ud83d", or raw CESU bytes ED A0 BD, which it
decodes with surrogatepass), and such a string cannot be encoded to UTF-8 —
SQLite would raise and kill the whole language build. So every string the
builder STORES is checked here: a title or opening text containing
U+D800..U+DFFF excludes the page with reason `bad_unicode` (counted with the
other exclusion reasons, logged per shard and shown by the probe); a
redirect title containing one is dropped from the aliases. Fields the
builder only compares (categories, templates) are harmless and not checked.
"""

import json
import re

from .abstract import make_abstract
from .fold import fold

ACTION_KEYS = frozenset(("index", "create", "update", "delete"))
_SURROGATE = re.compile("[\ud800-\udfff]")


def has_surrogate(text):
    return _SURROGATE.search(text) is not None


class LineStats:
    def __init__(self):
        self.lines = 0
        self.blank = 0
        self.malformed = 0
        self.action_lines = 0
        self.action_ids = 0
        self.docs = 0
        self.docs_with_page_id = 0
        self.docs_id_from_action = 0
        self.docs_without_id = 0

    def as_dict(self):
        return dict(self.__dict__)


def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    # ASCII digits only: str.isdigit() is also true for "²" or "١", which
    # int() rejects (ValueError) or reads as a different number.
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def is_action(obj):
    if len(obj) != 1:
        return False
    (key, value), = obj.items()
    return key in ACTION_KEYS and (value is None or isinstance(value, dict))


class DocReader:
    """Turns lines into (page_id, doc) pairs. Stateful across calls to
    `docs()` — an action line at the end of one batch still names the
    document at the start of the next batch of the SAME shard. One reader
    per shard copy; a fresh reader is used when a shard is re-read."""

    def __init__(self, stats):
        self.stats = stats
        self.pending_id = None

    def docs(self, lines):
        stats = self.stats
        for raw in lines:
            stats.lines += 1
            if not raw.strip():
                stats.blank += 1
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                stats.malformed += 1
                self.pending_id = None
                continue
            if not isinstance(obj, dict):
                stats.malformed += 1
                self.pending_id = None
                continue
            if is_action(obj):
                stats.action_lines += 1
                meta = next(iter(obj.values())) or {}
                self.pending_id = _as_int(meta.get("_id"))
                if self.pending_id is not None:
                    stats.action_ids += 1
                continue
            stats.docs += 1
            page_id = _as_int(obj.get("page_id"))
            if page_id is not None:
                stats.docs_with_page_id += 1
            elif self.pending_id is not None:
                page_id = self.pending_id
                stats.docs_id_from_action += 1
            else:
                stats.docs_without_id += 1
            self.pending_id = None
            yield page_id, obj


def iter_docs(lines, stats):
    """Yield (page_id or None, doc dict) for every document line of one
    shard. `lines` yields bytes or str, one JSON value per line."""
    yield from DocReader(stats).docs(lines)


def _norm_name(value):
    return " ".join(value.replace("_", " ").split()).casefold()


class LangRules:
    """Per-language exclusion and trimming rules, from config/packs.json."""

    def __init__(self, lang_config, abstract_config, alias_config):
        dis = lang_config.get("disambiguation", {})
        self.title_suffixes = tuple(s.casefold() for s in dis.get("title_suffixes", ()))
        self.categories = frozenset(_norm_name(c) for c in dis.get("categories", ()))
        self.templates = frozenset(_norm_name(t) for t in dis.get("templates", ()))
        self.redirect_markers = tuple(m.casefold() for m in lang_config.get("redirect_markers", ()))
        # The language's own list plus the shared one: names such as "Warner
        # Bros." or "Kramer vs. Kramer" are quoted verbatim in every language
        # (DC3-01 — the pt list had none of the English forms).
        self.abbreviations = frozenset(
            a.casefold() for a in (tuple(lang_config.get("abbreviations", ()))
                                   + tuple(abstract_config.get("shared_abbreviations", ()))))
        # Exact tokens that re-trust a "." after an unlisted short capitalised
        # word ("... in Peru. The ..."); a missing one only costs content.
        self.sentence_starters = frozenset(lang_config.get("sentence_starters", ()))
        # Words the abbreviation shapes would misread ("Sul", "Ohio", "press"):
        # they end a sentence like any word (DC4-02). Never an abbreviation.
        self.not_abbreviations = frozenset(
            w.casefold() for w in (tuple(lang_config.get("not_abbreviations", ()))
                                   + tuple(abstract_config.get("shared_not_abbreviations", ()))))
        self.max_chars = abstract_config.get("max_chars", 600)
        self.ellipsis = abstract_config.get("ellipsis", "…")
        self.max_aliases = alias_config.get("max_per_article", 8)
        self.min_alias_chars = alias_config.get("min_key_chars", 2)

    def disambiguation_signals(self, doc):
        """Names of the signals that mark `doc` as a disambiguation page."""
        hits = []
        title = doc.get("title")
        if isinstance(title, str) and title.casefold().endswith(self.title_suffixes):
            hits.append("title_suffix")
        cats = doc.get("category")
        if isinstance(cats, list) and any(
                isinstance(c, str) and _norm_name(c) in self.categories for c in cats):
            hits.append("category")
        temps = doc.get("template")
        if isinstance(temps, list) and any(
                isinstance(t, str) and _norm_name(t) in self.templates for t in temps):
            hits.append("template")
        return hits

    def is_redirect_page(self, doc):
        source = doc.get("source_text")
        if not isinstance(source, str) or not self.redirect_markers:
            return False
        return source.lstrip()[:40].casefold().startswith(self.redirect_markers)


def select_aliases(title_key, redirects, max_aliases=8, min_chars=2):
    """Up to `max_aliases` DISTINCT fold keys of namespace-0 redirect titles,
    excluding the title's own key and keys shorter than `min_chars` code
    points; shortest first, then by code point order."""
    keys = set()
    if isinstance(redirects, list):
        for item in redirects:
            if not isinstance(item, dict):
                continue
            ns = _as_int(item.get("namespace"))
            title = item.get("title")
            if ns != 0 or not isinstance(title, str) or has_surrogate(title):
                continue
            key = fold(title)
            if len(key) >= min_chars and key != title_key:
                keys.add(key)
    return sorted(keys, key=lambda k: (len(k), k))[:max_aliases]


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


class Candidate:
    __slots__ = ("page_id", "title", "abstract", "popularity", "incoming_links", "aliases")

    def __init__(self, page_id, title, abstract, popularity, incoming_links, aliases):
        self.page_id = page_id
        self.title = title
        self.abstract = abstract
        self.popularity = popularity
        self.incoming_links = incoming_links
        self.aliases = aliases


def classify(page_id, doc, rules):
    """Return (Candidate, None) for a usable page, or (None, reason)."""
    if page_id is None:
        return None, "no_page_id"
    namespace = _as_int(doc.get("namespace"))
    if namespace is None:
        return None, "no_namespace"
    if namespace != 0:
        return None, "non_ns0"
    title = doc.get("title")
    if not isinstance(title, str) or not title.strip():
        return None, "no_title"
    opening = doc.get("opening_text")
    if has_surrogate(title) or (isinstance(opening, str) and has_surrogate(opening)):
        return None, "bad_unicode"
    if rules.is_redirect_page(doc):
        return None, "redirect_page"
    if rules.disambiguation_signals(doc):
        return None, "disambiguation"
    abstract = make_abstract(opening, rules.max_chars, rules.ellipsis,
                             rules.abbreviations, rules.sentence_starters,
                             rules.not_abbreviations)
    if not abstract:
        return None, "empty_opening_text"
    title_key = fold(title)
    aliases = select_aliases(title_key, doc.get("redirect"), rules.max_aliases,
                             rules.min_alias_chars)
    if not title_key and not aliases:
        # e.g. "!!!", "?", "...": fold_v1 maps the title to "" and nothing
        # else names the page, so no query can ever reach it (DC-08).
        return None, "unsearchable"
    return Candidate(page_id, title.strip(), abstract, _number(doc.get("popularity_score")),
                     _as_int(doc.get("incoming_links")), aliases), None
