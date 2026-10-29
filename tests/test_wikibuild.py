import os
import shutil
import tempfile
import threading
import unittest

from rowtalk_packs import dumpsource, packdb, wikibuild
from rowtalk_packs.cirrus import LangRules
from rowtalk_packs.staging import Staging
from tests.helpers import DumpServer, bulk_lines, bz2_bytes, doc, test_config

BASE = "/d/"


class StageShardsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = test_config()
        lang_cfg = self.cfg["wiki"]["languages"]["en"]
        self.rules = LangRules(lang_cfg, self.cfg["abstract"], self.cfg["aliases"])

    def tearDown(self):
        shutil.rmtree(self.dir)

    def stage(self, server, paths, staging, workers=2, doc_hook=None, connections=1):
        stats = wikibuild.BuildStats()
        wikibuild.stage_shards([server.url(p) for p in paths], self.rules, staging, "ua",
                               self.cfg["dump"], stats, os.path.join(self.dir, "shards"),
                               workers=workers, sleep=lambda s: None, doc_hook=doc_hook,
                               connections=connections)
        return stats

    def shard_files(self, count, docs_per_shard=60):
        files, paths = {}, []
        for n in range(count):
            path = BASE + "shard-%d.json.bz2" % n
            files[path] = bz2_bytes(bulk_lines([doc(n * 100 + i, "T %d %d" % (n, i), pop=0.5)
                                                for i in range(docs_per_shard)]))
            paths.append(path)
        return files, paths

    def test_corrupt_copy_is_discarded_and_downloaded_again(self):
        old_docs = [doc(i, "Old %d" % i, pop=1.0) for i in range(1, 400)]
        new_docs = [doc(i, "New %d" % i, pop=1.0) for i in range(1000, 1100)]
        # Several bz2 streams, so the corrupt copy really yields staged rows
        # before the damage is reached (one bz2 block yields nothing until
        # it is complete).
        bad = bytearray(bz2_bytes(bulk_lines(old_docs), streams=8))
        for i in range(len(bad) - 300, len(bad) - 200):
            bad[i] ^= 0xFF
        path = BASE + "shard.json.bz2"
        staging = Staging(os.path.join(self.dir, "s.db"), keep=10_000, prune_every=50, batch=7)
        with DumpServer({path: b""}) as server:
            server.state["versions"] = {path: [bytes(bad), bz2_bytes(bulk_lines(new_docs))]}
            stats = self.stage(server, [path], staging)
        titles = {row[2] for row in staging.ordered("popularity_score", 10_000)}
        self.assertEqual(titles, {"New %d" % i for i in range(1000, 1100)})
        self.assertEqual(staging.totals()["candidates"], 100)
        self.assertEqual(stats.corrupt_rereads, 1)
        self.assertGreater(stats.lines.docs, 100 + 50)  # the corrupt copy WAS parsed in part
        staging.close()
        self.assertEqual(os.listdir(os.path.join(self.dir, "shards")), [])

    def test_disk_buffer_is_bounded_and_emptied(self):
        files, paths = self.shard_files(8)
        shard_dir = os.path.join(self.dir, "shards")
        seen = []

        def hook(page_id, d, candidate, reason):
            seen.append(len([f for f in os.listdir(shard_dir) if f.startswith("shard-")]))

        staging = Staging(os.path.join(self.dir, "s.db"), keep=10_000)
        with DumpServer(files) as server:
            # TH-03: the first shard GET waits for a second concurrent request
            # (up to 1 s). With one connection none comes — the bounded wait
            # is the observation window; a second downloader would overlap.
            server.state.update(hold_requests=1, hold_s=1.0)
            stats = self.stage(server, paths, staging, workers=2, doc_hook=hook)
            self.assertEqual(server.state["max_active"], 1)
        self.assertEqual(stats.candidates, 480)
        self.assertLessEqual(max(seen), 3)  # workers + connections
        self.assertEqual(os.listdir(shard_dir), [])
        staging.close()

    def test_two_connections_overlap_and_never_more(self):
        files, paths = self.shard_files(8)
        shard_dir = os.path.join(self.dir, "shards")
        seen = []

        def hook(page_id, d, candidate, reason):
            seen.append(len([f for f in os.listdir(shard_dir) if f.startswith("shard-")]))

        staging = Staging(os.path.join(self.dir, "s.db"), keep=10_000)
        connections = 2
        with DumpServer(files) as server:
            # Both downloaders' first GETs are held until both are open (a
            # condition a correct client always reaches), then linger up to
            # 0.5 s for one MORE: an excess downloader is seen in that window
            # (TH4-02, the e2e's TH2-03 machinery at the stage level).
            # TH3-01: the hold is SHORTER than the client's socket timeout
            # and must never time out (a serial client's timeout-retry would
            # otherwise fake the overlap).
            hold_s = self.cfg["dump"]["socket_timeout_s"] - 2
            self.assertGreater(hold_s, 0)
            server.state.update(hold_requests=connections, hold_until=connections,
                                hold_s=hold_s, hold_linger_s=0.5)
            stats = self.stage(server, paths, staging, workers=2, doc_hook=hook,
                               connections=connections)
            self.assertEqual(server.state["hold_timeouts"], 0)
            self.assertEqual(server.state["max_active"], connections)
        # "Never more", seen from the client too: the peak number of
        # simultaneously open downloads (their wall-clock spans) is exactly
        # `connections` — independent of when the server happened to look.
        from rowtalk_packs.probe import download_overlap
        self.assertEqual(download_overlap(stats.download_spans)[1], connections)
        self.assertEqual(stats.candidates, 480)
        self.assertLessEqual(max(seen), 4)  # workers + connections
        self.assertEqual(os.listdir(shard_dir), [])
        staging.close()

    def test_persistently_corrupt_shard_gives_up_after_the_cap(self):
        # TH-06: a shard that is ALWAYS corrupt (right length, bad CRC) must end
        # the build after max_full_restarts_per_shard + 1 downloads.
        bad = bytearray(bz2_bytes(bulk_lines([doc(i, "D %d" % i, pop=1.0) for i in range(300)]),
                                  streams=4))
        for i in range(len(bad) - 300, len(bad) - 200):
            bad[i] ^= 0xFF
        path = BASE + "always-bad.json.bz2"
        outcome = {}
        finished = threading.Event()

        def run():
            staging = Staging(os.path.join(self.dir, "s.db"), keep=10_000)  # this thread's
            try:
                self.stage(server, [path], staging)
            except BaseException as error:  # noqa: BLE001
                outcome["error"] = error
            finally:
                staging.close()
                finished.set()

        with DumpServer({path: bytes(bad)}) as server:
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            # Wait on the CONDITION (the build ended); the timeout only turns
            # a missing give-up (an endless re-download loop) into a failure.
            terminated = finished.wait(30)
            gets = server.state["counts"][path]
        worker.join(30)
        self.assertTrue(terminated, "no give-up: %d downloads of an always-corrupt shard" % gets)
        cap = self.cfg["dump"]["retry"]["max_full_restarts_per_shard"]
        self.assertIsInstance(outcome.get("error"), dumpsource.DumpError)
        self.assertEqual(gets, cap + 1)
        self.assertIn("still corrupt after %d downloads" % (cap + 1), str(outcome["error"]))
        self.assertEqual(os.listdir(os.path.join(self.dir, "shards")), [])

    def test_missing_shard_raises_and_leaves_nothing_behind(self):
        path = BASE + "ok.json.bz2"
        files = {path: bz2_bytes(bulk_lines([doc(1, "A", pop=1.0)]))}
        staging = Staging(os.path.join(self.dir, "s.db"), keep=10)
        with DumpServer(files) as server:
            with self.assertRaises(dumpsource.DumpError):
                self.stage(server, [path, BASE + "missing.json.bz2"], staging)
        staging.close()
        self.assertEqual(os.listdir(os.path.join(self.dir, "shards")), [])

    def test_results_match_across_decompressor_counts(self):
        files = {}
        paths = []
        for n in range(5):
            path = BASE + "p%d.json.bz2" % n
            docs = [doc(n * 1000 + i, "Page %d %d" % (n, i), pop=((n * 7 + i * 13) % 50) / 50.0)
                    for i in range(80)]
            docs.append(doc(999_999, "Duplicate from shard %d" % n, pop=0.99))  # same id in every shard
            files[path] = bz2_bytes(bulk_lines(docs), streams=3)
            paths.append(path)
        results = []
        for workers in (1, 3):
            staging = Staging(os.path.join(self.dir, "s%d.db" % workers), keep=150,
                              prune_every=20, batch=5)
            with DumpServer(dict(files)) as server:
                self.stage(server, paths, staging, workers=workers)
            results.append(list(staging.ordered("popularity_score", 150)))
            staging.close()
        self.assertEqual(results[0], results[1])
        # Every copy scores 0.99, so the best copy is the lowest shard's.
        self.assertIn("Duplicate from shard 0", [r[2] for r in results[0]])
        # (Arrival order is not forced here; order independence with real
        # score differences is pinned by StagingTest's oracle property test.)

    def test_incoming_links_fallback_is_recorded(self):
        docs = [doc(i, "Article %d" % i, links=i) for i in range(1, 80)]  # no popularity_score
        path = BASE + "shard.json.bz2"
        with DumpServer({path: bz2_bytes(bulk_lines(docs))}) as server:
            staging = Staging(os.path.join(self.dir, "s.db"), keep=60)
            self.stage(server, [path], staging)
        ranking = staging.ranking(self.cfg["wiki"]["min_popularity_presence"])
        self.assertEqual(ranking, "incoming_links")
        self.cfg["wiki"]["languages"]["en"]["golden"] = []
        reports = wikibuild.build_tiers(self.cfg, "en", "20261018", ["essential", "extended"],
                                        staging, ranking, self.dir, os.path.join(self.dir, "ed"),
                                        {"url": "u"}, force=True)
        staging.close()
        db = packdb.open_pack(os.path.join(self.dir, "wiki-en-extended.db"))
        self.assertEqual(packdb.read_meta(db)["ranking"], "incoming_links")
        top = db.execute("SELECT title FROM articles WHERE rank = 1").fetchone()[0]
        self.assertEqual(top, "Article 79")
        db.close()
        self.assertEqual(reports[0]["ranking"], "incoming_links")
        # Every eligible doc (79) is kept: below the 150 target, the tier is
        # simply smaller (the first-edition floor would catch it; forced here).
        self.assertEqual(reports[1]["article_count"], 79)


