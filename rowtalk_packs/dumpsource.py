"""Dump discovery and resumable shard streaming.

INVARIANT (I3, courtesy + bound): one build process holds at most
`dump.connections_per_job` connections to the dump host (one per downloader
thread, each fetching one file at a time; listings are fetched before the
downloaders start), and the wiki workflow runs ONE build job at a time
(max-parallel: 1), so a run never holds more than
`dump.max_connections_per_run` (2). Every request carries the configured
descriptive User-Agent.

INVARIANT (download integrity): `download_file` either leaves at `dest` a
file whose bytes are exactly one version of the remote file (size equal to
the advertised length), or raises. A dropped connection resumes with an HTTP
Range request guarded by If-Range, so a file that changed between attempts
is re-sent in full (HTTP 200) and the local copy is truncated and rewritten
from byte 0 — never spliced. Nothing downstream ever sees a partial file:
decompression starts only after the download is complete.

Layout (verified 2026-10-28):
  <base>/<YYYYMMDD>/index_name=<wiki>_content/<wiki>_content-<YYYYMMDD>-<NNNNN>.json.bz2
  plus a zero-byte `_SUCCESS` marker per index dir. Listings are nginx
  autoindex HTML; the `=` is URL-encoded (%3D) in hrefs.
"""

import bz2
import datetime
import email.utils
import http.client
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .log import log

_HREF = re.compile(r'href="([^"]+)"')
_DATE = re.compile(r"^(\d{8})/?$")


class DumpError(Exception):
    """A non-retryable problem with the dump (missing file, bad listing)."""


def http_get(url, user_agent, timeout, headers=None):
    request = urllib.request.Request(url, headers=dict({"User-Agent": user_agent}, **(headers or {})))
    return urllib.request.urlopen(request, timeout=timeout)


# Listing requests (resolve, list_index, probe). Overridden by config
# dump.listing_retry; these defaults only serve callers without a config.
LISTING_RETRY = {"max_attempts": 5, "backoff_initial_s": 2, "backoff_max_s": 30,
                 "retry_after_max_s": 300, "retry_after_budget_s": 900,
                 "run_deadline_s": 420}
RETRYABLE_HTTP = (408, 429, 500, 502, 503, 504)


def listing_deadline(retry=None, clock=time.monotonic):
    """The absolute (clock) time past which no listing request of this run
    waits any more: `run_deadline_s` from now."""
    return clock() + dict(LISTING_RETRY, **(retry or {}))["run_deadline_s"]


