"""Probe: stream a few shards of a real dump and report what is there.

This is the FIRST thing to run against the real dump (the new sharded
dump's line shape is unverified). It reuses the production parser and
classifier through `stage_shards(doc_hook=...)`, so what it measures is what
the build will do. Output: markdown (GITHUB_STEP_SUMMARY in Actions).
Never prints article text — only counts, sizes and field names.
"""

import collections
import math
import os
import random
import tempfile
import time

from . import artifacts, dumpsource, packdb
from .cirrus import Candidate, LangRules
from .staging import Staging
from .wikibuild import BuildStats, default_workers, entities, stage_shards

GO_SHARE = 0.8  # "GO" needs the estimate under 80% of the job timeout


def download_overlap(spans):
    """(union seconds, peak concurrency) of download wall-clock spans
    [(started, finished, bytes)]."""
    events = sorted([(a, 1) for a, b, _ in spans] + [(b, -1) for a, b, _ in spans],
                    key=lambda e: (e[0], e[1]))
    union, active, peak, since = 0.0, 0, 0, None
    for when, step in events:
        if active and since is not None:
            union += when - since
        active += step
        peak = max(peak, active)
        since = when
    return union, peak


def budget_estimate(total_shards, probed_shards, compressed_bytes, net_seconds, download_spans,
                    decompressed_bytes, decompress_seconds, parse_seconds, candidates,
                    connections, workers, cpus, prune_every, prune_seconds,
                    write_seconds_per_article, target_articles, timeout_minutes):
    """Go/no-go estimate for a FULL build of one language, from per-STAGE
    rates (OPS-4) — never from probe wall time x shards, which serializes
    what the real pipeline overlaps and hides the prunes.

    The stages run concurrently, so the staging time is the slowest of:
      network    = all bz2 bytes / the AGGREGATE rate the probe OBSERVED
                   (bytes over the wall time any download was active)
      decompress = all JSON bytes / (per-thread rate x usable decompressors),
                   usable = min(workers, cpus - 1) (the main thread needs a core)
      main thread= parse at the measured rate + every prune, each costed at
                   the measured cost of ONE prune at the REAL keep (the late,
                   most expensive regime — an upper bound)
    plus the tail that runs after staging: writing every tier (measured
    per article on the mini-packs, scaled to the largest tier).

    INVARIANT (OPS2-03): the verdict never assumes a speed-up the probe did
    not observe. Multiplying one connection's rate by `connections` assumes
    the host limits per CONNECTION; if it limits per IP, two connections
    share one rate and that estimate is 2x optimistic. So the network
    figure is always the observed aggregate: with fewer concurrent
    downloads than `connections` (one probed shard) it is the single-
    connection rate — conservative; with `connections` downloads observed
    overlapping it is their measured combined rate. The per-connection x
    connections figure is reported only as `network_if_per_connection`."""
    scale = total_shards / float(max(1, probed_shards))
    total_bz2 = compressed_bytes * scale
    total_json = decompressed_bytes * scale
    union, peak = download_overlap(download_spans)
    aggregate = compressed_bytes / union if union > 0 else 0.0
    per_connection = compressed_bytes / net_seconds if net_seconds > 0 else 0.0
    per_thread = decompressed_bytes / decompress_seconds if decompress_seconds > 0 else 0.0
    parse_rate = decompressed_bytes / parse_seconds if parse_seconds > 0 else 0.0
    usable = max(1, min(workers, cpus - 1))
    minutes = {
        "network": total_bz2 / aggregate / 60 if aggregate else float("inf"),
        "decompress": total_json / (per_thread * usable) / 60 if per_thread else float("inf"),
        "parse": total_json / parse_rate / 60 if parse_rate else float("inf"),
        "prunes": (candidates * scale / max(1, prune_every)) * prune_seconds / 60,
        "write": write_seconds_per_article * target_articles / 60,
    }
    staging = max(minutes["network"], minutes["decompress"], minutes["parse"] + minutes["prunes"])
    total = staging + minutes["write"]
    verdict = ("GO" if total <= GO_SHARE * timeout_minutes else
               "MARGINAL" if total <= timeout_minutes else "NO-GO")
    return {"minutes": minutes, "staging": staging, "total": total, "verdict": verdict,
            "rates": {"per_connection_MBps": per_connection / 1e6,
                      "aggregate_MBps": aggregate / 1e6,
                      "per_thread_MBps": per_thread / 1e6, "parse_MBps": parse_rate / 1e6,
                      "usable_decompressors": usable},
            "observed_concurrency": peak, "concurrency_observed": peak >= connections,
            "network_if_per_connection": (total_bz2 / (per_connection * connections) / 60
                                          if per_connection else float("inf")),
            "total_bz2_GB": total_bz2 / 1e9, "timeout_minutes": timeout_minutes,
            "connections": connections}


