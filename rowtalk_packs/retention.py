"""Release retention (I3).

Keeps the newest `wiki_keep` published wiki editions and, for dictionaries,
the editions the manifest names PLUS, per dict family, the newest
`dict_keep_previous` published editions an editions log RECORDS (served
once) — the rollback target (DC4-03: with only the named edition kept, a
dictionary published from bad upstream content had no way back: the prior
edition was pruned by the very publish that replaced it, and a rebuild of
the same upstream reproduces the bad content). NEVER selects a release whose
tag the current manifest names (I2: an asset a manifest names is never
deleted), and never touches a release whose tag does not match one of this
repo's tag patterns. Stale DRAFTS with our patterns are selected (a draft is
never named by a manifest), except the draft `keep_draft_id` of the run doing
the prune.

INVARIANT (DC5-01/OPS5-01): the edition a dict family keeps is the one a
rollback would want — the edition served before the current one that is not
known bad. Two kinds of recorded edition are SUSPECT: one a rollback
abandoned (reason 'rolled-back' in editions.recorded_tags) and a revision
sibling of a NAMED edition (same upstream base, other -rN: a rebuild replaced
it because it was bad). Among the unnamed recorded editions the slot goes to
the newest NON-suspect one by release id; a suspect one takes it only when
no non-suspect one exists (so a family with one prior edition still keeps
it). This is a ranking, never an exclusion: it cannot leave a family with
fewer kept editions than before. Under dict_keep_previous = 1 it picks the
edition served just before the current one in each recovery flow the README
prescribes (publish, rebuild, rollback, a publish or rebuild after a
rollback) — tests/test_dict.py drives each with a prune after every step.
RESIDUAL: the reason is the log's, which is sticky — an edition once rolled
back FROM stays 'rolled-back' even if a later rollback re-served it; it then
loses the slot to an older non-suspect edition (one release, never a named
one; the next publish re-decides).

Who clears a kept previous dict edition: the next publish of that family
(it then ranks below the newly displaced edition and is pruned). If that
family never publishes again, it stays — one release per family, a bounded
cost. An unnamed dict release NO log records (the orphan of a failed commit)
never takes that slot: it is pruned as before (prepare_release would delete
and rebuild it anyway).

"Newest" is the highest release id, i.e. the most recently CREATED release
(GitHub ids only grow, and a dict release is created and published in one
run), not the lexicographically largest tag (upstream tags such as "v1.10.0"
/ "v1.9.0" do not sort by time).
"""

import re

WIKI_TAG = re.compile(r"^wiki-(\d{4})\.(\d{2})\.(\d{2})$")
DICT_TAG = re.compile(r"^(dict-[a-z]{2,3})-[A-Za-z0-9._-]+$")


def is_ours(tag):
    return bool(WIKI_TAG.match(tag or "") or DICT_TAG.match(tag or ""))


def select_prunable(releases, protected_tags, wiki_keep=3, keep_draft_id=None,
                    dict_keep_previous=0, recorded_tags=()):
    """releases: [{"id", "tag_name", "draft"}]. Returns the releases to delete.

    `recorded_tags`: tags an editions log records — the {tag: reason} of
    editions.recorded_tags (a plain set means "no reasons known"); only
    those can be kept as a dict family's previous edition."""
    protected = set(protected_tags)
    recorded = set(recorded_tags)
    reasons = dict(recorded_tags) if hasattr(recorded_tags, "items") else {}
    named_bases = {_revision_base(tag) for tag in protected}
    prune = []
    published_wiki = []
    unnamed_dict = {}
    for release in releases:
        tag = release.get("tag_name") or ""
        if not is_ours(tag):
            continue
        if release.get("draft"):
            if release.get("id") != keep_draft_id and tag not in protected:
                prune.append(release)
            continue
        if WIKI_TAG.match(tag):
            published_wiki.append(release)
        elif tag not in protected:
            unnamed_dict.setdefault(DICT_TAG.match(tag).group(1), []).append(release)
    published_wiki.sort(key=lambda r: r["tag_name"], reverse=True)
    for index, release in enumerate(published_wiki):
        if index >= wiki_keep and release["tag_name"] not in protected:
            prune.append(release)

    def rank(release):  # non-suspect first, then newest (see the INVARIANT)
        tag = release["tag_name"]
        suspect = reasons.get(tag) == "rolled-back" or _revision_base(tag) in named_bases
        return (suspect, -(release.get("id") or 0))

    for family in sorted(unnamed_dict):
        kept = 0
        for release in sorted(unnamed_dict[family], key=rank):
            if kept < dict_keep_previous and release["tag_name"] in recorded:
                kept += 1  # the family's rollback target
                continue
            prune.append(release)  # older dict editions and unrecorded orphans
    return prune


_REVISION = re.compile(r"^(.+)-r(\d+)$")


def _revision_base(tag):
    """The upstream base of a dict tag (dictbuild.revision_tag's inverse):
    "<base>-r<N>" with N >= 2 -> "<base>", else the tag itself."""
    match = _REVISION.match(tag)
    if match and int(match.group(2)) >= 2:
        return match.group(1)
    return tag
