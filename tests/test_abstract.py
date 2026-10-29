import unittest

from rowtalk_packs.abstract import make_abstract, scrub, sentence_ends

EN_ABBR = frozenset(["mr", "dr", "st", "jr", "c", "ca", "etc"])
STARTERS = frozenset(["The", "It", "A", "In"])


class ScrubTest(unittest.TestCase):
    def test_template_leftovers(self):
        self.assertEqual(scrub("Lisbon () is the capital."), "Lisbon is the capital.")
        self.assertEqual(scrub("Lisbon ( ) is the capital."), "Lisbon is the capital.")
        self.assertEqual(scrub("Lisbon (; ) is the capital."), "Lisbon is the capital.")
        self.assertEqual(scrub("Lisbon (, ;) is."), "Lisbon is.")
        self.assertEqual(scrub("Pelé (; born 1940) was"), "Pelé (born 1940) was")
        self.assertEqual(scrub("X (born 1940; ) was"), "X (born 1940) was")
        self.assertEqual(scrub("a , b ; c ."), "a, b; c.")
        self.assertEqual(scrub("nested (( )) gone"), "nested gone")

    def test_whitespace(self):
        self.assertEqual(scrub("  a\n\tb   c  "), "a b c")
        self.assertEqual(scrub("a  b"), "a b")

    def test_space_before_a_leading_period_is_kept(self):
        # DC-03: a period that STARTS a token is not template debris.
        self.assertEqual(scrub("The .NET Framework is a platform. It runs C# code."),
                         "The .NET Framework is a platform. It runs C# code.")
        self.assertEqual(scrub("The Colt .45 is a pistol."), "The Colt .45 is a pistol.")
        self.assertEqual(scrub("Files end in .txt or .md usually."),
                         "Files end in .txt or .md usually.")
        self.assertEqual(scrub("See example .com sites ."), "See example .com sites.")
        self.assertEqual(scrub("Ends here . Then (a .) and \"b .\""), 'Ends here. Then (a.) and "b."')

    def test_separator_before_close_only_inside_an_open_group(self):
        self.assertEqual(scrub("He said :) and left"), "He said :) and left")
        self.assertEqual(scrub("A (see also:) b"), "A (see also) b")
        self.assertEqual(scrub("(x (y) z; ) w"), "(x (y) z; ) w")  # nested: left alone

    def test_control_characters_removed_format_characters_kept(self):
        # DC-07: NUL and other C0/C1 controls never reach an abstract (SQLite
        # length() stops at NUL); ZWJ / ZWNJ / bidi marks are content.
        self.assertEqual(scrub("a\x00b\x07c\x1bd\x7fe\x9ff"), "abcdef")
        family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
        self.assertEqual(scrub("Family %s emoji." % family), "Family %s emoji." % family)
        self.assertEqual(scrub("\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645 \u200fRTL"),
                         "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645 \u200fRTL")

    def test_real_parentheses_kept(self):
        self.assertEqual(scrub("Rio de Janeiro (Portuguese: [ˈʁi.u]) is"),
                         "Rio de Janeiro (Portuguese: [ˈʁi.u]) is")