def measure_prune(work_dir, keep, prune_every, abstract_chars, seed=1):
    """Seconds for ONE prune at the real `keep`: stage keep + prune_every
    synthetic candidates (abstracts as long as the probe's average), then
    prune once. This is the cost the build pays per prune late in a run."""
    rng = random.Random(seed)
    path = os.path.join(work_dir, "prune-bench.db")
    staging = Staging(path, keep, prune_every=10 ** 12, batch=5000)
    try:
        filler = "x" * max(1, int(abstract_chars))
        for pid in range(1, keep + prune_every + 1):
            staging.add(0, Candidate(pid, "Title %d" % pid, filler, rng.random(),
                                     rng.randrange(10 ** 5), ["alias %d" % pid]))
        staging.flush()
        started = time.perf_counter()
        staging.prune()
        return time.perf_counter() - started
    finally:
        staging.close()


def _percentiles(values, points=(0, 10, 50, 90, 99, 100)):
    if not values:
        return {}
    ordered = sorted(values)
    out = {}
    for p in points:
        index = min(len(ordered) - 1, int(round(p / 100.0 * (len(ordered) - 1))))
        out["p%d" % p] = ordered[index]
    return out


class Collector:
    def __init__(self, rules):
        self.rules = rules
        self.docs = 0
        self.keys = collections.Counter()
        self.popularity = []
        self.links = []
        self.opening_lengths = []
        self.redirect_counts = []
        self.namespaces = collections.Counter()
        self.signals = collections.Counter()
        self.excluded = collections.Counter()
        self.redirect_ns = collections.Counter()

    def __call__(self, page_id, doc, candidate, reason):
        self.docs += 1
        # A key is printed in the report: never let a lone surrogate in a
        # field NAME make the summary unwritable (see cirrus INVARIANT).
        self.keys.update(k.encode("utf-8", "backslashreplace").decode("utf-8") for k in doc)
        pop = doc.get("popularity_score")
        if isinstance(pop, (int, float)) and not isinstance(pop, bool):
            self.popularity.append(pop)
        links = doc.get("incoming_links")
        if isinstance(links, int) and not isinstance(links, bool):
            self.links.append(links)
        opening = doc.get("opening_text")
        self.opening_lengths.append(len(opening) if isinstance(opening, str) else -1)
        redirects = doc.get("redirect")
        self.redirect_counts.append(len(redirects) if isinstance(redirects, list) else -1)
        if isinstance(redirects, list):
            for item in redirects[:50]:
                if isinstance(item, dict):
                    self.redirect_ns[str(item.get("namespace"))] += 1
        self.namespaces[str(doc.get("namespace"))] += 1
        for signal in self.rules.disambiguation_signals(doc):
            self.signals[signal] += 1
        if reason:
            self.excluded[reason] += 1


