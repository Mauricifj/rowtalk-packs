"""Pack validation: schema + meta + internal consistency.

A pack that fails validation is never published, not even with force=true
(force overrides the SANITY gate — golden queries and size drift — never the
structural checks that make a pack openable and correct).
"""

import random
import sqlite3

from . import packdb
from .abstract import _CONTROL
from .fold import FOLD_VERSION, PREFIX_SENTINEL, fold

REQUIRED_META = {
    "wiki": ["schema_version", "fold_version", "kind", "pack_id", "language", "tier", "edition",
             "version", "article_count", "key_count", "ranking", "heavy_prefix_rows",
             "heavy_prefix_count", "suggestions", "min_query_chars", "license", "license_url",
             "attribution", "source", "source_url", "source_date", "built_at", "abstract_max_chars"],
    "dict": ["schema_version", "fold_version", "kind", "pack_id", "language", "tier", "edition",
             "version", "article_count", "key_count", "ranking", "heavy_prefix_rows",
             "heavy_prefix_count", "suggestions", "min_query_chars", "license", "license_url",
             "attribution", "source", "source_url", "source_version", "built_at"],
}


class ValidationError(Exception):
    pass


def _check(condition, message, errors):
    if not condition:
        errors.append(message)


def validate_pack(path, kind, expect=None, sample=300, seed=7):
    """Return (meta, errors). `expect` optionally pins meta values."""
    errors = []
    try:
        db = packdb.open_pack(path)
    except Exception as error:  # noqa: BLE001
        return {}, ["cannot open: %s" % error]
    try:
        quick = db.execute("PRAGMA quick_check").fetchone()[0]
        _check(quick == "ok", "quick_check: %s" % quick, errors)
        tables = {name for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        _check(tables == set(packdb.TABLES[kind]),
               "tables %s != %s" % (sorted(tables), sorted(packdb.TABLES[kind])), errors)
        for table in packdb.TABLES[kind]:
            if table not in tables:
                continue
            info = list(db.execute("PRAGMA table_info(%s)" % table))
            columns = [row[1] for row in info]
            _check(columns == packdb.EXPECTED_COLUMNS[table],
                   "%s columns %s" % (table, columns), errors)
            pk_columns = [row[1] for row in sorted((r for r in info if r[5]), key=lambda r: r[5])]
            want_pk, want_without_rowid = packdb.PRIMARY_KEYS[table]
            _check(pk_columns == want_pk, "%s primary key %s != %s" % (table, pk_columns, want_pk),
                   errors)
            sql = db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                             (table,)).fetchone()[0] or ""
            _check(("WITHOUT ROWID" in " ".join(sql.upper().split())) == want_without_rowid,
                   "%s WITHOUT ROWID must be %s" % (table, want_without_rowid), errors)
        # Key order is BINARY = memcmp of the stored encoding; code point order
        # (what the prefix range relies on) only for UTF-8 (UTF-16 orders
        # supplementary characters below U+E000..U+FFFF).
        encoding = db.execute("PRAGMA encoding").fetchone()[0]
        _check(encoding == "UTF-8", "encoding %s != UTF-8" % encoding, errors)
        if errors:
            return {}, errors
        meta = packdb.read_meta(db)
        for key in REQUIRED_META[kind]:
            _check(key in meta and meta[key] != "", "meta %s missing" % key, errors)
        _check(meta.get("schema_version") == str(packdb.SCHEMA_VERSION), "schema_version", errors)
        _check(meta.get("fold_version") == str(FOLD_VERSION), "fold_version", errors)
        _check(meta.get("kind") == kind, "kind", errors)
        for key, value in (expect or {}).items():
            _check(meta.get(key) == str(value), "meta %s=%r expected %r"
                   % (key, meta.get(key), value), errors)
        if errors:
            return meta, errors
        table, column = packdb.ENTITY[kind]
        count, low, high = db.execute("SELECT COUNT(*), MIN(rank), MAX(rank) FROM %s" % table).fetchone()
        _check(count == int(meta["article_count"]), "article_count %d != meta" % count, errors)
        _check(count == 0 or (low == 1 and high == count), "ranks not contiguous", errors)
        _check(count > 0, "empty pack", errors)
        empty = db.execute("SELECT COUNT(*) FROM %s WHERE %s = ''" % (table, column)).fetchone()[0]
        _check(empty == 0, "%d empty %s" % (empty, column), errors)
        if kind == "wiki":
            distinct = db.execute("SELECT COUNT(DISTINCT page_id) FROM articles").fetchone()[0]
            _check(distinct == count, "duplicate page_id", errors)
            # Measured in Python (code points, exact): SQLite length() stops
            # at a NUL and would undercount.
            limit = int(meta["abstract_max_chars"])
            bad = sum(1 for (text,) in db.execute("SELECT abstract FROM articles")
                      if not text or len(text) > limit or _CONTROL.search(text))
            _check(bad == 0, "%d abstracts empty, longer than %d code points, or holding "
                             "control characters" % (bad, limit), errors)
            unreachable = db.execute("SELECT COUNT(*) FROM articles WHERE rank NOT IN "
                                     "(SELECT rank FROM search)").fetchone()[0]
            _check(unreachable == 0, "%d articles have no search key (unreachable)" % unreachable,
                   errors)
        else:
            orphan = db.execute("SELECT COUNT(*) FROM lemmas WHERE lemma NOT IN "
                                "(SELECT lemma FROM words)").fetchone()[0]
            _check(orphan == 0, "%d lemmas without words" % orphan, errors)
        keys = db.execute("SELECT COUNT(*) FROM search").fetchone()[0]
        _check(keys == int(meta["key_count"]), "key_count", errors)
        dangling = db.execute("SELECT COUNT(*) FROM search WHERE rank NOT IN "
                              "(SELECT rank FROM %s)" % table).fetchone()[0]
        _check(dangling == 0, "%d search rows point nowhere" % dangling, errors)
        if errors:
            return meta, errors  # the sampled checks below assume sound tables
        not_folded = 0
        for (key,) in db.execute("SELECT key FROM search"):
            if not key or fold(key) != key:
                not_folded += 1
        _check(not_folded == 0, "%d search keys are not fold_v1 fixed points" % not_folded, errors)
        heavy = int(meta["heavy_prefix_rows"])
        top_n = int(meta["suggestions"])
        rows = db.execute("SELECT prefix, ranks FROM prefix_top").fetchall()
        _check(len(rows) == int(meta["heavy_prefix_count"]), "heavy_prefix_count", errors)
        rng = random.Random(seed)
        for prefix, ranks in rng.sample(rows, min(sample, len(rows))):
            parsed = [int(r) for r in ranks.split(",")]
            expected = [r for (r,) in db.execute(packdb.SQL_PREFIX_RANGE,
                                                 (prefix, prefix + PREFIX_SENTINEL, top_n))]
            _check(parsed == expected, "prefix_top %r stale" % prefix, errors)
            _check(len(prefix) >= 2 and not prefix.endswith(" "), "prefix_top %r shape" % prefix,
                   errors)
        # Completeness: a sampled prefix matched by more than `heavy` rows must
        # have a prefix_top row (else the app's range scan is unbounded).
        # Keys are sampled through random entities (O(log n) each) rather
        # than OFFSET scans over the search table.
        sample_keys = []
        for _ in range(min(sample, count)):
            display = db.execute("SELECT %s FROM %s WHERE rank = ?" % (column, table),
                                 (rng.randint(1, count),)).fetchone()[0]
            if fold(display):
                sample_keys.append(fold(display))
        for key in sample_keys:
            for length in (2, 3, 4):
                prefix = key[:length].rstrip(" ")
                if len(prefix) < 2:
                    continue
                matched = db.execute("SELECT COUNT(*) FROM search WHERE key >= ? AND key < ?",
                                     (prefix, prefix + PREFIX_SENTINEL)).fetchone()[0]
                has_row = db.execute(packdb.SQL_PREFIX_TOP, (prefix,)).fetchone() is not None
                _check(has_row == (matched > heavy),
                       "prefix %r: %d rows, prefix_top row %s" % (prefix, matched, has_row), errors)
                if len(errors) > 20:
                    return meta, errors
        return meta, errors
    except sqlite3.Error as error:
        # Validation never raises: an unreadable pack is a failed pack.
        return {}, errors + ["sqlite: %s" % error]
    finally:
        db.close()


def run_golden(path, kind, golden, min_chars=2, limit=8):
    """Run golden queries. Returns list of {query, mode, expect, ok, got}."""
    results = []
    db = packdb.open_pack(path)
    try:
        table, column = packdb.ENTITY[kind]
        for item in golden:
            if item["mode"] == "exact":
                rank = packdb.exact_rank(db, item["query"])
                got = packdb.display_for(db, kind, [rank]) if rank is not None else []
            else:
                got = packdb.suggest(db, kind, item["query"], limit, min_chars)
            within = item.get("within", limit)
            results.append({"query": item["query"], "mode": item["mode"],
                            "expect": item["expect_title"], "within": within,
                            "ok": item["expect_title"] in got[:within], "got": got[:within]})
    finally:
        db.close()
    return results
