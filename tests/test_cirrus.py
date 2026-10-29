import json
import unittest

from rowtalk_packs.cirrus import DocReader, LangRules, LineStats, classify, iter_docs, select_aliases
from tests.helpers import bulk_lines, doc, test_config


def rules(lang="en"):
    cfg = test_config()
    return LangRules(cfg["wiki"]["languages"][lang], cfg["abstract"], cfg["aliases"])


def parse(text):
    stats = LineStats()
    return list(iter_docs(text.encode("utf-8").split(b"\n"), stats)), stats


class LineShapeTest(unittest.TestCase):
    def test_bulk_format_with_action_ids(self):
        docs, stats = parse(bulk_lines([doc(1, "A"), doc(2, "B")]))
        self.assertEqual([pid for pid, _ in docs], [1, 2])
        self.assertEqual(stats.action_lines, 2)
        self.assertEqual(stats.docs, 2)

    def test_doc_only_lines(self):
        text = "\n".join(json.dumps(doc(n, "T%d" % n)) for n in (5, 6)) + "\n"
        docs, stats = parse(text)
        self.assertEqual([pid for pid, _ in docs], [5, 6])
        self.assertEqual(stats.action_lines, 0)
        self.assertEqual(stats.docs_with_page_id, 2)

    def test_id_only_in_action_line(self):
        docs, stats = parse(bulk_lines([doc(7, "A")], page_id_in_doc=False))
        self.assertEqual(docs[0][0], 7)
        self.assertEqual(stats.docs_id_from_action, 1)

    def test_doc_page_id_wins_over_action(self):
        text = json.dumps({"index": {"_id": "99"}}) + "\n" + json.dumps(doc(8, "A")) + "\n"
        docs, _ = parse(text)
        self.assertEqual(docs[0][0], 8)

    def test_malformed_lines_are_skipped_and_do_not_leak_ids(self):
        text = "\n".join([
            json.dumps({"index": {"_id": "11"}}),
            "{not json",
            json.dumps({k: v for k, v in doc(0, "No id").items() if k != "page_id"}),
            "[1, 2, 3]",
            "42",
            json.dumps(doc(12, "Fine")),
        ]) + "\n"
        docs, stats = parse(text)
        self.assertEqual([pid for pid, _ in docs], [None, 12])
        self.assertEqual(stats.malformed, 3)
        self.assertEqual(stats.docs_without_id, 1)

    def test_blank_lines_and_crlf(self):
        text = "\r\n" + json.dumps(doc(3, "A")) + "\r\n\n"
        docs, stats = parse(text)
        self.assertEqual(len(docs), 1)
        self.assertGreaterEqual(stats.blank, 1)

    def test_action_variants(self):
        text = (json.dumps({"create": {"_id": 21}}) + "\n" + json.dumps(doc(None, "A")) + "\n"
                + json.dumps({"index": None}) + "\n" + json.dumps(doc(22, "B")) + "\n")
        docs, stats = parse(text)
        self.assertEqual([pid for pid, _ in docs], [21, 22])
        self.assertEqual(stats.action_lines, 2)

    def test_action_line_at_the_end_of_a_batch_names_the_next_batch_doc(self):
        stats = LineStats()
        reader = DocReader(stats)
        first = list(reader.docs([json.dumps({"index": {"_id": "31"}}).encode()]))
        second = list(reader.docs([json.dumps({"title": "x", "namespace": 0}).encode()]))
        self.assertEqual(first, [])
        self.assertEqual(second[0][0], 31)


