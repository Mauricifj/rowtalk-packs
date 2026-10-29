"""Dictionary packs (dict-en, dict-pt) from WN-LMF wordnets.

Ported from the app's tool/build_dictionary.py: the `words(lemma, pos,
senses)` WITHOUT ROWID table, the short sense keys d/s/e and every size knob
are unchanged, so the app's existing dictionary reader keeps working on the
`words` table as-is. ADDED: the same fold_v1 search structure the wiki packs
use (lemmas + search + prefix_top), so `/dict` gets prefix suggestions.

Suggestion ranking (recorded in meta `ranking` = "sense_count"): wordnets
carry no frequency, so lemmas are ordered by their number of senses across
all parts of speech (more senses ~ more common word), then by length, then
by code point order.

Sources are downloaded in the GitHub Action only; locally a cached file can
be passed with --source (read-only; .xml, .xml.gz or .tar.xz).
"""

import gzip
import json
import os
import tarfile
import time
import unicodedata
import xml.etree.ElementTree as ET

from . import artifacts, editions, packdb, sanity, validate
from .fold import FOLD_VERSION, fold
from .log import log


def normalize_lemma(raw):
    # Lookup normalization v1 of the app's dictionary reader: NFC + lowercase
    # + trim, diacritics KEPT. (The `words.lemma` exact-lookup key; search
    # suggestions use fold_v1 keys instead.)
    return unicodedata.normalize("NFC", raw).strip().lower()


def open_source(path):
    """A binary file object over the WN-LMF XML inside `path`."""
    if path.endswith(".tar.xz") or path.endswith(".tar.gz") or path.endswith(".tar"):
        tar = tarfile.open(path)
        members = sorted((m for m in tar.getmembers()
                          if m.isfile() and m.name.endswith(".xml")
                          and not os.path.basename(m.name).startswith("._")),
                         key=lambda m: m.name)
        if not members:
            raise SystemExit("no .xml member inside %s" % path)
        return tar.extractfile(members[-1])
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    return open(path, "rb")


def parse_lmf(stream, max_definition_len, max_example_len):
    entries = []
    synsets = {}
    cur_lemma = None
    cur_pos = None
    cur_synsets = []
    for _event, el in ET.iterparse(stream, events=("end",)):
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "Lemma":
            cur_lemma = el.get("writtenForm") or ""
            cur_pos = el.get("partOfSpeech") or "?"
        elif tag == "Sense":
            synset = el.get("synset")
            if synset:
                cur_synsets.append(synset)
        elif tag == "LexicalEntry":
            if cur_lemma and cur_synsets:
                entries.append((cur_lemma, cur_pos, cur_synsets))
            cur_lemma, cur_pos, cur_synsets = None, None, []
            el.clear()
        elif tag == "Synset":
            sid = el.get("id")
            definition = ""
            example = ""
            for child in el:
                ctag = child.tag.rsplit("}", 1)[-1]
                if ctag == "Definition" and not definition:
                    definition = (child.text or "").strip()
                elif ctag == "Example" and not example:
                    example = (child.text or "").strip().strip('"')
            synsets[sid] = (definition[:max_definition_len], example[:max_example_len])
            el.clear()
    return entries, synsets


def build_words(entries, synsets, knobs):
    """(normalized lemma, pos) -> senses, exactly as tool/build_dictionary.py.
    Also returns {lemma: total synset count} for ranking."""
    members = {}
    for lemma, _pos, sids in entries:
        for sid in sids:
            members.setdefault(sid, []).append(lemma)
    words = {}
    seen_defs = {}
    sense_count = {}
    for lemma, pos, sids in entries:
        key = (normalize_lemma(lemma), pos)
        if not key[0] or key[0].count(" ") >= knobs["max_lemma_words"]:
            continue
        bucket = words.setdefault(key, [])
        dedup = seen_defs.setdefault(key, set())
        for sid in sids:
            definition, example = synsets.get(sid, ("", ""))
            synonyms = [m for m in members.get(sid, ()) if normalize_lemma(m) != key[0]]
            synonyms = list(dict.fromkeys(synonyms))[:knobs["max_synonyms"]]
            if not definition and not synonyms:
                continue
            fingerprint = (definition, sid)
            if fingerprint in dedup:
                continue
            dedup.add(fingerprint)
            sense = {"d": definition, "s": synonyms}
            if example and len(bucket) < knobs["examples_on_first_n_senses"]:
                sense["e"] = example
            bucket.append(sense)
    rows = []
    for (lemma, pos), senses in words.items():
        if not senses:
            continue
        sense_count[lemma] = sense_count.get(lemma, 0) + len(senses)
        rows.append((lemma, pos, json.dumps(senses[:knobs["max_senses"]], ensure_ascii=False,
                                            separators=(",", ":"))))
    rows.sort()
    return rows, sense_count


def ranked_lemmas(sense_count):
    ordered = sorted(sense_count, key=lambda lemma: (-sense_count[lemma], len(lemma), lemma))
    return [(rank, lemma, [fold(lemma)], ()) for rank, lemma in enumerate(ordered, start=1)]


