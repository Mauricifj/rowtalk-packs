import gzip
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import unittest

from rowtalk_packs import dictbuild, packdb, validate
from tests.helpers import test_config

LMF = """<?xml version="1.0" encoding="UTF-8"?>
<LexicalResource xmlns:dc="https://globalwordnet.github.io/schemas/dc/">
 <Lexicon id="t" label="t" language="en" email="x" license="x" version="1">
  <LexicalEntry id="e1"><Lemma writtenForm="House" partOfSpeech="n"/>
   <Sense id="s1" synset="n1"/><Sense id="s2" synset="n2"/></LexicalEntry>
  <LexicalEntry id="e2"><Lemma writtenForm="home" partOfSpeech="n"/><Sense id="s3" synset="n1"/></LexicalEntry>
  <LexicalEntry id="e3"><Lemma writtenForm="house" partOfSpeech="v"/><Sense id="s4" synset="v1"/></LexicalEntry>
  <LexicalEntry id="e4"><Lemma writtenForm="houseboat" partOfSpeech="n"/><Sense id="s5" synset="n3"/></LexicalEntry>
  <LexicalEntry id="e5"><Lemma writtenForm="ice cream" partOfSpeech="n"/><Sense id="s6" synset="n4"/></LexicalEntry>
  <LexicalEntry id="e6"><Lemma writtenForm="a b c" partOfSpeech="n"/><Sense id="s7" synset="n4"/></LexicalEntry>
  <LexicalEntry id="e7"><Lemma writtenForm="lonely" partOfSpeech="a"/><Sense id="s8" synset="a1"/></LexicalEntry>
  <LexicalEntry id="e8"><Lemma writtenForm="Coração" partOfSpeech="n"/><Sense id="s9" synset="n5"/></LexicalEntry>
  <Synset id="n1" partOfSpeech="n"><Definition>a dwelling</Definition><Example>"the house is big"</Example></Synset>
  <Synset id="n2" partOfSpeech="n"><Definition>a family line</Definition><Example>second example</Example></Synset>
  <Synset id="v1" partOfSpeech="v"><Definition>to shelter</Definition></Synset>
  <Synset id="n3" partOfSpeech="n"><Definition>a boat used as a home</Definition></Synset>
  <Synset id="n4" partOfSpeech="n"><Definition>frozen dessert</Definition></Synset>
  <Synset id="a1" partOfSpeech="a"></Synset>
  <Synset id="n5" partOfSpeech="n"><Definition>the organ</Definition></Synset>
 </Lexicon>
</LexicalResource>
"""


class DictBuildTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = test_config()
        self.cfg["dict"]["languages"]["en"]["first_edition"] = {"standard": {"min_articles": 1}}
        self.source_meta = {"version": "2025-edition", "tag": "dict-en-2025-edition",
                            "url": "local", "edition": "2025-edition", "version_int": 20250101}

    def tearDown(self):
        shutil.rmtree(self.dir)

    def build(self, source_path):
        return dictbuild.build_dict_pack(self.cfg, "en", source_path, self.source_meta, self.dir,
                                         os.path.join(self.dir, "editions"))

    def test_words_table_matches_the_app_builder_shape(self):
        path = os.path.join(self.dir, "src.xml.gz")
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(LMF)
        report = self.build(path)
        self.assertTrue(report["gate"]["passed"], report["gate"])
        db = sqlite3.connect(os.path.join(self.dir, "dict-en.db"))
        rows = {(lemma, pos): json.loads(senses) for lemma, pos, senses in
                db.execute("SELECT lemma, pos, senses FROM words")}
        # NFC + lowercase + trim; "House" and "house"(n) merge; 3-word lemma dropped
        self.assertEqual(rows[("house", "n")], [
            {"d": "a dwelling", "s": ["home"], "e": "the house is big"},
            {"d": "a family line", "s": []}])
        self.assertEqual(rows[("house", "v")], [{"d": "to shelter", "s": []}])
        self.assertEqual(rows[("home", "n")], [{"d": "a dwelling", "s": ["House"], "e": "the house is big"}])
        self.assertIn(("ice cream", "n"), rows)
        self.assertNotIn(("a b c", "n"), rows)
        self.assertNotIn(("lonely", "a"), rows)  # no definition, no synonyms -> dropped
        self.assertIn(("coração", "n"), rows)
        ddl = db.execute("SELECT sql FROM sqlite_master WHERE name = 'words'").fetchone()[0]
        self.assertIn("WITHOUT ROWID", ddl)
        db.close()

    def test_search_and_ranking(self):
        path = os.path.join(self.dir, "src.xml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(LMF)
        self.build(path)
        db = packdb.open_pack(os.path.join(self.dir, "dict-en.db"))
        # "house" has 3 senses (n2 + v1) -> rank 1; prefix finds house before houseboat
        self.assertEqual(packdb.suggest(db, "dict", "hou"), ["house", "houseboat"])
        self.assertEqual(packdb.suggest(db, "dict", "CORACA"), ["coração"])
        self.assertEqual(packdb.display_for(db, "dict", [packdb.exact_rank(db, "Ice Cream")]),
                         ["ice cream"])
        meta = packdb.read_meta(db)
        self.assertEqual(meta["ranking"], "sense_count")
        self.assertEqual(meta["fold_version"], "1")
        db.close()
        _, errors = validate.validate_pack(os.path.join(self.dir, "dict-en.db"), "dict")
        self.assertEqual(errors, [])

    def test_tar_xz_source_ignores_appledouble_members(self):
        path = os.path.join(self.dir, "own-pt-2026.04.07.tar.xz")
        with tarfile.open(path, "w:xz") as tar:
            for name, text in (("own-pt/._own-pt.xml", "junk"), ("own-pt/own-pt.xml", LMF)):
                data = text.encode("utf-8")
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        report = self.build(path)
        self.assertGreater(report["article_count"], 3)

    def test_dict_run_publishes_once_per_upstream_version(self):
        import json as _json
        import rowtalk_packs.__main__ as cli
        from rowtalk_packs import manifest
        from tests.helpers import DumpServer, FakeGitHub
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)
        manifest.write(os.path.join(repo, "manifest.json"), manifest.empty())
        cfg_path = os.path.join(self.dir, "cfg.json")
        with open(cfg_path, "w", encoding="utf-8") as handle:
            _json.dump(self.cfg, handle)
        files = {"/dl/english-wordnet-2025.xml.gz": gzip.compress(LMF.encode("utf-8"))}
        gh = FakeGitHub(self.dir, repo)
        original = cli._gh
        cli._gh = lambda cfg, args: gh
        try:
            with DumpServer(files) as server:
                gh.upstream["globalwordnet/english-wordnet"] = {
                    "tag_name": "2025-edition", "published_at": "2025-03-01T00:00:00Z",
                    "assets": [{"name": "english-wordnet-2025.xml.gz",
                                "browser_download_url": server.url("/dl/english-wordnet-2025.xml.gz")},
                               {"name": "english-wordnet-2025.json.zip", "browser_download_url": "x"}]}
                argv = ["--config", cfg_path, "--repo-root", repo, "dict-run", "--langs", "en",
                        "--work", os.path.join(self.dir, "work")]
                out1 = os.path.join(self.dir, "o1")
                self.assertEqual(cli.main(argv + ["--github-output", out1]), 0)
                out2 = os.path.join(self.dir, "o2")
                self.assertEqual(cli.main(argv + ["--github-output", out2]), 0)
        finally:
            cli._gh = original
        data = manifest.load(os.path.join(repo, "manifest.json"))
        self.assertEqual([e["id"] for e in data["packs"]], ["dict-en"])
        entry = data["packs"][0]
        self.assertEqual(entry["tag"], "dict-en-2025-edition")
        self.assertEqual(entry["version"], 2025030100)  # date x 100 + revision 0
        self.assertEqual(entry["kind"], "dict")
        self.assertFalse(gh.find_release("dict-en-2025-edition")["draft"])
        with open(out1) as h1, open(out2) as h2:
            self.assertIn("changed=true", h1.read())
            self.assertIn("changed=false", h2.read())

    def test_dict_run_publishes_a_language_even_when_another_fails(self):
        # OPS2-05: one language failing (here: pt's upstream has no matching
        # asset) must not keep the other off main. Outputs are written before
        # the non-zero exit, and the published language is named.
        import json as _json
        import rowtalk_packs.__main__ as cli
        from rowtalk_packs import manifest
        from tests.helpers import DumpServer, FakeGitHub
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)
        manifest.write(os.path.join(repo, "manifest.json"), manifest.empty())
        cfg_path = os.path.join(self.dir, "cfg.json")
        with open(cfg_path, "w", encoding="utf-8") as handle:
            _json.dump(self.cfg, handle)
        files = {"/dl/english-wordnet-2025.xml.gz": gzip.compress(LMF.encode("utf-8"))}
        gh = FakeGitHub(self.dir, repo)
        original = cli._gh
        cli._gh = lambda cfg, args: gh

        def outputs(path):
            with open(path, "r", encoding="utf-8") as handle:
                return dict(line.rstrip("\n").split("=", 1) for line in handle)

        try:
            with DumpServer(files) as server:
                gh.upstream["globalwordnet/english-wordnet"] = {
                    "tag_name": "2025-edition", "published_at": "2025-03-01T00:00:00Z",
                    "assets": [{"name": "english-wordnet-2025.xml.gz",
                                "browser_download_url": server.url("/dl/english-wordnet-2025.xml.gz")}]}
                gh.upstream["own-pt/openWordnet-PT"] = {
                    "tag_name": "v9", "published_at": "2026-01-01T00:00:00Z",
                    "assets": [{"name": "own-pt-v9.zip", "browser_download_url": "x"}]}
                base = ["--config", cfg_path, "--repo-root", repo, "dict-run",
                        "--work", os.path.join(self.dir, "work")]
                for order, out_name in (("pt,en", "o1"), ("en,pt", "o2")):
                    out = os.path.join(self.dir, out_name)
                    code = cli.main(base + ["--langs", order, "--github-output", out])
                    self.assertEqual(code, 1, order)  # the run stays red
                    if out_name == "o1":
                        got = outputs(out)
                        self.assertEqual(got["changed"], "true")
                        self.assertEqual(_json.loads(got["published"]), ["dict-en-2025-edition"])
                        self.assertEqual(got["skipped"], "pt")
                        self.assertEqual([e["id"] for e in manifest.load(
                            os.path.join(repo, "manifest.json"))["packs"]], ["dict-en"])
                    else:
                        # The second run finds en named: nothing rebuilt or deleted.
                        got = outputs(out)
                        self.assertEqual((got["changed"], got["skipped"]), ("false", "pt"))
                        self.assertFalse([c for c in gh.calls
                                          if isinstance(c, tuple) and c[0] == "delete"])
        finally:
            cli._gh = original
        self.assertFalse(gh.find_release("dict-en-2025-edition")["draft"])

    # ---- dict-run scenarios over the fake GitHub seam (TH3-02/TH3-03/DC3-03)
    def dict_env(self):
        """A repo with an empty manifest, a config file, a fake GitHub with
        two upstream wordnets (en: .xml.gz, pt: .tar.xz) served over HTTP.
        Returns (run, gh, server); run(*argv) -> (exit code, outputs)."""
        import json as _json
        import rowtalk_packs.__main__ as cli
        from rowtalk_packs import manifest
        from tests.helpers import DumpServer, FakeGitHub
        self.repo = os.path.join(self.dir, "repo")
        os.makedirs(self.repo)
        manifest.write(os.path.join(self.repo, "manifest.json"), manifest.empty())
        self.cfg["dict"]["languages"]["pt"]["first_edition"] = {"standard": {"min_articles": 1}}
        self.cfg["dict"]["languages"]["pt"]["golden"] = [
            {"mode": "exact", "query": "house", "expect_title": "house", "within": 1}]
        tar = io.BytesIO()
        with tarfile.open(fileobj=tar, mode="w:xz") as archive:
            data = LMF.encode("utf-8")
            info = tarfile.TarInfo("own-pt/own-pt.xml")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        files = {"/dl/en-2024.xml.gz": gzip.compress(LMF.encode("utf-8")),
                 "/dl/en-2025.xml.gz": gzip.compress(LMF.encode("utf-8")),
                 "/dl/own-pt-1.1.0.tar.xz": tar.getvalue()}
        server = DumpServer(files).__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        gh = FakeGitHub(self.dir, self.repo)
        original = cli._gh
        cli._gh = lambda cfg, args: gh
        self.addCleanup(setattr, cli, "_gh", original)
        gh.upstream["own-pt/openWordnet-PT"] = {
            "tag_name": "v1.1.0", "published_at": "2024-06-01T00:00:00Z",
            "assets": [{"name": "own-pt-1.1.0.tar.xz",
                        "browser_download_url": server.url("/dl/own-pt-1.1.0.tar.xz")}]}
        self.set_en_upstream(gh, server, "2025")
        counter = [0]

        def run(*argv):
            with open(os.path.join(self.dir, "cfg.json"), "w", encoding="utf-8") as handle:
                _json.dump(self.cfg, handle)
            counter[0] += 1
            out = os.path.join(self.dir, "out%d" % counter[0])
            code = cli.main(["--config", os.path.join(self.dir, "cfg.json"), "--repo-root",
                             self.repo] + list(argv) + ["--github-output", out]
                            if argv and argv[0] == "dict-run" else
                            ["--config", os.path.join(self.dir, "cfg.json"), "--repo-root",
                             self.repo] + list(argv))
            got = {}
            if os.path.exists(out):
                with open(out, "r", encoding="utf-8") as handle:
                    got = dict(line.rstrip("\n").split("=", 1) for line in handle)
            return code, got
        return run, gh, server

    def set_en_upstream(self, gh, server, year):
        gh.upstream["globalwordnet/english-wordnet"] = {
            "tag_name": "%s-edition" % year, "published_at": "%s-03-01T00:00:00Z" % year,
            "assets": [{"name": "english-wordnet-%s.xml.gz" % year,
                        "browser_download_url": server.url("/dl/en-%s.xml.gz" % year)}]}

    def served(self):
        from rowtalk_packs import manifest
        return {e["id"]: (e["tag"], e["version"]) for e in
                manifest.load(os.path.join(self.repo, "manifest.json"))["packs"]}

    def work(self):
        return ["--work", os.path.join(self.dir, "work")]

    def test_dict_run_a_gate_failure_never_stops_the_other_language(self):
        # TH3-02 (OPS2-05's GATE path): the FIRST language fails its sanity
        # gate; the second is still published and named, the outputs are
        # written before the exit, and the exit is GATE_FAILED (2), not 1.
        import rowtalk_packs.__main__ as cli
        run, gh, _ = self.dict_env()
        self.cfg["dict"]["languages"]["en"]["first_edition"] = {
            "standard": {"min_articles": 10 ** 6}}
        code, out = run("dict-run", "--langs", "en,pt", *self.work())
        self.assertEqual(code, cli.GATE_FAILED)
        self.assertEqual((out["changed"], out["skipped"]), ("true", "en"))
        self.assertEqual(json.loads(out["published"]), ["dict-pt-v1.1.0"])
        self.assertEqual(set(self.served()), {"dict-pt"})
        self.assertIsNone(gh.find_release("dict-en-2025-edition"))

    def test_dict_run_never_undoes_a_dict_rollback(self):
        # TH3-03: the dict twin of the wiki rollback guard. Publish 2024,
        # publish 2025, roll dict-en back to 2024: the next run (upstream
        # still 2025) must not delete 2025 as an orphan and re-point to it.
        # DC4-03: with `prune` after each publish, exactly as build-dict.yml
        # runs it — the state the workflow actually reaches. Retention keeps
        # the family's previous edition, so the rollback target exists.
        run, gh, server = self.dict_env()
        self.set_en_upstream(gh, server, "2024")
        self.assertEqual(run("dict-run", "--langs", "en", *self.work())[0], 0)
        self.assertEqual(run("prune")[0], 0)
        self.set_en_upstream(gh, server, "2025")
        self.assertEqual(run("dict-run", "--langs", "en", *self.work())[0], 0)
        self.assertEqual(run("prune")[0], 0)
        self.assertEqual(self.served()["dict-en"], ("dict-en-2025-edition", 2025030100))
        self.assertIsNotNone(gh.find_release("dict-en-2024-edition"))
        self.assertEqual(run("rollback", "--tag", "dict-en-2024-edition", "--families",
                             "dict-en")[0], 0)
        # OPS4-02: a rollback serves a LOWER version than the abandoned one.
        # Installed apps that compare versions ("same or older: nothing
        # changes") keep the abandoned edition — the documented contract is
        # that a manifest naming a different edition is the update (SCHEMA
        # "Updates and rollbacks"); this pins the relation it must handle.
        self.assertEqual(self.served()["dict-en"], ("dict-en-2024-edition", 2024030100))
        self.assertEqual(run("prune")[0], 0)  # a prune after the rollback keeps both
        self.assertIsNotNone(gh.find_release("dict-en-2025-edition"))
        gh.calls.clear()
        code, out = run("dict-run", "--langs", "en", *self.work())
        self.assertEqual((code, out["changed"]), (0, "false"))
        self.assertFalse([c for c in gh.calls if isinstance(c, tuple) and c[0] == "delete"])
        self.assertEqual(self.served()["dict-en"][0], "dict-en-2024-edition")
        self.assertIsNotNone(gh.find_release("dict-en-2025-edition"))
        # Only --force rebuilds the abandoned edition (lead decision 3).
        code, out = run("dict-run", "--langs", "en", "--force", *self.work())
        self.assertEqual((code, out["changed"]), (0, "true"))
        self.assertIn(("delete", "dict-en-2025-edition"), gh.calls)

    def assert_dict_en_releases(self, gh, expected):
        self.assertEqual(sorted(r["tag_name"] for r in gh.releases.values()
                                if r["tag_name"].startswith("dict-en-")), sorted(expected))

    def test_rebuild_then_rollback_reaches_the_edition_served_before(self):
        # OPS5-01/DC5-01: README's recovery order, with `prune` after every
        # step as build-dict.yml runs it: a rebuild that reproduces the bad
        # upstream content must leave the previous upstream edition in place,
        # so the rollback to it succeeds.
        run, gh, server = self.dict_env()
        for year in ("2024", "2025"):
            self.set_en_upstream(gh, server, year)
            self.assertEqual(run("dict-run", "--langs", "en", *self.work())[0], 0)
            self.assertEqual(run("prune")[0], 0)
        self.assertEqual(run("dict-run", "--langs", "en", "--rebuild", *self.work())[0], 0)
        self.assertEqual(run("prune")[0], 0)
        self.assertEqual(self.served()["dict-en"], ("dict-en-2025-edition-r2", 2025030101))
        self.assert_dict_en_releases(gh, ["dict-en-2024-edition", "dict-en-2025-edition-r2"])
        self.assertEqual(run("rollback", "--tag", "dict-en-2024-edition", "--families",
                             "dict-en")[0], 0)
        self.assertEqual(self.served()["dict-en"][0], "dict-en-2024-edition")

    def rollback_then(self, step):
        # DC5-01/R5-TH-01: 2024 good, 2025 bad -> rollback to 2024. Then
        # `step` (a fixed rebuild of 2025 as r2, or a new upstream 2026) is
        # published and pruned: 2024 must survive as the rollback target,
        # not the abandoned 2025.
        run, gh, server = self.dict_env()
        server.httpd.files["/dl/en-2026.xml.gz"] = gzip.compress(LMF.encode("utf-8"))
        for year in ("2024", "2025"):
            self.set_en_upstream(gh, server, year)
            self.assertEqual(run("dict-run", "--langs", "en", *self.work())[0], 0)
            self.assertEqual(run("prune")[0], 0)
        self.assertEqual(run("rollback", "--tag", "dict-en-2024-edition", "--families",
                             "dict-en")[0], 0)
        self.assertEqual(run("prune")[0], 0)
        if step == "rebuild":
            argv, current = ("dict-run", "--langs", "en", "--rebuild"), "dict-en-2025-edition-r2"
        else:
            self.set_en_upstream(gh, server, "2026")
            argv, current = ("dict-run", "--langs", "en"), "dict-en-2026-edition"
        self.assertEqual(run(*argv, *self.work())[0], 0)
        self.assertEqual(run("prune")[0], 0)
        self.assertEqual(self.served()["dict-en"][0], current)
        self.assert_dict_en_releases(gh, ["dict-en-2024-edition", current])
        self.assertEqual(run("rollback", "--tag", "dict-en-2024-edition", "--families",
                             "dict-en")[0], 0)

    def test_after_a_rollback_a_rebuild_keeps_the_good_edition(self):
        self.rollback_then("rebuild")

    def test_after_a_rollback_a_new_upstream_keeps_the_good_edition(self):
        self.rollback_then("2026")

    def test_force_rebuilds_the_newest_revision_never_revision_one(self):
        # TH4-04: publish 2024, publish 2025, rebuild (r2), roll back to 2024
        # (r2 abandoned; r1 is still a release — no prune ran). A forced run
        # re-mints the NEWEST revision with its own version, never r1.
        run, gh, server = self.dict_env()
        for year in ("2024", "2025"):
            self.set_en_upstream(gh, server, year)
            self.assertEqual(run("dict-run", "--langs", "en", *self.work())[0], 0)
        self.assertEqual(run("dict-run", "--langs", "en", "--rebuild", *self.work())[0], 0)
        self.assertEqual(run("rollback", "--tag", "dict-en-2024-edition", "--families",
                             "dict-en")[0], 0)
        r1 = gh.find_release("dict-en-2025-edition")["id"]
        gh.calls.clear()
        code, out = run("dict-run", "--langs", "en", "--force", *self.work())
        self.assertEqual((code, out["changed"]), (0, "true"))
        self.assertEqual(self.served()["dict-en"], ("dict-en-2025-edition-r2", 2025030101))
        self.assertIn(("delete", "dict-en-2025-edition-r2"), gh.calls)
        self.assertNotIn(("delete", "dict-en-2025-edition"), gh.calls)
        self.assertEqual(gh.find_release("dict-en-2025-edition")["id"], r1)

    def test_dict_rebuild_publishes_a_new_revision_and_force_never_replaces(self):
        # DC3-03: a dictionary whose upstream never changes is not stuck on a
        # bad build. --rebuild mints <tag>-r2 with a HIGHER version (what an
        # app ordering editions by version needs); the published tag is
        # untouched; the gate still runs.
        run, gh, _ = self.dict_env()
        self.assertEqual(run("dict-run", "--langs", "en", *self.work())[0], 0)
        first = gh.find_release("dict-en-2025-edition")
        self.assertEqual(self.served()["dict-en"], ("dict-en-2025-edition", 2025030100))
        # force alone cannot replace a named edition, and says so — a visible
        # warning naming the way out, not a silent no-op (no write call).
        import contextlib
        gh.calls.clear()
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            code, out = run("dict-run", "--langs", "en", "--force", *self.work())
        self.assertEqual((code, out["changed"]), (0, "false"))
        self.assertRegex(said.getvalue(), r"(?m)^::warning::dict en: .*rebuild=true")
        self.assertFalse([c for c in gh.calls if isinstance(c, tuple)])
        # A rebuild that fails its gate publishes nothing.
        self.cfg["dict"]["languages"]["en"]["first_edition"] = {
            "standard": {"min_articles": 10 ** 6}}
        self.cfg["sanity"]["tolerance"] = 0.0
        self.cfg["dict"]["languages"]["en"]["golden"] = [
            {"mode": "exact", "query": "zzz", "expect_title": "zzz", "within": 1}]
        import rowtalk_packs.__main__ as cli
        code, out = run("dict-run", "--langs", "en", "--rebuild", *self.work())
        self.assertEqual((code, out["changed"]), (cli.GATE_FAILED, "false"))
        self.assertEqual(self.served()["dict-en"][0], "dict-en-2025-edition")
        # A passing rebuild: revision 2, then revision 3.
        self.cfg["dict"]["languages"]["en"]["golden"] = []
        for revision in (2, 3):
            code, out = run("dict-run", "--langs", "en", "--rebuild", *self.work())
            self.assertEqual((code, out["changed"]), (0, "true"), revision)
            tag = "dict-en-2025-edition-r%d" % revision
            self.assertEqual(self.served()["dict-en"], (tag, 2025030100 + revision - 1))
            self.assertFalse(gh.find_release(tag)["draft"])
        self.assertEqual(gh.find_release("dict-en-2025-edition")["id"], first["id"])
        self.assertFalse([c for c in gh.calls if isinstance(c, tuple) and c[0] == "delete"])
        # And the next scheduled run sees the upstream as published.
        code, out = run("dict-run", "--langs", "en", *self.work())
        self.assertEqual((code, out["changed"]), (0, "false"))

    def test_revision_helpers(self):
        base = "dict-en-2025-edition"
        self.assertEqual(dictbuild.revision_tag(base, 1), base)
        self.assertEqual(dictbuild.revision_tag(base, 12), base + "-r12")
        self.assertEqual(dictbuild.revision_of(base + "-r12", base), 12)
        self.assertIsNone(dictbuild.revision_of(base + "-r1", base))
        self.assertIsNone(dictbuild.revision_of(base + "-rx", base))
        self.assertIsNone(dictbuild.revision_of("dict-en-2024-edition", base))
        self.assertEqual(dictbuild.dict_version("20250301", 1), 2025030100)
        self.assertGreater(dictbuild.dict_version("20250301", dictbuild.REVISION_MAX),
                           dictbuild.dict_version("20250301", 1))
        self.assertLess(dictbuild.dict_version("20250301", dictbuild.REVISION_MAX),
                        dictbuild.dict_version("20250302", 1))
        for bad in (0, dictbuild.REVISION_MAX + 1):
            with self.assertRaises(ValueError):
                dictbuild.revision_tag(base, bad)

    def test_dict_tag(self):
        self.assertEqual(dictbuild.dict_tag("en", "2025-edition"), "dict-en-2025-edition")
        self.assertEqual(dictbuild.dict_tag("pt", "v1.1.0"), "dict-pt-v1.1.0")
        self.assertEqual(dictbuild.dict_tag("pt", "v1 (x)"), "dict-pt-v1--x-")


if __name__ == "__main__":
    unittest.main()
