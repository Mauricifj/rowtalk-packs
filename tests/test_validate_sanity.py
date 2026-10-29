import os
import shutil
import sqlite3
import tempfile
import unittest

from rowtalk_packs import packdb, sanity, validate
from rowtalk_packs.fold import fold

META = {"pack_id": "wiki-xx-essential", "language": "xx", "tier": "essential",
        "edition": "2026.10.25", "version": "20261025", "fold_version": 1,
        "ranking": "popularity_score", "min_query_chars": 2, "license": "L", "license_url": "U",
        "attribution": "A", "source": "S", "source_url": "U", "source_date": "20261025",
        "built_at": "now", "abstract_max_chars": 600}


def entities(n=60):
    out = []
    for rank in range(1, n + 1):
        title = "Rio %d" % rank if rank % 2 else "Roma %d" % rank
        out.append((rank, title, [fold(title)], (rank * 10, "Abstract %d." % rank)))
    return out


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "p.db")
        packdb.write_pack(self.path, "wiki", META, entities(), heavy_rows=5)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def mutate(self, sql, params=()):
        db = sqlite3.connect(self.path)
        db.execute(sql, params)
        db.commit()
        db.close()

    def test_good_pack_passes(self):
        meta, errors = validate.validate_pack(self.path, "wiki", expect={"language": "xx"})
        self.assertEqual(errors, [])
        self.assertEqual(meta["article_count"], "60")

    def test_expect_mismatch(self):
        _, errors = validate.validate_pack(self.path, "wiki", expect={"language": "pt"})
        self.assertTrue(any("language" in e for e in errors))

    def test_missing_meta(self):
        self.mutate("DELETE FROM meta WHERE key = 'license'")
        _, errors = validate.validate_pack(self.path, "wiki")
        self.assertIn("meta license missing", errors)

    def test_wrong_kind_schema(self):
        _, errors = validate.validate_pack(self.path, "dict")
        self.assertTrue(errors)

    def test_stale_prefix_top(self):
        self.mutate("UPDATE prefix_top SET ranks = '60' WHERE prefix = 'ri'")
        _, errors = validate.validate_pack(self.path, "wiki", sample=10_000)
        self.assertTrue(any("stale" in e for e in errors), errors)

    def test_missing_heavy_row(self):
        self.mutate("DELETE FROM prefix_top WHERE prefix = 'ro'")
        self.mutate("UPDATE meta SET value = CAST(value AS INTEGER) - 1 WHERE key = 'heavy_prefix_count'")
        _, errors = validate.validate_pack(self.path, "wiki", sample=10_000)
        self.assertTrue(any("'ro'" in e for e in errors), errors)

    def test_non_folded_key(self):
        self.mutate("INSERT INTO search VALUES ('Rio X', 1)")
        self.mutate("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'key_count'")
        _, errors = validate.validate_pack(self.path, "wiki")
        self.assertTrue(any("fixed points" in e for e in errors), errors)

    def test_dangling_rank_and_gap(self):
        self.mutate("DELETE FROM articles WHERE rank = 3")
        _, errors = validate.validate_pack(self.path, "wiki")
        self.assertTrue(any("article_count" in e for e in errors))
        self.assertTrue(any("point nowhere" in e for e in errors))

    def test_abstract_too_long(self):
        self.mutate("UPDATE articles SET abstract = ? WHERE rank = 1", ("x" * 601,))
        _, errors = validate.validate_pack(self.path, "wiki")
        self.assertTrue(any("abstracts" in e for e in errors))

    def rebuild(self, script):
        """Rewrite the pack through `script` (sqlite3 executescript)."""
        db = sqlite3.connect(self.path)
        db.executescript(script)
        db.commit()
        db.close()

    def test_search_without_its_primary_key_fails(self):
        # DC-08: a rowid search table still answers queries, but the range
        # scan is no longer a bounded covering-index walk.
        self.rebuild("CREATE TABLE s2(key TEXT NOT NULL, rank INTEGER NOT NULL);"
                     "INSERT INTO s2 SELECT key, rank FROM search; DROP TABLE search;"
                     "ALTER TABLE s2 RENAME TO search;")
        _, errors = validate.validate_pack(self.path, "wiki")
        self.assertTrue(any("search primary key" in e for e in errors), errors)
        self.assertTrue(any("search WITHOUT ROWID" in e for e in errors), errors)

    def test_utf16_pack_fails(self):
        utf16 = os.path.join(self.dir, "u16.db")
        db = sqlite3.connect(utf16)
        db.execute("PRAGMA encoding = 'UTF-16le'")
        db.executescript(packdb.COMMON_SQL + packdb.WIKI_SQL)
        db.close()
        _, errors = validate.validate_pack(utf16, "wiki")
        self.assertTrue(any("encoding" in e for e in errors), errors)

    def test_article_without_a_search_key_fails(self):
        self.mutate("DELETE FROM search WHERE rank = 7")
        self.mutate("UPDATE meta SET value = CAST(value AS INTEGER) - 1 WHERE key = 'key_count'")
        _, errors = validate.validate_pack(self.path, "wiki")
        self.assertTrue(any("no search key" in e for e in errors), errors)

    def test_control_character_in_abstract_fails(self):
        self.mutate("UPDATE articles SET abstract = ? WHERE rank = 2", ("a\x00" + "b" * 700,))
        _, errors = validate.validate_pack(self.path, "wiki")
        self.assertTrue(any("abstracts" in e for e in errors), errors)
        self.mutate("UPDATE articles SET abstract = ? WHERE rank = 2", ("ok\x07",))
        _, errors = validate.validate_pack(self.path, "wiki")
        self.assertTrue(any("abstracts" in e for e in errors), errors)

    def test_not_sqlite(self):
        with open(self.path, "wb") as handle:
            handle.write(b"not a database at all" * 100)
        _, errors = validate.validate_pack(self.path, "wiki")
        self.assertTrue(errors)

    def test_golden(self):
        results = validate.run_golden(self.path, "wiki", [
            {"mode": "exact", "query": "rio 1", "expect_title": "Rio 1", "within": 1},
            {"mode": "prefix", "query": "rom", "expect_title": "Roma 2", "within": 8},
            {"mode": "prefix", "query": "rom", "expect_title": "Roma 60", "within": 8},
        ])
        self.assertEqual([r["ok"] for r in results], [True, True, False])