def fetch_text(url, user_agent, timeout=60, retry=None, sleep=time.sleep, deadline=None,
               clock=time.monotonic):
    """GET a listing page as text.

    INVARIANT (OPS2-06): a transient failure (408/429/5xx, connect or read
    error, timeout) is retried under the same bounded policy as shard
    downloads (_RetryState): at most `max_attempts` requests, doubling
    backoff, a server Retry-After (seconds OR HTTP-date) honoured up to
    `retry_after_max_s` per wait and `retry_after_budget_s` in total. Past
    either bound it raises DumpError — a retry always has a give-up. Any
    other HTTP status (e.g. 404, which list_index reads as "incomplete") is
    raised at once, unretried.

    INVARIANT (OPS3-02, per-RUN bound): the per-request bounds above restart
    with every request, and one resolve makes up to 1 + dates x wikis of
    them, so they alone exceed the resolve job's timeout. `deadline` (from
    `listing_deadline`, shared by every listing of one run) is the run's
    bound: no wait ever sleeps past it — a failure whose wait would cross it
    raises DumpError at once, so the operator sees a clean give-up, never a
    job-timeout kill. Without a `deadline`, this request gets its own
    (`run_deadline_s` from now). Who clears it: nothing; it is per run.
    OPS4-03: the deadline also gates every NEW request — none STARTS once
    the clock has reached it (DumpError at once). Otherwise a host that is
    flaky first and slow afterwards spends the deadline on waits and then
    carries a scan of answering-but-slow listings past the job timeout.
    What it does NOT bound: a request already IN FLIGHT when the deadline
    passes — it keeps only the socket timeout as its bound (cutting it
    would reject a slow but working host), so the job timeout must exceed
    run_deadline_s + one in-flight listing (pinned by a test)."""
    policy = dict(LISTING_RETRY, **(retry or {}))
    if deadline is None:
        deadline = clock() + policy["run_deadline_s"]
    state = _RetryState({"max_attempts_per_shard": policy["max_attempts"],
                         "max_attempts_without_progress": policy["max_attempts"],
                         "progress_reset_bytes": 1,
                         "backoff_initial_s": policy["backoff_initial_s"],
                         "backoff_max_s": policy["backoff_max_s"],
                         "retry_after_max_s": policy["retry_after_max_s"],
                         "retry_after_budget_s": policy["retry_after_budget_s"]}, sleep,
                        what="listing", deadline=deadline, clock=clock)
    while True:
        if clock() >= deadline:
            raise DumpError("giving up: this run's listing deadline passed before requesting "
                            "%s (after %d failed attempt(s) of it)" % (url, state.total))
        try:
            with http_get(url, user_agent, timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            error.close()
            if error.code not in RETRYABLE_HTTP:
                raise
            state.failed(0, "listing http %d for %s" % (error.code, url),
                         _retry_after(error.headers.get("Retry-After") if error.headers else None))
        except (OSError, http.client.HTTPException) as error:
            state.failed(0, "listing %s for %s" % (type(error).__name__, url))


def parse_listing(html):
    """Names linked from an autoindex page (URL-decoded, without '../').

    INVARIANT (FR5-01): each name appears ONCE, in first-link order, whatever
    the index's markup — nginx autoindex links a file once, Apache's fancy
    index (the Debian mirror) twice (its icon and its name), and a repeated
    name would make every shard download, decompress and parse twice. Only
    an exact repeat is dropped; distinct names are never merged."""
    names = []
    for href in _HREF.findall(html):
        name = urllib.parse.unquote(href)
        if name.startswith(("?", "/", "..")) or "://" in name:
            continue
        names.append(name)
    return list(dict.fromkeys(names))


def index_dir(base_url, date, wiki):
    return "%s%s/index_name%%3D%s_content/" % (_slash(base_url), date, wiki)


def _slash(url):
    return url if url.endswith("/") else url + "/"


def shard_names(names, wiki, date):
    pattern = re.compile(r"^%s_content-%s-(\d{5})\.json\.bz2$" % (re.escape(wiki), date))
    shards = sorted((int(m.group(1)), n) for n in names for m in [pattern.match(n)] if m)
    return [name for _, name in shards]


class IndexListing:
    def __init__(self, wiki, date, url, names):
        self.wiki = wiki
        self.date = date
        self.url = url
        self.names = names
        self.shards = shard_names(names, wiki, date)
        self.success = "_SUCCESS" in names

    @property
    def complete(self):
        return self.success and bool(self.shards)

    def shard_urls(self):
        return [self.url + urllib.parse.quote(name) for name in self.shards]

    def other_files(self):
        return [n for n in self.names if n not in self.shards and n != "_SUCCESS"]


def list_dates(base_url, user_agent, timeout=60, retry=None, sleep=time.sleep, deadline=None,
               clock=time.monotonic):
    names = parse_listing(fetch_text(_slash(base_url), user_agent, timeout, retry, sleep,
                                     deadline, clock))
    dates = sorted({m.group(1) for n in names for m in [_DATE.match(n)] if m}, reverse=True)
    return dates


def list_index(base_url, date, wiki, user_agent, timeout=60, retry=None, sleep=time.sleep,
               deadline=None, clock=time.monotonic):
    url = index_dir(base_url, date, wiki)
    try:
        names = parse_listing(fetch_text(url, user_agent, timeout, retry, sleep, deadline,
                                         clock))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return IndexListing(wiki, date, url, [])
        raise
    return IndexListing(wiki, date, url, names)


def resolve_latest(base_url, wikis, user_agent, timeout=60, dates_to_scan=8, date=None,
                   retry=None, sleep=time.sleep, clock=time.monotonic):
    """Newest dump date whose index dirs for ALL `wikis` are complete.

    Returns (date, {wiki: IndexListing}) or (None, {}) when none of the
    newest `dates_to_scan` dates is complete. With `date` given, only that
    date is checked. Every listing of the scan shares ONE deadline
    (OPS3-02, see fetch_text).
    """
    deadline = listing_deadline(retry, clock)
    candidates = ([date] if date else
                  list_dates(base_url, user_agent, timeout, retry, sleep, deadline,
                             clock)[:dates_to_scan])
    for candidate in candidates:
        listings = {}
        for wiki in wikis:
            listing = list_index(base_url, candidate, wiki, user_agent, timeout, retry, sleep,
                                 deadline, clock)
            if not listing.complete:
                log("dump %s %s incomplete (success=%s shards=%d)"
                    % (candidate, wiki, listing.success, len(listing.shards)))
                break
            listings[wiki] = listing
        else:
            return candidate, listings
    return None, {}


# --------------------------------------------------------------------------
# Resumable download
# --------------------------------------------------------------------------


class DownloadStats:
    def __init__(self):
        self.bytes = 0
        self.seconds = 0.0
        self.attempts = 0
        self.resumes = 0
        self.restarts = 0
        # Wall-clock span of this download (time.perf_counter, one clock for
        # every thread): the probe derives the AGGREGATE rate and the observed
        # concurrency from the spans of all downloads (OPS2-03).
        self.started = None
        self.finished = None

    def add(self, other):
        for key in ("bytes", "seconds", "attempts", "resumes", "restarts"):
            setattr(self, key, getattr(self, key) + getattr(other, key))


class _RetryState:
    """Bounded retries; one instance per download (cleared per file).

    `consecutive` counts failures without at least `progress_reset_bytes` of
    progress since the previous failure; progress clears it. If progress
    never comes, `consecutive` passes the cap and the download raises.
    `total` is never cleared, so a link that makes tiny progress forever
    still ends at max_attempts_per_shard. `full_restarts` (the server sent
    the whole file again) is capped separately.

    A server-directed wait (HTTP Retry-After on 408/429/5xx) is HONOURED IN
    FULL (Wikimedia asks clients not to evade its limits), up to
    `retry_after_max_s` per wait, and is not a "failure without progress"
    (the server told us to wait; it is not a broken link). Its bounds, so it
    can never become a retry with no give-up: `total` still counts it
    (max_attempts_per_shard), and the waits of one download may add up to at
    most `retry_after_budget_s`; past either, the download raises.

    INVARIANT (OPS3-03): a server-directed wait is NEVER SHORTER than our own
    backoff would have been — it is floored at backoff_initial_s doubled per
    server-directed wait (capped at backoff_max_s). A Retry-After that parses
    to 0 (an HTTP-date in the past: a clock ahead of the server's, or a
    server stamping its own "now") must not turn "slow down" into a burst of
    immediate retries at the host that asked for it.

    `deadline` (listings only, OPS3-02): no wait sleeps past it; a failure
    whose wait would cross it raises. None for downloads (their bound is the
    per-file caps above plus the job timeout).
    """

    def __init__(self, cfg, sleep, what="download", deadline=None, clock=time.monotonic):
        self.cfg = cfg
        self.sleep = sleep
        self.what = what
        self.deadline = deadline
        self.clock = clock
        self.directed = 0  # server-directed waits so far; never cleared
        self.total = 0
        self.consecutive = 0
        self.full_restarts = 0
        self.mark = 0
        self.waited = 0.0  # cumulative server-directed wait; never cleared

    def failed(self, received, reason, retry_after=None):
        self.total += 1
        if self.total >= self.cfg["max_attempts_per_shard"]:
            raise DumpError("giving up after %d attempts: %s" % (self.total, reason))
        if retry_after is not None:
            floor = min(self.cfg["backoff_max_s"],
                        self.cfg["backoff_initial_s"] * (2 ** self.directed))
            self.directed += 1
            delay = min(max(retry_after, floor, 0.0), self.cfg.get("retry_after_max_s", 900))
            budget = self.cfg.get("retry_after_budget_s", 3600)
            if self.waited + delay > budget:
                raise DumpError("giving up: the server asked for %.0fs more after %.0fs of "
                                "Retry-After waits (budget %ds): %s"
                                % (delay, self.waited, budget, reason))
            self._check_deadline(delay, reason)
            self.waited += delay
            log("%s retry %d (%s) after %.0fs (Retry-After) at byte %d"
                % (self.what, self.total, reason, delay, received))
            self.sleep(delay)
            return
        if received - self.mark >= self.cfg["progress_reset_bytes"]:
            self.consecutive = 0
        self.consecutive += 1
        self.mark = received
        if self.consecutive > self.cfg["max_attempts_without_progress"]:
            raise DumpError("giving up after %d attempts (%d without progress): %s"
                            % (self.total, self.consecutive, reason))
        delay = min(self.cfg["backoff_max_s"],
                    self.cfg["backoff_initial_s"] * (2 ** (self.consecutive - 1)))
        self._check_deadline(delay, reason)
        log("%s retry %d (%s) after %.0fs at byte %d"
            % (self.what, self.total, reason, delay, received))
        self.sleep(delay)

    def _check_deadline(self, delay, reason):
        if self.deadline is not None and self.clock() + delay > self.deadline:
            raise DumpError("giving up: a %.0fs wait would pass this run's %s deadline "
                            "(%.0fs left): %s" % (delay, self.what,
                                                  max(0.0, self.deadline - self.clock()), reason))

    def restarted(self, reason):
        self.full_restarts += 1
        if self.full_restarts > self.cfg["max_full_restarts_per_shard"]:
            raise DumpError("giving up after %d full restarts: %s" % (self.full_restarts, reason))
        self.mark = 0


def _content_range(value):
    match = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", value or "")
    if not match:
        return None, None
    total = None if match.group(3) == "*" else int(match.group(3))
    return int(match.group(1)), total


def _length(headers):
    value = headers.get("Content-Length")
    return int(value) if value and value.isascii() and value.isdigit() else None


def download_file(url, dest, user_agent, dump_cfg, sleep=time.sleep, stop=None):
    """Download `url` to `dest` (see the module invariant). Returns stats."""
    retry = _RetryState(dump_cfg["retry"], sleep)
    stats = DownloadStats()
    stats.started = time.perf_counter()
    chunk = dump_cfg["read_chunk_bytes"]
    timeout = dump_cfg["socket_timeout_s"]
    received = 0
    total = None
    validator = None
    with open(dest, "wb") as out:
        while True:
            if stop is not None and stop.is_set():
                raise DumpError("download cancelled")
            headers = {}
            if received:
                headers["Range"] = "bytes=%d-" % received
                if validator:
                    headers["If-Range"] = validator
            stats.attempts += 1
            try:
                response = http_get(url, user_agent, timeout, headers)
            except urllib.error.HTTPError as error:
                error.close()
                if error.code == 416 and total is not None and received >= total:
                    break
                if error.code in (408, 429, 500, 502, 503, 504):
                    retry.failed(received, "http %d" % error.code,
                                 _retry_after(error.headers.get("Retry-After") if error.headers else None))
                    continue
                raise DumpError("http %d for %s" % (error.code, url))
            except (OSError, http.client.HTTPException) as error:
                retry.failed(received, "connect: %s" % type(error).__name__)
                continue
            with response:
                status = response.status
                if received and status == 206:
                    start, range_total = _content_range(response.headers.get("Content-Range"))
                    if start != received:
                        raise DumpError("resume at %d answered from %r" % (received, start))
                    total = range_total if range_total is not None else total
                    stats.resumes += 1
                elif status == 200:
                    if received:
                        # Range ignored, or the file changed (If-Range missed):
                        # the new body is a whole file; rewrite from byte 0.
                        retry.restarted("server sent the whole file on resume")
                        stats.restarts += 1
                        out.seek(0)
                        out.truncate()
                        received = 0
                    total = _length(response.headers)
                    validator = _validator(response.headers)
                elif status == 206:
                    start, total = _content_range(response.headers.get("Content-Range"))
                    validator = _validator(response.headers)
                else:
                    raise DumpError("unexpected http %d for %s" % (status, url))
                try:
                    while True:
                        if stop is not None and stop.is_set():
                            raise DumpError("download cancelled")
                        started = time.perf_counter()
                        data = response.read(chunk)
                        stats.seconds += time.perf_counter() - started
                        if not data:
                            break
                        out.write(data)
                        received += len(data)
                        stats.bytes += len(data)
                except (OSError, ValueError, http.client.HTTPException) as error:
                    retry.failed(received, "read: %s" % type(error).__name__)
                    continue
            if total is not None and received < total:
                retry.failed(received, "connection dropped at %d/%d" % (received, total))
                continue
            if total is not None and received > total:
                raise DumpError("received %d bytes, more than the advertised %d" % (received, total))
            break
    stats.finished = time.perf_counter()
    return stats


class DumpCorrupt(Exception):
    """The downloaded bytes are not a complete, valid bz2 file."""


def _validator(headers):
    etag = headers.get("ETag")
    if etag and not etag.startswith("W/"):
        return etag
    return headers.get("Last-Modified")


def _retry_after(value, now=None):
    """Seconds to wait from a Retry-After header: delta-seconds or an
    HTTP-date (RFC 9110 §10.2.3); a date in the past means 0 (which
    _RetryState floors at its own backoff, OPS3-03). None when the header is
    absent or unparseable (then the normal backoff applies)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, when.timestamp() - (time.time() if now is None else now))


class DecompressStats:
    def __init__(self):
        self.input_bytes = 0
        self.output_bytes = 0
        self.seconds = 0.0


def iter_bz2_lines(path, stats, chunk=1 << 20, batch_bytes=4 << 20):
    """Yield lists of lines (bytes, newline stripped) from a local .bz2 file,
    about `batch_bytes` per list. Handles multi-stream bz2. Raises
    DumpCorrupt on invalid data or a file that ends inside a stream."""
    decomp = bz2.BZ2Decompressor()
    rest = b""
    batch = []
    size = 0
    with open(path, "rb") as handle:
        while True:
            data = handle.read(chunk)
            if not data:
                break
            stats.input_bytes += len(data)
            started = time.perf_counter()
            blocks = []
            try:
                while data:
                    if decomp.eof:
                        # Multi-stream: `data` is exactly what follows the
                        # previous stream's end, i.e. the next stream.
                        decomp = bz2.BZ2Decompressor()
                    blocks.append(decomp.decompress(data))
                    data = decomp.unused_data if decomp.eof else b""
            except (OSError, EOFError) as error:
                raise DumpCorrupt("bz2 data error in %s: %s" % (os.path.basename(path), error))
            stats.seconds += time.perf_counter() - started
            block = b"".join(blocks)
            if not block:
                continue
            stats.output_bytes += len(block)
            parts = (rest + block).split(b"\n")
            rest = parts.pop()
            batch.extend(parts)
            size += len(block)
            if size >= batch_bytes:
                yield batch
                batch = []
                size = 0
    if not decomp.eof:
        raise DumpCorrupt("bz2 stream ends early in %s" % os.path.basename(path))
    if rest:
        batch.append(rest)
    if batch:
        yield batch
