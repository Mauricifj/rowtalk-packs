import os
import random
import shutil
import tempfile
import unittest

from rowtalk_packs import packdb
from rowtalk_packs.cirrus import Candidate
from rowtalk_packs.fold import PREFIX_SENTINEL, fold
from rowtalk_packs.staging import Staging


DISCARD = object()  # run_order marker: discard that shard (a corrupt copy)


JUNK_BASE = 10_000  # page ids no real shard of the property test uses


def cand(pid, pop=None, links=None, title=None):
    return Candidate(pid, title or "T%d" % pid, "Abstract %d." % pid, pop, links, [])


class StagingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_popularity_ties_break_by_page_id(self):
        st = Staging(os.path.join(self.dir, "s.db"), keep=10)
        for pid in (30, 10, 20):
            st.add(0, cand(pid, pop=0.5))
        st.add(0, cand(5, pop=0.9))
        order = [row[1] for row in st.ordered("popularity_score", 10)]
        self.assertEqual(order, [5, 10, 20, 30])
        st.close()

    def test_order_expressions_break_ties_by_page_id_whatever_the_plan(self):
        # TH-09: the staged table's rowid IS the page id, so any plan over it
        # yields ties in page-id order and cannot tell whether POP_ORDER
        # itself breaks ties. Evaluate the SAME expressions over rows whose
        # physical order disagrees with page id.
        import sqlite3
        from rowtalk_packs import staging as staging_mod
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE t(n INTEGER PRIMARY KEY, page_id INTEGER, pop REAL, links INTEGER)")
        pids = [40, 7, 33, 2, 19, 25, 11]
        db.executemany("INSERT INTO t VALUES (?,?,?,?)",
                       [(n, pid, 0.5, 3) for n, pid in enumerate(pids)])
        for order in (staging_mod.POP_ORDER, staging_mod.LINKS_ORDER):
            got = [pid for (pid,) in db.execute("SELECT page_id FROM t ORDER BY %s" % order)]
            self.assertEqual(got, sorted(pids), order)
        db.close()

    def test_fallback_ranking_when_popularity_absent(self):
        st = Staging(os.path.join(self.dir, "s.db"), keep=10)
        st.add(0, cand(1, links=5))
        st.add(0, cand(2, links=50))
        st.add(0, cand(3, pop=0.1, links=1))
        self.assertEqual(st.ranking(0.5), "incoming_links")
        self.assertEqual([r[1] for r in st.ordered("incoming_links", 10)], [2, 1, 3])
        st.add(0, cand(4, pop=0.2))
        self.assertEqual(st.ranking(0.5), "popularity_score")
        st.close()

    def test_prune_never_changes_the_final_top_k(self):
        rng = random.Random(3)
        rows = [cand(pid, pop=rng.choice([None, rng.random(), 0.5]),
                     links=rng.choice([None, rng.randrange(100)])) for pid in rng.sample(range(1, 10000), 2000)]
        keep = 100
        st = Staging(os.path.join(self.dir, "s.db"), keep=keep, prune_every=37, batch=11)
        for row in rows:
            st.add(row.page_id % 3, row)
        for ranking, key in (("popularity_score", lambda c: (-(c.popularity or 0), c.page_id)),
                             ("incoming_links", lambda c: (-(c.incoming_links or 0), c.page_id))):
            expected = [c.page_id for c in sorted(rows, key=key)[:keep]]
            self.assertEqual([r[1] for r in st.ordered(ranking, keep)], expected, ranking)
        st.close()

    def test_discard_shard_forgets_rows_and_counts(self):
        st = Staging(os.path.join(self.dir, "s.db"), keep=10)
        st.add(0, cand(1, pop=0.1))
        st.add(1, cand(2, pop=0.2))
        st.discard_shard(1)
        st.add(1, cand(2, pop=0.2))
        self.assertEqual(st.totals()["candidates"], 2)
        self.assertEqual(sorted(r[1] for r in st.ordered("popularity_score", 10)), [1, 2])
        st.close()

    def test_duplicate_page_id_takes_the_max_score_and_the_best_copy(self):
        st = Staging(os.path.join(self.dir, "s.db"), keep=10, batch=1)
        st.add(3, cand(1, pop=0.9, links=1, title="From shard 3"))
        st.add(1, cand(1, pop=0.1, links=50, title="From shard 1"))
        st.add(2, cand(1, pop=0.9, links=None, title="From shard 2"))  # ties shard 3 on pop
        st.add(1, cand(1, pop=0.7, links=50, title="Later in shard 1"))
        st.add(0, cand(2, pop=0.8, links=40, title="Other page"))
        # pop: max 0.9 beats 0.8; content = best own pop (0.9), lowest shard (2).
        self.assertEqual([(r[1], r[2]) for r in st.ordered("popularity_score", 10)],
                         [(1, "From shard 2"), (2, "Other page")])
        # links: max 50; content = best own links (50), lowest shard (1), first in shard.
        self.assertEqual([(r[1], r[2]) for r in st.ordered("incoming_links", 10)],
                         [(1, "From shard 1"), (2, "Other page")])
        self.assertEqual(st.totals()["duplicates"], 3)
        st.close()

    def run_order(self, arrivals, keep, ranking="popularity_score", prune_every=1, batch=1,
                  totals=None, valid_copies=None):
        """arrivals: (shard, candidate) to stage, or (shard, DISCARD) to
        discard that shard as stage_shards does on a 'corrupt' message.
        `totals` (a dict) receives st.totals() after the last arrival."""
        st = Staging(os.path.join(self.dir, "o.db"), keep=keep, prune_every=prune_every,
                     batch=batch)
        for shard, candidate in arrivals:
            if candidate is DISCARD:
                st.discard_shard(shard)
            else:
                st.add(shard, candidate)
        out = [(r[1], r[2]) for r in st.ordered(ranking, keep)]
        if totals is not None:
            totals.update(st.totals())
        if valid_copies is not None:
            # TH3-05: no staged page holds a copy the replay did not deliver
            # (a discard that kept rows doubles them), and no junk survives.
            held = {}
            for (pid,) in st.db.execute("SELECT page_id FROM staged UNION ALL "
                                        "SELECT page_id FROM extra"):
                held[pid] = held.get(pid, 0) + 1
            for pid, count in held.items():
                self.assertLessEqual(count, valid_copies.get(pid, 0), pid)
        # The staging docstring's "who clears extra": no copy outlives its page.
        self.assertEqual(st.db.execute("SELECT count(*) FROM extra WHERE page_id NOT IN "
                                       "(SELECT page_id FROM staged)").fetchone()[0], 0)
        st.close()
        return out

    def test_reported_orders_agree(self):
        # DC-05: page 5 in shards 0 (pop 0) and 1 (pop 100), page 9 (pop 50).
        a, b, c = ((0, cand(5, pop=0.0, title="5 from shard 0")),
                   (1, cand(5, pop=100.0, title="5 from shard 1")),
                   (2, cand(9, pop=50.0, title="9")))
        self.assertEqual(self.run_order([b, c, a], 1), self.run_order([a, c, b], 1))
        self.assertEqual(self.run_order([a, c, b], 1), [(5, "5 from shard 1")])
        # TH-05: page 7 in shards 0 (.10) and 2 (.90).
        a, b, c, e = ((0, cand(7, pop=.10, title="T7-s0")), (1, cand(1, pop=.50, title="T1")),
                      (1, cand(2, pop=.40, title="T2")), (2, cand(7, pop=.90, title="T7-s2")))
        self.assertEqual(self.run_order([a, b, c, e], 2), self.run_order([e, b, c, a], 2))
        self.assertEqual(self.run_order([e, b, c, a], 2), [(7, "T7-s2"), (1, "T1")])

    def test_any_arrival_order_and_prune_schedule_gives_the_oracle(self):
        # DC-05/TH-05 property: interleave shards at random (each shard in its
        # own order, as the pipeline does), prune often, compare to the
        # order-free definition in the staging module docstring.
        exercised = 0
        for seed in range(60):
            rng = random.Random(seed)
            shards = {s: [] for s in range(4)}
            for pid in rng.sample(range(1, 60), 30):
                for _ in range(rng.choice([1, 1, 1, 2, 3])):
                    shard = rng.randrange(4)
                    shards[shard].append(cand(pid, pop=rng.choice([None, 0.0, 0.3, 0.3, rng.random()]),
                                              links=rng.choice([None, 1, 5, rng.randrange(9)]),
                                              title="P%d s%d #%d" % (pid, shard, len(shards[shard]))))
            copies = [(s, n, c) for s, lst in shards.items() for n, c in enumerate(lst)]
            keep = rng.choice([3, 5, 10])
            for ranking, own in (("popularity_score", "popularity"), ("incoming_links", "incoming_links")):
                best = {}
                for s, n, c in copies:
                    score = getattr(c, own) or 0
                    entry = best.setdefault(c.page_id, [score, (-score, s, n), c])
                    entry[0] = max(entry[0], score)
                    if (-score, s, n) < entry[1]:
                        entry[1], entry[2] = (-score, s, n), c
                expected = [(pid, e[2].title) for pid, e in
                            sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))][:keep]
                for trial in range(4):
                    # TH2-02: trials 2 and 3 also discard shards mid-read (a
                    # corrupt copy delivered a PREFIX, possibly all of it) and
                    # replay them from the start — exactly what stage_shards
                    # does on 'corrupt', up to max_full_restarts_per_shard.
                    # TH3-05: the DISCARD must be observable. A corrupt copy
                    # yields a GENUINE prefix (staging docstring: no byte of a
                    # corrupt bz2 block escapes the decompressor), so an exact
                    # replay alone cannot tell a no-op discard from a real
                    # one. Three things can: (1) the totals — every valid copy
                    # counted once; (2) no page holds more staged copies than
                    # it has valid ones; (3) just before each DISCARD the shard
                    # stages a JUNK page (an id no real shard has, ranked
                    # BELOW every real page, so it can never evict one) that
                    # must be gone afterwards. Junk that OUTRANKS real pages
                    # is deliberately not generated: it would contradict the
                    # genuine-prefix premise the staging design rests on.
                    discards = {s: (2 if trial >= 2 else 0) for s in shards}
                    position = {s: 0 for s in shards}
                    arrivals = []

                    def discard(s):
                        discards[s] -= 1
                        arrivals.append((s, cand(JUNK_BASE + s, pop=-1.0, links=-1,
                                                 title="JUNK s%d" % s)))
                        arrivals.append((s, DISCARD))
                        position[s] = 0
                    while any(position[s] < len(lst) for s, lst in shards.items()):
                        s = rng.choice([s for s, lst in shards.items() if position[s] < len(lst)])
                        if position[s] and discards[s] and rng.random() < 0.25:
                            discard(s)
                            continue
                        arrivals.append((s, shards[s][position[s]]))
                        position[s] += 1
                        if (position[s] == len(shards[s]) and discards[s]
                                and rng.random() < 0.3):
                            discard(s)  # corrupt detected at EOF: all staged
                    totals = {}
                    valid = {}
                    for _, _, c in copies:
                        valid[c.page_id] = valid.get(c.page_id, 0) + 1
                    got = self.run_order(arrivals, keep, ranking, prune_every=rng.choice([1, 2, 7]),
                                         batch=rng.choice([1, 3, 50]), totals=totals,
                                         valid_copies=valid)
                    self.assertEqual(got, expected, (seed, ranking, trial))
                    # Every valid copy counted once; no discarded copy counted.
                    self.assertEqual(totals["candidates"], len(copies), (seed, ranking, trial))
                    if trial >= 2:
                        exercised += sum(1 for _, c in arrivals if c is DISCARD)
        # Not vacuous: the discard path really ran, many times.
        self.assertGreater(exercised, 200)

    def test_discard_keeps_a_max_raised_by_another_shard(self):
        # TH2-02 named regression: P's stored copy and a duplicate are in
        # shard 2, a better copy in shard 3; shard 2 is discarded and re-read.
        # The shard-3 copy (and the 0.9 max it raised) must survive.
        st = Staging(os.path.join(self.dir, "s.db"), keep=10, batch=1)
        st.add(2, cand(7, pop=0.2, title="P s2 a"))
        st.add(2, cand(7, pop=0.2, title="P s2 b"))
        st.add(3, cand(7, pop=0.9, title="P s3"))
        st.add(0, cand(8, pop=0.5, title="Q"))
        st.add(2, cand(9, pop=5.0, title="JUNK s2"))  # TH3-05: the corrupt prefix's junk
        st.discard_shard(2)
        st.add(2, cand(7, pop=0.2, title="P s2 a"))
        st.add(2, cand(7, pop=0.2, title="P s2 b"))
        self.assertEqual([(r[1], r[2]) for r in st.ordered("popularity_score", 10)],
                         [(7, "P s3"), (8, "Q")])
        self.assertEqual(st.totals()["candidates"], 4)
        st.close()

    def test_discard_shard_keeps_pages_other_shards_hold(self):
        st = Staging(os.path.join(self.dir, "s.db"), keep=10, batch=1)
        st.add(1, cand(1, pop=0.5, title="1 from shard 1"))
        st.add(2, cand(1, pop=0.4, title="1 from shard 2"))
        st.add(1, cand(3, pop=0.3, title="3 only in shard 1"))
        st.discard_shard(1)
        self.assertEqual([(r[1], r[2]) for r in st.ordered("popularity_score", 10)],
                         [(1, "1 from shard 2")])
        self.assertEqual(st.totals(), {"candidates": 1, "with_popularity": 1, "with_links": 0,
                                       "duplicates": 1})
        st.add(1, cand(1, pop=0.5, title="1 from shard 1"))  # the re-read
        st.add(1, cand(3, pop=0.3, title="3 only in shard 1"))
        self.assertEqual([(r[1], r[2]) for r in st.ordered("popularity_score", 10)],
                         [(1, "1 from shard 1"), (3, "3 only in shard 1")])
        st.close()


