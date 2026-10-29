"""Pack schema v2: writer and the query recipes (the SQL in SCHEMA.md).

The query functions here are the REFERENCE implementation of the recipes the
app must run; validation, golden queries, the latency measurement and the
tests all go through them, so the SQL in SCHEMA.md is exercised, not just
documented.

Search structure (chosen by measurement, see README "Search latency"):
  * search(key, rank): one row per (fold key, entity). PK (key, rank) is the
    covering index for prefix range scans.
  * prefix_top(prefix, ranks): for every HEAVY prefix — a prefix of a search
    key, at least 2 code points long, not ending in U+0020, matched by MORE
    than `heavy_prefix_rows` search rows — the best (lowest) `suggestions`
    distinct ranks.
  INVARIANT: for any folded query q (len >= 2), either prefix_top has a row
  for q (and it holds the exact top ranks), or at most `heavy_prefix_rows`
  search rows match q, so the range scan below is bounded. Either way the
  answer equals "the `suggestions` best distinct ranks over all keys that
  start with q".
"""

import os
import sqlite3

from .fold import PREFIX_SENTINEL, fold

SCHEMA_VERSION = 2

COMMON_SQL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE search(
  key  TEXT NOT NULL,
  rank INTEGER NOT NULL,
  PRIMARY KEY (key, rank)
) WITHOUT ROWID;
CREATE TABLE prefix_top(
  prefix TEXT PRIMARY KEY,
  ranks  TEXT NOT NULL
) WITHOUT ROWID;
"""

WIKI_SQL = """
CREATE TABLE articles(
  rank     INTEGER PRIMARY KEY,
  page_id  INTEGER NOT NULL,
  title    TEXT NOT NULL,
  abstract TEXT NOT NULL
);
"""

DICT_SQL = """
CREATE TABLE words(
  lemma  TEXT NOT NULL,
  pos    TEXT NOT NULL,
  senses TEXT NOT NULL,
  PRIMARY KEY (lemma, pos)
) WITHOUT ROWID;
CREATE TABLE lemmas(
  rank  INTEGER PRIMARY KEY,
  lemma TEXT NOT NULL
);
"""

# kind -> (entity table, display column)
ENTITY = {"wiki": ("articles", "title"), "dict": ("lemmas", "lemma")}

EXPECTED_COLUMNS = {
    "meta": ["key", "value"],
    "search": ["key", "rank"],
    "prefix_top": ["prefix", "ranks"],
    "articles": ["rank", "page_id", "title", "abstract"],
    "words": ["lemma", "pos", "senses"],
    "lemmas": ["rank", "lemma"],
}
# table -> (primary key columns in order, WITHOUT ROWID). The search PK
# (key, rank) WITHOUT ROWID is what makes the prefix range scan a bounded
# covering-index walk; the validator checks these, not just column names.
PRIMARY_KEYS = {
    "meta": (["key"], True),
    "search": (["key", "rank"], True),
    "prefix_top": (["prefix"], True),
    "articles": (["rank"], False),
    "words": (["lemma", "pos"], True),
    "lemmas": (["rank"], False),
}
TABLES = {"wiki": ["meta", "search", "prefix_top", "articles"],
          "dict": ["meta", "search", "prefix_top", "words", "lemmas"]}


def _open_new(path):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript("PRAGMA page_size = 4096; PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;")
    return db


def heavy_prefixes(rows, threshold, top_n):
    """Stream (key, rank) rows sorted by (key, rank) and return
    {prefix: [ranks]} for every heavy prefix (see module doc).

    Blocks of rows sharing key[:l] are contiguous in sorted order, so each
    block is opened when the key's prefix at length l first appears and
    closed when it changes. A row's rank enters the block of its full key
    (the deepest); a closing block merges its top ranks into its parent
    (length l-1), which is open while any child is open. Work is
    proportional to the number of distinct prefixes, not keys x depth.
    """
    result = {}
    stack = []  # stack[i] = [length, start_row, top ranks], lengths ascending
    previous = ""
    index = -1

    def close_to(length, at_row):
        while stack and stack[-1][0] > length:
            depth, start, top = stack.pop()
            prefix = previous[:depth]
            if at_row - start > threshold and not prefix.endswith(" "):
                result[prefix] = top
            if stack and stack[-1][0] == depth - 1:
                stack[-1][2] = sorted(set(stack[-1][2]) | set(top))[:top_n]

    for index, (key, rank) in enumerate(rows):
        if key != previous:
            common = 0
            limit = min(len(key), len(previous))
            while common < limit and key[common] == previous[common]:
                common += 1
            close_to(common, index)
            previous = key
            depth = stack[-1][0] if stack else 1
            for length in range(max(depth, common, 1) + 1, len(key) + 1):
                stack.append([length, index, []])
        if stack and stack[-1][0] == len(key):
            top = stack[-1][2]
            if rank not in top and (len(top) < top_n or rank < top[-1]):
                top.append(rank)
                top.sort()
                del top[top_n:]
    close_to(0, index + 1)
    return result


def write_pack(path, kind, meta, entities, words=None, heavy_rows=1000, suggestions=8):
    """Write a schema-v2 pack at `path`.

    entities: iterable of (rank, display, keys, extra) best first, where
      wiki extra = (page_id, abstract) and dict extra = ().
    words (dict only): iterable of (lemma, pos, senses_json).
    Returns counts.
    """
    db = _open_new(path)
    try:
        return _fill(db, kind, meta, entities, words, heavy_rows, suggestions)
    except BaseException:
        db.close()
        os.remove(path)  # never leave a half-written pack behind
        raise


def _fill(db, kind, meta, entities, words, heavy_rows, suggestions):
    db.executescript(COMMON_SQL + (WIKI_SQL if kind == "wiki" else DICT_SQL))
    keys = []
    count = 0
    batch = []
    if kind == "wiki":
        insert = "INSERT INTO articles VALUES (?,?,?,?)"
    else:
        insert = "INSERT INTO lemmas VALUES (?,?)"
    for rank, display, entity_keys, extra in entities:
        count += 1
        if rank != count:
            raise ValueError("entities must be ranked 1..N in order (got %d at %d)" % (rank, count))
        if kind == "wiki":
            page_id, abstract = extra
            batch.append((rank, page_id, display, abstract))
        else:
            batch.append((rank, display))
        for key in entity_keys:
            if key:
                keys.append((key, rank))
        if len(batch) >= 10000:
            db.executemany(insert, batch)
            batch = []
    if batch:
        db.executemany(insert, batch)
    if words is not None:
        db.executemany("INSERT INTO words VALUES (?,?,?)", words)
    keys = sorted(set(keys))
    db.executemany("INSERT INTO search VALUES (?,?)", keys)
    heavy = heavy_prefixes(keys, heavy_rows, suggestions)
    db.executemany("INSERT INTO prefix_top VALUES (?,?)",
                   sorted((p, ",".join(str(r) for r in ranks)) for p, ranks in heavy.items()))
    meta = dict(meta)
    meta.update({
        "schema_version": str(SCHEMA_VERSION),
        "kind": kind,
        "article_count": str(count),
        "key_count": str(len(keys)),
        "heavy_prefix_rows": str(heavy_rows),
        "heavy_prefix_count": str(len(heavy)),
        "suggestions": str(suggestions),
    })
    db.executemany("INSERT INTO meta VALUES (?,?)", sorted((k, str(v)) for k, v in meta.items()))
    db.commit()
    db.execute("VACUUM")
    db.close()
    return {"article_count": count, "key_count": len(keys), "heavy_prefix_count": len(heavy)}


# --------------------------------------------------------------------------
# Query recipes (mirrored verbatim in SCHEMA.md)
# --------------------------------------------------------------------------

SQL_PREFIX_TOP = "SELECT ranks FROM prefix_top WHERE prefix = ?1"
SQL_PREFIX_RANGE = ("SELECT DISTINCT rank FROM search WHERE key >= ?1 AND key < ?2 "
                    "ORDER BY rank LIMIT ?3")
SQL_EXACT = "SELECT rank FROM search WHERE key = ?1 ORDER BY rank LIMIT 1"


def open_pack(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


def read_meta(db):
    return dict(db.execute("SELECT key, value FROM meta").fetchall())


def suggest_ranks(db, query, limit=8, min_chars=2):
    """Ranks of the best `limit` entities with a key starting with fold(query)."""
    q = fold(query)
    if len(q) < min_chars:
        return []
    row = db.execute(SQL_PREFIX_TOP, (q,)).fetchone()
    if row is not None:
        return [int(r) for r in row[0].split(",")][:limit]
    return [r for (r,) in db.execute(SQL_PREFIX_RANGE, (q, q + PREFIX_SENTINEL, limit))]


def display_for(db, kind, ranks):
    table, column = ENTITY[kind]
    if not ranks:
        return []
    marks = ",".join("?" * len(ranks))
    rows = dict(db.execute("SELECT rank, %s FROM %s WHERE rank IN (%s)" % (column, table, marks),
                           ranks).fetchall())
    return [rows[r] for r in ranks if r in rows]


def suggest(db, kind, query, limit=8, min_chars=2):
    return display_for(db, kind, suggest_ranks(db, query, limit, min_chars))


def exact_rank(db, query):
    q = fold(query)
    if not q:
        return None
    row = db.execute(SQL_EXACT, (q,)).fetchone()
    return row[0] if row else None


def lookup_article(db, query):
    rank = exact_rank(db, query)
    if rank is None:
        return None
    return db.execute("SELECT title, abstract FROM articles WHERE rank = ?", (rank,)).fetchone()
