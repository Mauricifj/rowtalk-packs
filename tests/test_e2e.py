"""End-to-end: a synthetic multi-shard dump served over HTTP on 127.0.0.1
(with dropped connections and a server that ignores Range once), through
resolve -> build both tiers per language -> validate -> gate -> upload ->
verify -> publish -> manifest, with the GitHub calls behind the fake seam."""

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest

import rowtalk_packs.__main__ as cli
from rowtalk_packs import editions, manifest, packdb, publish, validate
from tests.helpers import (EN_TITLES, PT_TITLES, DumpServer, FakeGitHub, synthetic_docs,
                           test_config, write_dump_tree)

BASE = "/other/cirrus_search_index/"


def read_outputs(path):
    out = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            key, _, value = line.rstrip("\n").partition("=")
            out[key] = value
    return out


def rows(db_path, sql):
    db = sqlite3.connect(db_path)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.files = {}
        cls.pt_docs = synthetic_docs("pt", 150, PT_TITLES)
        cls.en_docs = synthetic_docs("en", 180, EN_TITLES)
        write_dump_tree(cls.files, BASE, "20261011", "ptwiki", cls.pt_docs, 1)
        write_dump_tree(cls.files, BASE, "20261011", "enwiki", cls.en_docs, 1)
        for date in ("20261018", "20261025"):
            # Multi-stream shards, so a drop mid-shard lands AFTER some docs
            # were decompressed and staged (a single bz2 block yields nothing
            # until complete, which would make the restart path vacuous).
            write_dump_tree(cls.files, BASE, date, "ptwiki", cls.pt_docs, 3, streams=4)
            write_dump_tree(cls.files, BASE, date, "enwiki", cls.en_docs, 3, streams=4,
                            success=(date == "20261018"), action_ids=(date == "20261018"))
        en_shard = BASE + "20261018/index_name=enwiki_content/enwiki_content-20261018-00001.json.bz2"
        pt_shard = BASE + "20261018/index_name=ptwiki_content/ptwiki_content-20261018-00002.json.bz2"
        cls.en_shard, cls.pt_shard = en_shard, pt_shard
        drops = {en_shard: [len(cls.files[en_shard]) // 3, 50],
                 pt_shard: [len(cls.files[pt_shard]) // 2]}
        cls.server = DumpServer(cls.files, drops=drops, ignore_range=[pt_shard]).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__(None, None, None)
        shutil.rmtree(cls.dir)

    def setUp(self):
        # The server is shared by the class, but its SCRIPT (drops are keyed
        # by per-path request count; ignore_range is consumed on use) must
        # restart for every test, or the tests depend on their run order.
        with self.server.state["lock"]:
            self.server.state.update(counts={}, requests=[], active=0, max_active=0,
                                     hold_timeouts=0,
                                     ignore_range={self.pt_shard}, hold_requests=0,
                                     hold_until=2, hold_linger_s=0.0)
        self.work = tempfile.mkdtemp(dir=self.dir)
        self.repo = os.path.join(self.work, "repo")
        os.makedirs(self.repo)
        manifest.write(os.path.join(self.repo, "manifest.json"), manifest.empty())
        cfg = test_config()
        cfg["dump"]["base_url"] = self.server.url(BASE)
        self.cfg = cfg
        self.cfg_path = os.path.join(self.work, "packs.json")
        with open(self.cfg_path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle)
        self.gh = FakeGitHub(self.work, self.repo)
        self._orig_gh = cli._gh
        cli._gh = lambda cfg, args: self.gh

    def tearDown(self):
        cli._gh = self._orig_gh

    def run_cli(self, *argv):
        return cli.main(["--config", self.cfg_path, "--repo-root", self.repo] + list(argv))

    def test_full_pipeline(self):
        out_file = os.path.join(self.work, "gh_output")
        self.assertEqual(self.run_cli("resolve-wiki", "--github-output", out_file), 0)
        outputs = read_outputs(out_file)
        self.assertEqual(outputs["build"], "true")
        self.assertEqual(outputs["date"], "20261018")  # 20261025 lacks enwiki _SUCCESS
        tag = outputs["tag"]
        self.assertEqual(tag, "wiki-2026.10.18")
        release_id = outputs["release_id"]
        self.assertTrue(self.gh.get_release(release_id)["draft"])
        requests_before = len(self.server.state["requests"])
        outs = {}
        per_job = self.cfg["dump"]["connections_per_job"]
        for lang in json.loads(outputs["langs"]):
            outs[lang] = os.path.join(self.work, "out-%s" % lang)
            # TH2-03: hold the first `per_job` shard GETs until all `per_job`
            # are open (a condition: a correct client always gets there),
            # then linger up to 0.5 s for one more; an extra downloader would
            # be seen during the linger. TH3-01: the hold is SHORTER than the
            # client's socket timeout and must never time out — a serial
            # client would otherwise time out, reconnect while the first
            # handler is still parked, and fake max_active == 2.
            hold_s = self.cfg["dump"]["socket_timeout_s"] - 2
            self.assertGreater(hold_s, 0)
            with self.server.state["lock"]:
                self.server.state.update(max_active=0, hold_requests=per_job, hold_timeouts=0,
                                         hold_until=per_job, hold_s=hold_s, hold_linger_s=0.5)
            code = self.run_cli("build-wiki", "--lang", lang, "--date", outputs["date"],
                                "--out", outs[lang], "--release-id", release_id, "--keep-db")
            self.assertEqual(code, 0)
            # I3: exactly dump.connections_per_job connections, no more —
            # and reached by the client, not by a timed-out hold.
            self.assertEqual(self.server.state["hold_timeouts"], 0, lang)
            self.assertEqual(self.server.state["max_active"], per_job, lang)
        build_requests = self.server.state["requests"][requests_before:]
        self.assertTrue(all(ua == self.cfg["user_agent"] for _, _, ua in build_requests))
        # Range resume and a full restart both happened.
        self.assertTrue(any(p.endswith(os.path.basename(self.en_shard)) and r
                            for p, r, _ in build_requests))
        self.assertGreaterEqual(self.server.state["counts"][self.pt_shard], 2)
        # TH-08: the resume really resumed (a broken resume would be caught
        # by the bz2 check and re-downloaded whole, i.e. corrupt_rereads > 0,
        # with packs still identical — so assert the counters).
        build = {}
        for lang in outs:
            with open(os.path.join(outs[lang], "wiki-%s-essential.json" % lang), "r",
                      encoding="utf-8") as handle:
                build[lang] = json.load(handle)["build"]
        self.assertEqual((build["en"]["resumes"], build["en"]["restarts"],
                          build["en"]["corrupt_rereads"]), (2, 0, 0))
        self.assertEqual((build["pt"]["resumes"], build["pt"]["restarts"],
                          build["pt"]["corrupt_rereads"]), (0, 1, 0))

        self.assertEqual(self.run_cli("publish-wiki", "--release-id", release_id,
                                      "--langs", "pt,en"), 0)
        self.assertFalse(self.gh.get_release(release_id)["draft"])
        data = manifest.load(os.path.join(self.repo, "manifest.json"))
        self.assertEqual(data["manifest_version"], 2)
        self.assertEqual(sorted(e["id"] for e in data["packs"]),
                         ["wiki-en-essential", "wiki-en-extended", "wiki-pt-essential",
                          "wiki-pt-extended"])
        for entry in data["packs"]:
            self.assertEqual(entry["url"], "https://github.com/Mauricifj/rowtalk-packs/releases/"
                                           "download/%s/%s.db.gz" % (tag, entry["id"]))
            blob = self.gh.asset_bytes(tag, "%s.db.gz" % entry["id"])
            self.assertEqual(len(blob), entry["sizeBytes"])
            self.assertEqual(hashlib.sha256(blob).hexdigest(), entry["sha256"])
            installed = gzip.decompress(blob)
            self.assertEqual(len(installed), entry["installed_bytes"])
            self.assertEqual(hashlib.sha256(installed).hexdigest(), entry["installed_sha256"])
            self.assertEqual(entry["encoding"], "gzip")
            self.assertEqual(entry["version"], 20261018)
            self.assertEqual(entry["license"], "CC BY-SA 4.0")
        by_id = {e["id"]: e for e in data["packs"]}
        self.assertEqual(by_id["wiki-pt-extended"]["article_count"], 120)
        self.assertEqual(by_id["wiki-pt-essential"]["article_count"], 30)
        for lang in ("pt", "en"):
            log = editions.read(os.path.join(self.repo, "editions"), "wiki-%s" % lang)
            self.assertEqual(log[-1]["tag"], tag)

        for lang in ("pt", "en"):
            ess = os.path.join(outs[lang], "wiki-%s-essential.db" % lang)
            ext = os.path.join(outs[lang], "wiki-%s-extended.db" % lang)
            for path in (ess, ext):
                _, errors = validate.validate_pack(path, "wiki")
                self.assertEqual(errors, [], path)
            # Extended ⊇ Essential, and Essential is exactly its prefix (the
            # fixture's link order runs opposite to popularity, so a tier
            # ranked differently cannot pass — TH-07).
            ess_rows = rows(ess, "SELECT rank, page_id, title, abstract FROM articles ORDER BY rank")
            ext_rows = rows(ext, "SELECT rank, page_id, title, abstract FROM articles ORDER BY rank")
            self.assertEqual(ess_rows, ext_rows[:len(ess_rows)])
            self.assertEqual(ess_rows[0][2], PT_TITLES[0] if lang == "pt" else EN_TITLES[0])
            self.assertEqual(rows(ess, "SELECT value FROM meta WHERE key = 'ranking'"),
                             [("popularity_score",)])
            self.assertEqual(rows(ext, "SELECT value FROM meta WHERE key = 'ranking'"),
                             [("popularity_score",)])
            titles = {r[2] for r in ext_rows}
            for excluded in ("Talk page", "Empty lead", "Redirect page"):
                self.assertNotIn(excluded, titles)
            self.assertFalse(any(t.startswith("Mercury") for t in titles))
        db = packdb.open_pack(os.path.join(outs["en"], "wiki-en-essential.db"))
        self.assertEqual(packdb.lookup_article(db, "usa")[0], "United States")  # alias
        self.assertEqual(packdb.lookup_article(db, "U.S.A.")[0], "United States")
        self.assertIn("Rio de Janeiro", packdb.suggest(db, "wiki", "rio de j"))
        db.close()
        db = packdb.open_pack(os.path.join(outs["pt"], "wiki-pt-extended.db"))
        self.assertEqual(packdb.suggest(db, "wiki", "SAO")[0], "São Paulo")
        db.close()

        # The restarted / resumed shards produced exactly what a clean read does.
        clean = DumpServer(dict(self.files)).__enter__()
        try:
            cfg = dict(self.cfg)
            cfg["dump"] = dict(self.cfg["dump"], base_url=clean.url(BASE))
            clean_cfg = os.path.join(self.work, "clean.json")
            with open(clean_cfg, "w", encoding="utf-8") as handle:
                json.dump(cfg, handle)
            for lang in ("pt", "en"):
                clean_out = os.path.join(self.work, "clean-%s" % lang)
                self.assertEqual(cli.main(["--config", clean_cfg, "--repo-root", self.repo,
                                           "build-wiki", "--lang", lang, "--date", "20261018",
                                           "--out", clean_out, "--keep-db", "--force"]), 0)
                for tier in ("essential", "extended"):
                    name = "wiki-%s-%s.db" % (lang, tier)
                    for sql in ("SELECT * FROM articles ORDER BY rank",
                                "SELECT * FROM search ORDER BY key, rank",
                                "SELECT * FROM prefix_top ORDER BY prefix"):
                        self.assertEqual(rows(os.path.join(outs[lang], name), sql),
                                         rows(os.path.join(clean_out, name), sql), (name, sql))
        finally:
            clean.__exit__(None, None, None)

        # A second scheduled resolve finds nothing new.
        out_file2 = os.path.join(self.work, "gh_output2")
        self.assertEqual(self.run_cli("resolve-wiki", "--github-output", out_file2), 0)
        self.assertEqual(read_outputs(out_file2)["build"], "false")
        self.assertEqual(read_outputs(out_file2)["reason"], "already-published")

        # An OLDER complete dump never moves the manifest backwards...
        out_file3 = os.path.join(self.work, "gh_output3")
        self.assertEqual(self.run_cli("resolve-wiki", "--date", "20261011",
                                      "--github-output", out_file3), 0)
        self.assertEqual(read_outputs(out_file3)["reason"], "not-newer")
        # ...unless forced deliberately.
        out_file4 = os.path.join(self.work, "gh_output4")
        self.assertEqual(self.run_cli("resolve-wiki", "--date", "20261011", "--force",
                                      "--dry-run", "--github-output", out_file4), 0)
        self.assertEqual(read_outputs(out_file4)["build"], "true")

        # Retention keeps the only (named) release.
        self.assertEqual(self.run_cli("prune"), 0)
        self.assertIsNotNone(self.gh.find_release(tag))

    def test_gate_failure_uploads_nothing_and_cleanup_removes_the_draft(self):
        cfg = test_config()
        cfg["dump"]["base_url"] = self.server.url(BASE)
        cfg["wiki"]["languages"]["pt"]["golden"] = [
            {"mode": "exact", "query": "atlantida", "expect_title": "Atlântida", "within": 1}]
        with open(self.cfg_path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle)
        out_file = os.path.join(self.work, "gh_output")
        self.assertEqual(self.run_cli("resolve-wiki", "--langs", "pt", "--github-output", out_file), 0)
        outputs = read_outputs(out_file)
        code = self.run_cli("build-wiki", "--lang", "pt", "--date", outputs["date"], "--out",
                            os.path.join(self.work, "out"), "--release-id", outputs["release_id"])
        self.assertEqual(code, cli.GATE_FAILED)
        self.assertFalse([c for c in self.gh.calls if isinstance(c, tuple) and c[0] == "upload"])
        manifest_path = os.path.join(self.repo, "manifest.json")
        with open(manifest_path, "rb") as handle:
            manifest_before = handle.read()
        with self.assertRaises(Exception):
            self.run_cli("publish-wiki", "--release-id", outputs["release_id"], "--langs", "pt")
        with open(manifest_path, "rb") as handle:
            self.assertEqual(handle.read(), manifest_before)
        self.assertEqual(self.run_cli("cleanup", "--release-ids", outputs["release_id"]), 0)
        self.assertIsNone(self.gh.find_release(outputs["tag"]))

    def build_pt_report(self, out_name):
        out_file = os.path.join(self.work, "gh_out_%s" % out_name)
        self.assertEqual(self.run_cli("resolve-wiki", "--langs", "pt", "--github-output",
                                      out_file), 0)
        outputs = read_outputs(out_file)
        out = os.path.join(self.work, out_name)
        code = self.run_cli("build-wiki", "--lang", "pt", "--date", outputs["date"], "--out", out,
                            "--release-id", outputs["release_id"])
        with open(os.path.join(out, "wiki-pt-essential.json"), "r", encoding="utf-8") as handle:
            return code, json.load(handle)

    def test_build_gate_reads_the_previous_edition(self):
        # TH2-01: the BUILD-time gate must receive the previous edition's
        # stats from the editions log (a None there silently downgrades every
        # edition to the absolute first-edition floors).
        editions.append(os.path.join(self.repo, "editions"), "wiki-pt", "publish",
                        "wiki-2026.10.04", "2026.10.04", {
                            "wiki-pt-essential": {"article_count": 60, "installed_bytes": 1,
                                                  "download_bytes": 1, "sha256": "x",
                                                  "installed_sha256": "y"},
                            "wiki-pt-extended": {"article_count": 240, "installed_bytes": 1,
                                                 "download_bytes": 1, "sha256": "x",
                                                 "installed_sha256": "y"}})
        code, report = self.build_pt_report("drift")
        self.assertEqual(code, cli.GATE_FAILED)
        failed = {c["name"] for c in report["gate"]["checks"] if not c["ok"]}
        self.assertIn("drift:article_count", failed)
        self.assertFalse([c for c in report["gate"]["checks"] if c["name"].startswith("first:")])
        self.assertFalse([c for c in self.gh.calls if isinstance(c, tuple) and c[0] == "upload"])

    def test_build_gate_blocks_a_pack_that_fails_validation(self):
        # TH2-01: the BUILD-time gate must see validate_pack's errors (not
        # only the publish-time copy): nothing is uploaded.
        original = validate.validate_pack

        def broken(path, kind, expect=None):
            meta, errors = original(path, kind, expect=expect)
            return meta, errors + ["injected: articles.rank has a gap"]

        validate.validate_pack = broken
        try:
            code, report = self.build_pt_report("invalid")
        finally:
            validate.validate_pack = original
        self.assertEqual(code, cli.GATE_FAILED)
        self.assertIn("injected: articles.rank has a gap", report["validation_errors"])
        self.assertFalse(next(c for c in report["gate"]["checks"]
                              if c["name"] == "validation")["ok"])
        self.assertFalse([c for c in self.gh.calls if isinstance(c, tuple) and c[0] == "upload"])

    def test_probe_report(self):
        summary = os.path.join(self.work, "summary.md")
        # Force the two shard GETs to overlap, so the report's concurrency
        # line is deterministic (OPS2-03): each is held until BOTH are open —
        # a condition, not a duration (TH4-01: the hold no longer misses its
        # wakeup and burns hold_s). hold_s stays below the client's socket
        # timeout and must never expire (TH3-01: a serial client's
        # timeout-retry would otherwise fake the overlap).
        hold_s = self.cfg["dump"]["socket_timeout_s"] - 2
        self.assertGreater(hold_s, 0)
        with self.server.state["lock"]:
            self.server.state.update(hold_requests=2, hold_until=2, hold_s=hold_s)
        self.assertEqual(self.run_cli("probe", "--wiki", "ptwiki", "--shards", "2",
                                      "--summary", summary), 0)
        self.assertEqual(self.server.state["hold_timeouts"], 0)
        with open(summary, "r", encoding="utf-8") as handle:
            text = handle.read()
        for section in ("## Line shape", "## Key presence", "## popularity_score",
                        "## opening_text length", "## Redirects", "## Namespaces",
                        "## Disambiguation signals", "## Throughput", "## Dump directory",
                        "## Extrapolated pack sizes"):
            self.assertIn(section, text)
        self.assertIn("Shards probed: 2 of 3", text)
        self.assertIn("## Time budget (go/no-go for a full build)", text)
        measured = re.search(r"one prune at keep=120 measured: (\S+) s", text)
        self.assertIsNotNone(measured, "the prune cost at the real keep must be measured")
        self.assertGreater(float(measured.group(1)), 0.0)
        self.assertRegex(text, r"Verdict: \*\*(GO|MARGINAL|NO-GO)\*\*")
        self.assertNotIn("Extrapolated staging time", text)
        self.assertIn("| action lines (bulk format) |", text)
        self.assertNotIn("is a place. It has people", text)  # never article text
        self.assertIn("Concurrency observed: the network figure is the measured rate of 2 "
                      "overlapping downloads.", text)

    def resolve_and_build(self, build_langs):
        out_file = os.path.join(self.work, "gh_output_rb")
        self.assertEqual(self.run_cli("resolve-wiki", "--github-output", out_file), 0)
        outputs = read_outputs(out_file)
        for lang in build_langs:
            self.assertEqual(self.run_cli("build-wiki", "--lang", lang, "--date", outputs["date"],
                                          "--out", os.path.join(self.work, "out-%s" % lang),
                                          "--release-id", outputs["release_id"]), 0)
        return outputs

    def test_a_failed_language_does_not_block_the_other(self):
        # OPS-3: en's build died (nothing uploaded); pt is still published.
        outputs = self.resolve_and_build(["pt"])
        out_file = os.path.join(self.work, "gh_output_pub")
        self.assertEqual(self.run_cli("publish-wiki", "--release-id", outputs["release_id"],
                                      "--langs", "pt,en", "--github-output", out_file), 0)
        self.assertEqual(read_outputs(out_file)["skipped"], "en")
        data = manifest.load(os.path.join(self.repo, "manifest.json"))
        self.assertEqual(sorted(e["id"] for e in data["packs"]),
                         ["wiki-pt-essential", "wiki-pt-extended"])
        self.assertFalse(self.gh.get_release(outputs["release_id"])["draft"])

    def test_a_language_failing_verification_is_skipped_alone(self):
        outputs = self.resolve_and_build(["pt", "en"])
        self.gh.corrupt_digest.add("wiki-en-extended.db.gz")
        out_file = os.path.join(self.work, "gh_output_pub")
        self.assertEqual(self.run_cli("publish-wiki", "--release-id", outputs["release_id"],
                                      "--langs", "pt,en", "--github-output", out_file), 0)
        self.assertEqual(read_outputs(out_file)["skipped"], "en")
        ids = sorted(e["id"] for e in manifest.load(os.path.join(self.repo, "manifest.json"))["packs"])
        self.assertEqual(ids, ["wiki-pt-essential", "wiki-pt-extended"])

    def test_nothing_complete_publishes_nothing(self):
        outputs = self.resolve_and_build([])
        manifest_path = os.path.join(self.repo, "manifest.json")
        with open(manifest_path, "rb") as handle:
            before = handle.read()
        with self.assertRaises(Exception):
            self.run_cli("publish-wiki", "--release-id", outputs["release_id"], "--langs", "pt,en")
        with open(manifest_path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertTrue(self.gh.get_release(outputs["release_id"])["draft"])

    def test_resolve_never_deletes_a_release_mains_manifest_names(self):
        # OPS-2: the dispatched ref's manifest (local) is older than main's.
        tag = "wiki-2026.10.18"
        named = self.gh.create_draft(tag, tag, "b")
        self.gh.publish_release(named["id"])
        main_root = os.path.join(self.work, "main")
        os.makedirs(main_root)
        manifest.write(os.path.join(main_root, "manifest.json"), {
            "manifest_version": 2, "packs": [
                {"id": "wiki-pt-essential", "family": "wiki-pt", "tag": tag,
                 "source": {"dump_date": "20261018"}}]})
        self.gh.main_root = main_root
        for extra in ((), ("--force",)):
            out_file = os.path.join(self.work, "gh_output_main%d" % len(extra))
            self.assertEqual(self.run_cli("resolve-wiki", "--date", "20261018",
                                          "--github-output", out_file, *extra), 0)
            outputs = read_outputs(out_file)
            self.assertEqual(outputs["build"], "false")
            self.assertEqual(outputs["reason"], "already-published")
        self.assertIsNotNone(self.gh.find_release(tag))
        self.assertFalse([c for c in self.gh.calls if isinstance(c, tuple) and c[0] == "delete"])

    def test_over_cap_connections_are_refused_before_any_dump_request(self):
        # TH2-03: the max_connections_per_run guard on the CLI path.
        before = len(self.server.state["requests"])
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("build-wiki", "--lang", "pt", "--date", "20261018", "--out",
                         os.path.join(self.work, "out"), "--connections",
                         str(self.cfg["dump"]["max_connections_per_run"] + 1))
        self.assertIn("exceeds dump.max_connections_per_run", str(caught.exception))
        self.assertEqual(len(self.server.state["requests"]), before)

    def test_build_with_a_deleted_draft_fails_before_downloading(self):
        # OPS-7: "Re-run failed jobs" reuses a release_id cleanup deleted.
        draft = self.gh.create_draft("wiki-2026.10.18", "x", "b")
        self.gh.delete_release(draft)
        published = self.gh.create_draft("wiki-2026.10.11", "x", "b")
        self.gh.publish_release(published["id"])
        before = len(self.server.state["requests"])
        for release_id in (draft["id"], published["id"]):
            with self.assertRaises(SystemExit) as caught:
                self.run_cli("build-wiki", "--lang", "pt", "--date", "20261018", "--out",
                             os.path.join(self.work, "out"), "--release-id", str(release_id))
            self.assertIn("re-run ALL jobs", str(caught.exception))
        self.assertEqual(len(self.server.state["requests"]), before)

    def publish_pt(self, date=None):
        out_file = os.path.join(self.work, "gh_out_pub_%s" % (date or "latest"))
        argv = ["resolve-wiki", "--langs", "pt", "--github-output", out_file]
        if date:
            argv += ["--date", date]
        self.assertEqual(self.run_cli(*argv), 0)
        outputs = read_outputs(out_file)
        self.assertEqual(outputs["build"], "true", outputs)
        self.assertEqual(self.run_cli("build-wiki", "--lang", "pt", "--date", outputs["date"],
                                      "--out", os.path.join(self.work, "out-%s" % outputs["date"]),
                                      "--release-id", outputs["release_id"]), 0)
        self.assertEqual(self.run_cli("publish-wiki", "--release-id", outputs["release_id"],
                                      "--langs", "pt"), 0)
        return outputs["tag"]

    def test_a_rollback_is_never_undone_by_the_next_resolve(self):
        # DC2-03/OPS2-02: publish A, publish B, roll back to A; the newest
        # complete dump is still B's. A scheduled resolve must not delete B
        # as an "orphan" and republish it.
        old = self.publish_pt("20261018")
        new = self.publish_pt()  # latest complete for pt alone: 20261025
        self.assertEqual((old, new), ("wiki-2026.10.18", "wiki-2026.10.25"))
        self.assertEqual(self.run_cli("rollback", "--tag", old, "--families", "wiki-pt"), 0)
        served = {e["tag"] for e in manifest.load(os.path.join(self.repo, "manifest.json"))["packs"]}
        self.assertEqual(served, {old})
        log = editions.read(os.path.join(self.repo, "editions"), "wiki-pt")
        self.assertEqual((log[-1]["action"], log[-1]["from_tags"]), ("rollback", [new]))
        self.gh.calls.clear()
        out_file = os.path.join(self.work, "gh_out_after")
        self.assertEqual(self.run_cli("resolve-wiki", "--langs", "pt", "--github-output",
                                      out_file), 0)
        outputs = read_outputs(out_file)
        self.assertEqual((outputs["build"], outputs["reason"], outputs["tag"]),
                         ("false", "rolled-back", new))
        self.assertFalse([c for c in self.gh.calls if isinstance(c, tuple)])  # no write call
        self.assertFalse(self.gh.find_release(new)["draft"])
        # resolve itself decides (a dry run never reaches prepare_release, so
        # this pins the resolve layer independently of the deletion-site one).
        out_file = os.path.join(self.work, "gh_out_after_dry")
        self.assertEqual(self.run_cli("resolve-wiki", "--langs", "pt", "--dry-run",
                                      "--github-output", out_file), 0)
        self.assertEqual(read_outputs(out_file)["reason"], "rolled-back")
        # The deletion site refuses on its own too (defence in depth).
        _, decision = publish.prepare_release(self.gh, new, {old}, "b",
                                              protected={new: "rolled-back"})
        self.assertEqual(decision, "rolled-back")
        self.assertIsNotNone(self.gh.find_release(new))
        # Only an explicit --force rebuilds the abandoned edition.
        out_file = os.path.join(self.work, "gh_out_forced")
        self.assertEqual(self.run_cli("resolve-wiki", "--langs", "pt", "--force",
                                      "--github-output", out_file), 0)
        self.assertEqual(read_outputs(out_file)["build"], "true")
        self.assertIn(("delete", new), self.gh.calls)

    def test_a_langs_subset_never_rebuilds_another_familys_abandoned_edition(self):
        # OPS3-01: a wiki tag is shared by every wiki family. pt rolls back
        # away from T; a later dispatch for en alone at T's date must not
        # delete T as an "orphan" (en's own log never recorded it).
        old = self.publish_pt("20261011")
        new = self.publish_pt("20261018")
        self.assertEqual(self.run_cli("rollback", "--tag", old, "--families", "wiki-pt"), 0)
        self.gh.calls.clear()
        out_file = os.path.join(self.work, "gh_out_subset")
        self.assertEqual(self.run_cli("resolve-wiki", "--langs", "en", "--date", "20261018",
                                      "--github-output", out_file), 0)
        outputs = read_outputs(out_file)
        self.assertEqual(outputs["build"], "false", outputs)
        self.assertEqual((outputs["reason"], outputs["tag"]), ("rolled-back", new))
        self.assertFalse([c for c in self.gh.calls if isinstance(c, tuple)])  # no write call
        self.assertFalse(self.gh.find_release(new)["draft"])

    def test_an_orphan_of_a_failed_commit_is_still_rebuilt(self):
        # The mirror image of the rollback guard: a published release that NO
        # committed log line records (its publish died before the commit) is
        # still deleted and rebuilt by the next run (OPS-5).
        orphan = self.gh.create_draft("wiki-2026.10.25", "x", "b")
        self.gh.publish_release(orphan["id"])
        out_file = os.path.join(self.work, "gh_out_orphan")
        self.assertEqual(self.run_cli("resolve-wiki", "--langs", "pt", "--github-output",
                                      out_file), 0)
        self.assertEqual(read_outputs(out_file)["build"], "true")
        self.assertIn(("delete", "wiki-2026.10.25"), self.gh.calls)

    def test_a_dry_run_rollback_writes_nothing(self):
        # OPS2-01: verify the target, show the change, write nothing.
        old = self.publish_pt("20261018")
        self.publish_pt()
        manifest_path = os.path.join(self.repo, "manifest.json")
        log_path = editions.log_path(os.path.join(self.repo, "editions"), "wiki-pt")
        with open(manifest_path, "rb") as h1, open(log_path, "rb") as h2:
            before = (h1.read(), h2.read())
        self.gh.calls.clear()
        self.assertEqual(self.run_cli("rollback", "--tag", old, "--families", "wiki-pt",
                                      "--dry-run"), 0)
        with open(manifest_path, "rb") as h1, open(log_path, "rb") as h2:
            self.assertEqual((h1.read(), h2.read()), before)
        self.assertFalse([c for c in self.gh.calls if isinstance(c, tuple)])
        # It still verifies: a target that is not a published release fails.
        with self.assertRaises(publish.PublishError):
            self.run_cli("rollback", "--tag", "wiki-1999.01.01", "--families", "wiki-pt",
                         "--dry-run")
        # TH3-04: and a REAL published target with a fault fails too — each
        # check of the real rollback runs in the dry run (size, digest, the
        # public URL), and still nothing is written.
        asset = "wiki-pt-essential.db.gz"
        faults = (("size", lambda: self.gh.size_offset.__setitem__(asset, 1),
                   lambda: self.gh.size_offset.pop(asset)),
                  ("digest", lambda: self.gh.corrupt_digest.add(asset),
                   lambda: self.gh.corrupt_digest.discard(asset)),
                  ("public url", lambda: self.gh.public_failures.__setitem__(asset, -1),
                   lambda: self.gh.public_failures.pop(asset)))
        for name, inject, clear in faults:
            inject()
            try:
                with self.assertRaises(publish.PublishError, msg=name):
                    self.run_cli("rollback", "--tag", old, "--families", "wiki-pt", "--dry-run")
            finally:
                clear()
            with open(manifest_path, "rb") as h1, open(log_path, "rb") as h2:
                self.assertEqual((h1.read(), h2.read()), before, name)
        # The same target, unfaulted, passes the dry run again.
        self.assertEqual(self.run_cli("rollback", "--tag", old, "--families", "wiki-pt",
                                      "--dry-run"), 0)
        self.assertFalse([c for c in self.gh.calls if isinstance(c, tuple)])

    def test_dry_run_creates_nothing(self):
        out_file = os.path.join(self.work, "gh_output")
        self.assertEqual(self.run_cli("resolve-wiki", "--dry-run", "--github-output", out_file), 0)
        self.assertEqual(read_outputs(out_file)["release_id"], "")
        self.assertEqual(self.gh.releases, {})


if __name__ == "__main__":
    unittest.main()
