"""Build the wiki packs of ONE language from a CirrusSearch dump.

Pipeline: stream shards (at most `connections` at a time, I3) -> parse
(cirrus) -> classify -> stage on disk (staging) -> rank once (I4) -> write
every tier as a PREFIX of that one ranking (so Extended ⊇ Essential by
construction) -> validate -> golden queries -> sanity gate (I6) -> gzip ->
report. Nothing here talks to GitHub; uploading is the caller's step and
happens only for packs whose gate passed.
"""

import collections
import os
import queue
import threading
import time

from . import artifacts, dumpsource, editions, packdb, sanity, validate
from .cirrus import DocReader, LineStats, classify
from .fold import FOLD_VERSION, fold
from .log import log


def tag_for_date(date):
    return "wiki-%s.%s.%s" % (date[:4], date[4:6], date[6:8])


def edition_for_date(date):
    return "%s.%s.%s" % (date[:4], date[4:6], date[6:8])


def pack_id(lang, tier):
    return "wiki-%s-%s" % (lang, tier)


class BuildStats:
    def __init__(self):
        self.lines = LineStats()
        self.excluded = collections.Counter()
        self.candidates = 0
        self.compressed_bytes = 0
        self.decompressed_bytes = 0
        self.net_seconds = 0.0
        self.download_spans = []  # (started, finished, bytes) per completed download
        self.decompress_seconds = 0.0
        self.parse_seconds = 0.0
        self.resumes = 0
        self.restarts = 0
        self.corrupt_rereads = 0
        self.workers = 0


def default_workers():
    return max(1, min(3, (os.cpu_count() or 2) - 1))