def run_probe(cfg, lang, shards=1, date=None, base_url=None, work_dir=None, sleep=time.sleep,
              prune_bench=True):
    lang_cfg = cfg["wiki"]["languages"][lang]
    wiki = lang_cfg["wiki"]
    ua = cfg["user_agent"]
    base = base_url or cfg["dump"]["base_url"]
    retry = cfg["dump"].get("listing_retry")
    date, listings = dumpsource.resolve_latest(base, [wiki], ua, cfg["dump"]["socket_timeout_s"],
                                               cfg["dump"]["dates_to_scan"], date, retry=retry,
                                               sleep=sleep)
    if date is None:
        return "# Probe %s\n\nNo complete dump found under %s.\n" % (wiki, base)
    listing = listings[wiki]
    total_shards = len(listing.shards)
    urls = listing.shard_urls()[:shards]
    fraction = len(urls) / float(total_shards)
    date_dir_names = dumpsource.parse_listing(
        dumpsource.fetch_text("%s%s/" % (base if base.endswith("/") else base + "/", date), ua,
                              cfg["dump"]["socket_timeout_s"], retry, sleep))
    rules = LangRules(lang_cfg, cfg["abstract"], cfg["aliases"])
    collector = Collector(rules)
    stats = BuildStats()
    work = work_dir or tempfile.mkdtemp(prefix="probe-")
    tiers = lang_cfg["tiers"]
    keep = max(1, int(math.ceil(max(tiers.values()) * fraction)))
    staging = Staging(os.path.join(work, "probe-staging.db"), keep, cfg["staging"]["prune_every"], cfg["staging"]["batch"])
    connections = cfg["dump"]["connections_per_job"]
    workers = default_workers()
    started = time.perf_counter()
    try:
        stage_shards(urls, rules, staging, ua, cfg["dump"], stats, os.path.join(work, "shards"),
                     sleep=sleep, doc_hook=collector, workers=workers, connections=connections)
        wall = time.perf_counter() - started
        ranking = staging.ranking(cfg["wiki"]["min_popularity_presence"])
        candidates = staging.totals()["candidates"]
        sizes = {}
        write_seconds_per_article = 0.0
        for tier, target in sorted(tiers.items(), key=lambda kv: kv[1]):
            mini = max(1, int(math.ceil(target * fraction)))
            path = os.path.join(work, "mini-%s.db" % tier)
            written = time.perf_counter()
            counts = packdb.write_pack(path, "wiki", _probe_meta(lang, tier, date, ranking),
                                       entities(staging, ranking, mini),
                                       heavy_rows=cfg["search"]["heavy_prefix_rows"],
                                       suggestions=cfg["search"]["suggestions"])
            gz = artifacts.gzip_file(path, path + ".gz")
            n = max(1, counts["article_count"])
            write_seconds_per_article = max(write_seconds_per_article,
                                            (time.perf_counter() - written) / n)
            sizes[tier] = {"mini_articles": counts["article_count"], "target": target,
                           "installed_per_article": os.path.getsize(path) / n,
                           "gzip_per_article": os.path.getsize(gz) / n}
            os.remove(path)
            os.remove(gz)
    finally:
        staging.close()
    keep = max(tiers.values())
    opening = [n for n in collector.opening_lengths if n > 0]
    abstract_chars = min(cfg["abstract"]["max_chars"], sum(opening) / len(opening)) if opening else 200
    prune_seconds = (measure_prune(work, keep, cfg["staging"]["prune_every"], abstract_chars)
                     if prune_bench else 0.0)
    budget = budget_estimate(total_shards, len(urls), stats.compressed_bytes, stats.net_seconds,
                             stats.download_spans, stats.decompressed_bytes, stats.decompress_seconds, stats.parse_seconds,
                             candidates, connections, workers, os.cpu_count() or 2,
                             cfg["staging"]["prune_every"], prune_seconds,
                             write_seconds_per_article, keep,
                             cfg["wiki"]["build_timeout_minutes"])
    budget["prune_bench"] = prune_bench
    budget["prune_seconds"] = prune_seconds
    budget["keep"] = keep
    return _report(wiki, date, listing, date_dir_names, urls, total_shards, collector, stats, wall,
                   ranking, sizes, budget)


def _probe_meta(lang, tier, date, ranking):
    return {"pack_id": "probe-%s-%s" % (lang, tier), "language": lang, "tier": tier,
            "edition": date, "version": date, "fold_version": 1, "ranking": ranking,
            "min_query_chars": 2, "license": "-", "license_url": "-", "attribution": "-",
            "source": "probe", "source_url": "-", "source_date": date, "built_at": "-",
            "abstract_max_chars": 600}


