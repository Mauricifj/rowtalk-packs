"""On-disk staging of wiki candidates while shards stream.

A page id can arrive more than once (in two shards, or twice in one shard),
in an order that thread timing decides. Every arrival is a COPY of the page.

INVARIANT (I4 selection, deterministic for a given dump): the final
`ordered(R, n)` (n <= keep) equals what an unbounded build that ignores
arrival order would produce, where
  * a page's score under ranking R is the MAXIMUM over all its copies
    (`pop`, `links`; a missing value counts as 0, ties break on page_id);
  * a page's content (title, abstract, aliases) is the copy that is best
    under R: highest own R-score, then lowest shard, then earliest in that
    shard (`seq`).
Neither rule depends on arrival order or on when prunes happen.

WHY PRUNING CANNOT BREAK IT (bounded staging): a page's staged scores only
ever RISE (max), so a prune that evicts page P had `keep` other pages
scoring above P's current max under BOTH rankings, and those pages can only
rise. A copy that arrived before P's last eviction therefore scored at most
that max: it can be neither what puts P in the final top-`keep` nor P's
best copy under R when P makes the cut. So when P makes the final cut under
R, every copy holding P's top R-score arrived after the last eviction and
is still staged. (The previous rule, "lowest shard wins", was not monotone:
a later lower-shard copy could LOWER a score that had already justified
evicting other pages — DC-05/TH-05.)

STORAGE: `staged` holds one row per page: the max scores that the prune and
the ranking indexes use, plus ONE copy's content and that copy's own
scores. Further copies of a staged page go to `extra` (empty in practice;
counted as `duplicates` and reported). Who clears `extra`: every prune
deletes the extra rows whose page left `staged`, and `discard_shard`
deletes the shard's copies.

A shard whose decompression fails is discarded (`discard_shard`: its copies
and counters) before it is read again, so a re-read shard is never counted
twice. Max scores already raised by the discarded copies are KEPT: the
re-read delivers the same documents (a corrupt copy yields a prefix of the
real shard), so it restores exactly those copies, and keeping the max keeps
every score monotone in the meantime.
THE PREMISE, and its evidence (TH3-05): CPython's bz2 decodes a whole block
before emitting any of it and checks the block CRC as that output ends,
inside the same decompress() call, which then raises and returns nothing —
so no byte of a corrupt block reaches the parser (fixer-3 probe: 0 of 200
random bit flips in a 13 MB multi-block stream emitted a corrupted byte).
What it does NOT cover: a remote file that CHANGED between two downloads
(a re-published dump — they are immutable once `_SUCCESS` exists). There,
a max raised by the discarded version survives and the selection can
differ from a clean build's; the rows and counters are still exact.
"""

import json
import os
import sqlite3

POP_ORDER = "coalesce(pop, 0) DESC, page_id"
LINKS_ORDER = "coalesce(links, 0) DESC, page_id"
COPY_COLUMNS = "page_id, shard, seq, title, abstract, own_pop, own_links, aliases"
_IN_CHUNK = 900  # below SQLite's oldest default limit of 999 bound parameters
# max() that ignores NULL on either side (SQLite's max(x, NULL) is NULL).
_RAISE_SCORES = ("UPDATE staged SET "
                 "pop = CASE WHEN ?1 IS NULL THEN pop WHEN pop IS NULL THEN ?1 "
                 "ELSE max(pop, ?1) END, "
                 "links = CASE WHEN ?2 IS NULL THEN links WHEN links IS NULL THEN ?2 "
                 "ELSE max(links, ?2) END "
                 "WHERE page_id = ?3")


