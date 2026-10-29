# rowtalk-packs

Offline knowledge packs for the Rowtalk app (offline 1:1 messenger):
Wikipedia abstracts (Portuguese and English, two tiers each) and WordNet
dictionaries, built and refreshed by GitHub Actions and published as
immutable release assets. The app reads `manifest.json` from this
repository and downloads packs over plain HTTPS; lookups then run entirely
on the device.

* **Data contract (pack schema, fold_v1, query SQL, manifest):** [SCHEMA.md](SCHEMA.md)
* **Data licenses and attribution:** [DATA-LICENSES.md](DATA-LICENSES.md)
* **Builder code:** MIT ([LICENSE](LICENSE)); the packs themselves carry the data licenses above

| pack | content | approx. download |
|---|---|---|
| `wiki-pt-essential` / `wiki-pt-extended` | top 50k / 300k pt articles | see the first probe run |
| `wiki-en-essential` / `wiki-en-extended` | top 100k / 1M en articles | see the first probe run |
| `dict-en` | Open English WordNet 2025: 120,776 headwords | 9.4 MB (26.3 MB installed) |
| `dict-pt` | OpenWordnet-PT v1.1.0: 28,290 headwords | 1.6 MB (4.3 MB installed) |

## Invariants

Every mechanism in this repository serves one of these. When changing code,
keep the comment at each guard/latch/timer stating who clears it and what
happens if that path never runs.

* **I1 — fold parity.** A query finds a key iff `fold(query)` is a prefix of
  `fold(title or alias)`. fold_v1 is DATA (`fold/fold_table.json`, frozen) plus
  three trivial rules, so the Dart port cannot drift; `fold/fold_vectors.json`
  (274 hand-authored cases) is asserted by both sides, and
  `fold/prefix_vectors.json` pins the prefix-range sentinel.
* **I2 — publish atomicity.** The manifest only names assets that exist, are
  complete and match the size/sha256 it states. Packs go into a DRAFT release,
  every asset is verified, the release is published, and only then is
  `manifest.json` committed. A failed run never touches the manifest and
  deletes its own draft. A named asset is never replaced or deleted.
* **I3 — bounded run (quota).** Every job has `timeout-minutes`; pack data
  never travels as workflow artifacts or caches; one concurrency group for
  all publishing workflows; at most 2 connections to the dump host per run,
  with a descriptive User-Agent; a scheduled run with nothing new exits in
  seconds; retention keeps 3 wiki editions, the current dictionary editions,
  and each dictionary's previous edition (its rollback target: the newest
  recorded edition that a rollback did not abandon and a rebuild did not
  replace — see `retention.py`).
* **I4 — selection.** Tiers are prefixes of one ranking per language:
  `popularity_score` DESC, then page id ASC (fallback: `incoming_links` when
  fewer than half the candidates carry a popularity score — recorded in pack
  meta `ranking`). Excluded: namespace ≠ 0, redirect pages, disambiguation
  pages, empty leads.
* **I5 — keepalive + version record.** Each publish commits `manifest.json`
  and `editions/<family>.jsonl`, which keeps the schedules alive and makes
  git history the version record. GitHub disables schedules after 60 days
  without activity and can DROP scheduled runs under load, so a separate
  weekly `keepalive.yml` (its own concurrency group, never cancelled by a
  publish run) commits a heartbeat when nothing was committed for 21 days:
  21 + 7 x 5 = 56 < 60, so four consecutive dropped runs are survivable.
  UNVERIFIED PREMISE: GitHub does not document that a bot (GITHUB_TOKEN)
  push counts as "repository activity", so keepalive also re-enables the
  three scheduled workflows through the documented `.../enable` API every
  week (a second belt, itself only reported — by third-party tools — to
  reset the counter). Neither revives keepalive once GitHub disables it.
  Watch for GitHub's warning email before a workflow is disabled: it means
  "dispatch a workflow by hand now".
  The keepalive never re-enables a workflow an operator disabled by hand
  (`disabled_manually`): it enables only `active` and
  `disabled_inactivity` ones. To PAUSE a pipeline (e.g. after a rollback,
  while investigating), disable it in the Actions tab; keepalive leaves it
  disabled and repeats a weekly warning until you enable it again.
  Rollback = re-point the manifest at a kept edition; the editions line
  records the abandoned tags (`from_tags`), and no later run rebuilds an
  edition an editions log records unless forced. A rollback LOWERS each
  entry's `version`, so it reaches only apps that replace an installed pack
  whose `installed_sha256` differs from the manifest's (SCHEMA.md "Updates
  and rollbacks"). Today's app replaces NO installed pack from the manifest
  (in either direction) and ignores `dict-*` entries altogether (SCHEMA.md
  "Today's app and this manifest"), so until it changes a rollback — like a
  new edition or a rebuild — reaches no device that already has the pack,
  and no device at all for a dictionary (the run warns per entry).