def brute_suggest(entries, query, limit=8):
    q = fold(query)
    ranks = sorted({rank for key, rank in entries if key.startswith(q)})
    return ranks[:limit]


def brute_heavy(rows, threshold, limit):
    counts = {}
    tops = {}
    for key, rank in rows:
        for length in range(2, len(key) + 1):
            prefix = key[:length]
            counts[prefix] = counts.get(prefix, 0) + 1
            tops.setdefault(prefix, set()).add(rank)
    return {p: sorted(tops[p])[:limit] for p, n in counts.items()
            if n > threshold and not p.endswith(" ")}


class HeavyPrefixTest(unittest.TestCase):
    def test_matches_brute_force(self):
        rng = random.Random(11)
        alphabet = "ab c"
        rows = set()
        for rank in range(1, 400):
            for _ in range(rng.randint(1, 3)):
                key = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 7))).strip()
                if key:
                    rows.add((key, rank))
        rows = sorted(rows)
        for threshold in (0, 3, 10, 50):
            self.assertEqual(packdb.heavy_prefixes(rows, threshold, 8),
                             brute_heavy(rows, threshold, 8), threshold)

    def test_non_ascii_and_astral(self):
        rows = sorted({("ça va", 3), ("çb", 1), ("ça", 2), ("😀a", 4), ("😀b", 5), ("😀", 6)})
        self.assertEqual(packdb.heavy_prefixes(rows, 1, 8), brute_heavy(rows, 1, 8))

    def test_empty(self):
        self.assertEqual(packdb.heavy_prefixes([], 0, 8), {})


class PackQueryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        rng = random.Random(5)
        words = ["rio", "rio de janeiro", "rio grande", "riviera", "rome", "roma", "são paulo",
                 "sao tome", "brasil", "brazil", "bra", "zz", "x"]
        self.entities = []
        self.rows = []
        for rank in range(1, 301):
            title = "%s %d" % (rng.choice(words), rank) if rank > 5 else words[rank - 1].title()
            aliases = [fold("%s alias" % rng.choice(words))] if rank % 3 == 0 else []
            keys = [fold(title)] + aliases
            self.entities.append((rank, title, keys, (10_000 + rank, "Abstract of %s." % title)))
            for key in keys:
                self.rows.append((key, rank))
        # TH-02: astral and high-BMP continuations of "ab", so a BMP range
        # sentinel (U+FFFF) would visibly miss keys.
        for rank, title in enumerate(["Ab\U0001F600", "Ab\U00020000 x", "Ab\u4e2d", "Ab\ufffd",
                                      "Ab\uffef", "Abc"], start=301):
            self.entities.append((rank, title, [fold(title)], (10_000 + rank, "A of %s." % title)))
            self.rows.append((fold(title), rank))
        self.path = os.path.join(self.dir, "p.db")
        meta = {"pack_id": "wiki-xx-essential", "language": "xx", "tier": "essential",
                "edition": "2026.10.25", "version": "20261025", "fold_version": 1,
                "ranking": "popularity_score", "min_query_chars": 2, "license": "L",
                "license_url": "U", "attribution": "A", "source": "S", "source_url": "U",
                "source_date": "20261025", "built_at": "now", "abstract_max_chars": 600}
        packdb.write_pack(self.path, "wiki", meta, self.entities, heavy_rows=7, suggestions=8)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_suggestions_equal_brute_force(self):
        db = packdb.open_pack(self.path)
        queries = ["ri", "rio", "Rio de", "RIO D", "ro", "rom", "sã", "sa", "SAO", "br", "bra",
                   "braz", "zz", "x", "nothing", "rio de janeiro 1", "alias", "rio alias",
                   "ab", "AB", "ab\U0001F600", "ab\ufffd"]
        rng = random.Random(9)
        queries += [k[:rng.randint(1, len(k))] for k, _ in rng.sample(self.rows, 200)]
        for query in queries:
            expected = brute_suggest(self.rows, query) if len(fold(query)) >= 2 else []
            self.assertEqual(packdb.suggest_ranks(db, query), expected, query)
        db.close()

    def test_both_paths_are_exercised(self):
        # The equality test above is only meaningful if it hit BOTH the
        # prefix_top path and the bounded range path with real results.
        db = packdb.open_pack(self.path)
        heavy = {p for (p,) in db.execute("SELECT prefix FROM prefix_top")}
        light_hits = [k[:n] for k, _ in self.rows for n in range(2, len(k) + 1)
                      if k[:n] not in heavy and not k[:n].endswith(" ")]
        self.assertIn("ri", heavy)
        self.assertTrue(light_hits)
        self.assertTrue(packdb.suggest_ranks(db, light_hits[0]))
        db.close()

    def test_exact_lookup_prefers_best_rank(self):
        db = packdb.open_pack(self.path)
        self.assertEqual(packdb.lookup_article(db, "rio")[0], "Rio")
        self.assertEqual(packdb.lookup_article(db, "  RIO  ")[0], "Rio")
        self.assertIsNone(packdb.lookup_article(db, "nope"))
        self.assertIsNone(packdb.lookup_article(db, "!!!"))
        db.close()

    def test_range_bound_is_exact(self):
        db = packdb.open_pack(self.path)
        for q in ("rio", "ab"):
            got = {k for (k,) in db.execute("SELECT key FROM search WHERE key >= ? AND key < ?",
                                            (q, q + PREFIX_SENTINEL))}
            self.assertEqual(got, {k for k, _ in self.rows if k.startswith(q)}, q)
        self.assertEqual(len(got), 6)  # every non-ASCII continuation of "ab" is in range
        db.close()

    def test_entities_must_be_ranked_in_order(self):
        with self.assertRaises(ValueError):
            packdb.write_pack(os.path.join(self.dir, "bad.db"), "wiki", {},
                              [(2, "x", ["x"], (1, "a"))])


if __name__ == "__main__":
    unittest.main()