def build_dict_pack(cfg, lang, source_path, source_meta, out_dir, editions_dir, force=False):
    """Build dict-<lang>. source_meta: {version, tag, url, edition, version_int}."""
    dcfg = cfg["dict"]
    lcfg = dcfg["languages"][lang]
    search = cfg["search"]
    pid = "dict-%s" % lang
    family = pid
    started = time.perf_counter()
    with open_source(source_path) as stream:
        entries, synsets = parse_lmf(stream, dcfg["max_definition_len"], dcfg["max_example_len"])
    log("%s: %d lexical entries, %d synsets (%.0fs)" % (pid, len(entries), len(synsets),
                                                        time.perf_counter() - started))
    rows, sense_count = build_words(entries, synsets, dcfg)
    db_path = os.path.join(out_dir, "%s.db" % pid)
    gz_path = os.path.join(out_dir, "%s.db.gz" % pid)
    meta = {
        "pack_id": pid, "language": lang, "tier": "standard", "edition": source_meta["edition"],
        "version": source_meta["version_int"], "fold_version": FOLD_VERSION,
        "ranking": "sense_count", "min_query_chars": search["min_query_chars"],
        "license": lcfg["license"], "license_url": lcfg["license_url"],
        "attribution": lcfg["attribution"], "source": lcfg["label"],
        "source_url": source_meta["url"], "source_version": source_meta["version"],
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "builder_commit": os.environ.get("GITHUB_SHA", "local"),
    }
    counts = packdb.write_pack(db_path, "dict", meta, ranked_lemmas(sense_count), words=rows,
                               heavy_rows=search["heavy_prefix_rows"],
                               suggestions=search["suggestions"])
    log("%s: %d words rows, %d lemmas, %d keys, %d heavy prefixes, %.1f MB" % (
        pid, len(rows), counts["article_count"], counts["key_count"],
        counts["heavy_prefix_count"], os.path.getsize(db_path) / 1e6))
    meta_read, errors = validate.validate_pack(db_path, "dict", expect={"pack_id": pid,
                                                                        "language": lang})
    golden = validate.run_golden(db_path, "dict", lcfg.get("golden", []),
                                 search["min_query_chars"], search["suggestions"])
    gate = sanity.evaluate(counts["article_count"], os.path.getsize(db_path), golden, errors,
                           editions.previous_stats(editions_dir, family, pid),
                           lcfg.get("first_edition", {}).get("standard"),
                           cfg["sanity"]["tolerance"], force)
    log("%s: %s" % (pid, sanity.summarize(gate)))
    artifacts.gzip_file(db_path, gz_path)
    report = artifacts.pack_report(
        pid, family, "dict", lang, "standard", source_meta["edition"], source_meta["version_int"],
        source_meta["tag"], db_path, gz_path, meta_read or {
            "article_count": 0, "key_count": 0, "schema_version": packdb.SCHEMA_VERSION,
            "fold_version": FOLD_VERSION, "ranking": "sense_count", "license": lcfg["license"],
            "license_url": lcfg["license_url"], "attribution": lcfg["attribution"]},
        errors, golden, gate, {"name": lcfg["label"], "version": source_meta["version"],
                               "url": source_meta["url"]})
    artifacts.write_json(os.path.join(out_dir, "%s.json" % pid), report)
    log("%s: gz %.1f MB (installed %.1f MB)" % (pid, report["download_bytes"] / 1e6,
                                                report["installed_bytes"] / 1e6))
    return report


def dict_tag(lang, upstream_tag):
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in upstream_tag)
    return "dict-%s-%s" % (lang, safe)


# REBUILD REVISIONS (DC3-03). A dict tag is derived from the upstream version,
# which can stay unchanged for years, and a tag a manifest names is never
# replaced (I2). So a corrected build of the SAME upstream version is a NEW
# tag: revision 1 is `dict_tag(...)` itself, revision N >= 2 is
# `<base>-r<N>`. Its integer `version` must also grow, or an app that has the
# broken build installed ("same or older version: nothing changes") never
# takes the fix: version = upstream release date YYYYMMDD x 100 + (N - 1).
# The bound: at most REVISION_MAX revisions per upstream version (a request
# past it raises; it never wraps into the next date's range).
REVISION_MAX = 99


def revision_tag(base, revision):
    if not 1 <= revision <= REVISION_MAX:
        raise ValueError("revision %d of %s outside 1..%d" % (revision, base, REVISION_MAX))
    return base if revision == 1 else "%s-r%d" % (base, revision)


def revision_of(tag, base):
    """The revision `tag` is of `base` (1 for base itself), or None. An
    upstream version that itself ends in "-r<N>" can be misread as a
    revision of a shorter one; that only skips revision numbers."""
    if tag == base:
        return 1
    prefix = base + "-r"
    if tag.startswith(prefix) and tag[len(prefix):].isdigit():
        revision = int(tag[len(prefix):])
        return revision if revision >= 2 else None
    return None


def dict_version(yyyymmdd, revision):
    if not 1 <= revision <= REVISION_MAX:
        raise ValueError("revision %d outside 1..%d" % (revision, REVISION_MAX))
    return int(yyyymmdd) * 100 + (revision - 1)
