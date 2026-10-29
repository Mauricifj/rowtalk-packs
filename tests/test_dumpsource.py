import bz2
import os
import shutil
import tempfile
import threading
import unittest

from rowtalk_packs import dumpsource
from rowtalk_packs.dumpsource import (DecompressStats, DumpCorrupt, DumpError, download_file,
                                      iter_bz2_lines)
from tests.helpers import REPO, DumpServer, bz2_bytes, test_config

BASE = "/other/cirrus_search_index/"


def payload(lines=4000):
    return "".join('{"n": %d, "pad": "%s"}\n' % (i, "x" * (i % 37)) for i in range(lines))


class ListingTest(unittest.TestCase):
    def test_parse_nginx_listing(self):
        html = ('<html><body><pre><a href="../">../</a>\n'
                '<a href="20261025/">20261025/</a> 25-Oct-2026\n'
                '<a href="index_name%3Dptwiki_content/">index_name=ptwiki_content/</a>\n'
                '<a href="?C=M">sort</a><a href="https://x/">abs</a></pre></body></html>')
        self.assertEqual(dumpsource.parse_listing(html),
                         ["20261025/", "index_name=ptwiki_content/"])

    def test_parse_apache_fancy_index_lists_each_file_once(self):
        # FR5-01: Apache's fancy index (cdimage.debian.org's mirror) links
        # every file twice — icon and name — plus sort links and an absolute
        # parent link. Each shard must be listed (and so fetched) once.
        def row(name):
            return ('<tr><td class="indexcolicon"><a href="%s"><img src="/icons2/x.png" '
                    'alt="[ARC]"></a></td><td class="indexcolname"><a href="%s">%s</a></td>'
                    '</tr>\n' % (name, name, name))
        shards = ["ptwiki_content-20261025-%05d.json.bz2" % n for n in range(3)]
        html = ('<html><head><link rel="stylesheet" href="/layout/autoindex.css"></head>'
                '<body><table><tr><th><a href="?C=N;O=D">Name</a></th>'
                '<th><a href="?C=M;O=A">Last modified</a></th></tr>\n'
                '<tr><td><a href="/mirror/other/cirrus_search_index/20261025/"><img alt="[PARENTDIR]">'
                '</a></td><td><a href="/mirror/other/cirrus_search_index/20261025/">Parent '
                'Directory</a></td></tr>\n' + "".join(row(n) for n in ["_SUCCESS"] + shards)
                + '</table></body></html>')
        names = dumpsource.parse_listing(html)
        self.assertEqual(names, ["_SUCCESS"] + shards)
        listing = dumpsource.IndexListing("ptwiki", "20261025", BASE, names)
        self.assertTrue(listing.complete)
        self.assertEqual(listing.shard_urls(), [BASE + n for n in shards])

    def test_shard_names_sorted_numerically(self):
        names = ["ptwiki_content-20261025-00010.json.bz2", "ptwiki_content-20261025-00002.json.bz2",
                 "_SUCCESS", "ptwiki_content-20261018-00001.json.bz2", "enwiki_content-20261025-00001.json.bz2"]
        self.assertEqual(dumpsource.shard_names(names, "ptwiki", "20261025"),
                         ["ptwiki_content-20261025-00002.json.bz2",
                          "ptwiki_content-20261025-00010.json.bz2"])

    def test_resolve_latest_complete_for_all_wikis(self):
        files = {}
        for date in ("20261018", "20261025"):
            for wiki in ("ptwiki", "enwiki"):
                prefix = "%s%s/index_name=%s_content/" % (BASE, date, wiki)
                files[prefix + "%s_content-%s-00000.json.bz2" % (wiki, date)] = b"x"
                if not (date == "20261025" and wiki == "enwiki"):
                    files[prefix + "_SUCCESS"] = b""
        files[BASE + "20261101/index_name=ptwiki_content/_SUCCESS"] = b""  # no shards
        with DumpServer(files) as server:
            date, listings = dumpsource.resolve_latest(server.url(BASE), ["ptwiki", "enwiki"], "ua")
            self.assertEqual(date, "20261018")
            self.assertEqual(len(listings["enwiki"].shards), 1)
            date, _ = dumpsource.resolve_latest(server.url(BASE), ["ptwiki"], "ua")
            self.assertEqual(date, "20261025")
            date, _ = dumpsource.resolve_latest(server.url(BASE), ["enwiki"], "ua", date="20261025")
            self.assertIsNone(date)
            self.assertTrue(all(ua == "ua" for _, _, ua in server.state["requests"]))