class ClassifyTest(unittest.TestCase):
    def test_usable_article(self):
        cand, reason = classify(1, doc(1, "Brazil", pop=0.5, links=10, redirects=["Brasil"]),
                                rules())
        self.assertIsNone(reason)
        self.assertEqual(cand.title, "Brazil")
        self.assertEqual(cand.aliases, ["brasil"])
        cand, _ = classify(1, doc(1, "Brazil", redirects=["Federative Republic of Brazil"]), rules())
        self.assertEqual(cand.aliases, ["federative republic of brazil"])

    def test_unknown_fields_ignored(self):
        cand, reason = classify(1, doc(1, "A", extra={"future_field": {"x": [1]}, "wiki": 5}),
                                rules())
        self.assertIsNone(reason)

    def test_missing_fields(self):
        self.assertEqual(classify(None, doc(1, "A"), rules())[1], "no_page_id")
        d = doc(1, "A")
        del d["namespace"]
        self.assertEqual(classify(1, d, rules())[1], "no_namespace")
        d = doc(1, "A")
        del d["title"]
        self.assertEqual(classify(1, d, rules())[1], "no_title")
        d = doc(1, "A")
        del d["opening_text"]
        self.assertEqual(classify(1, d, rules())[1], "empty_opening_text")
        d = doc(1, "A")  # no popularity, no links: still a candidate
        cand, _ = classify(1, d, rules())
        self.assertIsNone(cand.popularity)
        self.assertIsNone(cand.incoming_links)

    def test_wrong_types_degrade(self):
        d = doc(1, "A", pop="high", links="many", redirects=None)
        d["redirect"] = "not a list"
        cand, reason = classify(1, d, rules())
        self.assertIsNone(reason)
        self.assertIsNone(cand.popularity)
        self.assertEqual(cand.aliases, [])

    def test_non_ns0(self):
        self.assertEqual(classify(1, doc(1, "Talk:A", ns=1), rules())[1], "non_ns0")
        self.assertEqual(classify(1, doc(1, "A", ns="4"), rules())[1], "non_ns0")

    def test_redirect_page(self):
        self.assertEqual(classify(1, doc(1, "A", source_text="#REDIRECT [[B]]"), rules())[1],
                         "redirect_page")
        self.assertEqual(classify(1, doc(1, "A", source_text="  #redirecionamento [[B]]"),
                                  rules("pt"))[1], "redirect_page")
        self.assertIsNone(classify(1, doc(1, "A", source_text="Normal text"), rules())[1])

    def test_disambiguation_signals(self):
        r = rules()
        self.assertEqual(classify(1, doc(1, "Mercury (disambiguation)"), r)[1], "disambiguation")
        self.assertEqual(classify(1, doc(1, "Mercury", categories=["All_disambiguation_pages"]),
                                  r)[1], "disambiguation")
        self.assertEqual(classify(1, doc(1, "Mercury", templates=["Template:Disambiguation"]),
                                  r)[1], "disambiguation")
        self.assertIsNone(classify(1, doc(1, "Mercury", categories=["Planets"]), r)[1])
        rp = rules("pt")
        self.assertEqual(classify(1, doc(1, "Mercúrio (desambiguação)"), rp)[1], "disambiguation")
        self.assertEqual(classify(1, doc(1, "X", templates=["Predefinição:Desambiguação"]), rp)[1],
                         "disambiguation")

    def test_title_that_folds_to_nothing(self):
        # DC-08: unreachable by any query unless an alias names it.
        for title in ("!!!", "?", "...", "\u2014"):
            self.assertEqual(classify(1, doc(1, title), rules())[1], "unsearchable", title)
        cand, reason = classify(1, doc(1, "!!!", redirects=["Triple exclamation"]), rules())
        self.assertIsNone(reason)
        self.assertEqual(cand.aliases, ["triple exclamation"])

    def test_empty_opening_text(self):
        self.assertEqual(classify(1, doc(1, "A", opening="   "), rules())[1], "empty_opening_text")
        self.assertEqual(classify(1, doc(1, "A", opening="()"), rules())[1], "empty_opening_text")

    def test_the_article_path_passes_the_configured_real_words(self):
        # R5-TH-02: classify() is the builder's only make_abstract call; a
        # lead whose first sentence ends at a configured not-abbreviation
        # ("Asia.", "the press.") before a non-starter keeps that sentence
        # instead of a ~590-char "…" cut (DC4-02).
        filler = " and more" * 120 + "."
        for first, second in (("Smyrna figs come from Asia.", " Turkey grows them"),
                              ("The law restrained the press.", " Such laws")):
            got, reason = classify(1, doc(1, "X", opening=first + second + filler), rules())
            self.assertIsNone(reason)
            self.assertEqual(got.abstract, first)


