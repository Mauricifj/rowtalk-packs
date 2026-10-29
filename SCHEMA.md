# Rowtalk packs — data contract

This document is the contract between this repository (the builder) and the
Rowtalk app (the reader). It is written so the app side can be implemented in
Dart without reading any Python. Every SQL statement here is the exact one
the builder's own tests, validator and golden queries run
(`rowtalk_packs/packdb.py`).

Contents: [Packs](#packs) · [fold_v1](#fold_v1) · [Pack schema v2](#pack-schema-v2) ·
[Query recipes](#query-recipes) · [Manifest v2](#manifest-v2) ·
[Install procedure](#install-procedure-app-side) · [Versioning](#versioning)

---

## Packs

| pack id | kind | tier | content |
|---|---|---|---|
| `wiki-pt-essential` | wiki | essential | top 50,000 pt Wikipedia articles |
| `wiki-pt-extended` | wiki | extended | top 300,000 pt Wikipedia articles |
| `wiki-en-essential` | wiki | essential | top 100,000 en Wikipedia articles |
| `wiki-en-extended` | wiki | extended | top 1,000,000 en Wikipedia articles |
| `dict-en` | dict | standard | Open English WordNet |
| `dict-pt` | dict | standard | OpenWordnet-PT |

Tier sizes live in `config/packs.json`. Both wiki tiers of a language are
prefixes of ONE ranking (popularity, then page id), so **Extended ⊇ Essential**
and an article has the same `rank` in both. A device needs only one tier per
language; Extended replaces Essential.

Delivery: each pack is ONE SQLite file, gzip-compressed (`<pack-id>.db.gz`,
gzip level 9, no embedded file name, mtime 0), attached to an immutable GitHub
release. The manifest describes both the download (`.gz`) and the installed
(`.db`) bytes.

---

## fold_v1

`fold` turns any string into a **search key**. It is the only text
normalization used for search, on both sides:

> **Parity rule (I1).** A query typed in the app finds an index key iff
> `fold(query)` is a prefix of the key — and keys are `fold(title)` and
> `fold(alias)`.

Dart core has no Unicode normalization and its `toLowerCase` is not
guaranteed to match any other implementation across Unicode versions, so
fold_v1 performs **no runtime Unicode logic at all**. Every decision is data,
in `fold/fold_table.json`, generated once (Python 3.14.2, Unicode 16.0.0)
and frozen.

### Algorithm (complete)

```
fold(s):
  1. out = ""
     for each CODE POINT c of s:            // Dart: s.runes (not UTF-16 units)
       out += TABLE[c] if c in TABLE else c  // a lone surrogate passes through
  2. replace every run of U+0020 SPACE in out by a single U+0020
  3. remove leading and trailing U+0020
  return out
```

That is all. There is no separate lowercase step: even `A`–`Z` are table
entries. Whitespace of every kind is mapped to U+0020 by the table, so step 2
only ever needs to look for U+0020.

### Table file

```json
{
  "fold_version": 1,
  "unicode_version": "16.0.0",
  "python_version": "3.14.2",
  "generator": "tools/gen_fold_table.py",
  "note": "...",
  "entry_count": 9134,
  "entries": {
    "0041": "a",
    "00C7": "c",
    "00DF": "ss",
    "0130": "i",
    ...
  }
}
```

* Keys are uppercase hexadecimal code points (`int.parse(key, radix: 16)`).
* Values are strings (possibly empty, possibly several code points). JSON
  escapes inside values are ordinary JSON `\uXXXX` (astral characters as
  surrogate pairs) — any JSON decoder yields the right string.
* A code point that is NOT a key maps to itself.
* Every value consists only of fixed points (code points that are not keys,
  or map to themselves). Hence `fold(fold(x)) == fold(x)`, and the app never
  needs to apply the table twice.
* No value contains U+10FFFF, and U+10FFFF itself maps to `""`. The prefix
  query below relies on this.
* SHA-256 of the committed file:
  `17884aad20ea7bb68a9a2d2b7881d93e7fdee8b059e386eb21f3bb6bf8367e6f`
  (pinned by `tests/test_fold.py`; the app should pin it too).

A Dart port is mechanical:

```dart
String fold(String s, Map<int, String> table) {
  final b = StringBuffer();
  for (final c in s.runes) {
    final m = table[c];
    if (m != null) { b.write(m); } else { b.writeCharCode(c); }
  }
  return b.toString().replaceAll(RegExp(' +'), ' ').trim();  // trim(): see note
}
```

Note: Dart's `String.trim()` removes all Unicode whitespace, not only U+0020.
That is still equivalent here, because after step 1 the only whitespace
character that can remain is U+0020 (the table maps every other whitespace
code point to it). A strict port may trim U+0020 only.

### What the table does (rules used to generate it, for understanding only)

| rule | effect | examples |
|---|---|---|
| noncharacters | dropped | U+FDD0, U+10FFFF |
| whitespace (incl. NBSP, ideographic space, line separators) | `" "` | `a b` → `a b` |
| controls and format characters (ZWSP, ZWJ, soft hyphen, BOM, bidi marks) | dropped | `x​y` → `xy` |
| combining diacritics, Hebrew points, Arabic harakat, variation selectors | dropped | `é` → `e`, `❤️` → `❤` |
| joiners: apostrophes (straight/curly/okina), `.`, `·` | dropped | `O’Brien` → `obrien`, `E.U.A.` → `eua` |
| `#` `&` `@` `%` (and their fullwidth forms) | kept | `C#`, `AT&T`, `100%` |
| all other punctuation (hyphens, dashes, brackets, quotes, `,;:!?/*_`) | `" "` | `Rio-de-Janeiro` → `rio de janeiro` |
| modifier symbols (`^`, `¨`, emoji skin-tone modifiers) | dropped | |
| non-ASCII decimal digits | ASCII digit | `٣` → `3` |
| Hangul syllables | unchanged | |
| everything else | NFKD → casefold → strip the marks above → ligature/letter extras (`æ→ae`, `œ→oe`, `ø→o`, `ł→l`, `đ/ð→d`, `þ→th`, `ı→i`), then each resulting code point folded again | `Straße` → `strasse`, `İstanbul` → `istanbul`, `ﬁ` → `fi`, `ＡＢＣ` → `abc`, `Ⅻ` → `xii` |

Known, accepted consequences: `Й` folds to `и`, `ñ` to `n` (diacritic-blind
search, like Latin); kana keep their voicing (`が` stays `が`) but a
decomposed `か + U+3099` does NOT become `が` (Japanese input is precomposed
in practice); `½` becomes `1⁄2` (the fraction slash is a math symbol and is
kept).

### Parity corpus

`fold/fold_vectors.json` holds ≥ 200 hand-authored cases
(`{"in": ..., "out": ..., "note": ...}`) covering pt/en accents, ç, ã/õ, ß,
İ/ı, ligatures, full-width forms, curly vs straight apostrophes,
hyphens/dashes, parentheses, dotted acronyms, digits, emoji, CJK, RTL,
combining sequences, empty strings, whitespace runs and NBSP. The builder's
tests assert every case; **the app's tests must assert the same file**.

### Changing fold

Never edit fold_v1. A change is a new version: new table file, new
`fold_version`, new vector corpus, and every pack records the version it was
built with (`meta.fold_version`, `manifest.fold_version`). An app must refuse
(or ignore) a pack whose `fold_version` it does not implement.

---

## Pack schema v2

Plain SQLite (page size 4096, no WAL), public reference data — never opened
with a SQLCipher key. All tables exist in every pack of the kind.

### Wiki pack (`kind = wiki`)

```sql
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;

CREATE TABLE articles(
  rank     INTEGER PRIMARY KEY,   -- 1 = most popular; contiguous 1..article_count
  page_id  INTEGER NOT NULL,      -- Wikipedia page id (unique)
  title    TEXT NOT NULL,         -- display title, as on Wikipedia
  abstract TEXT NOT NULL          -- plain text, <= meta.abstract_max_chars CODE POINTS
);                                --   (Dart: runes.length; String.length counts UTF-16
                                  --   units and can be up to 2x for astral text)

CREATE TABLE search(
  key  TEXT NOT NULL,             -- fold_v1(title) or fold_v1(alias); never ''
  rank INTEGER NOT NULL,          -- articles.rank
  PRIMARY KEY (key, rank)
) WITHOUT ROWID;

CREATE TABLE prefix_top(
  prefix TEXT PRIMARY KEY,        -- a HEAVY prefix (see below)
  ranks  TEXT NOT NULL            -- up to meta.suggestions distinct ranks,
) WITHOUT ROWID;                  --   ascending, comma-separated decimal
```

Every article has at least one `search` row (an article whose title folds to
`""`, e.g. `!!!`, and that no alias names is excluded at build time as
unreachable). The validator checks this, the primary keys and `WITHOUT
ROWID` of every table, and `PRAGMA encoding = UTF-8` (the prefix range below
relies on UTF-8 byte order); a pack failing any of them is never published.

#### Abstract

`abstract` is the lead's opening text: C0/C1 control characters removed
(format characters such as ZWJ, ZWNJ and bidi marks are kept), whitespace
collapsed, stripped-template leftovers scrubbed (`()`, `( )`, `(; )`, a
separator just inside a group's parentheses, a space before `,`/`;`, a space
before a `.` that ends a token — `.NET`, `.45`, `.com` keep theirs). A lead
that fits in `abstract_max_chars` is kept whole; otherwise the longest run of
whole sentences that fits and is balanced (every `(`, `[`, `“`, `«` closed
by a later partner, checked with a running depth; an even number of `"`) —
of any length, so a short clean first sentence is the abstract; otherwise a
word-boundary cut plus `…` (a single over-long token is cut at a grapheme
boundary). An abstract without `…` always ends at a boundary of a TRUSTED
KIND; a kind that is ambiguous in encyclopedic text is never trusted, and the
kinds are decided by the shape of the text rather than by a word list being
complete. The token before a `.` is judged whole — or, when it is JOINED by
a hyphen, dash or slash and its last segment starts with an uppercase letter,
by that segment ("Lt-Col." as "Col.", "Vice-Adm." as "Adm."). Not trusted:
- `!` and `?` are never sentence ends (in leads they are almost always part
  of a name or title: "Yahoo!", "!!!", "O que é isso, companheiro? (1997)");
- a `.` after an empty token, a single character ("J. Smith"), or a dotted
  token ("U.S.");
- a `.` after a short capitalised token (≤ 4 letters, uppercase first, rest
  lowercase: "Mr.", "Pte.", "Msgr.", "Bros.") unless the WHOLE next token is
  one of the language's `sentence_starters` ("The", "It", "O", "Em",
  "Located", "Localizado", …) — such an abbreviation introduces a name, never
  an article or a lead's participle; a missing starter only costs content;
- a `.` after a configured abbreviation — the language's own list plus
  `abstract.shared_abbreviations`, which every language uses because names
  are quoted verbatim ("Warner Bros.", "Kramer vs. Kramer") — or its plural:
  a lowercase plural ("arts.") or a capitalised plural of exactly 5 letters
  ("Profs.", "Capts."). A lowercase or capitalised listed form is never
  re-trusted by a starter. An ALL-CAPS listed form ("CIA.", "PTE.", "CO.")
  is the agency or a state code as often as a company name in capitals
  ("CIA. HERING", "PTE. LTD."), so it is trusted only before a starter; an
  all-caps token no list names is an acronym ("NASA." ends a sentence);
- a `.` before a digit after a non-numeric word of at most 3 characters
  that does not itself follow a number ("Op. 27", "art. 5"; but "12 km. 18
  villages" is a real end — after a number the word is a unit).

Words the rules above would misread — short place names ("Sul", "Pará",
"Ohio", "Peru") and plurals that are words ("press", "corps") — are listed
as `not_abbreviations` (per language plus `abstract.shared_not_abbreviations`)
and end a sentence like any word. That list RE-TRUSTS, so it names only words
that are never abbreviations (a test keeps it disjoint from every
abbreviation list); "figs", "reps", "gens" stay out ("Figs. 3–5" and "Reps.
Smith and Jones" are abbreviations).

Rejecting a real boundary only shortens an abstract or ends it in `…`.
RESIDUAL, stated rather than hidden: an abbreviation no list names that is
followed by an uppercase word, a digit or an opener is still trusted when it
is LOWERCASE (including a joined token whose last segment is lowercase:
"vice-adm."), capitalised and longer than 4 letters (other than a listed
form's 5-letter plural: "Comdr."), or ALL-CAPS (named by no list this
language's rules hold: "PTY.", or a pt-only "STO." in an English lead); and
a short capitalised or listed all-caps abbreviation is re-trusted when the
name after it is spelled like a starter. Each one found is closed by adding
it to a list.

### Dictionary pack (`kind = dict`)

```sql
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;

CREATE TABLE words(               -- UNCHANGED from the app's bundled assets
  lemma  TEXT NOT NULL,           -- NFC + lowercase + trim, diacritics kept
  pos    TEXT NOT NULL,           -- n | v | a | s | r (WN-LMF partOfSpeech)
  senses TEXT NOT NULL,           -- JSON [{"d": def, "s": [synonyms], "e": example?}]
  PRIMARY KEY (lemma, pos)
) WITHOUT ROWID;

CREATE TABLE lemmas(
  rank  INTEGER PRIMARY KEY,      -- 1 = most senses; contiguous
  lemma TEXT NOT NULL             -- equals words.lemma (exact-lookup key)
);

CREATE TABLE search(key TEXT NOT NULL, rank INTEGER NOT NULL,
                    PRIMARY KEY (key, rank)) WITHOUT ROWID;   -- key = fold_v1(lemma)
CREATE TABLE prefix_top(prefix TEXT PRIMARY KEY, ranks TEXT NOT NULL) WITHOUT ROWID;
```

The `words` rows are produced by the same code and knobs as the app's
`tool/build_dictionary.py` (verified row-for-row identical on the 2025 OEWN
and OWN-PT v1.1.0 sources), so the existing dictionary reader and lemmatizer
pipeline work unchanged against `words`. Suggestion rank = number of senses
across all parts of speech (descending), then lemma length, then code point
order (`meta.ranking = "sense_count"`).

### Heavy prefixes

A prefix `p` is HEAVY when it is at least 2 code points long, does not end
in U+0020, is a prefix of some search key, and MORE than
`meta.heavy_prefix_rows` rows of `search` start with it. `prefix_top` holds
exactly the heavy prefixes, each with the best (smallest) distinct ranks
among all rows starting with it.

Invariant the recipe relies on: for any folded query `q`, either
`prefix_top` has a row for `q` (and it is the exact answer), or at most
`heavy_prefix_rows` search rows start with `q`, so the range scan is bounded.
The validator samples both directions of this on every build.

### `meta` keys

All values are TEXT.

| key | wiki | dict | meaning |
|---|---|---|---|
| `schema_version` | `2` | `2` | this document |
| `fold_version` | `1` | `1` | fold used for `search.key` / `prefix_top.prefix` |
| `kind` | `wiki` | `dict` | |
| `pack_id` | `wiki-pt-extended` | `dict-en` | |
| `language` | `pt` | `en` | BCP-47 primary subtag |
| `tier` | `essential`/`extended` | `standard` | |
| `edition` | `2026.10.25` | `2025-edition` | human edition label |
| `version` | `20261025` | `2025030100` | integer edition: wiki = dump date; dict = upstream release date x 100 + (rebuild revision - 1), so a corrected rebuild of the same upstream is a HIGHER version. LIMIT: it orders by DATE only — two upstream versions released the same day get the same `version`, and a rebuild of the older one outranks the newer one. Never an update test (see "Updates and rollbacks") |
| `article_count` | articles | lemmas | rows in the entity table |
| `key_count` | | | rows in `search` |
| `ranking` | `popularity_score` or `incoming_links` | `sense_count` | how ranks were assigned |
| `heavy_prefix_rows` | `1000` | `1000` | threshold used for `prefix_top` |
| `heavy_prefix_count` | | | rows in `prefix_top` |
| `suggestions` | `8` | `8` | ranks stored per heavy prefix |
| `min_query_chars` | `2` | `2` | suggestions start at this folded length |
| `abstract_max_chars` | `600` | — | abstract bound (code points) |
| `license`, `license_url`, `attribution` | | | data license (see DATA-LICENSES.md) |
| `source`, `source_url` | | | origin of the data |
| `source_date` | `20261025` | — | dump date |
| `source_version` | — | upstream tag | |
| `built_at`, `builder_commit` | | | provenance |

---

## Query recipes

Bind parameters positionally. `?2` in the prefix scan is the folded query
followed by the single code point U+10FFFF (`'$q\u{10FFFF}'` in Dart; in
UTF-8 the bytes `F4 8F BF BF`). Because fold_v1 never emits U+10FFFF, every
key that starts with `q` sorts in `[q, q + U+10FFFF)` under SQLite's BINARY
collation (UTF-8 byte order = code point order), and nothing else does — the
range is exact, not an approximation. `fold/prefix_vectors.json` pins this
with keys whose next code point is astral (U+1F600, U+20000) or high-BMP
(U+FFEF, U+FFFD): an implementation using U+FFFF (a natural "max char" in
UTF-16 Dart) as the sentinel fails it. **The app's tests must assert that
file too.**

### Suggestions (type-ahead, both kinds)

```
q = fold(input)
if codePointLength(q) < meta.min_query_chars: return []       // 2

row = SELECT ranks FROM prefix_top WHERE prefix = ?1           -- ?1 = q
if row exists:
    ranks = row.ranks split on ',' as ints, first meta.suggestions
else:
    ranks = SELECT DISTINCT rank FROM search
            WHERE key >= ?1 AND key < ?2
            ORDER BY rank LIMIT ?3                             -- ?1 = q, ?2 = q + U+10FFFF, ?3 = 8

wiki: SELECT rank, title FROM articles WHERE rank IN (<ranks>)   -- display in ranks order
dict: SELECT rank, lemma FROM lemmas   WHERE rank IN (<ranks>)
```

Results are ordered by rank (most popular / most senses first), deduplicated
per article (an article matched by its title and an alias appears once).

### Exact lookup (`/wiki <term>` + Enter)

```
q = fold(input); if q == "": miss
rank = SELECT rank FROM search WHERE key = ?1 ORDER BY rank LIMIT 1
SELECT title, abstract FROM articles WHERE rank = ?1
```

An alias that equals the query ("usa" → United States) is an exact hit. The
best-ranked article wins when several fold to the same key.

### Dictionary entry

Unchanged: `SELECT pos, senses FROM words WHERE lemma = ?` with the app's
existing normalization (NFC + lowercase + trim) and lemmatizer. The new
`search`/`prefix_top` tables only add type-ahead; selecting a suggestion
yields a `lemmas.lemma`, which IS a `words.lemma`.

### Measured latency (reference recipe, Python sqlite3, Apple M3 Pro, warm cache)

Synthetic 1,000,000-article index (2,399,284 keys, 432 MB):

| structure | 2-char p95 | 3-char p95 | 4-char p95 | worst bounded scan |
|---|---|---|---|---|
| range scan only (no prefix_top) | 17.91 ms (max 31.8) | 5.52 ms | 3.82 ms | — |
| prefix_top, threshold 250 (6,693 rows) | 0.02 ms | 0.03 ms | 0.04 ms | 0.04 ms |
| **prefix_top, threshold 1000 (1,841 rows) — chosen** | **0.02 ms** | **0.10 ms** | **0.13 ms** | **0.10 ms** |
| prefix_top, threshold 4000 (157 rows) | 0.02 ms | 0.45 ms | 0.32 ms | 0.49 ms |

Real dictionaries (threshold 1000): dict-en 2-char p95 0.13 ms, dict-pt
0.10 ms. Reproduce with `python3 tools/bench_search.py --dir <tmp>`.

---

## Manifest v2

Committed at the repository root and served by GitHub as
`https://raw.githubusercontent.com/Mauricifj/rowtalk-packs/main/manifest.json`.

```json
{
  "manifest_version": 2,
  "updated_at": "2026-11-03T05:41:07Z",
  "packs": [
    {
      "id": "wiki-pt-essential",
      "kind": "wiki",
      "language": "pt",
      "version": 20261025,
      "sizeBytes": 31234567,
      "sha256": "<hex sha-256 of the .db.gz>",
      "url": "https://github.com/Mauricifj/rowtalk-packs/releases/download/wiki-2026.10.25/wiki-pt-essential.db.gz",

      "family": "wiki-pt",
      "tier": "essential",
      "edition": "2026.10.25",
      "tag": "wiki-2026.10.25",
      "schema_version": 2,
      "fold_version": 1,
      "encoding": "gzip",
      "installed_bytes": 71234567,
      "installed_sha256": "<hex sha-256 of the .db>",
      "article_count": 50000,
      "license": "CC BY-SA 4.0",
      "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
      "attribution": "Wikipedia contributors",
      "source": {"wiki": "ptwiki", "dump_date": "20261025",
                 "url": "https://dumps.wikimedia.org/other/cirrus_search_index/20261025/index_name%3Dptwiki_content/",
                 "site": "https://pt.wikipedia.org/"}
    }
  ]
}
```

* The first seven keys are the v1 keys the app's current tolerant parser
  reads, with their v1 meaning: `sizeBytes` and `sha256` describe the
  DOWNLOADED bytes (the `.gz`). `version` is always an integer (a string
  there would make today's parser throw).
* `kind` is `wiki` or `dict`. Dict entries use `tier: "standard"`,
  `article_count` = number of lemmas, and `source = {name, version, url}`.
* `url` is absolute and points at an IMMUTABLE release asset; GitHub answers
  it with a redirect to `objects.githubusercontent.com` /
  `release-assets.githubusercontent.com`, which honours `Range` (resume).
* Unknown keys must be ignored; keys are never removed in later manifest
  versions.
* A publish replaces the entries whose `id` it publishes and keeps every
  other entry, including the other tier of the same language (a run that
  rebuilds only `essential` leaves `extended` pointing at its older, still
  kept release). Tiers of one language can therefore name different
  editions for a while.
* Guarantee (I2): every asset the manifest names exists, is complete and
  matches `sizeBytes`/`sha256`; a named asset is never replaced or deleted.

### Editions and tags

* Wiki editions: tag `wiki-YYYY.MM.DD` (the dump date), one release holding
  every wiki pack built from that dump. The newest 3 wiki editions are kept
  (plus any edition the manifest names, e.g. after a rollback).
* Dictionary editions: tag `dict-<lang>-<upstream tag>` (e.g.
  `dict-en-2025-edition`, `dict-pt-v1.1.0`; a corrected build of the same
  upstream version is `…-r2`, `-r3`, …). Kept: the edition the manifest
  names plus, per dictionary family, one edition an editions log records —
  the rollback target: the most recently created one that a rollback did
  not abandon and a rebuild did not replace (either kind is kept only when
  no other is recorded).
* `editions/<family>.jsonl` (families `wiki-pt`, `wiki-en`, `dict-en`,
  `dict-pt`) is an append-only log of every publish and rollback with counts,
  sizes and digests; git history of `manifest.json` is the version record.
  A rollback line also carries `from_tags` (the tags the family was served
  before). A tag any log line records as published (or abandoned by a
  rollback) is never deleted or rebuilt by a later run unless `force=true`;
  only a published release NO log records — a publish that died before its
  commit — is treated as an orphan and rebuilt. (The `heartbeat.jsonl` file
  in the same directory is the keepalive's, not a family log.)

---

## Install procedure (app side)

1. GET the manifest; skip entries whose `schema_version` or `fold_version`
   the app does not implement, or whose `kind` it does not know.
2. Download `url` with `Range` resume (follow redirects). Abort as soon as
   more than `sizeBytes` bytes arrive.
3. Verify SHA-256 of the downloaded bytes against `sha256`.
4. Gunzip to a temporary file, aborting as soon as the output exceeds
   `installed_bytes` (decompression-bomb bound); verify `installed_bytes`
   and `installed_sha256`.
5. Open read-only, check `meta.schema_version`, `meta.fold_version`,
   `meta.kind`, `meta.language`, `meta.pack_id` against the manifest entry.
6. Atomically rename into place; delete the `.gz`.

### Updates and rollbacks

The manifest names exactly ONE edition per pack `id`, and that edition is
the one a device should have. An installed pack is out of date when its
`installed_sha256` differs from the entry's — whichever way `version`
moved. A rollback (the operator re-pointing the manifest at an older kept
edition after a bad one shipped) LOWERS `version`; an app that only takes a
higher version would keep the bad edition, and the rollback would reach
only new installs. `version` stays useful for display and for ordering
imported files, never as the update test for a manifest entry.

### Today's app and this manifest (until the app implements v2)

The app's current reader (`lib/core/knowledge/pack_manager.dart`) parses v2
entries without error but cannot use them correctly. Keep this manifest's URL
out of any shipped default until the app changes below land:

* **Both wiki tiers collapse onto one id.** The packs list maps both
  entries to `wiki_<lang>` and keeps the LAST (Extended, after the manifest's
  tier sort) — and, as its manifest loop does not de-duplicate, lists that
  row twice — so the rows show Extended's size, while the download path
  picks the FIRST entry (Essential). Shown size and downloaded pack
  disagree. Fix: key rows by `id` and add a tier choice (design brief).
* **Every tap downloads the full `.gz` and throws it away.** The `.gz`
  passes the sha256/size gate (both describe the `.gz`), then fails the
  "is a pack" validation because nobody gunzipped it; it is deleted with
  `reason=not-a-pack` and the pill returns. Honouring `encoding: "gzip"`
  (step 4 below) is NOT enough on its own: the gunzipped pack still fails
  that validation (next item). Until the reader moves to v2, the only fix is
  to skip entries whose `schema_version` or `encoding` the app does not
  implement.
* **The wiki validator and reader query a column v2 does not have.** The
  "is a pack" probe (`SELECT <len>(lemma), <len>(title), <len>(abstract)
  FROM articles LIMIT 1`) and the `/wiki` lookup (`SELECT title, abstract
  FROM articles WHERE lemma = ?`) both fail on a v2 pack with "no such
  column: lemma": v2 `articles` is `(rank, page_id, title, abstract)`.
  Fix: move both to the v2 schema — validate `meta`, `articles`, `search`
  and `prefix_top`, and look up through `search` on the fold_v1 key, joined
  to `articles` by `rank`, using `prefix_top` for heavy prefixes, exactly as
  in "Query recipes" above. This is the largest app change v2 implies.
* **Dictionary entries are ignored.** The packs list skips every entry
  whose kind is not the encyclopedia (the dictionary is a bundled asset
  today), so `dict-*` entries are neither listed nor downloaded: no
  dictionary publish, rebuild or rollback here reaches any device. Fix:
  list and install `kind = dict` entries (the bundled asset becomes the
  fallback), with the dict reader on the v2 `words` + `search` schema.
* **No manifest change reaches an installed pack.** `download` returns at
  once for a pack already installed, and the installed scan records no
  version or digest, so neither a newer edition, a `-rN` rebuild nor a
  rollback replaces an installed pack; only new installs see it. Fix: the
  next item's rule, with the installed `installed_sha256` recorded.
* **A rollback does not reach installed packs** (even once the previous
  item is fixed, if the update is gated on `version`). Its only version
  rule today is the file-import one ("same or older version: nothing
  changes"), and a rollback lowers `version`; a newer dictionary upstream
  released on the same day as the served one ties it, or is lower than a
  rebuild of it (the `version` LIMIT in `meta` keys). Fix: treat an entry whose
  `installed_sha256` differs from the installed pack's as the update
  ("Updates and rollbacks" above); never gate a manifest-driven update on
  `version` going up.
* Also implied: the pack id scheme (`wiki-<lang>-<tier>`, `dict-<lang>`
  instead of `wiki_<lang>`), `maxPackSizeBytes` (512 MiB) against
  wiki-en-extended's installed size, following redirects with `Range` to
  `objects.githubusercontent.com`, the fold_v1 port with
  `fold_vectors.json` + `prefix_vectors.json`, and abstracts measured in
  `runes`.

---

## Versioning

| what | where | changes when |
|---|---|---|
| `manifest_version` | manifest root | the manifest's shape gains meaning that old readers would misread |
| `schema_version` | pack meta + manifest entry | a table/column contract changes |
| `fold_version` | pack meta + manifest entry | the fold table or algorithm changes (never edit v1) |
| `edition` / `version` / `tag` | pack meta + manifest entry | new data |
