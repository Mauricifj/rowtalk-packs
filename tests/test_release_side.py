"""Manifest generation, retention, and the publish orchestration over the
fake GitHub seam."""

import json
import os
import shutil
import tempfile
import unittest

from rowtalk_packs import editions, manifest, publish, retention
from tests.helpers import FakeGitHub, test_config


def report(pid, family, tag, kind="wiki", lang="pt", tier="essential", count=100, size=1000,
           ok=True):
    return {"pack_id": pid, "family": family, "kind": kind, "language": lang, "tier": tier,
            "edition": tag.split("-", 1)[1], "version": 20261025, "tag": tag,
            "asset": "%s.db.gz" % pid, "download_bytes": 0, "sha256": "",
            "installed_bytes": size, "installed_sha256": "i" * 64, "article_count": count,
            "key_count": count, "schema_version": 2, "fold_version": 1,
            "ranking": "popularity_score", "license": "CC BY-SA 4.0", "license_url": "u",
            "attribution": "Wikipedia contributors", "source": {"wiki": "ptwiki"},
            "validation_errors": [] if ok else ["broken"],
            "golden": [], "gate": {"passed": ok}}


URL = lambda tag, asset: "https://github.com/o/r/releases/download/%s/%s" % (tag, asset)  # noqa: E731


class ManifestTest(unittest.TestCase):
    def test_keeps_other_families_and_v1_types(self):
        current = {"manifest_version": 2, "packs": [
            {"id": "dict-en", "family": "dict-en", "kind": "dict", "language": "en", "tag": "dict-en-x"},
            {"id": "wiki-pt-essential", "family": "wiki-pt", "kind": "wiki", "language": "pt",
             "tag": "wiki-2026.09.27"}]}
        r = report("wiki-pt-essential", "wiki-pt", "wiki-2026.10.25")
        new = manifest.build_manifest(current, ["wiki-pt"], [r], {("wiki-2026.10.25", r["asset"])}, URL)
        ids = {e["id"]: e for e in new["packs"]}
        self.assertEqual(ids["dict-en"]["tag"], "dict-en-x")
        entry = ids["wiki-pt-essential"]
        self.assertEqual(entry["tag"], "wiki-2026.10.25")
        self.assertIsInstance(entry["version"], int)
        self.assertIsInstance(entry["sizeBytes"], int)
        self.assertEqual(entry["encoding"], "gzip")
        self.assertTrue(entry["url"].startswith("https://"))
        self.assertEqual(new["manifest_version"], 2)
        for key in ("id", "kind", "language", "version", "sizeBytes", "sha256", "url", "tier",
                    "edition", "schema_version", "fold_version", "installed_bytes",
                    "installed_sha256", "article_count", "license", "attribution", "source"):
            self.assertIn(key, entry)

    def test_never_names_an_unverified_asset(self):
        r = report("wiki-pt-essential", "wiki-pt", "wiki-2026.10.25")
        with self.assertRaises(manifest.ManifestError):
            manifest.build_manifest(manifest.empty(), ["wiki-pt"], [r], set(), URL)
        with self.assertRaises(manifest.ManifestError):
            manifest.build_manifest(manifest.empty(), ["wiki-pt"], [r],
                                    {("wiki-2026.10.18", r["asset"])}, URL)

    def test_partial_tier_publish_keeps_the_untouched_tier(self):
        # DC-01: replacement is by pack id. A tiers=essential publish replaces
        # wiki-pt-essential and must carry wiki-pt-extended over, untouched.
        current = {"packs": [
            {"id": "wiki-pt-essential", "family": "wiki-pt", "tag": "wiki-2026.10.04"},
            {"id": "wiki-pt-extended", "family": "wiki-pt", "tag": "wiki-2026.10.04"}]}
        r = report("wiki-pt-essential", "wiki-pt", "wiki-2026.10.25")
        new = manifest.build_manifest(current, ["wiki-pt"], [r], {(r["tag"], r["asset"])}, URL)
        self.assertEqual(sorted((e["id"], e["tag"]) for e in new["packs"]),
                         [("wiki-pt-essential", "wiki-2026.10.25"),
                          ("wiki-pt-extended", "wiki-2026.10.04")])
        self.assertEqual(manifest.named_tags(new), {"wiki-2026.10.25", "wiki-2026.10.04"})


