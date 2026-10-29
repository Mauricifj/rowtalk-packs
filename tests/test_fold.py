import hashlib
import json
import os
import subprocess
import sys
import unicodedata
import unittest

from rowtalk_packs import fold as foldmod
from rowtalk_packs.fold import PREFIX_SENTINEL, fold

# fold_v1 is FROZEN: these pins fail on ANY edit of the table. A change to
# fold is a new fold_version (new file, new pin), never an edit of v1.
FOLD_TABLE_SHA256 = "17884aad20ea7bb68a9a2d2b7881d93e7fdee8b059e386eb21f3bb6bf8367e6f"


def _sha(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class FoldTableTest(unittest.TestCase):
    def setUp(self):
        with open(foldmod.TABLE_PATH, "r", encoding="utf-8") as handle:
            self.table = json.load(handle)

    def test_table_is_frozen(self):
        self.assertEqual(_sha(foldmod.TABLE_PATH), FOLD_TABLE_SHA256,
                         "fold_table.json changed: fold_v1 is frozen — mint fold_v2 instead")
        self.assertEqual(self.table["fold_version"], 1)
        self.assertEqual(self.table["entry_count"], len(self.table["entries"]))

    def test_values_are_fixed_points(self):
        entries = self.table["entries"]
        for key, value in entries.items():
            for ch in value:
                hexkey = "%04X" % ord(ch)
                self.assertTrue(hexkey not in entries or entries[hexkey] == ch,
                                "U+%s -> %r contains non-fixed U+%s" % (key, value, hexkey))

    def test_no_value_emits_the_prefix_sentinel(self):
        for value in self.table["entries"].values():
            self.assertNotIn(PREFIX_SENTINEL, value)
        self.assertEqual(fold("a" + PREFIX_SENTINEL), "a")

    def test_ascii_letters_are_in_the_table(self):
        # The algorithm has NO separate case rule: even ASCII lives in data.
        for cp in range(ord("A"), ord("Z") + 1):
            self.assertEqual(self.table["entries"]["%04X" % cp], chr(cp + 32))

    def test_generator_reproduces_table_on_same_unicode(self):
        if unicodedata.unidata_version != self.table["unicode_version"]:
            self.skipTest("table generated on Unicode %s, running %s (expected on CI: the table "
                          "is data, not recomputed)" % (self.table["unicode_version"],
                                                        unicodedata.unidata_version))
        result = subprocess.run([sys.executable, os.path.join(foldmod.REPO_ROOT, "tools",
                                                              "gen_fold_table.py"),
                                 "--out", foldmod.TABLE_PATH, "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class FoldVectorsTest(unittest.TestCase):
    def setUp(self):
        with open(foldmod.VECTORS_PATH, "r", encoding="utf-8") as handle:
            self.vectors = json.load(handle)

    def test_corpus_size_and_version(self):
        self.assertEqual(self.vectors["fold_version"], 1)
        self.assertGreaterEqual(len(self.vectors["cases"]), 200)

    def test_every_vector(self):
        failures = []
        for case in self.vectors["cases"]:
            got = fold(case["in"])
            if got != case["out"]:
                failures.append((case["note"], case["in"], case["out"], got))
        self.assertEqual(failures, [])

    def test_idempotent_on_corpus(self):
        for case in self.vectors["cases"]:
            self.assertEqual(fold(case["out"]), case["out"], case["note"])
            self.assertEqual(fold(fold(case["in"])), fold(case["in"]))

    def test_corpus_covers_required_families(self):
        notes = " ".join(c["note"] for c in self.vectors["cases"]).lower()
        for family in ("pt accents", "sharp s", "dotted capital i", "dotless i", "ligature",
                       "fullwidth", "apostrophe", "hyphen", "dash", "paren", "acronym",
                       "digits", "emoji", "kanji", "hangul", "hebrew", "arabic", "combining",
                       "empty", "nbsp", "collapse", "u+10ffff", "lone high surrogate",
                       "lone low surrogate"):
            self.assertIn(family, notes)

    def test_corpus_pins_the_sentinel_and_surrogate_rules(self):
        # DC-08: the two properties the app side depends on most.
        ins = {c["in"]: c["out"] for c in self.vectors["cases"]}
        self.assertEqual(ins["\U0010FFFF"], "")
        self.assertEqual(ins["A\ud800b"], "a\ud800b")


class PrefixVectorsTest(unittest.TestCase):
    """TH-02: the exclusive upper bound of the prefix range, pinned by data
    with astral and high-BMP continuations (a BMP sentinel such as U+FFFF
    misses them, and ASCII-only fixtures cannot tell)."""

    def setUp(self):
        import sqlite3
        with open(os.path.join(foldmod.REPO_ROOT, "fold", "prefix_vectors.json"), "r",
                  encoding="utf-8") as handle:
            self.vectors = json.load(handle)
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE search(key TEXT NOT NULL, rank INTEGER NOT NULL, "
                        "PRIMARY KEY (key, rank)) WITHOUT ROWID")
        self.db.executemany("INSERT INTO search VALUES (?, ?)",
                            [(k, n) for n, k in enumerate(self.vectors["keys"], 1)])

    def tearDown(self):
        self.db.close()

    def scan(self, prefix, sentinel):
        return [k for (k,) in self.db.execute(
            "SELECT key FROM search WHERE key >= ? AND key < ? ORDER BY key",
            (prefix, prefix + sentinel))]

    def test_keys_are_fold_fixed_points(self):
        for key in self.vectors["keys"]:
            self.assertEqual(fold(key), key)

    def test_every_case_with_the_builder_sentinel(self):
        self.assertEqual(self.vectors["sentinel"], "U+%04X" % ord(PREFIX_SENTINEL))
        for case in self.vectors["cases"]:
            self.assertEqual(self.scan(case["prefix"], PREFIX_SENTINEL), case["matches"],
                             case["prefix"].encode("unicode_escape"))
            self.assertEqual(case["matches"],
                             sorted(k for k in self.vectors["keys"] if k.startswith(case["prefix"])))

    def test_the_vectors_reject_a_bmp_sentinel(self):
        # The file is only worth having if a wrong sentinel FAILS it.
        for wrong in ("\uffff", "\ufffd", "~"):
            self.assertTrue(any(self.scan(c["prefix"], wrong) != c["matches"]
                                for c in self.vectors["cases"]), repr(wrong))


class FoldBehaviourTest(unittest.TestCase):
    def test_prefix_semantics(self):
        self.assertTrue(fold("Rio de Janeiro").startswith(fold("rio de j")))
        self.assertTrue(fold("São Paulo").startswith(fold("SAO P")))
        self.assertFalse(fold("Rio de Janeiro").startswith(fold("rio  dx")))

    def test_query_trailing_space_is_trimmed(self):
        self.assertEqual(fold("rio "), "rio")


if __name__ == "__main__":
    unittest.main()