class Staging:
    def __init__(self, path, keep, prune_every=200000, batch=5000):
        if os.path.exists(path):
            os.remove(path)
        self.path = path
        self.keep = keep
        self.prune_every = prune_every
        self.batch = batch
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            PRAGMA temp_store = FILE;
            CREATE TABLE staged(
              page_id   INTEGER PRIMARY KEY,
              shard     INTEGER NOT NULL,   -- the stored copy
              seq       INTEGER NOT NULL,
              title     TEXT NOT NULL,
              abstract  TEXT NOT NULL,
              own_pop   REAL,
              own_links INTEGER,
              aliases   TEXT NOT NULL,
              pop       REAL,               -- max over every staged copy
              links     INTEGER
            );
            CREATE INDEX staged_pop ON staged(coalesce(pop, 0) DESC, page_id);
            CREATE INDEX staged_links ON staged(coalesce(links, 0) DESC, page_id);
            CREATE INDEX staged_shard ON staged(shard);
            CREATE TABLE extra(
              page_id   INTEGER NOT NULL,
              shard     INTEGER NOT NULL,
              seq       INTEGER NOT NULL,
              title     TEXT NOT NULL,
              abstract  TEXT NOT NULL,
              own_pop   REAL,
              own_links INTEGER,
              aliases   TEXT NOT NULL,
              PRIMARY KEY (page_id, shard, seq)
            ) WITHOUT ROWID;
            CREATE INDEX extra_shard ON extra(shard);
        """)
        self._pending = []
        self._since_prune = 0
        self._seq = {}
        # Per-shard counters so a restarted shard can be un-counted.
        self.shard_counts = {}

    def _counts(self, shard):
        return self.shard_counts.setdefault(shard, {"candidates": 0, "with_popularity": 0,
                                                    "with_links": 0, "duplicates": 0})

    def add(self, shard, candidate):
        """Stage one copy. Copies of one shard must be added in that shard's
        order (`seq` is the position within the shard)."""
        counts = self._counts(shard)
        counts["candidates"] += 1
        if candidate.popularity is not None:
            counts["with_popularity"] += 1
        if candidate.incoming_links is not None:
            counts["with_links"] += 1
        seq = self._seq.get(shard, 0) + 1
        self._seq[shard] = seq
        self._pending.append((candidate.page_id, shard, seq, candidate.title, candidate.abstract,
                              candidate.popularity, candidate.incoming_links,
                              json.dumps(candidate.aliases, ensure_ascii=False)))
        if len(self._pending) >= self.batch:
            self.flush()

    def _staged_ids(self, ids):
        found = set()
        ids = list(ids)
        for start in range(0, len(ids), _IN_CHUNK):
            chunk = ids[start:start + _IN_CHUNK]
            found.update(pid for (pid,) in self.db.execute(
                "SELECT page_id FROM staged WHERE page_id IN (%s)" % ",".join("?" * len(chunk)),
                chunk))
        return found

    def flush(self):
        if not self._pending:
            return
        rows, self._pending = self._pending, []
        staged = self._staged_ids({row[0] for row in rows})
        fresh, copies = [], []
        for row in rows:
            if row[0] in staged:
                copies.append(row)
                self._counts(row[1])["duplicates"] += 1
            else:
                staged.add(row[0])
                fresh.append(row)
        self.db.executemany("INSERT INTO staged (%s, pop, links) VALUES (?,?,?,?,?,?,?,?,?,?)"
                            % COPY_COLUMNS, [row + (row[5], row[6]) for row in fresh])
        if copies:
            self.db.executemany("INSERT OR IGNORE INTO extra (%s) VALUES (?,?,?,?,?,?,?,?)"
                                % COPY_COLUMNS, copies)
            self.db.executemany(_RAISE_SCORES, [(row[5], row[6], row[0]) for row in copies])
        self.db.commit()
        self._since_prune += len(rows)
        if self._since_prune >= self.prune_every:
            self.prune()

    def prune(self):
        self._since_prune = 0
        self.db.execute(
            "DELETE FROM staged WHERE page_id NOT IN "
            "(SELECT page_id FROM staged ORDER BY %s LIMIT ?) AND page_id NOT IN "
            "(SELECT page_id FROM staged ORDER BY %s LIMIT ?)" % (POP_ORDER, LINKS_ORDER),
            (self.keep, self.keep))
        self.db.execute("DELETE FROM extra WHERE page_id NOT IN (SELECT page_id FROM staged)")
        self.db.commit()

    def discard_shard(self, shard):
        """Forget every copy staged from `shard` (its copy was corrupt). A
        page whose stored copy came from `shard` keeps its row if another
        copy of it is staged (the earliest such copy is promoted into the
        row); otherwise the row goes."""
        self._pending = [row for row in self._pending if row[1] != shard]
        self.db.execute("DELETE FROM extra WHERE shard = ?", (shard,))
        candidates = self.db.execute(
            "SELECT %s FROM extra WHERE page_id IN (SELECT page_id FROM staged WHERE shard = ?) "
            "ORDER BY page_id, shard, seq" % COPY_COLUMNS, (shard,)).fetchall()
        promote = {}
        for row in candidates:
            promote.setdefault(row[0], row)
        for row in promote.values():
            self.db.execute("UPDATE staged SET shard = ?, seq = ?, title = ?, abstract = ?, "
                            "own_pop = ?, own_links = ?, aliases = ? WHERE page_id = ?",
                            row[1:] + (row[0],))
            self.db.execute("DELETE FROM extra WHERE page_id = ? AND shard = ? AND seq = ?",
                            row[:3])
        self.db.execute("DELETE FROM staged WHERE shard = ?", (shard,))
        self.db.commit()
        self.shard_counts.pop(shard, None)
        self._seq.pop(shard, None)

    def totals(self):
        totals = {"candidates": 0, "with_popularity": 0, "with_links": 0, "duplicates": 0}
        for counts in self.shard_counts.values():
            for key in totals:
                totals[key] += counts[key]
        return totals

    def ranking(self, min_presence):
        """'popularity_score' when at least `min_presence` of the candidates
        carry it, else the documented fallback 'incoming_links'."""
        totals = self.totals()
        if totals["candidates"] and totals["with_popularity"] / totals["candidates"] >= min_presence:
            return "popularity_score"
        return "incoming_links"

    def ordered(self, ranking, limit):
        """Yield (rank, page_id, title, abstract, aliases list) best first."""
        self.flush()
        popularity = ranking == "popularity_score"
        order = POP_ORDER if popularity else LINKS_ORDER
        extras = {}
        for row in self.db.execute("SELECT %s FROM extra" % COPY_COLUMNS):
            extras.setdefault(row[0], []).append(row)
        own = 5 if popularity else 6

        def best_first(copy):
            return (-(copy[own] or 0), copy[1], copy[2])

        cursor = self.db.execute("SELECT %s FROM staged ORDER BY %s LIMIT ?"
                                 % (COPY_COLUMNS, order), (limit,))
        for rank, row in enumerate(cursor, start=1):
            if row[0] in extras:
                row = min([row] + extras[row[0]], key=best_first)
            yield rank, row[0], row[3], row[4], json.loads(row[7])

    def close(self, delete=True):
        self.db.close()
        if delete and os.path.exists(self.path):
            os.remove(self.path)