def stage_shards(shard_urls, rules, staging, user_agent, dump_cfg, stats, work_dir,
                 workers=None, sleep=time.sleep, doc_hook=None, poll=0.2, connections=1):
    """Download, decompress and stage every shard.

    Threads: `connections` downloaders (the only threads that talk to the
    dump host; each holds at most one connection, fetching one shard at a
    time — I3: at most `connections` connections per process; the workflow
    runs one build job at a time, so a run never exceeds
    dump.max_connections_per_run), `workers` decompressors (bz2 releases
    the GIL, so they run in parallel), and this calling thread, which parses
    and stages (json + classify + SQLite; it holds the GIL).

    INVARIANTS
      * Bounded disk: at most `workers + connections` shard files exist at
        once. A slot is taken before a download starts and released when
        the decompressor that read the file deletes it — on success,
        corruption or shutdown alike (finally). If a decompressor never runs
        (shutdown before it picks the file up), `_shutdown` deletes the file.
      * Exactly once: rows staged for a shard come from exactly one complete,
        valid copy. A copy that fails to decompress is discarded from
        staging and re-downloaded (attempt + 1); batches still queued from
        the failed attempt carry the old attempt number and are ignored.
        Re-downloads are capped by retry.max_full_restarts_per_shard.
      * Termination: the loop ends when every shard is done, or raises on
        the first downloader/decompressor error; either way `_shutdown`
        stops and joins every thread.

    `doc_hook(page_id, doc, candidate, reason)` lets the probe observe
    documents without a second parser.
    """
    workers = workers or default_workers()
    stats.workers = workers
    os.makedirs(work_dir, exist_ok=True)
    stop = threading.Event()
    todo = queue.PriorityQueue()
    ready = queue.Queue()
    inbox = queue.Queue(maxsize=4 * workers)
    slots = threading.Semaphore(workers + connections)
    max_rereads = dump_cfg["retry"]["max_full_restarts_per_shard"]
    for index in range(len(shard_urls)):
        todo.put((index, 0))

    def put(item):
        while not stop.is_set():
            try:
                inbox.put(item, timeout=poll)
                return True
            except queue.Full:
                continue
        return False

    def downloader():
        try:
            while not stop.is_set():
                try:
                    index, attempt = todo.get(timeout=poll)
                except queue.Empty:
                    continue
                while not slots.acquire(timeout=poll):
                    if stop.is_set():
                        return
                path = os.path.join(work_dir, "shard-%05d-a%d.json.bz2" % (index, attempt))
                try:
                    dstats = dumpsource.download_file(shard_urls[index], path, user_agent,
                                                      dump_cfg, sleep=sleep, stop=stop)
                except BaseException:
                    _remove(path)
                    slots.release()
                    raise
                put(("downloaded", index, attempt, dstats))
                ready.put((index, attempt, path))
        except BaseException as error:  # noqa: BLE001 — forwarded to the main thread
            put(("error", None, None, error))

    def decompressor():
        while not stop.is_set():
            try:
                item = ready.get(timeout=poll)
            except queue.Empty:
                continue
            if item is None:
                return
            index, attempt, path = item
            dstats = dumpsource.DecompressStats()
            try:
                for batch in dumpsource.iter_bz2_lines(
                        path, dstats, chunk=dump_cfg["read_chunk_bytes"],
                        batch_bytes=dump_cfg["decompress_batch_bytes"]):
                    if not put(("batch", index, attempt, batch)):
                        return
                put(("done", index, attempt, dstats))
            except dumpsource.DumpCorrupt as error:
                put(("corrupt", index, attempt, (error, dstats)))
            except BaseException as error:  # noqa: BLE001
                put(("error", None, None, error))
            finally:
                _remove(path)
                slots.release()

    threads = [threading.Thread(target=downloader, name="downloader-%d" % n, daemon=True)
               for n in range(connections)]
    threads += [threading.Thread(target=decompressor, name="bz2-%d" % n, daemon=True)
                for n in range(workers)]
    for thread in threads:
        thread.start()
    current = [0] * len(shard_urls)
    readers = [DocReader(stats.lines) for _ in shard_urls]
    candidates = collections.Counter()
    excluded = [collections.Counter() for _ in shard_urls]
    done = set()
    try:
        while len(done) < len(shard_urls):
            kind, index, attempt, payload = inbox.get()
            if kind == "error":
                raise payload
            if index is not None and attempt != current[index]:
                # DEFENSIVE, unreachable today: a copy's messages are FIFO
                # from one decompressor and its "corrupt" is its last, so
                # nothing of a discarded attempt follows it. Kept so a future
                # change to that ordering cannot stage a discarded copy.
                continue
            if kind == "downloaded":
                stats.compressed_bytes += payload.bytes
                stats.net_seconds += payload.seconds
                stats.download_spans.append((payload.started, payload.finished, payload.bytes))
                stats.resumes += payload.resumes
                stats.restarts += payload.restarts
            elif kind == "batch":
                started = time.perf_counter()
                for page_id, doc in readers[index].docs(payload):
                    candidate, reason = classify(page_id, doc, rules)
                    if candidate is not None:
                        staging.add(index, candidate)
                        candidates[index] += 1
                    else:
                        excluded[index][reason] += 1
                    if doc_hook is not None:
                        doc_hook(page_id, doc, candidate, reason)
                stats.parse_seconds += time.perf_counter() - started
            elif kind == "corrupt":
                error, dstats = payload
                stats.corrupt_rereads += 1
                if current[index] >= max_rereads:
                    raise dumpsource.DumpError("shard %d still corrupt after %d downloads: %s"
                                               % (index, current[index] + 1, error))
                log("shard %d: %s — discarding it and downloading again" % (index, error))
                staging.discard_shard(index)
                candidates[index] = 0
                excluded[index].clear()
                current[index] += 1
                readers[index] = DocReader(stats.lines)
                todo.put((index, current[index]))
            elif kind == "done":
                done.add(index)
                stats.decompressed_bytes += payload.output_bytes
                stats.decompress_seconds += payload.seconds
                stats.candidates += candidates[index]
                stats.excluded.update(excluded[index])
                log("shard %d/%d done: %.0f MB bz2 -> %.0f MB, %d candidates, excluded %s, "
                    "bz2 %.0f MB/s" % (index + 1, len(shard_urls), payload.input_bytes / 1e6,
                                       payload.output_bytes / 1e6, candidates[index],
                                       dict(excluded[index]),
                                       payload.output_bytes / 1e6 / max(payload.seconds, 1e-9)))
        staging.flush()
    finally:
        _shutdown(stop, threads, todo, ready, inbox, workers, work_dir)


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _shutdown(stop, threads, todo, ready, inbox, workers, work_dir):
    stop.set()  # the downloader polls `todo` and `stop`; no sentinel needed
    for _ in range(workers):
        ready.put(None)
    for thread in threads:
        while thread.is_alive():
            try:
                inbox.get_nowait()  # unblock a producer stuck on a full inbox
            except queue.Empty:
                thread.join(0.05)
    # Files a decompressor never picked up (shutdown on error).
    while True:
        try:
            item = ready.get_nowait()
        except queue.Empty:
            break
        if item is not None:
            _remove(item[2])
    for name in os.listdir(work_dir):
        if name.startswith("shard-") and name.endswith(".json.bz2"):
            _remove(os.path.join(work_dir, name))


