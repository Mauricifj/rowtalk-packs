#!/usr/bin/env python3
"""Search latency benchmark on a SYNTHETIC wiki pack (no real data needed).

Builds a pack with N articles (default 1,000,000) whose titles follow a
skewed (Zipf-like) word distribution plus a large "List of ..." cluster, ~1.5
aliases per article and ~300-char abstracts, then measures suggestion latency
through the reference query recipe (packdb.suggest: fold -> prefix_top or
bounded range scan -> titles) for 2-, 3- and 4-character prefixes, for each
heavy-prefix threshold given, and for the naive structure (range scan only).

    python3 tools/bench_search.py --dir /tmp/bench --articles 1000000

Writes ~0.5 GB under --dir; delete it afterwards.
"""

import argparse
import os
import random
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rowtalk_packs import packdb  # noqa: E402
from rowtalk_packs.fold import PREFIX_SENTINEL, fold  # noqa: E402

SYLLABLES = ["ra", "ri", "ro", "ma", "me", "mi", "sa", "so", "ta", "te", "ca", "co", "la", "li",
             "na", "no", "pa", "pe", "da", "de", "ba", "be", "ga", "go", "fa", "fe", "va", "vi",
             "ja", "jo", "ka", "ki", "ha", "he", "wa", "zu", "an", "en", "in", "on", "un", "st",
             "tr", "ch", "sh", "th", "br", "gr", "pl", "cr", "são", "ção", "ñu", "ü", "é"]


def make_vocabulary(rng, size):
    words = set()
    while len(words) < size:
        words.add("".join(rng.choice(SYLLABLES) for _ in range(rng.randint(1, 4))))
    words = sorted(words)
    rng.shuffle(words)
    return words


def zipf_pick(rng, words, s=1.1):
    # Inverse-CDF-free approximation: index ~ floor(n * u^(1/(1-s)))-ish,
    # biased toward the head without precomputing weights.
    u = rng.random()
    index = int(len(words) * (u ** 3))
    return words[min(index, len(words) - 1)]


def build(path, articles, seed=1):
    rng = random.Random(seed)
    words = make_vocabulary(rng, 60000)

    def title_for(rank):
        if rank % 33 == 0:
            return "List of %s %s" % (zipf_pick(rng, words), zipf_pick(rng, words))
        n = rng.choice((1, 1, 2, 2, 2, 3, 3, 4))
        return " ".join(zipf_pick(rng, words) for _ in range(n)).title()

    def entities():
        for rank in range(1, articles + 1):
            title = title_for(rank)
            keys = [fold(title)]
            for _ in range(rng.choice((0, 1, 1, 2, 3))):
                keys.append(fold(" ".join(zipf_pick(rng, words) for _ in range(rng.randint(1, 3)))))
            abstract = (" ".join(zipf_pick(rng, words) for _ in range(45)) + ".")[:300]
            yield rank, title, keys, (rank * 7, abstract)

    meta = {"pack_id": "bench", "language": "xx", "tier": "extended", "edition": "bench",
            "version": "0", "fold_version": 1, "ranking": "synthetic", "min_query_chars": 2,
            "license": "-", "license_url": "-", "attribution": "-", "source": "synthetic",
            "source_url": "-", "source_date": "-", "built_at": "-", "abstract_max_chars": 600}
    started = time.perf_counter()
    counts = packdb.write_pack(path, "wiki", meta, entities(), heavy_rows=10 ** 9)
    return counts, time.perf_counter() - started


def add_threshold_table(path, threshold, limit=8):
    """Rebuild prefix_top for another threshold (same keys) and return the
    row count and the seconds it took."""
    db = sqlite3.connect(path)
    started = time.perf_counter()
    heavy = packdb.heavy_prefixes(db.execute("SELECT key, rank FROM search ORDER BY key, rank"),
                                  threshold, limit)
    db.execute("DELETE FROM prefix_top")
    db.executemany("INSERT INTO prefix_top VALUES (?, ?)",
                   sorted((p, ",".join(map(str, r))) for p, r in heavy.items()))
    db.commit()
    db.close()
    return len(heavy), time.perf_counter() - started