class RetentionTest(unittest.TestCase):
    def rel(self, rid, tag, draft=False):
        return {"id": rid, "tag_name": tag, "draft": draft}

    def test_keeps_newest_three_and_named(self):
        releases = [self.rel(i, "wiki-2026.%02d.01" % i) for i in range(1, 8)]
        releases.append(self.rel(50, "dict-en-2024-edition"))
        releases.append(self.rel(51, "dict-en-2025-edition"))
        releases.append(self.rel(60, "v1.0"))  # not ours
        releases.append(self.rel(61, "wiki-2026.08.15", draft=True))
        releases.append(self.rel(62, "wiki-2026.08.22", draft=True))
        protected = {"wiki-2026.02.01", "dict-en-2025-edition"}
        doomed = {r["tag_name"] for r in retention.select_prunable(releases, protected, 3,
                                                                   keep_draft_id=62)}
        self.assertEqual(doomed, {"wiki-2026.01.01", "wiki-2026.03.01", "wiki-2026.04.01",
                                  "dict-en-2024-edition", "wiki-2026.08.15"})

    def test_newest_means_newest_tag_not_newest_release(self):
        # TH2-04: an older dump dispatched later (force) has a NEWER release id
        # but an OLDER tag. Retention keeps the three newest EDITIONS (tags).
        tags = ["wiki-2026.%02d.01" % month for month in range(12, 0, -1)]
        releases = [self.rel(rid, tag) for rid, tag in enumerate(tags, start=1)]
        # id 1 = wiki-2026.12.01 (oldest release, newest edition) ... id 12 = .01.01
        doomed = {r["tag_name"] for r in retention.select_prunable(releases, set(), 3)}
        kept = {r["tag_name"] for r in releases} - doomed
        self.assertEqual(kept, {"wiki-2026.12.01", "wiki-2026.11.01", "wiki-2026.10.01"})

    def test_each_dict_family_keeps_its_newest_good_recorded_previous_edition(self):
        # DC4-03: the rollback target of a dictionary survives the prune that
        # follows the publish replacing it. Per family; newest = highest id
        # (creation order: "v1.10.0" was published after "v1.9.0" although
        # it sorts first); an unrecorded orphan never takes the slot.
        # DC5-01/OPS5-01: a revision sibling of the named edition (the build
        # a rebuild replaced) never takes it from a good older edition.
        releases = [self.rel(1, "dict-pt-v1.9.0"), self.rel(2, "dict-pt-v1.10.0"),
                    self.rel(3, "dict-pt-v1.11.0"), self.rel(4, "dict-pt-v1.12.0"),
                    self.rel(5, "dict-en-2024-edition"), self.rel(6, "dict-en-2025-edition"),
                    self.rel(7, "dict-en-2025-edition-r2")]
        named = {"dict-pt-v1.11.0", "dict-en-2025-edition-r2"}
        recorded = {"dict-pt-v1.9.0", "dict-pt-v1.10.0", "dict-pt-v1.11.0",
                    "dict-en-2024-edition", "dict-en-2025-edition", "dict-en-2025-edition-r2"}
        doomed = {r["tag_name"] for r in retention.select_prunable(
            releases, named, 3, dict_keep_previous=1, recorded_tags=recorded)}
        # pt: v1.12.0 is newest but unrecorded (an orphan) -> pruned; v1.10.0
        # is the newest recorded unnamed -> kept; v1.9.0 -> pruned.
        # en: 2025-edition is newer but is the r1 that the named r2 replaced
        # (a sibling) -> pruned; 2024 (served before it) kept.
        self.assertEqual(doomed, {"dict-pt-v1.12.0", "dict-pt-v1.9.0",
                                  "dict-en-2025-edition"})
        # Without the keep (or without the log), the old rule: only named.
        doomed = {r["tag_name"] for r in retention.select_prunable(releases, named, 3)}
        self.assertEqual(doomed, {r["tag_name"] for r in releases} - named)

    def test_a_rolled_back_dict_edition_never_takes_the_slot_from_a_good_one(self):
        # DC5-01/OPS5-01: 2024 good, 2025 bad (rolled back from), 2026 now
        # named. The kept edition is 2024, although 2025 has the higher id;
        # a suspect edition is kept only when nothing else is recorded.
        releases = [self.rel(1, "dict-en-2024-edition"), self.rel(2, "dict-en-2025-edition"),
                    self.rel(3, "dict-en-2026-edition")]
        recorded = {"dict-en-2024-edition": "published-before",
                    "dict-en-2025-edition": "rolled-back",
                    "dict-en-2026-edition": "published-before"}
        doomed = [r["tag_name"] for r in retention.select_prunable(
            releases, {"dict-en-2026-edition"}, 3, dict_keep_previous=1,
            recorded_tags=recorded)]
        self.assertEqual(doomed, ["dict-en-2025-edition"])
        doomed = [r["tag_name"] for r in retention.select_prunable(
            releases[1:], {"dict-en-2026-edition"}, 3, dict_keep_previous=1,
            recorded_tags=recorded)]
        self.assertEqual(doomed, [])  # the only recorded one: kept, suspect or not

    def test_never_prunes_a_named_release(self):
        releases = [self.rel(i, "wiki-2026.%02d.01" % i) for i in range(1, 13)]
        named = {r["tag_name"] for r in releases}
        self.assertEqual(retention.select_prunable(releases, named, 3), [])


class PublishFlowTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.repo = os.path.join(self.dir, "repo")
        os.makedirs(self.repo)
        self.manifest_path = os.path.join(self.repo, "manifest.json")
        self.editions = os.path.join(self.repo, "editions")
        manifest.write(self.manifest_path, manifest.empty())
        self.gh = FakeGitHub(self.dir, self.repo)
        self.cfg = test_config()
        self.out = os.path.join(self.dir, "out")
        os.makedirs(self.out)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def stage(self, pid, tag, count=100, ok=True, golden_ok=True):
        gz = os.path.join(self.out, "%s.db.gz" % pid)
        with open(gz, "wb") as handle:
            handle.write(("payload of %s" % pid).encode())
        import hashlib
        r = report(pid, "wiki-pt", tag, count=count, ok=ok)
        r["download_bytes"] = os.path.getsize(gz)
        with open(gz, "rb") as handle:
            r["sha256"] = hashlib.sha256(handle.read()).hexdigest()
        r["tier"] = pid.rsplit("-", 1)[1]
        r["version"] = int(tag[len("wiki-"):].replace(".", ""))  # the dump date, as built
        r["golden"] = [{"query": "q", "mode": "exact", "expect": "Q", "within": 1,
                        "ok": golden_ok, "got": []}]
        with open(os.path.join(self.out, "%s.json" % pid), "w") as handle:
            json.dump(r, handle)
        return r

    def manifest_bytes(self):
        with open(self.manifest_path, "rb") as handle:
            return handle.read()

    def test_happy_path(self):
        release, decision = publish.prepare_release(self.gh, "wiki-2026.10.25", set(), "b")
        self.assertEqual(decision, "build")
        for pid in ("wiki-pt-essential", "wiki-pt-extended"):
            self.stage(pid, "wiki-2026.10.25", count=60000 if "ess" in pid else 300000)
            publish.upload_pack(self.gh, release["id"], self.out, pid)
        reports, verified = publish.verify_release(self.gh, release,
                                                   ["wiki-pt-essential", "wiki-pt-extended"],
                                                   self.cfg, self.editions)
        self.assertEqual(len(verified), 2)
        new = publish.publish_release(self.gh, self.gh.get_release(release["id"]), reports,
                                      verified, self.cfg, self.manifest_path, self.editions)
        self.assertFalse(self.gh.get_release(release["id"])["draft"])
        self.assertEqual(len(new["packs"]), 2)
        for entry in new["packs"]:
            self.assertTrue(self.gh.public_url_ok(entry["url"].replace(
                "Mauricifj/rowtalk-packs", "Mauricifj/rowtalk-packs")))
        log = editions.read(self.editions, "wiki-pt")
        self.assertEqual(log[-1]["action"], "publish")
        self.assertEqual(log[-1]["packs"]["wiki-pt-extended"]["article_count"], 300000)

    def verify_fails(self, mutate):
        release, _ = publish.prepare_release(self.gh, "wiki-2026.10.25", set(), "b")
        self.stage("wiki-pt-essential", "wiki-2026.10.25", count=60000)
        publish.upload_pack(self.gh, release["id"], self.out, "wiki-pt-essential")
        mutate(release)
        before = self.manifest_bytes()
        with self.assertRaises(publish.PublishError):
            # The publish-wiki sequence: verify, then publish what verified.
            reports, verified = publish.verify_release(self.gh, release, ["wiki-pt-essential"],
                                                       self.cfg, self.editions)
            publish.publish_release(self.gh, release, reports, verified, self.cfg,
                                    self.manifest_path, self.editions)
        self.assertEqual(self.manifest_bytes(), before)
        self.assertTrue(self.gh.get_release(release["id"])["draft"])
        self.assertNotIn("publish", [c[0] for c in self.gh.calls if isinstance(c, tuple)])

    def test_missing_asset_blocks(self):
        def drop(release):
            del self.gh.releases[release["id"]]["assets"]["wiki-pt-essential.db.gz"]
        self.verify_fails(drop)

    def test_size_mismatch_blocks(self):
        # Only the LISTED size disagrees; the digest is still right, so this
        # pins the size check itself (TH-09: growing the data also changed
        # the digest, which the digest check caught on its own).
        def grow(release):
            self.gh.size_offset["wiki-pt-essential.db.gz"] = 1
        self.verify_fails(grow)

    def test_digest_mismatch_blocks(self):
        def corrupt(release):
            self.gh.corrupt_digest.add("wiki-pt-essential.db.gz")
        self.verify_fails(corrupt)

    def test_gate_failure_blocks_unless_forced(self):
        release, _ = publish.prepare_release(self.gh, "wiki-2026.10.25", set(), "b")
        self.stage("wiki-pt-essential", "wiki-2026.10.25", count=60000, golden_ok=False)
        publish.upload_pack(self.gh, release["id"], self.out, "wiki-pt-essential")
        with self.assertRaises(publish.PublishError):
            publish.verify_release(self.gh, release, ["wiki-pt-essential"], self.cfg, self.editions)
        reports, verified = publish.verify_release(self.gh, release, ["wiki-pt-essential"],
                                                   self.cfg, self.editions, force=True)
        self.assertTrue(reports[0]["gate"]["forced"])
        publish.publish_release(self.gh, release, reports, verified, self.cfg, self.manifest_path,
                                self.editions)
        self.assertTrue(editions.read(self.editions, "wiki-pt")[-1]["forced"])

    def test_broken_pack_blocks_even_when_forced(self):
        release, _ = publish.prepare_release(self.gh, "wiki-2026.10.25", set(), "b")
        self.stage("wiki-pt-essential", "wiki-2026.10.25", count=60000, ok=False)
        publish.upload_pack(self.gh, release["id"], self.out, "wiki-pt-essential")
        with self.assertRaises(publish.PublishError):
            publish.verify_release(self.gh, release, ["wiki-pt-essential"], self.cfg,
                                   self.editions, force=True)

    def test_prepare_release_states(self):
        named_release = self.gh.create_draft("wiki-2026.10.18", "x", "b")
        self.gh.publish_release(named_release["id"])
        _, decision = publish.prepare_release(self.gh, "wiki-2026.10.18", {"wiki-2026.10.18"}, "b")
        self.assertEqual(decision, "already-published")
        self.assertIsNotNone(self.gh.find_release("wiki-2026.10.18"))
        orphan = self.gh.create_draft("wiki-2026.10.25", "x", "b")
        self.gh.publish_release(orphan["id"])
        fresh, decision = publish.prepare_release(self.gh, "wiki-2026.10.25", set(), "b")
        self.assertEqual(decision, "build")
        self.assertNotEqual(fresh["id"], orphan["id"])
        self.assertTrue(fresh["draft"])
        again, _ = publish.prepare_release(self.gh, "wiki-2026.10.25", set(), "b")
        self.assertNotIn(fresh["id"], self.gh.releases)
        self.assertIn(again["id"], self.gh.releases)

    def test_cleanup_never_deletes_a_named_release(self):
        draft = self.gh.create_draft("wiki-2026.10.25", "x", "b")
        self.assertEqual(publish.cleanup_run(self.gh, draft["id"], manifest.empty()), "deleted-draft")
        named = self.gh.create_draft("wiki-2026.10.18", "x", "b")
        self.gh.publish_release(named["id"])
        on_main = {"packs": [{"tag": "wiki-2026.10.18"}]}
        self.assertEqual(publish.cleanup_run(self.gh, named["id"], on_main), "kept-named")
        # Published but unnamed (the publish died after the undraft): kept,
        # so "Re-run failed jobs" can finish it (OPS-5).
        orphan = self.gh.create_draft("wiki-2026.10.11", "x", "b")
        self.gh.publish_release(orphan["id"])
        self.assertEqual(publish.cleanup_run(self.gh, orphan["id"], on_main),
                         "kept-published-unnamed")
        self.assertIsNotNone(self.gh.find_release("wiki-2026.10.11"))
        self.assertIsNotNone(self.gh.find_release("wiki-2026.10.18"))

    def test_rollback_repoints_and_logs(self):
        for tag, count in (("wiki-2026.10.18", 60000), ("wiki-2026.10.25", 61000)):
            release, _ = publish.prepare_release(self.gh, tag, set(), "b")
            self.stage("wiki-pt-essential", tag, count=count)
            publish.upload_pack(self.gh, release["id"], self.out, "wiki-pt-essential")
            reports, verified = publish.verify_release(self.gh, release, ["wiki-pt-essential"],
                                                       self.cfg, self.editions)
            publish.publish_release(self.gh, release, reports, verified, self.cfg,
                                    self.manifest_path, self.editions)
        before = manifest.load(self.manifest_path)["packs"][0]["version"]
        import contextlib
        import io
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            new = publish.rollback(self.gh, "wiki-2026.10.18", ["wiki-pt"], self.cfg,
                                   self.manifest_path, self.editions)
        self.assertEqual(new["packs"][0]["tag"], "wiki-2026.10.18")
        # OPS4-02: the served version goes DOWN, and the operator is told
        # what that reaches (a visible warning naming the contract).
        self.assertLess(new["packs"][0]["version"], before)
        self.assertRegex(said.getvalue(), r"(?m)^::warning::rollback: wiki-pt-essential "
                                          r"version %d -> %d\." % (before, new["packs"][0]["version"]))
        self.assertEqual(editions.read(self.editions, "wiki-pt")[-1]["action"], "rollback")
        self.assertEqual(editions.previous_stats(self.editions, "wiki-pt", "wiki-pt-essential")
                         ["article_count"], 60000)
        with self.assertRaises(publish.PublishError):
            publish.rollback(self.gh, "wiki-1999.01.01", ["wiki-pt"], self.cfg,
                             self.manifest_path, self.editions)

    def test_publish_gate_compares_against_the_previous_edition(self):
        # TH2-01: through the real call site (verify_release reads the
        # editions log), not sanity.evaluate in isolation. 60,000 -> 30,000
        # articles is -50%: far outside +-15%, while every absolute
        # first-edition floor in the test config still passes.
        release, _ = publish.prepare_release(self.gh, "wiki-2026.10.18", set(), "b")
        self.stage("wiki-pt-essential", "wiki-2026.10.18", count=60000)
        publish.upload_pack(self.gh, release["id"], self.out, "wiki-pt-essential")
        reports, verified = publish.verify_release(self.gh, release, ["wiki-pt-essential"],
                                                   self.cfg, self.editions)
        publish.publish_release(self.gh, release, reports, verified, self.cfg,
                                self.manifest_path, self.editions)
        release, _ = publish.prepare_release(self.gh, "wiki-2026.10.25", set(), "b")
        self.stage("wiki-pt-essential", "wiki-2026.10.25", count=30000)
        publish.upload_pack(self.gh, release["id"], self.out, "wiki-pt-essential")
        before = self.manifest_bytes()
        with self.assertRaises(publish.PublishError) as caught:
            publish.verify_release(self.gh, release, ["wiki-pt-essential"], self.cfg, self.editions)
        self.assertIn("drift:article_count", str(caught.exception))
        self.assertEqual(self.manifest_bytes(), before)
        reports, verified = publish.verify_release(self.gh, release, ["wiki-pt-essential"],
                                                   self.cfg, self.editions, force=True)
        self.assertTrue(reports[0]["gate"]["forced"])
        publish.publish_release(self.gh, release, reports, verified, self.cfg,
                                self.manifest_path, self.editions)
        last = editions.read(self.editions, "wiki-pt")[-1]
        self.assertEqual((last["tag"], last.get("forced")), ("wiki-2026.10.25", True))

    # -- publish ordering (TH-01) and the public-URL wait (OPS-5) -----------
    def staged_release(self, tag="wiki-2026.10.25", pids=("wiki-pt-essential",)):
        release, _ = publish.prepare_release(self.gh, tag, set(), "b")
        for pid in pids:
            self.stage(pid, tag, count=60000 if "ess" in pid else 300000)
            publish.upload_pack(self.gh, release["id"], self.out, pid)
        reports, verified = publish.verify_release(self.gh, release, list(pids), self.cfg,
                                                   self.editions)
        return release, reports, verified

    def test_unreachable_public_url_never_writes_the_manifest(self):
        release, reports, verified = self.staged_release()
        self.gh.public_failures["wiki-pt-essential.db.gz"] = -1
        sleeps = []
        before = self.manifest_bytes()
        with self.assertRaises(publish.PublishError):
            publish.publish_release(self.gh, release, reports, verified, self.cfg,
                                    self.manifest_path, self.editions, sleep=sleeps.append)
        self.assertEqual(self.manifest_bytes(), before)
        self.assertEqual(editions.read(self.editions, "wiki-pt"), [])
        # Bounded: exactly `tries` GETs with doubling backoff between them.
        self.assertEqual(len(self.gh.public_requests), 5)
        self.assertEqual(sleeps, [5.0, 10.0, 20.0, 40.0])
        # The verified release stays published for a re-run (cleanup keeps it).
        self.assertFalse(self.gh.get_release(release["id"])["draft"])
        self.assertEqual(publish.cleanup_run(self.gh, release["id"], manifest.empty()),
                         "kept-published-unnamed")

    def test_public_url_that_answers_late_still_publishes(self):
        release, reports, verified = self.staged_release()
        self.gh.public_failures["wiki-pt-essential.db.gz"] = 2
        sleeps = []
        new = publish.publish_release(self.gh, release, reports, verified, self.cfg,
                                      self.manifest_path, self.editions, sleep=sleeps.append)
        self.assertEqual(sleeps, [5.0, 10.0])
        self.assertEqual([e["id"] for e in new["packs"]], ["wiki-pt-essential"])
        self.assertEqual(manifest.load(self.manifest_path)["packs"][0]["tag"], "wiki-2026.10.25")

    def test_failed_undraft_never_writes_the_manifest(self):
        release, reports, verified = self.staged_release()
        self.gh.publish_error = RuntimeError("PATCH failed")
        before = self.manifest_bytes()
        with self.assertRaises(RuntimeError):
            publish.publish_release(self.gh, release, reports, verified, self.cfg,
                                    self.manifest_path, self.editions)
        self.assertEqual(self.manifest_bytes(), before)
        self.assertTrue(self.gh.get_release(release["id"])["draft"])
        self.assertEqual(self.gh.public_requests, [])

    def test_report_without_a_verify_pass_is_refused_before_the_release_is_touched(self):
        release, reports, verified = self.staged_release()
        before = self.manifest_bytes()
        for bad_verified in (set(), {("wiki-2026.10.18", "wiki-pt-essential.db.gz")}):
            with self.assertRaises(publish.PublishError):
                publish.publish_release(self.gh, release, reports, bad_verified, self.cfg,
                                        self.manifest_path, self.editions)
        with self.assertRaises(publish.PublishError):
            publish.publish_release(self.gh, release, [], verified, self.cfg,
                                    self.manifest_path, self.editions)
        self.assertEqual(self.manifest_bytes(), before)
        self.assertTrue(self.gh.get_release(release["id"])["draft"])
        self.assertNotIn("publish", [c[0] for c in self.gh.calls if isinstance(c, tuple)])

    def test_partial_tier_publish_flow_keeps_the_other_tier(self):
        # DC-01 through the real publish path.
        release, reports, verified = self.staged_release(
            "wiki-2026.10.04", ("wiki-pt-essential", "wiki-pt-extended"))
        publish.publish_release(self.gh, release, reports, verified, self.cfg,
                                self.manifest_path, self.editions)
        release, reports, verified = self.staged_release("wiki-2026.10.25", ("wiki-pt-essential",))
        new = publish.publish_release(self.gh, release, reports, verified, self.cfg,
                                      self.manifest_path, self.editions)
        self.assertEqual(sorted((e["id"], e["tag"]) for e in new["packs"]),
                         [("wiki-pt-essential", "wiki-2026.10.25"),
                          ("wiki-pt-extended", "wiki-2026.10.04")])
        # Retention protects both editions, because both are named.
        self.assertEqual(publish.prune(self.gh, manifest.named_tags(new), 1), [])

    def test_prune_protects_a_tag_only_mains_manifest_names(self):
        # TH-09: cmd_prune unions the local manifest with main's.
        import rowtalk_packs.__main__ as cli
        main_root = os.path.join(self.dir, "main")
        os.makedirs(main_root)
        manifest.write(os.path.join(main_root, "manifest.json"),
                       {"manifest_version": 2, "packs": [{"id": "x", "tag": "wiki-2026.10.01"}]})
        self.gh.main_root = main_root
        for day in range(1, 7):
            rel = self.gh.create_draft("wiki-2026.10.%02d" % day, "x", "b")
            self.gh.publish_release(rel["id"])
        original = cli._gh
        cli._gh = lambda cfg, args: self.gh
        try:
            self.assertEqual(cli.main(["--repo-root", self.repo, "prune"]), 0)
        finally:
            cli._gh = original
        self.assertIsNotNone(self.gh.find_release("wiki-2026.10.01"))
        self.assertIsNone(self.gh.find_release("wiki-2026.10.02"))

    def test_prune_protects_manifest(self):
        for day in range(1, 7):
            release = self.gh.create_draft("wiki-2026.10.%02d" % day, "x", "b")
            self.gh.publish_release(release["id"])
        pruned = publish.prune(self.gh, {"wiki-2026.10.01"}, 3)
        self.assertEqual(sorted(pruned), ["wiki-2026.10.02", "wiki-2026.10.03"])
        self.assertIsNotNone(self.gh.find_release("wiki-2026.10.01"))


if __name__ == "__main__":
    unittest.main()