* **I6 — sanity gate.** Before publishing: schema + meta validation, golden
  queries, and article count / installed size within ±15% of the previous
  edition of the same pack (first edition: floors/ceilings from config).
  Failures block publishing unless `force=true`; structural validation can
  never be forced.

## Layout

```
config/packs.json        every content decision (tiers, filters, goldens, limits)
fold/fold_table.json     fold_v1 data (frozen) + fold_vectors.json (parity corpus)
tools/gen_fold_table.py  the generator that produced the table (new versions only)
tools/bench_search.py    1M-article search latency benchmark (synthetic)
tools/bench_parse.py     stream-parse throughput benchmark (synthetic)
rowtalk_packs/           the builder (Python standard library only)
  dumpsource.py          dump listing/resolve, resumable download, bz2 lines
  cirrus.py              tolerant CirrusSearch line parser + page classifier
  abstract.py            lead -> abstract trimming
  staging.py             on-disk candidate staging with bounded pruning
  wikibuild.py           download/decompress/parse pipeline + tier assembly
  packdb.py              pack schema v2 writer + the query recipes
  validate.py sanity.py  validation, golden queries, the I6 gate
  manifest.py editions.py retention.py publish.py github.py
  dictbuild.py           dictionary packs (port of the app's build_dictionary.py)
  probe.py               the probe report
tests/                   unittest suite incl. an end-to-end run on a local server
manifest.json            what the app reads (committed by the workflows)
editions/                append-only publish/rollback log (created on first publish)
```

## How a wiki build runs

`build-wiki.yml` (monthly cron `17 4 3 * *`, or manual dispatch):

1. **resolve** — finds the newest dump date whose `ptwiki_content` AND
   `enwiki_content` index directories both have their `_SUCCESS` marker under
   `https://dumps.wikimedia.org/other/cirrus_search_index/` (`dump.base_url`
   in `config/packs.json`; to build from a mirror such as
   `https://cdimage.debian.org/mirror/wikimedia.org/other/cirrus_search_index/`,
   edit that key — `probe.yml` takes it as its `dump_base` input). If the manifest on `main` (or the checkout) already names that
   edition, or ANY wiki family's editions log records it (published before
   — e.g. the edition a rollback abandoned; a wiki tag is shared by every
   language, so `langs` never narrows this; `force` overrides), the run
   ends here.
   Otherwise it creates the draft release `wiki-YYYY.MM.DD`. A non-dry-run
   refuses to start from any ref but `main`. Listing requests are retried
   under the same bounded policy as downloads (`dump.listing_retry`), and the
   whole scan shares ONE deadline (`run_deadline_s`, below the job timeout),
   so a flaky dump host ends in a clean error, not a timeout kill. A server
   wait (Retry-After) is never shorter than our own backoff, even when its
   date is in the past.
2. **build** (one job per language, ONE AT A TIME, `fail-fast: false`): two
   downloader threads (`dump.connections_per_job`; with one job at a time a
   run never holds more than 2 dump connections) each fetch one shard at a
   time (HTTP Range resume guarded by If-Range, bounded retries, Retry-After
   honoured in both its seconds and HTTP-date forms) into a disk buffer of at most `workers + connections` shards;
   `workers` (3) decompressor threads run bz2 in parallel; the main thread
   parses, classifies and stages candidates in SQLite, pruning to the top N
   under both rankings as it goes. Then it ranks once, writes both tiers as
   prefixes of that ranking, validates, runs the golden queries and the gate,
   gzips, and uploads `<pack>.db.gz` + `<pack>.json` (the report, including
   resume/restart counters) to the draft. A failed gate uploads nothing.
3. **publish** (runs even if a build failed) — publishes every LANGUAGE whose
   packs are all present and verified (size, sha256 via the API digest or a
   full download, the gate re-evaluated) and skips the others, so an enwiki
   that ran out of time never costs ptwiki its edition. It publishes the
   release, waits (bounded, with backoff) for each public URL to answer a
   Range request, writes `manifest.json` + `editions/*.jsonl` (replacing only
   the published pack ids), commits and pushes, prunes old releases, and then
   fails the job if a language was skipped, so the run is red.
4. **cleanup** (only on failure/cancel) — deletes the run's draft. A
   published release no manifest names and no committed editions log
   records (the publish died after undrafting) is kept so "Re-run failed
   jobs" can finish it; the next run for that tag, or retention, removes it
   otherwise.

The keepalive is its own workflow, `keepalive.yml` (weekly, see I5).

Re-running: after a failed BUILD, re-run ALL jobs (the failed run's cleanup
deleted the draft; a build leg re-run on its own stops at once and says so).
After a failed PUBLISH, "Re-run failed jobs" finishes it without rebuilding.

Dispatch inputs: `dump_date`, `langs`, `tiers`, `force`, `dry_run` (build and
validate, publish nothing, create nothing), `rollback_to` (re-point the
manifest at a kept edition; builds nothing). `rollback_to` with `dry_run`
verifies the target (assets, digests, public URLs) and logs the manifest
change, and commits nothing.

