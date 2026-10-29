"""Append-only editions log: editions/<family>.jsonl (I5 — git history plus
this log is the version record). One JSON object per line, never rewritten.
Families: wiki-<lang>, dict-<lang>."""

import json
import os
import time


def log_path(editions_dir, family):
    return os.path.join(editions_dir, "%s.jsonl" % family)


def parse(text):
    return [json.loads(line) for line in (text or "").splitlines() if line.strip()]


def read(editions_dir, family):
    path = log_path(editions_dir, family)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        return parse(handle.read())


def recorded_tags(entries):
    """{tag: reason} for every tag these log lines show was PUBLISHED (once
    named by a committed manifest): 'rolled-back' when a rollback line
    abandoned it (`from_tags`), else 'published-before'.

    INVARIANT (DC2-03/OPS2-02): a recorded tag is a real edition, never "the
    orphan of a failed commit" — that orphan is exactly a published release
    no committed log line records (its publish died before the commit, so
    its line never reached main). Resolve and prepare_release therefore
    never delete or rebuild a recorded tag without force: otherwise the
    next run after a rollback deletes the abandoned edition and republishes
    the same dump, silently undoing the rollback. Who clears it: nothing —
    the log is append-only by design. What that costs: after a rollback the
    family stays on the rollback target until a NEWER dump exists (a new
    tag) or an operator dispatches force=true — or, for a dictionary whose
    upstream version never changes, rebuild=true, which publishes a NEW
    revision tag and deletes nothing (DC3-03). A dictionary family can also
    be rolled back: retention keeps one recorded unnamed edition as the
    target, ranking a 'rolled-back' one last (DC4-03, DC5-01)."""
    out = {}
    for entry in entries:
        if entry.get("action") == "publish" and entry.get("tag"):
            out.setdefault(entry["tag"], "published-before")
        for tag in entry.get("from_tags") or ():
            out[tag] = "rolled-back"
    return out


def previous_stats(editions_dir, family, pack_id):
    """Stats of `pack_id` in the most recent line naming it (publish or
    rollback), or None for a first edition."""
    for entry in reversed(read(editions_dir, family)):
        stats = entry.get("packs", {}).get(pack_id)
        if stats:
            return stats
    return None


def append(editions_dir, family, action, tag, edition, packs, extra=None):
    os.makedirs(editions_dir, exist_ok=True)
    entry = {"action": action, "tag": tag, "edition": edition,
             "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "packs": packs}
    entry.update(extra or {})
    with open(log_path(editions_dir, family), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return entry


def stats_from_report(report):
    return {key: report[key] for key in ("article_count", "installed_bytes", "download_bytes",
                                         "sha256", "installed_sha256")}