def sample_prefixes(path, length, count, rng):
    db = packdb.open_pack(path)
    total = db.execute("SELECT MAX(rank) FROM articles").fetchone()[0]
    out = []
    while len(out) < count:
        title = db.execute("SELECT title FROM articles WHERE rank = ?",
                           (rng.randint(1, total),)).fetchone()[0]
        key = fold(title)
        if len(key) >= length and not key[:length].endswith(" "):
            out.append(key[:length])
    db.close()
    return out


def time_queries(path, queries, naive=False):
    db = packdb.open_pack(path)
    samples = []
    for query in queries:
        started = time.perf_counter()
        if naive:
            q = fold(query)
            ranks = [r for (r,) in db.execute(packdb.SQL_PREFIX_RANGE, (q, q + PREFIX_SENTINEL, 8))]
            packdb.display_for(db, "wiki", ranks)
        else:
            packdb.suggest(db, "wiki", query)
        samples.append((time.perf_counter() - started) * 1000.0)
    db.close()
    samples.sort()
    return {"n": len(samples), "p50": statistics.median(samples),
            "p95": samples[int(0.95 * (len(samples) - 1))],
            "p99": samples[int(0.99 * (len(samples) - 1))], "max": samples[-1]}


def worst_light_prefix(path, threshold):
    """The non-heavy prefix (len 2-4) with the most matching rows."""
    db = packdb.open_pack(path)
    heavy = {p for (p,) in db.execute("SELECT prefix FROM prefix_top")}
    best = (0, None)
    for length in (2, 3, 4):
        rows = db.execute("SELECT substr(key, 1, ?) AS p, COUNT(*) FROM search GROUP BY p",
                          (length,))
        for prefix, count in rows:
            if prefix not in heavy and not prefix.endswith(" ") and len(prefix) == length \
                    and count > best[0]:
                best = (count, prefix)
    db.close()
    return best


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True)
    parser.add_argument("--articles", type=int, default=1_000_000)
    parser.add_argument("--thresholds", default="250,1000,4000")
    parser.add_argument("--queries", type=int, default=2000)
    args = parser.parse_args()
    os.makedirs(args.dir, exist_ok=True)
    path = os.path.join(args.dir, "bench.db")
    counts, seconds = build(path, args.articles)
    print("built %d articles, %d keys in %.0fs; file %.0f MB" % (
        counts["article_count"], counts["key_count"], seconds, os.path.getsize(path) / 1e6))
    rng = random.Random(7)
    queries = {n: sample_prefixes(path, n, args.queries, rng) for n in (2, 3, 4)}
    print("naive range scan only:")
    for n in (2, 3, 4):
        print("  %d-char %s" % (n, fmt(time_queries(path, queries[n], naive=True))))
    for threshold in [int(t) for t in args.thresholds.split(",")]:
        rows, secs = add_threshold_table(path, threshold)
        worst = worst_light_prefix(path, threshold)
        print("heavy_prefix_rows=%d: %d prefix_top rows (%.0fs); worst light prefix %r = %d rows" % (
            threshold, rows, secs, worst[1], worst[0]))
        for n in (2, 3, 4):
            print("  %d-char %s" % (n, fmt(time_queries(path, queries[n]))))
        if worst[1]:
            print("  worst light %s" % fmt(time_queries(path, [worst[1]] * 50)))
    print("file %.0f MB" % (os.path.getsize(path) / 1e6))


def fmt(stats):
    return "n=%d p50=%.2fms p95=%.2fms p99=%.2fms max=%.2fms" % (
        stats["n"], stats["p50"], stats["p95"], stats["p99"], stats["max"])


if __name__ == "__main__":
    main()