`build-dict.yml` (quarterly, or manual): asks the two upstream repositories
for their latest release; builds, gates and publishes only a version not yet
published (`dict-<lang>-<upstream tag>`). Each language is a unit: one
failing (upstream asset renamed, download error, gate) is skipped and the
others are still published AND committed; the run then fails, red.

Recovering from a BAD dictionary build (the upstream version is unchanged,
so the scheduled run will never rebuild it): dispatch `build-dict` with
`langs=<lang>` and `rebuild=true`. That publishes the corrected build as a
NEW revision tag (`dict-<lang>-<upstream tag>-r2`, `-r3`, …) with a higher
`version` (no device takes it until the app installs dictionary packs and
updates installed ones — SCHEMA.md "Today's app and this manifest"); the
published tag is never replaced
(I2) and the sanity gate still applies. `force=true` is a different input:
it only waives the gate (and cannot replace a named edition — it says so in
a warning). When the UPSTREAM content is bad (a rebuild would reproduce
it), dispatch `build-dict` with `rollback_to=<previous dict tag>`: it
re-points `dict-<lang>` at the family's previous edition, which retention
keeps for this (`dry_run` verifies and shows the change, commits nothing).
The abandoned edition is recorded, so the scheduled run never re-publishes
it; once the builder can correct it, `rebuild=true` publishes the fix as a
new revision. Do
NOT hand-edit `manifest.json` to drop an entry.

Both publishing workflows share the concurrency group `rowtalk-packs-publish`
(they both write releases and `manifest.json`). GitHub keeps at most ONE
pending run per group: if a third run queues while one runs and one waits,
the waiting one is cancelled. So a quarterly dict run that lands while a
multi-hour wiki build runs, followed by any other dispatch, is silently
dropped until the next quarter — check the Actions tab after dispatching
during a wiki build, and dispatch `build-dict` by hand if its run was
cancelled.

`probe.yml` (manual): streams 1–4 shards of the real dump and writes a report
to the job summary — line shape, field presence, popularity distribution,
lead lengths, redirects, namespaces, disambiguation-signal hits, throughput
per stage, checksum files in the dump directory, extrapolated pack sizes, and
a GO / MARGINAL / NO-GO time budget for a full build (network, decompression
and main-thread stages costed separately — including one prune measured at
the real keep — not probe wall time x shards). The network figure is the
aggregate rate the probe OBSERVED: with 1 shard (the default) only one
connection ever runs, so the verdict assumes a second connection adds
nothing (a per-IP limit); probe 2+ shards to measure two concurrent
downloads. **Run it first** (ptwiki,
then enwiki): the new sharded dump's exact line shape is not yet verified,
and its budget decides whether the enwiki job fits in the 345-minute
timeout. If enwiki is NO-GO, the fallbacks are a smaller `extended` tier for
en or a mirror; ptwiki publishes either way.

## Quota notes

* Standard runners are free and unlimited for public repositories; the cost
  that matters is wall time per job (6 h cap) and courtesy to Wikimedia.
* Pack data never goes to workflow artifacts (500 MB on Free) or caches.
* enwiki is ~42 GB of bz2 in 66 shards. Throughput measured locally on a
  synthetic dump (Apple M3 Pro): bz2 decompression ~50–59 MB/s of output per
  thread, parse+classify+stage ~335 MB/s on the main thread; with 3
  decompressor threads the pipeline ran at ~95 MB/s of JSON on SYNTHETIC
  data (real-text bz2 calibrated at ~24 MB/s per thread). The download is
  expected to be the bottleneck on the runner (enwiki needs ~1.1 MB/s per
  connection over 2 connections to fit); the probe measures it.
* GITHUB_TOKEN requests per run: tens (limit 1000/hour).

## Local development

```
python3 -m unittest discover -s tests -t .            # full suite, no network
python3 tools/bench_search.py --dir /tmp/bench        # ~0.5 GB, delete afterwards
python3 tools/bench_parse.py --dir /tmp/bench
python3 -m rowtalk_packs build-dict-local --lang en \
    --source path/to/english-wordnet-2025.xml.gz --source-version 2025-edition --out /tmp/out
```

Python 3.12+ (CI pins 3.12), standard library only. Never download Wikimedia
dumps to a laptop for testing; the end-to-end test serves a synthetic
multi-shard dump from `127.0.0.1`, including dropped connections and a server
that ignores `Range`.

## Known limits and open items

* The CirrusSearch sharded dump format is documented only for the deprecated
  location; the parser accepts both bulk (action + document) and
  document-only lines, and the probe confirms the real shape.
  `popularity_score` is undocumented; the build falls back to
  `incoming_links` (logged and recorded) when it is mostly absent.
* Disambiguation detection is config-driven (title suffix, category,
  template names per language); the probe reports hits per signal so the
  lists can be tuned.
* Pushing `manifest.json` uses `GITHUB_TOKEN`; a branch protection rule on
  `main` that blocks it would make every publish fail (and clean up after
  itself).