def _pct(part, whole):
    return "%.1f%%" % (100.0 * part / whole) if whole else "n/a"


def _mbps(nbytes, seconds):
    return "%.1f MB/s" % (nbytes / 1e6 / seconds) if seconds > 0 else "n/a"


def _report(wiki, date, listing, date_dir_names, urls, total_shards, c, stats, wall, ranking, sizes,
            budget):
    ls = stats.lines
    lines = ["# Probe: %s dump %s" % (wiki, date), ""]
    lines.append("Shards probed: %d of %d (%s)." % (len(urls), total_shards,
                                                    ", ".join(u.rsplit("/", 1)[-1] for u in urls)))
    lines += ["", "## Line shape", "",
              "| measure | value |", "|---|---|",
              "| lines | %d |" % ls.lines,
              "| action lines (bulk format) | %d |" % ls.action_lines,
              "| action lines carrying `_id` | %d |" % ls.action_ids,
              "| document lines | %d |" % ls.docs,
              "| docs with `page_id` field | %d (%s) |" % (ls.docs_with_page_id,
                                                         _pct(ls.docs_with_page_id, ls.docs)),
              "| docs whose id came from the action line | %d |" % ls.docs_id_from_action,
              "| docs without any id | %d |" % ls.docs_without_id,
              "| malformed lines | %d |" % ls.malformed,
              "| blank lines | %d |" % ls.blank,
              "| download resumes / full restarts / corrupt re-downloads | %d / %d / %d |" % (
                  stats.resumes, stats.restarts, stats.corrupt_rereads)]
    lines += ["", "## Key presence (top 40 top-level document keys)", "", "| key | presence |",
              "|---|---|"]
    for key, count in c.keys.most_common(40):
        lines.append("| `%s` | %s |" % (key, _pct(count, c.docs)))
    pop = _percentiles(c.popularity)
    lines += ["", "## popularity_score", "",
              "Present in %s of docs. Range/percentiles: %s" % (
                  _pct(len(c.popularity), c.docs),
                  ", ".join("%s=%.3g" % kv for kv in pop.items()) or "absent"),
              "", "incoming_links present in %s; percentiles: %s" % (
                  _pct(len(c.links), c.docs),
                  ", ".join("%s=%s" % kv for kv in _percentiles(c.links).items()) or "absent"),
              "", "Ranking the build would use: **%s**." % ranking]
    opening = [n for n in c.opening_lengths if n >= 0]
    lines += ["", "## opening_text length (chars)", "",
              "Present in %s; empty in %d; percentiles: %s" % (
                  _pct(len(opening), c.docs), sum(1 for n in opening if n == 0),
                  ", ".join("%s=%d" % kv for kv in _percentiles(opening).items()))]
    redirects = [n for n in c.redirect_counts if n >= 0]
    lines += ["", "## Redirects", "",
              "`redirect` present in %s; per-doc count percentiles: %s; redirect namespaces: %s" % (
                  _pct(len(redirects), c.docs),
                  ", ".join("%s=%d" % kv for kv in _percentiles(redirects).items()),
                  dict(c.redirect_ns.most_common(8)))]
    lines += ["", "## Namespaces", "", ", ".join("ns %s: %d" % kv for kv in c.namespaces.most_common(12))]
    lines += ["", "## Disambiguation signals (configured names)", "",
              ", ".join("%s: %d" % kv for kv in sorted(c.signals.items())) or "no hits",
              "", "Exclusions by reason: %s" % dict(c.excluded.most_common())]
    lines += ["", "## Throughput (%d decompressor threads)" % stats.workers, "",
              "| stage | bytes | busy seconds | rate per thread |", "|---|---|---|---|",
              "| download (bz2, summed over connections) | %d | %.1f | %s per connection |" % (
                  stats.compressed_bytes, stats.net_seconds,
                  _mbps(stats.compressed_bytes, stats.net_seconds)),
              "| decompress (output, summed over threads) | %d | %.1f | %s |" % (
                  stats.decompressed_bytes, stats.decompress_seconds,
                  _mbps(stats.decompressed_bytes, stats.decompress_seconds)),
              "| parse+classify+stage (main thread) | %d | %.1f | %s |" % (
                  stats.decompressed_bytes, stats.parse_seconds,
                  _mbps(stats.decompressed_bytes, stats.parse_seconds)),
              "| wall | | %.1f | %s of bz2 |" % (wall, _mbps(stats.compressed_bytes, wall))]
    m, r = budget["minutes"], budget["rates"]
    lines += ["", "## Time budget (go/no-go for a full build)", "",
              "Stages overlap, so staging = the slowest of network, decompression and the main "
              "thread (parse + prunes); then the tier writing. Probe wall time x shards is NOT "
              "used: it serializes stages the build overlaps and never runs a prune at the real "
              "keep.", "",
              "| stage | basis | minutes |", "|---|---|---|",
              "| network | %.1f GB bz2 at the observed aggregate %.1f MB/s (%d concurrent "
              "download(s) seen; the build uses %d) | %.0f |" % (
                  budget["total_bz2_GB"], r["aggregate_MBps"], budget["observed_concurrency"],
                  budget["connections"], m["network"]),
              "| decompress | %.1f MB/s per thread x %d usable | %.0f |" % (
                  r["per_thread_MBps"], r["usable_decompressors"], m["decompress"]),
              "| parse (main thread) | %.1f MB/s | %.0f |" % (r["parse_MBps"], m["parse"]),
              "| prunes (main thread) | %s | %.0f |" % (
                  "one prune at keep=%d measured: %.3g s" % (budget["keep"], budget["prune_seconds"])
                  if budget["prune_bench"] else "NOT measured (prune bench off)", m["prunes"]),
              "| write tiers | measured per article on the mini-packs | %.0f |" % m["write"],
              "| **total** | staging %.0f + write | **%.0f** |" % (budget["staging"], budget["total"]),
              "", "Verdict: **%s** against the %d-minute job timeout (GO = under %d%%)." % (
                  budget["verdict"], budget["timeout_minutes"], int(GO_SHARE * 100)),
              "", ("Concurrency observed: the network figure is the measured rate of %d "
                   "overlapping downloads." % budget["observed_concurrency"]
                   if budget["concurrency_observed"] else
                   "Concurrency NOT observed (fewer probed shards than the build's %d "
                   "connections): the network figure assumes extra connections add nothing "
                   "(a per-IP limit). Probe at least %d shards to measure it." % (
                       budget["connections"], budget["connections"])),
              "If the host limits per connection instead, %.1f MB/s x %d would give a network "
              "stage of %.0f minutes (NOT used for the verdict)." % (
                  r["per_connection_MBps"], budget["connections"],
                  budget["network_if_per_connection"])]
    lines += ["", "## Dump directory", "",
              "Index dir files besides shards and `_SUCCESS`: %s" % (listing.other_files() or "none"),
              "", "Date dir entries: %s" % date_dir_names[:40],
              "", "Checksum-like files: %s" % ([n for n in listing.other_files() + date_dir_names
                                                if any(t in n.lower() for t in
                                                       ("md5", "sha1", "sha256", "checksum"))]
                                               or "none")]
    lines += ["", "## Extrapolated pack sizes", "",
              "| tier | target articles | mini-pack articles | installed MB (est.) | gzip MB (est.) |",
              "|---|---|---|---|---|"]
    for tier, s in sizes.items():
        lines.append("| %s | %d | %d | %.0f | %.0f |" % (
            tier, s["target"], s["mini_articles"], s["installed_per_article"] * s["target"] / 1e6,
            s["gzip_per_article"] * s["target"] / 1e6))
    lines.append("")
    lines.append("Estimates scale a mini-pack built from this shard's best N/shard_count "
                 "candidates; per-article size of the true top-N may differ (popular articles "
                 "tend to have longer leads and more redirects).")
    return "\n".join(lines) + "\n"