class BudgetEstimateTest(unittest.TestCase):
    """OPS-4: the go/no-go figure composes per-stage rates; OPS2-03: the
    network stage uses the OBSERVED aggregate rate, never an assumed one."""

    def estimate(self, **overrides):
        from rowtalk_packs.probe import budget_estimate
        # One probed shard of 66: 640 MB bz2 in 640 s of network (1 MB/s on
        # its one connection), 3200 MB JSON in 128 s of decompression
        # (25 MB/s per thread) and 32 s of parsing (100 MB/s), 100k candidates.
        args = dict(total_shards=66, probed_shards=1, compressed_bytes=640e6, net_seconds=640.0,
                    download_spans=[(0.0, 640.0, 640e6)],
                    decompressed_bytes=3200e6, decompress_seconds=128.0, parse_seconds=32.0,
                    candidates=100_000, connections=2, workers=3, cpus=4, prune_every=200_000,
                    prune_seconds=30.0, write_seconds_per_article=0.0003,
                    target_articles=1_000_000, timeout_minutes=345)
        args.update(overrides)
        return budget_estimate(**args)

    def two_shards(self, seconds_each, overlap=True):
        # Two probed shards, 640 MB each; `overlap`: downloaded concurrently.
        second = (0.0, seconds_each) if overlap else (seconds_each, 2 * seconds_each)
        return dict(probed_shards=2, compressed_bytes=1280e6, net_seconds=2 * seconds_each,
                    download_spans=[(0.0, seconds_each, 640e6), second + (640e6,)],
                    decompressed_bytes=6400e6, decompress_seconds=256.0, parse_seconds=64.0,
                    candidates=200_000)

    def test_stage_minutes(self):
        b = self.estimate()
        self.assertAlmostEqual(b["minutes"]["network"], 66 * 640 / 60)                # 704
        self.assertAlmostEqual(b["minutes"]["decompress"], 66 * 3200 / (25 * 3) / 60)  # ~46.9
        self.assertAlmostEqual(b["minutes"]["parse"], 66 * 32 / 60)
        self.assertAlmostEqual(b["minutes"]["prunes"], 66 * 100_000 / 200_000 * 30 / 60)
        self.assertAlmostEqual(b["staging"], b["minutes"]["network"])
        self.assertAlmostEqual(b["total"], b["staging"] + 5.0)
        self.assertEqual(b["verdict"], "NO-GO")

    def test_unobserved_concurrency_is_never_assumed(self):
        # OPS2-03: one shard measured at 1.4 MB/s. The old estimate (x2
        # connections) said 251 min, GO; if the host caps per IP the real
        # network stage is 503 min. The verdict must use what was observed.
        b = self.estimate(net_seconds=640 / 1.4, download_spans=[(0.0, 640 / 1.4, 640e6)])
        self.assertFalse(b["concurrency_observed"])
        self.assertEqual(b["observed_concurrency"], 1)
        self.assertAlmostEqual(b["minutes"]["network"], 66 * 640 / 1.4 / 60)  # ~503
        self.assertEqual(b["verdict"], "NO-GO")
        self.assertAlmostEqual(b["network_if_per_connection"], 66 * 640 / 1.4 / 2 / 60)  # reported only

    def test_observed_concurrency_uses_the_measured_combined_rate(self):
        # Two shards downloaded concurrently at 1 MB/s each = 2 MB/s combined.
        b = self.estimate(**self.two_shards(640.0))
        self.assertTrue(b["concurrency_observed"])
        self.assertAlmostEqual(b["rates"]["aggregate_MBps"], 2.0)
        self.assertAlmostEqual(b["minutes"]["network"], 66 * 640 / 2 / 60)  # 352
        # Per-IP cap: both connections share 1.4 MB/s, i.e. 0.7 each.
        capped = self.estimate(**self.two_shards(640 / 0.7))
        self.assertAlmostEqual(capped["minutes"]["network"], 66 * 640 / 1.4 / 60)  # ~503
        self.assertEqual(capped["verdict"], "NO-GO")
        # Two shards that did NOT overlap prove nothing about concurrency.
        serial = self.estimate(**self.two_shards(320.0, overlap=False))
        self.assertFalse(serial["concurrency_observed"])
        self.assertAlmostEqual(serial["minutes"]["network"], 66 * 640 / 2 / 60)  # 2 MB/s single
        # Verdict bands at 2 MB/s observed: 352 + 5 = NO-GO; faster links pass.
        self.assertEqual(self.estimate(**self.two_shards(560.0))["verdict"], "MARGINAL")  # 313
        self.assertEqual(self.estimate(**self.two_shards(400.0))["verdict"], "GO")        # 225

    def test_download_overlap(self):
        from rowtalk_packs.probe import download_overlap
        self.assertEqual(download_overlap([]), (0.0, 0))
        self.assertEqual(download_overlap([(0, 10, 1), (5, 20, 1)]), (20.0, 2))
        self.assertEqual(download_overlap([(0, 10, 1), (10, 20, 1)]), (20.0, 1))  # touching
        self.assertEqual(download_overlap([(0, 4, 1), (6, 8, 1)]), (6.0, 1))       # gap excluded

    def test_cpu_bound_and_prunes_on_the_main_thread(self):
        b = self.estimate(net_seconds=60.0, download_spans=[(0.0, 60.0, 640e6)],
                          parse_seconds=200.0, prune_seconds=120.0)
        self.assertAlmostEqual(b["staging"], b["minutes"]["parse"] + b["minutes"]["prunes"])
        # Fewer cores than decompressors: only cpus - 1 count.
        self.assertEqual(self.estimate(cpus=2)["rates"]["usable_decompressors"], 1)


if __name__ == "__main__":
    unittest.main()
