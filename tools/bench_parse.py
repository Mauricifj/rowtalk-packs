#!/usr/bin/env python3
"""Stream-parse throughput on a SYNTHETIC CirrusSearch shard.

Generates a bulk-format .json.bz2 shard whose documents are shaped like real
content-index docs (title, opening_text, a long `text` and `source_text`,
category/template/outgoing_link lists, redirects, popularity), serves it on
127.0.0.1 and runs the production pipeline (ShardStream -> iter_lines ->
iter_docs -> classify -> Staging), reporting MB/s per stage.

    python3 tools/bench_parse.py --dir /tmp/bench --docs 8000

Synthetic text compresses differently from real Wikipedia text, so the
decompression figure is indicative only; the probe workflow measures the
real dump.
"""

import argparse
import bz2
import http.server
import json
import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rowtalk_packs.cirrus import LangRules  # noqa: E402
from rowtalk_packs.config import load_config  # noqa: E402
from rowtalk_packs.staging import Staging  # noqa: E402
from rowtalk_packs.wikibuild import BuildStats, stage_shards  # noqa: E402

WORDS = ("the of and in to was is for on as by with he at from that his it an were are "
         "which this be also first city river world war national university state new "
         "album film species family county american born played team music history").split()


def sentence(rng, n):
    return " ".join(rng.choice(WORDS) for _ in range(n)).capitalize() + "."


def make_doc(rng, page_id):
    title = " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 4))).title()
    lead = " ".join(sentence(rng, rng.randint(8, 25)) for _ in range(rng.randint(2, 6)))
    body = " ".join(sentence(rng, rng.randint(8, 25)) for _ in range(rng.randint(60, 160)))
    return {
        "version": 123, "wiki": "enwiki", "namespace": 0, "namespace_text": "",
        "title": title, "timestamp": "2026-10-01T00:00:00Z", "page_id": page_id,
        "category": ["Category %d" % rng.randint(1, 5000) for _ in range(rng.randint(2, 12))],
        "template": ["Template:T%d" % rng.randint(1, 900) for _ in range(rng.randint(3, 30))],
        "outgoing_link": ["Link_%d" % rng.randint(1, 10 ** 6) for _ in range(rng.randint(10, 200))],
        "external_link": [], "heading": [sentence(rng, 3) for _ in range(rng.randint(2, 12))],
        "redirect": [{"namespace": 0, "title": "Redirect %d" % rng.randint(1, 10 ** 6)}
                     for _ in range(rng.choice((0, 0, 1, 2, 5)))],
        "incoming_links": rng.randint(0, 5000), "popularity_score": rng.random() * 1e-5,
        "opening_text": lead, "text": lead + " " + body, "auxiliary_text": [sentence(rng, 10)],
        "source_text": "'''%s''' %s\n== History ==\n%s [[Link]] {{cite web|url=x}}" % (title, lead, body),
        "text_bytes": len(body), "content_model": "wikitext", "language": "en",
        "coordinates": [], "defaultsort": False, "file_text": None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True)
    parser.add_argument("--docs", type=int, default=2000, help="documents per shard")
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    os.makedirs(args.dir, exist_ok=True)
    shards = []
    started = time.perf_counter()
    raw_total = 0
    for n in range(args.shards):
        rng = random.Random(n)
        lines = []
        for page_id in range(n * args.docs + 1, (n + 1) * args.docs + 1):
            lines.append(json.dumps({"index": {"_type": "page", "_id": str(page_id)}}))
            lines.append(json.dumps(make_doc(rng, page_id)))
        raw = ("\n".join(lines) + "\n").encode("utf-8")
        raw_total += len(raw)
        shards.append(bz2.compress(raw, 9))
    bz_total = sum(len(d) for d in shards)
    print("synthetic dump: %d shards, %.1f MB json, %.1f MB bz2 (ratio %.1f), built in %.0fs" % (
        args.shards, raw_total / 1e6, bz_total / 1e6, raw_total / bz_total,
        time.perf_counter() - started))

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            data = shards[int(self.path.rsplit("-", 1)[1].split(".")[0])]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    urls = ["http://127.0.0.1:%d/shard-%d.json.bz2" % (server.server_address[1], n)
            for n in range(args.shards)]
    cfg = load_config()
    rules = LangRules(cfg["wiki"]["languages"]["en"], cfg["abstract"], cfg["aliases"])
    for workers in sorted({1, args.workers}):
        staging = Staging(os.path.join(args.dir, "bench-staging.db"), keep=10 ** 6)
        stats = BuildStats()
        started = time.perf_counter()
        stage_shards(urls, rules, staging, cfg["user_agent"], cfg["dump"], stats,
                     os.path.join(args.dir, "shards"), workers=workers)
        wall = time.perf_counter() - started
        staging.close()
        mb_raw = stats.decompressed_bytes / 1e6
        print("workers=%d: candidates %d; wall %.1fs -> %.1f MB/s of JSON, %.2f MB/s of bz2; "
              "decompress %.1f MB/s per thread (%.1fs busy); parse+stage %.1f MB/s (%.1fs, "
              "main thread)" % (workers, stats.candidates, wall, mb_raw / wall,
                                stats.compressed_bytes / 1e6 / wall,
                                mb_raw / max(stats.decompress_seconds, 1e-9),
                                stats.decompress_seconds, mb_raw / max(stats.parse_seconds, 1e-9),
                                stats.parse_seconds))
    server.shutdown()


if __name__ == "__main__":
    main()