def entities(staging, ranking, limit):
    for rank, page_id, title, abstract, aliases in staging.ordered(ranking, limit):
        yield rank, title, [fold(title)] + list(aliases), (page_id, abstract)


def build_tiers(cfg, lang, date, tiers, staging, ranking, out_dir, editions_dir, source,
                force=False):
    """Write, validate, gate and gzip every tier. Returns the reports."""
    lang_cfg = cfg["wiki"]["languages"][lang]
    search = cfg["search"]
    reports = []
    tag = tag_for_date(date)
    edition = edition_for_date(date)
    family = "wiki-%s" % lang
    for tier in tiers:
        target = lang_cfg["tiers"][tier]
        pid = pack_id(lang, tier)
        db_path = os.path.join(out_dir, "%s.db" % pid)
        gz_path = os.path.join(out_dir, "%s.db.gz" % pid)
        meta = {
            "pack_id": pid, "language": lang, "tier": tier, "edition": edition,
            "version": date, "fold_version": FOLD_VERSION, "ranking": ranking,
            "min_query_chars": search["min_query_chars"],
            "license": cfg["wiki"]["license"], "license_url": cfg["wiki"]["license_url"],
            "attribution": cfg["wiki"]["attribution"],
            "source": "Wikipedia (%s) CirrusSearch content index dump" % lang_cfg["wiki"],
            "source_url": source["url"], "source_date": date,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "abstract_max_chars": cfg["abstract"]["max_chars"],
            "builder_commit": os.environ.get("GITHUB_SHA", "local"),
        }
        started = time.perf_counter()
        counts = packdb.write_pack(db_path, "wiki", meta, entities(staging, ranking, target),
                                   heavy_rows=search["heavy_prefix_rows"],
                                   suggestions=search["suggestions"])
        log("%s: %d articles, %d keys, %d heavy prefixes, %.1f MB, %.0fs" % (
            pid, counts["article_count"], counts["key_count"], counts["heavy_prefix_count"],
            os.path.getsize(db_path) / 1e6, time.perf_counter() - started))
        meta_read, errors = validate.validate_pack(db_path, "wiki", expect={
            "pack_id": pid, "language": lang, "tier": tier, "edition": edition})
        golden = validate.run_golden(db_path, "wiki", lang_cfg.get("golden", []),
                                     search["min_query_chars"], search["suggestions"])
        previous = editions.previous_stats(editions_dir, family, pid)
        gate = sanity.evaluate(counts["article_count"], os.path.getsize(db_path), golden, errors,
                               previous, lang_cfg.get("first_edition", {}).get(tier),
                               cfg["sanity"]["tolerance"], force)
        log("%s: %s" % (pid, sanity.summarize(gate)))
        artifacts.gzip_file(db_path, gz_path)
        report = artifacts.pack_report(pid, family, "wiki", lang, tier, edition, int(date), tag,
                                       db_path, gz_path, meta_read or {
                                           "article_count": 0, "key_count": 0,
                                           "schema_version": packdb.SCHEMA_VERSION,
                                           "fold_version": FOLD_VERSION, "ranking": ranking,
                                           "license": meta["license"],
                                           "license_url": meta["license_url"],
                                           "attribution": meta["attribution"]},
                                       errors, golden, gate, source)
        artifacts.write_json(os.path.join(out_dir, "%s.json" % pid), report)
        log("%s: gz %.1f MB (installed %.1f MB)" % (pid, report["download_bytes"] / 1e6,
                                                    report["installed_bytes"] / 1e6))
        reports.append(report)
    return reports