class SentenceTest(unittest.TestCase):
    def test_boundaries(self):
        text = "First one. It ends! Is it? \"Fourth one.\" Fifth"
        ends = list(sentence_ends(text))
        # DC3-01: only "." is ever trusted; "!" and "?" never are.
        self.assertEqual([text[:e] for e in ends],
                         ["First one.", 'First one. It ends! Is it? "Fourth one."'])

    def test_exclamation_and_question_marks_are_never_boundaries(self):
        # DC2-02 (capitalised word) and DC3-01 (every other shape: a
        # lowercase word, a bare mark, a sentence-case title before "(").
        for text in ("Mayer led Yahoo! Inc. for years", "He hosted Jeopardy! The show ran",
                     "the quiz Who Wants to Be a Millionaire? The British version",
                     'the song "Help!" Then he left', "!!! Is an American band",
                     "The service, branded as yahoo! Japan in that market",
                     "Fernanda Torres atuou em O que é isso, companheiro? (1997) e",
                     "O musical Ai, que saudade! Montado em 1980",
                     "The catchphrase ¡ay, caramba! Became popular"):
            self.assertEqual(list(sentence_ends(text, EN_ABBR, STARTERS)), [], text)

    def test_numbered_references_are_not_boundaries(self):
        for text in ("Sonata in C-sharp minor, Op. 27, No. 2", "See Vol. 2 of it",
                     "shown in Fig. 3 below", "conforme o art. 5 da lei",
                     "see pt. 2 of the report"):
            self.assertEqual(list(sentence_ends(text)), [], text)
        # A year, or a long word, before ". <digit>" is still a sentence end.
        self.assertEqual(len(list(sentence_ends("The war ended in 1945. 60 million died."))), 2)
        self.assertEqual(len(list(sentence_ends("It is a long story. 2 people saw it."))), 2)

    def test_a_unit_after_a_number_ends_a_sentence_before_a_digit(self):
        # DC3-02: "km" after "285" is a unit, not a numbered reference.
        for text in ("Fortaleza fica distante 2 285 km. 50 bairros compõem a cidade.",
                     "It lies on the bay, 12 km. 18 villages surround it."):
            ends = list(sentence_ends(text))
            self.assertEqual(len(ends), 2, text)
            self.assertTrue(text[:ends[0]].endswith("km."), text)

    def test_a_listed_all_caps_form_is_trusted_only_before_a_starter(self):
        # DC3-02: "cia" (Companhia) is listed, and "CIA." is often the agency.
        # DC4-01: it is just as often a company name in capitals ("CIA.
        # HERING", "PTE. LTD.", "ACME CO. LTD."), so a LISTED all-caps form
        # is trusted only before a starter, like a short capitalised token.
        pt = frozenset(["cia", "ed", "op", "pte", "co", "ltd"])
        starters = frozenset(["Durante", "The", "Em"])
        text = "Foi conduzida pela CIA. Durante a Guerra Fria, a Cia. Souza operou. Fim."
        ends = [text[:e] for e in sentence_ends(text, pt, starters)]
        self.assertEqual(ends[0], "Foi conduzida pela CIA.")
        self.assertNotIn("Foi conduzida pela CIA. Durante a Guerra Fria, a Cia.", ends)
        for text in ("Sua razão social é CIA. HERING, com sede", "RAZER ASIA PACIFIC PTE. LTD. Is",
                     "Acme is a brand. ACME CO. LTD. Of", "a CIA. Documentos mostram"):
            ends = [text[:e] for e in sentence_ends(text, pt, starters)]
            self.assertFalse([e for e in ends if e.endswith(("CIA.", "PTE.", "CO."))], text)
        # An all-caps token NO list names is an acronym: trusted.
        self.assertEqual(len(list(sentence_ends("It was built by NASA. Engineers came.", pt))), 2)

    def test_a_joined_token_is_judged_by_its_capitalised_last_segment(self):
        # DC4-01: "Lt-Col.", "Vice-Adm.", "Vice/Alm." are not isalpha, so the
        # shape rules never saw "Col"/"Adm"/"Alm" before.
        for text in ("The cemetery was restored by Lt-Col. Smith and others",
                     "O navio afundou com o Vice-Alm. Barroso a bordo",
                     "The bill was signed by Vice-Adm. Jones", "led by LT-COL. SMITH",
                     "under Vice/Adm. Nelson"):
            self.assertEqual(list(sentence_ends(text, frozenset(["col", "adm"]), STARTERS)), [],
                             text)
        # A long capitalised segment is a word ("Guinea-Bissau."), a short
        # one is re-trusted by a starter ("Porto-Novo. The"), and a
        # lowercase segment leaves the whole token to the word rules.
        for text in ("The capital is Guinea-Bissau. Its", "The capital is Porto-Novo. The",
                     "It was a co-founder. Then"):
            self.assertEqual(len(list(sentence_ends(text, frozenset(["co"]),
                                                    STARTERS | {"Its", "Then"}))), 1, text)

    def test_a_capitalised_plural_of_a_listed_form_is_an_abbreviation(self):
        # DC4-01 (c): "Profs.", "Capts." (SHORT_TITLE_MAX + 1 letters) are
        # the plural of a listed form, never re-trusted; "Mars." (4 letters)
        # stays a short capitalised token a starter re-trusts.
        listed = frozenset(["prof", "capt", "mar"])
        for text in ("The bill was debated. Profs. Smith and Jones", "led by Capts. Hook. The"):
            ends = [text[:e] for e in sentence_ends(text, listed, STARTERS)]
            self.assertFalse([e for e in ends if e.endswith(("Profs.", "Capts."))], text)
        mars = "The probe landed on Mars. The mission ended."
        self.assertEqual(len(list(sentence_ends(mars, listed, STARTERS))), 2)

    def test_words_the_shapes_misread_end_sentences(self):
        # DC4-02: real ends at short place names and word-plurals, before a
        # lead's typical non-starter opener, are kept — the configured
        # `not_abbreviations` and the lead participles as starters.
        from rowtalk_packs.cirrus import LangRules
        from rowtalk_packs.config import load_config
        cfg = load_config()
        cases = {
            "pt": ["Pelotas é um município do estado do Rio Grande do Sul. Localizado às margens",
                   "Cuiabá é a capital de Mato Grosso do Sul. Fundada em 1719",
                   "Belém é a capital do Pará. Localizada na foz do rio",
                   "Rio Branco é a capital do Acre. Localizado no leste",
                   "Qom é uma cidade do Irã. Sedia o santuário",
                   "Sua sede fica em Lima, Peru. Possui filiais",
                   # A short name no list re-trusts: only the starter does.
                   "A cidade fica perto de Bagé. Localizada na fronteira"],
            "en": ["Cuyahoga Falls is a city in Summit County, Ohio. Situated on the river",
                   "Lima is the capital of Peru. Located on the coast",
                   "Smyrna figs come from Asia. Turkey grows them",
                   "The law restrained the press. Such laws", "He joined the Marine Corps. Born in",
                   "Canterbury is a cathedral city in Kent. Located on the River Stour"],
        }
        for lang, texts in cases.items():
            rules = LangRules(cfg["wiki"]["languages"][lang], cfg["abstract"], cfg["aliases"])
            for text in texts:
                first = text[:text.index(". ") + 1]
                ends = [text[:e] for e in sentence_ends(text, rules.abbreviations,
                                                       rules.sentence_starters,
                                                       rules.not_abbreviations)]
                self.assertEqual(ends[:1], [first], (lang, text))
                # ... so a lead whose second sentence (with its real opener)
                # crosses 600 gets the clean first sentence, not a "…" cut.
                lead = text + " word" * 150 + "."
                out = make_abstract(lead, 600, "…", rules.abbreviations,
                                    rules.sentence_starters, rules.not_abbreviations)
                self.assertEqual(out, first, (lang, text))

    def test_not_abbreviations_are_never_abbreviations(self):
        # `not_abbreviations` RE-TRUSTS a boundary, so an entry that is also
        # an abbreviation would publish fragments: keep every entry disjoint
        # from every abbreviation list, in both languages (names are quoted
        # verbatim across languages).
        from rowtalk_packs.config import load_config
        cfg = load_config()
        langs = cfg["wiki"]["languages"]
        abbreviations = {a.casefold() for c in langs.values() for a in c["abbreviations"]}
        abbreviations |= {a.casefold() for a in cfg["abstract"]["shared_abbreviations"]}
        words = list(cfg["abstract"]["shared_not_abbreviations"])
        for lang_cfg in langs.values():
            words += lang_cfg.get("not_abbreviations", [])
        self.assertTrue(words)
        for word in words:
            self.assertNotIn(word.casefold(), abbreviations, word)
            self.assertGreater(len(word), 1, word)
            self.assertNotIn(".", word, word)

    def test_short_capitalised_tokens_are_abbreviations_listed_or_not(self):
        # DC3-01: "Pte.", "Msgr.", "Pvt.", "Hob.", "Vols." need no list.
        for text in ("Razer Asia Pacific Pte. Ltd., trading as Razer, is",
                     "The cathedral was consecrated by Msgr. John Smith",
                     "The memorial honours Pvt. James Walker", "Sonata Hob. XVI is",
                     "printed in Vols. 1 to 3", "Pvt. A. Smith led them"):
            self.assertEqual(list(sentence_ends(text, frozenset(), STARTERS)), [], text)
        # A LOWERCASE abbreviation needs its list entry (the stated residual).
        self.assertEqual(list(sentence_ends("Kramer vs. Kramer won", frozenset(["vs"]))), [])

    def test_a_starter_re_trusts_only_an_unlisted_short_capitalised_end(self):
        text = "The river rises in Peru. The basin is vast."
        self.assertEqual([text[:e] for e in sentence_ends(text, EN_ABBR, STARTERS)],
                         ["The river rises in Peru.", text])
        # Without the starter list it costs content, never safety.
        self.assertEqual([text[:e] for e in sentence_ends(text, EN_ABBR)], [text])
        # A LISTED abbreviation is never re-trusted ("Mr. The" stays a name).
        self.assertEqual(list(sentence_ends("Mr. The Smith", EN_ABBR, STARTERS)), [])
        # A capitalised plural of a listed form is a name, not an abbreviation.
        mars = "The probe landed on Mars. The mission ended."
        self.assertEqual([mars[:e] for e in sentence_ends(mars, frozenset(["mar"]), STARTERS)],
                         ["The probe landed on Mars.", mars])
        self.assertEqual(list(sentence_ends("in the fine arts. The", frozenset(["art"]),
                                            STARTERS)), [])
        # An initial is not the starter "A".
        self.assertEqual(list(sentence_ends("led by Pvt. A. Smith", EN_ABBR, STARTERS)), [])

    def test_balance_uses_a_running_depth(self):
        from rowtalk_packs.abstract import balanced
        self.assertTrue(balanced("a (b) [c] “d” «e» \"f\""))
        # DC2-02: stray closers ("a) b)") must not hide a later open group.
        self.assertFalse(balanced("two steps: a) collect, b) sort; Smith (born Jan."))
        self.assertTrue(balanced("two steps: a) collect, b) sort."))
        self.assertFalse(balanced("x ) ( y"))

    def test_abbreviations_and_initials_are_not_boundaries(self):
        text = "Dr. Smith met J. Doe in the U.S. Later they left."
        # "Dr." (listed), "J." (initial) and "U.S." (dotted token) are not
        # boundaries — the last one even though it ends a sentence here
        # (the accepted cost: an abstract is cut EARLIER, never longer).
        self.assertEqual([text[:e] for e in sentence_ends(text, EN_ABBR)], [text])

    def test_lowercase_continuation_is_not_a_boundary(self):
        text = "Approx. five km. but more"
        self.assertEqual(list(sentence_ends(text)), [])