class SanityTest(unittest.TestCase):
    GOOD = [{"query": "q", "mode": "exact", "expect": "Q", "within": 1, "ok": True, "got": ["Q"]}]
    BAD = [{"query": "q", "mode": "exact", "expect": "Q", "within": 1, "ok": False, "got": []}]
    FIRST = {"min_articles": 100, "min_installed_bytes": 1000, "max_installed_bytes": 10 ** 6}

    def test_pass_with_previous(self):
        gate = sanity.evaluate(110, 1100, self.GOOD, [], {"article_count": 100,
                                                          "installed_bytes": 1000}, None)
        self.assertTrue(gate["passed"])
        self.assertFalse(gate["forced"])

    def test_drift_fails_and_force_overrides(self):
        previous = {"article_count": 100, "installed_bytes": 1000}
        gate = sanity.evaluate(116, 1000, self.GOOD, [], previous, None, 0.15)
        self.assertFalse(gate["passed"])
        gate = sanity.evaluate(84, 1000, self.GOOD, [], previous, None, 0.15)
        self.assertFalse(gate["passed"])
        gate = sanity.evaluate(115, 1150, self.GOOD, [], previous, None, 0.15)
        self.assertTrue(gate["passed"])
        gate = sanity.evaluate(200, 1000, self.GOOD, [], previous, None, 0.15, force=True)
        self.assertTrue(gate["passed"])
        self.assertTrue(gate["forced"])

    def test_installed_bytes_drift_alone_fails(self):
        # TH-04: a stable article count must not hide a size change.
        previous = {"article_count": 100, "installed_bytes": 1000}
        for size in (1151, 849, 2000, 500):
            gate = sanity.evaluate(100, size, self.GOOD, [], previous, None, 0.15)
            self.assertFalse(gate["passed"], size)
            self.assertEqual([c["name"] for c in gate["checks"] if not c["ok"]],
                             ["drift:installed_bytes"])
        self.assertTrue(sanity.evaluate(100, 1150, self.GOOD, [], previous, None, 0.15)["passed"])
        self.assertTrue(sanity.evaluate(100, 850, self.GOOD, [], previous, None, 0.15)["passed"])

    def test_previous_edition_is_the_newest_line_naming_the_pack(self):
        # TH-04: publish 100, publish 200, rollback to an edition of 150 —
        # what the app is served now (150) is the baseline, not the oldest.
        import tempfile as tf
        from rowtalk_packs import editions
        directory = tf.mkdtemp()
        try:
            for action, count in (("publish", 100), ("publish", 200), ("rollback", 150)):
                editions.append(directory, "wiki-pt", action, "t%d" % count, "e", {
                    "wiki-pt-essential": {"article_count": count, "installed_bytes": count * 10}})
            editions.append(directory, "wiki-pt", "publish", "t-other", "e", {
                "wiki-pt-extended": {"article_count": 7, "installed_bytes": 70}})
            self.assertEqual(editions.previous_stats(directory, "wiki-pt", "wiki-pt-essential"),
                             {"article_count": 150, "installed_bytes": 1500})
        finally:
            shutil.rmtree(directory)

    def test_golden_failure(self):
        gate = sanity.evaluate(100, 1000, self.BAD, [], None, self.FIRST)
        self.assertFalse(gate["passed"])
        self.assertTrue(sanity.evaluate(100, 1000, self.BAD, [], None, self.FIRST, force=True)["passed"])

    def test_validation_failure_is_never_overridable(self):
        gate = sanity.evaluate(100, 1000, self.GOOD, ["quick_check: corrupt"], None, self.FIRST,
                               force=True)
        self.assertFalse(gate["passed"])
        self.assertFalse(gate["forced"])

    def test_first_edition_floors(self):
        self.assertTrue(sanity.evaluate(100, 1000, self.GOOD, [], None, self.FIRST)["passed"])
        self.assertFalse(sanity.evaluate(99, 1000, self.GOOD, [], None, self.FIRST)["passed"])
        self.assertFalse(sanity.evaluate(100, 999, self.GOOD, [], None, self.FIRST)["passed"])
        self.assertFalse(sanity.evaluate(100, 10 ** 6 + 1, self.GOOD, [], None, self.FIRST)["passed"])


if __name__ == "__main__":
    unittest.main()