class ListingRetryTest(unittest.TestCase):
    """OPS2-06: listing requests share the bounded retry policy."""

    def tree(self):
        files = {}
        for wiki in ("ptwiki", "enwiki"):
            prefix = "%s20261025/index_name=%s_content/" % (BASE, wiki)
            files[prefix + "%s_content-20261025-00000.json.bz2" % wiki] = b"x"
            files[prefix + "_SUCCESS"] = b""
        return files

    def test_transient_listing_errors_are_retried(self):
        sleeps = []
        with DumpServer(self.tree()) as server:
            server.state["errors"] = {
                BASE: [(503, None), (429, "7")],
                BASE + "20261025/index_name=enwiki_content/": [(502, None)]}
            date, listings = dumpsource.resolve_latest(server.url(BASE), ["ptwiki", "enwiki"],
                                                       "ua", sleep=sleeps.append)
            self.assertEqual(date, "20261025")
            self.assertEqual(server.state["counts"][BASE], 3)
        self.assertEqual(sorted(listings), ["enwiki", "ptwiki"])
        self.assertIn(7.0, sleeps)  # the server's Retry-After, honoured
        self.assertEqual(len(sleeps), 3)

    def test_listing_retries_give_up(self):
        sleeps = []
        with DumpServer(self.tree()) as server:
            server.state["errors"] = {BASE: [(503, None)] * 50}
            with self.assertRaises(DumpError):
                dumpsource.resolve_latest(server.url(BASE), ["ptwiki"], "ua",
                                          retry={"max_attempts": 4}, sleep=sleeps.append)
            self.assertEqual(server.state["counts"][BASE], 4)  # bounded: exactly max_attempts
        self.assertEqual(len(sleeps), 3)

    def fake_host(self, retry_after, fails=10 ** 6):
        """http_get stand-in: the root listing answers 9 dates; each index
        listing answers 503 with `retry_after` `fails` times, then an
        INCOMPLETE listing (no _SUCCESS), so the scan moves on to the next
        date with a FRESH per-request retry state. Returns the requests."""
        import email.message
        import io
        import urllib.error
        requests = []
        root = "".join('<a href="2026%02d%02d/">x</a>' % (10, d) for d in range(1, 29, 3))

        def http_get(url, user_agent, timeout, headers=None):
            requests.append(url)
            if url.endswith(BASE):
                return io.BytesIO(root.encode())
            if requests.count(url) > fails:
                return io.BytesIO(b'<a href="x_content-1-00000.json.bz2">x</a>')
            msg = email.message.Message()
            msg["Retry-After"] = retry_after
            raise urllib.error.HTTPError(url, 503, "busy", msg, io.BytesIO(b""))
        original = dumpsource.http_get
        dumpsource.http_get = http_get
        self.addCleanup(setattr, dumpsource, "http_get", original)
        return requests

    def run_scan(self, retry_after, fails):
        """A whole resolve_latest scan on a fake clock that sleep() advances."""
        from rowtalk_packs.config import load_config
        policy = load_config()["dump"]["listing_retry"]
        now = [1000.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
        requests = self.fake_host(retry_after, fails)
        with self.assertRaises(DumpError) as caught:
            dumpsource.resolve_latest("http://dumps.invalid" + BASE, ["ptwiki", "enwiki"], "ua",
                                      retry=policy, sleep=sleep, clock=lambda: now[0])
        return policy, sleeps, requests, str(caught.exception)

    def test_a_whole_scan_waits_at_most_the_run_deadline(self):
        # OPS3-02: the per-request bounds restart per listing; ONE deadline
        # bounds the run, below the resolve job's timeout-minutes.
        # Each listing fails 1-3 times then answers: no single request ever
        # reaches its own give-up, so only the run's deadline can stop it.
        for retry_after, fails in (("300", 1), ("120", 3), ("20", 3)):
            policy, sleeps, requests, message = self.run_scan(retry_after, fails)
            self.assertLessEqual(sum(sleeps), policy["run_deadline_s"], retry_after)
            self.assertIn("deadline", message, retry_after)
            self.assertGreater(len(set(requests)), 2, retry_after)  # crossed requests
        # The deadline leaves room inside the job (one in-flight listing
        # can still take a socket timeout after the last wait).
        import json
        import re as _re
        with open(os.path.join(REPO, ".github", "workflows", "build-wiki.yml"), "r",
                  encoding="utf-8") as handle:
            text = handle.read()
        resolve = text.split("\n  resolve:\n", 1)[1].split("\n  build:\n", 1)[0]
        minutes = int(_re.search(r"\n    timeout-minutes: (\d+)\n", resolve).group(1))
        with open(os.path.join(REPO, "config", "packs.json"), "r", encoding="utf-8") as handle:
            dump = json.load(handle)["dump"]
        self.assertLess(dump["listing_retry"]["run_deadline_s"] + 2 * dump["socket_timeout_s"],
                        minutes * 60)

    def test_no_listing_request_starts_after_the_run_deadline(self):
        # OPS4-03: the deadline gated only WAITS, so a host that was flaky
        # first (Retry-After waits spend most of the deadline) and slow
        # afterwards (each listing answers, but takes 25 s) carried the scan
        # past the job timeout with no DumpError. Here every request COSTS
        # clock time, which the sleep-only fake clock never charged.
        import io
        import urllib.error
        import email.message
        from rowtalk_packs.config import load_config
        policy = load_config()["dump"]["listing_retry"]
        now = [1000.0]
        starts = []
        root = "".join('<a href="2026%02d%02d/">x</a>' % (10, d) for d in range(1, 29, 3))
        root_failures = [2]

        def http_get(url, user_agent, timeout, headers=None):
            starts.append(now[0])
            if url.endswith(BASE) and root_failures[0]:
                root_failures[0] -= 1
                msg = email.message.Message()
                msg["Retry-After"] = "150"
                raise urllib.error.HTTPError(url, 503, "busy", msg, io.BytesIO(b""))
            if url.endswith(BASE):
                return io.BytesIO(root.encode())
            now[0] += 25.0  # an overloaded host that still answers
            return io.BytesIO(b'<a href="x_content-1-00000.json.bz2">x</a>')  # incomplete
        original = dumpsource.http_get
        dumpsource.http_get = http_get
        self.addCleanup(setattr, dumpsource, "http_get", original)

        def sleep(seconds):
            now[0] += seconds
        deadline = now[0] + policy["run_deadline_s"]
        with self.assertRaises(DumpError) as caught:
            dumpsource.resolve_latest("http://dumps.invalid" + BASE, ["ptwiki", "enwiki"], "ua",
                                      retry=policy, sleep=sleep, clock=lambda: now[0])
        self.assertIn("deadline", str(caught.exception))
        self.assertGreater(len(starts), 3)  # the scan did run on past the waits
        self.assertTrue(all(start < deadline for start in starts),
                        [start - deadline for start in starts])
        # The run ends within the deadline plus ONE in-flight request.
        self.assertLessEqual(now[0], deadline + 25.0)

    def test_a_past_retry_after_date_still_backs_off(self):
        # OPS3-03: an HTTP-date in the past parses to 0; the wait is floored
        # at our own backoff, so it never becomes a burst of 0 s retries.
        import email.utils
        past = email.utils.formatdate(1_000_000_000, usegmt=True)
        self.fake_host(past)
        sleeps = []
        with self.assertRaises(DumpError):
            dumpsource.fetch_text("http://dumps.invalid" + BASE + "20261001/", "ua",
                                  retry={"max_attempts": 5, "backoff_initial_s": 2,
                                         "backoff_max_s": 30}, sleep=sleeps.append)
        self.assertEqual(sleeps, [2, 4, 8, 16])
        # A download's _RetryState applies the same floor.
        cfg = dict(test_config()["dump"]["retry"], backoff_initial_s=2, backoff_max_s=60)
        sleeps = []
        state = dumpsource._RetryState(cfg, sleeps.append)
        for _ in range(7):
            state.failed(0, "http 429", retry_after=0.0)
        self.assertEqual(sleeps, [2, 4, 8, 16, 32, 60, 60])
        # A longer server wait still wins over the floor.
        state.failed(0, "http 429", retry_after=300)
        self.assertEqual(sleeps[-1], 300)

    def test_a_404_index_is_incomplete_not_retried(self):
        with DumpServer(self.tree()) as server:
            listing = dumpsource.list_index(server.url(BASE), "20261018", "ptwiki", "ua",
                                            sleep=lambda s: self.fail("404 retried"))
        self.assertFalse(listing.complete)


class HeaderTest(unittest.TestCase):
    def test_retry_after_accepts_seconds_and_http_dates(self):
        import email.utils
        now = 1_800_000_000.0
        self.assertEqual(dumpsource._retry_after("120"), 120.0)
        later = email.utils.formatdate(now + 90, usegmt=True)  # "Wed, 15 Jan 2027 ... GMT"
        self.assertAlmostEqual(dumpsource._retry_after(later, now=now), 90.0, delta=1.0)
        earlier = email.utils.formatdate(now - 90, usegmt=True)
        self.assertEqual(dumpsource._retry_after(earlier, now=now), 0.0)
        for junk in (None, "", "soon", "Wed, 99 Foo 2026"):
            self.assertIsNone(dumpsource._retry_after(junk, now=now), junk)

    def test_content_length_accepts_ascii_digits_only(self):
        self.assertEqual(dumpsource._length({"Content-Length": "123"}), 123)
        self.assertIsNone(dumpsource._length({"Content-Length": "\u00b2"}))  # isdigit(), int() raises
        self.assertIsNone(dumpsource._length({}))


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.cfg = test_config()["dump"]
        self.text = payload()
        self.data = bz2_bytes(self.text)
        self.expected = self.text.encode().split(b"\n")[:-1]
        self.path = BASE + "s.json.bz2"
        self.dir = tempfile.mkdtemp()
        self.dest = os.path.join(self.dir, "s.json.bz2")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def download(self, server, cfg=None):
        return download_file(server.url(self.path), self.dest, "ua", cfg or self.cfg,
                             sleep=lambda s: None)

    def local(self):
        with open(self.dest, "rb") as handle:
            return handle.read()

    def test_clean_download(self):
        with DumpServer({self.path: self.data}) as server:
            stats = self.download(server)
        self.assertEqual(self.local(), self.data)
        self.assertEqual(stats.bytes, len(self.data))
        self.assertEqual((stats.resumes, stats.restarts), (0, 0))

    def test_dropped_connections_resume_with_range(self):
        cuts = [len(self.data) // 5, 10, len(self.data) // 3]
        with DumpServer({self.path: self.data}, drops={self.path: cuts}) as server:
            stats = self.download(server)
            ranges = [r for p, r, _ in server.state["requests"] if p == self.path]
        self.assertEqual(self.local(), self.data)
        self.assertEqual(stats.resumes, 3)
        self.assertIsNone(ranges[0])
        self.assertTrue(all(r and r.startswith("bytes=") for r in ranges[1:]))
        self.assertEqual(stats.bytes, len(self.data))  # no byte fetched twice

    def test_server_ignoring_range_rewrites_from_zero(self):
        with DumpServer({self.path: self.data}, drops={self.path: [len(self.data) // 2]},
                        ignore_range=[self.path]) as server:
            stats = self.download(server)
        self.assertEqual(self.local(), self.data)
        self.assertEqual(stats.restarts, 1)

    def test_file_changed_between_attempts_is_not_spliced(self):
        files = {self.path: self.data}
        other = bz2_bytes(payload(3000).replace('"n"', '"m"'))
        with DumpServer(files, drops={self.path: [len(self.data) // 2]}) as server:
            # The file changes while the client is disconnected: the resume's
            # If-Range must miss, so the server re-sends the NEW file whole.
            server.state["on_drop"] = lambda path, count: files.__setitem__(path, other)
            stats = self.download(server)
        self.assertEqual(self.local(), other)
        self.assertEqual(stats.restarts, 1)

    def test_gives_up_without_progress(self):
        with DumpServer({self.path: self.data}, drops={self.path: [0] * 50}) as server:
            with self.assertRaises(DumpError):
                self.download(server)
            attempts = server.state["counts"][self.path]
        self.assertEqual(attempts, self.cfg["retry"]["max_attempts_without_progress"] + 1)

    def test_total_attempt_cap(self):
        cfg = dict(self.cfg, retry=dict(self.cfg["retry"], max_attempts_per_shard=4,
                                        progress_reset_bytes=1))
        cuts = [200 * (i + 1) for i in range(40)]
        with DumpServer({self.path: self.data}, drops={self.path: cuts}) as server:
            with self.assertRaises(DumpError):
                self.download(server, cfg)
            self.assertEqual(server.state["counts"][self.path], 4)

    def test_retry_after_is_honoured_in_full(self):
        # OPS-6: the server's Retry-After (120 s) wins over the 60 s backoff
        # cap, and the waits are not "failures without progress".
        sleeps = []
        with DumpServer({self.path: self.data}) as server:
            server.state["errors"] = {self.path: [(503, "120"), (429, "120"), (503, "120")]}
            download_file(server.url(self.path), self.dest, "ua", self.cfg, sleep=sleeps.append)
        self.assertEqual(self.local(), self.data)
        self.assertEqual(sleeps, [120.0, 120.0, 120.0])

    def test_retry_after_is_capped_and_budgeted(self):
        from rowtalk_packs.config import load_config
        cfg = load_config()["dump"]["retry"]
        sleeps = []
        state = dumpsource._RetryState(cfg, sleeps.append)
        state.failed(0, "http 429", retry_after=300)
        self.assertEqual((sleeps, state.consecutive), ([300], 0))
        state.failed(0, "http 429", retry_after=50_000)
        self.assertEqual(sleeps[-1], cfg["retry_after_max_s"])  # one wait never exceeds the cap
        # The cumulative budget ends a server that keeps asking.
        with self.assertRaises(DumpError):
            for _ in range(100):
                state.failed(0, "http 429", retry_after=300)
        self.assertLessEqual(sum(sleeps), cfg["retry_after_budget_s"])
        # ...and so does the total attempt cap, budget or not.
        state = dumpsource._RetryState(dict(cfg, retry_after_budget_s=10 ** 9), lambda s: None)
        with self.assertRaises(DumpError):
            for _ in range(cfg["max_attempts_per_shard"] + 1):
                state.failed(0, "http 503", retry_after=1)
        self.assertEqual(state.total, cfg["max_attempts_per_shard"])

    def test_missing_file_is_fatal(self):
        with DumpServer({}) as server:
            with self.assertRaises(DumpError):
                self.download(server)

    def test_cancel(self):
        stop = threading.Event()
        stop.set()
        with DumpServer({self.path: self.data}) as server:
            with self.assertRaises(DumpError):
                download_file(server.url(self.path), self.dest, "ua", self.cfg, stop=stop)


class Bz2LinesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.text = payload()
        self.expected = self.text.encode().split(b"\n")[:-1]

    def tearDown(self):
        shutil.rmtree(self.dir)

    def lines(self, data, **kwargs):
        path = os.path.join(self.dir, "f.bz2")
        with open(path, "wb") as handle:
            handle.write(data)
        stats = DecompressStats()
        out = []
        for batch in iter_bz2_lines(path, stats, **kwargs):
            out.extend(batch)
        return out, stats

    def test_single_stream_small_batches(self):
        out, stats = self.lines(bz2_bytes(self.text), chunk=997, batch_bytes=1000)
        self.assertEqual(out, self.expected)
        self.assertEqual(stats.output_bytes, len(self.text.encode()))

    def test_multistream(self):
        out, _ = self.lines(bz2_bytes(self.text, streams=5), chunk=1500)
        self.assertEqual(out, self.expected)

    def test_last_line_without_newline(self):
        out, _ = self.lines(bz2.compress(b"a\nb"))
        self.assertEqual(out, [b"a", b"b"])

    def test_truncated_file_is_corrupt(self):
        data = bz2_bytes(self.text)
        with self.assertRaises(DumpCorrupt):
            self.lines(data[: len(data) - 40])

    def test_garbage_is_corrupt(self):
        data = bytearray(bz2_bytes(self.text))
        for i in range(len(data) // 2, len(data) // 2 + 64):
            data[i] ^= 0xFF
        with self.assertRaises(DumpCorrupt):
            self.lines(bytes(data))


if __name__ == "__main__":
    unittest.main()