class BadUnicodeTest(unittest.TestCase):
    """DC-02: json.loads accepts lone surrogates; nothing holding one may
    reach SQLite (UnicodeEncodeError would kill the whole build)."""

    LINES = [
        b'{"page_id":1,"namespace":0,"title":"A","opening_text":"x \\ud83d y."}',
        b'{"page_id":2,"namespace":0,"title":"B\\udc00","opening_text":"Fine."}',
        b'{"page_id":3,"namespace":0,"title":"C","opening_text":"raw \xed\xa0\xbd y."}',
        b'{"page_id":4,"namespace":0,"title":"D","opening_text":"Fine.",'
        b'"redirect":[{"namespace":0,"title":"R\\udc00x"},{"namespace":0,"title":"Good alias"}]}',
        # A VALID surrogate pair is an astral character, not an error.
        b'{"page_id":5,"namespace":0,"title":"E \\ud83d\\ude00","opening_text":"Smile \\ud83d\\ude00."}',
    ]

    def test_lone_surrogates_are_excluded_or_dropped_and_staging_survives(self):
        import os, shutil, tempfile
        from rowtalk_packs.staging import Staging
        work = tempfile.mkdtemp()
        try:
            staging = Staging(os.path.join(work, "s.db"), keep=10, batch=1)
            reasons = {}
            for pid, d in DocReader(LineStats()).docs(self.LINES):
                cand, reason = classify(pid, d, rules())
                reasons[pid] = reason
                if cand is not None:
                    staging.add(0, cand)  # batch=1: flushes, i.e. hits SQLite now
            rows = {r[1]: r for r in staging.ordered("popularity_score", 10)}
            staging.close()
        finally:
            shutil.rmtree(work)
        self.assertEqual(reasons, {1: "bad_unicode", 2: "bad_unicode", 3: "bad_unicode",
                                   4: None, 5: None})
        self.assertEqual(rows[4][4], ["good alias"])
        self.assertEqual(rows[5][2], "E \U0001F600")

    def test_non_ascii_digits_are_not_numbers(self):
        from rowtalk_packs.cirrus import _as_int
        self.assertIsNone(_as_int("\u00b2"))      # superscript two: isdigit() but int() raises
        self.assertIsNone(_as_int("\u0661"))      # Arabic-Indic one: int() would read 1
        self.assertEqual(_as_int("42"), 42)
        self.assertEqual(classify(1, doc(1, "A", ns="\u00b2"), rules())[1], "no_namespace")
        text = json.dumps({"index": {"_id": "\u00b9"}}) + "\n" + json.dumps(
            {k: v for k, v in doc(0, "A").items() if k != "page_id"}) + "\n"
        docs, stats = parse(text)
        self.assertEqual(docs[0][0], None)
        self.assertEqual(stats.docs_without_id, 1)


class AliasTest(unittest.TestCase):
    def test_selection_rules(self):
        redirects = [{"namespace": 0, "title": t} for t in
                     ["USA", "U.S.A.", "United States of America", "US", "U", "United States",
                      "America", "Estados Unidos", "Amerika", "États-Unis", "The States",
                      "United-States"]]
        redirects.append({"namespace": 1, "title": "Talk redirect"})
        redirects.append({"title": "No namespace"})
        redirects.append("garbage")
        keys = select_aliases("united states", redirects, max_aliases=8, min_chars=2)
        # distinct fold keys; title key excluded ("United-States" folds to it);
        # "U" too short; "USA" and "U.S.A." are one key; shortest first then
        # code point order.
        self.assertEqual(keys, ["us", "usa", "america", "amerika", "etats unis", "the states",
                                "estados unidos", "united states of america"])

    def test_cap(self):
        redirects = ["Alias %02d" % n for n in range(20)]
        keys = select_aliases("t", [{"namespace": 0, "title": r} for r in redirects], 8, 2)
        self.assertEqual(keys, ["alias %02d" % n for n in range(8)])


if __name__ == "__main__":
    unittest.main()