class MakeAbstractTest(unittest.TestCase):
    def test_short_text_kept_whole(self):
        self.assertEqual(make_abstract("Short lead without period", 600), "Short lead without period")

    def test_exactly_max_chars_kept(self):
        text = "a" * 599 + "."
        self.assertEqual(make_abstract(text, 600), text)

    def test_whole_sentences_up_to_limit(self):
        s1 = "First sentence is here."
        s2 = "Second one follows it."
        s3 = "Third would overflow the budget by far."
        text = " ".join([s1, s2, s3])
        out = make_abstract(text, len(s1) + 1 + len(s2) + 5)
        self.assertEqual(out, s1 + " " + s2)

    def test_first_sentence_too_long_cuts_at_word_with_ellipsis(self):
        text = ("word " * 200).strip() + ". Next."
        out = make_abstract(text, 600)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 600)
        self.assertFalse(out[:-1].endswith(" "))
        self.assertTrue(out[:-1].endswith("word"))

    def test_single_giant_token_hard_cut(self):
        out = make_abstract("x" * 1000, 600)
        self.assertEqual(len(out), 600)
        self.assertTrue(out.endswith("…"))

    def test_trailing_separator_dropped_before_ellipsis(self):
        text = ("aaaa, " * 150)
        out = make_abstract(text, 600)
        self.assertFalse(out[:-1].endswith(","))

    def test_abbreviation_does_not_shorten_abstract(self):
        body = "The St. Louis Cardinals are a team " + "that plays baseball " * 40 + "."
        out = make_abstract(body + " Next sentence.", 600, abbreviations=EN_ABBR)
        self.assertGreater(len(out), 100)  # not cut at "St."

    LONG = " whose " + "very " * 150 + "web."

    def test_false_boundary_fragment_is_not_an_abstract(self):
        # DC-04: "!" inside a name and an unlisted abbreviation are false
        # boundaries; a fragment that short (or unbalanced) is not trusted.
        en = frozenset(["inc", "dr", "st"])
        for lead in ("Yahoo! Inc. was an American web services provider",
                     "Jeopardy! Is an American television game show",
                     "Smith (born Abc. 5, 1950) was a writer"):
            out = make_abstract(lead + self.LONG, 600, abbreviations=en)
            self.assertTrue(out.startswith(lead), out[:40])
            self.assertTrue(out.endswith("…"))
            self.assertLessEqual(len(out), 600)

    def test_month_abbreviations_are_configured(self):
        from rowtalk_packs.config import load_config
        en = load_config()["wiki"]["languages"]["en"]["abbreviations"]
        for month in ("jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec"):
            self.assertIn(month, en)

    def test_short_clean_first_sentence_is_the_abstract(self):
        # DC2-01 (SPEC: keep whole sentences up to 600): a short first
        # sentence followed by one sentence that overflows IS the abstract.
        first = "Lisbon is the capital and largest city of Portugal."
        second = " It " + ", ".join("has district number %d with its own parish council and "
                                    "market" % i for i in range(12)) + "."
        self.assertEqual(make_abstract(first + second, 600, abbreviations=EN_ABBR), first)
        self.assertEqual(make_abstract("Brazil is a country." + " It" + self.LONG, 600),
                         "Brazil is a country.")

    def test_balanced_sentences_kept_whole_unbalanced_cut(self):
        s1 = "A" + " word" * 60 + "."
        out = make_abstract(s1 + " Next" + self.LONG, 600)
        self.assertEqual(out, s1)
        unbalanced = "A (" + " word" * 60 + "."
        out = make_abstract(unbalanced + " Next" + self.LONG, 600)
        self.assertTrue(out.endswith("…"))

    def test_false_boundaries_past_any_share_never_end_an_abstract(self):
        # DC2-02: the false boundary sits at 280 code points (beyond the
        # round-1 40% share), inside a first sentence longer than 600.
        en = frozenset(["inc", "dr", "st", "jan"])
        tail = "oversaw acquisitions such as Tumblr and many others, " * 8 + "and so on."
        yahoo = ("Marissa Mayer is an American businesswoman and investor who worked at Google "
                 "for thirteen years in many roles, including engineering and product management "
                 "positions across search, maps and local products, and who later served as the "
                 "president and chief executive officer of Yahoo! Inc. from 2012 until its sale in "
                 "2017, where she " + tail)
        quiz = ("Chris Tarrant is an English broadcaster and television presenter known for a "
                "long career on radio and television, including breakfast shows, children's "
                "programmes and game shows, and he is most widely recognised as the original host "
                "of the quiz Who Wants to Be a Millionaire? The British version of which he "
                "presented from 1998 until 2014, " + "winning many awards and accolades, " * 10
                + "and more.")
        enum = ("The method has two steps: a) collect, b) sort; it was described by Smith (born "
                "Jan. 5, 1950 in Leeds, who also wrote about many other things across a long "
                "career spanning decades of research into sorting algorithms and their "
                "complexity, which is long. And then " + "word " * 120)
        opus = ("Piano Sonata No. 14 in C-sharp minor, Op. 27, No. 2, is a piano sonata by "
                "Ludwig van Beethoven " + "that is very famous and often played " * 20 + "today.")
        for name, text in (("yahoo", yahoo), ("quiz", quiz), ("enum", enum), ("opus", opus)):
            out = make_abstract(text, 600, abbreviations=en)
            self.assertTrue(out.endswith("…"), (name, out[-50:]))
            self.assertGreater(len(out), 500, name)
            self.assertLessEqual(len(out), 600, name)

    def test_names_from_either_language_never_end_an_abstract(self):
        # DC3-01: names are quoted verbatim in every language ("Warner Bros.",
        # "Kramer vs. Kramer", "Mr. Bean"), so each language's rules are run
        # over its OWN list and the shared list (lowercase AND capitalised),
        # the OTHER language's list (capitalised: how a name quotes it) and
        # unlisted names. The only real sentence end that fits is the first
        # one, so the abstract is exactly it or ends in "…" — never "…Bros.".
        from rowtalk_packs.cirrus import LangRules
        from rowtalk_packs.config import load_config
        cfg = load_config()
        langs = cfg["wiki"]["languages"]
        shared = cfg["abstract"]["shared_abbreviations"]
        first = "Short first sentence here."
        tail = " Xavier and " + "many other people " * 40 + "joined."
        unlisted = ["Hob", "Vols", "Arts", "Profs", "Capts", "Sgts", "Cmdrs"]
        # TH4-03: "unlisted" is a claim — hold it (Pte/Msgr/Pvt/Mgr/Cpl were
        # in this list while every one of them is listed).
        listed = {a.lower() for oc in langs.values() for a in oc["abbreviations"]}
        listed |= {a.lower() for a in shared}
        self.assertEqual([u for u in unlisted if u.lower() in listed], [])
        checked = 0
        for lang, lang_cfg in sorted(langs.items()):
            rules = LangRules(lang_cfg, cfg["abstract"], cfg["aliases"])
            own = list(lang_cfg["abbreviations"]) + shared
            other = [a for o, oc in langs.items() if o != lang for a in oc["abbreviations"]]
            capitalised = [a[:1].upper() + a[1:] for a in own + other + shared]
            # DC4-01: ALL-CAPS listed forms ("CIA", "PTE", "CO") and joined
            # forms ("Lt-Col", "Vice-Adm", "LT-COL"), before a name, a name
            # in capitals, another listed form, and a digit. All-caps forms
            # only of the lists THIS language's rules hold: an all-caps form
            # no list names is an acronym (the stated residual: "STO." in an
            # English lead).
            caps = [a.upper() for a in own if len(a) > 1]
            joined = (["Lt-" + c for c in capitalised] + ["Vice-" + c for c in capitalised]
                      + ["LT-" + c for c in caps])
            forms = own + capitalised + caps + joined + unlisted
            for form in forms:
                for after in (" Xavier", " HERING", " LTD.", " 27"):
                    text = first + " The work by " + form + "." + after + tail
                    out = make_abstract(text, 600, "…", rules.abbreviations,
                                        rules.sentence_starters, rules.not_abbreviations)
                    self.assertTrue(out == first or out.endswith("…"),
                                    (lang, form, after, out[-40:]))
                    checked += 1
        self.assertGreater(checked, 2000)

    def test_hard_cut_never_splits_a_grapheme(self):
        # DC-07: decomposed accents, ZWJ sequences, emoji modifiers, flags.
        out = make_abstract("e\u0301" * 700, 600)
        self.assertEqual(out[-2:], "\u0301…")
        self.assertLessEqual(len(out), 600)
        family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
        for unit in (family, "\U0001F44D\U0001F3FD", "\U0001F1E7\U0001F1F7", "a\ufe0f"):
            for pad in range(len(unit)):
                out = make_abstract("x" * pad + unit * 400, 600)
                body = out[:-1][pad:]
                self.assertEqual(len(body) % len(unit), 0, (unit, pad))
                self.assertEqual(body, unit * (len(body) // len(unit)), (unit, pad))
                self.assertLessEqual(len(out), 600)

    def test_non_string(self):
        self.assertEqual(make_abstract(None), "")
        self.assertEqual(make_abstract(["x"]), "")

    def test_code_points_not_bytes(self):
        text = ("ção " * 200).strip()
        out = make_abstract(text, 600)
        self.assertLessEqual(len(out), 600)


if __name__ == "__main__":
    unittest.main()
