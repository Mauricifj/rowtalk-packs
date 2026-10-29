"""Release-side orchestration: resolve, verify, publish, rollback, cleanup,
prune. Every GitHub call goes through the `gh` seam (github.GitHubClient in
the workflows, a fake in tests).

INVARIANT (I2, publish atomicity):
  1. Packs are uploaded into a DRAFT release; nothing outside the repo can
     see a draft.
  2. `verify_release` checks EVERY expected asset against its report
     (size + sha256 via the API digest or a full download) and the report's
     gate verdict; any miss raises before the release is touched.
  3. `publish_release` takes the verified set `verify_release` RETURNED (it
     never recomputes it from the reports) and refuses, before touching the
     release, any report that set does not hold. Only then is the draft
     published, and only then (after each public URL answers a Range GET,
     retried for a bounded window) is the new manifest written. The caller
     commits it.
  4. A manifest entry is built only from a verified (tag, asset) pair
     (manifest.build_manifest refuses anything else).
  5. A release the manifest names is never modified or deleted:
     `cleanup_run` deletes only a DRAFT. A published release that no
     manifest names AND no committed editions log records (a run that died
     between publishing and committing, e.g. the public URL never answered)
     is KEPT, so "Re-run failed jobs" can finish the publish without
     rebuilding; if nobody re-runs it, the next run for that tag deletes it
     as an orphan (`prepare_release`), and retention never counts it as
     protected. Retention protects every named tag.
  6. A published release an editions log RECORDS (it was served once — e.g.
     the edition a rollback abandoned) is never deleted or rebuilt by
     `prepare_release` unless the caller forces it (`protected` empty): see
     editions.recorded_tags (DC2-03/OPS2-02).
If a run fails anywhere, the previous manifest.json on main is untouched
because the commit is the LAST step.
"""

import json
import os
import time

from . import artifacts, editions, manifest, retention, sanity
from .log import log, warning_annotation


class PublishError(Exception):
    pass


def asset_url(cfg, tag, asset):
    return cfg["github"]["asset_url_template"].format(
        repository=cfg["github"]["repository"], tag=tag, asset=asset)


def expected_assets(pack_ids):
    names = []
    for pid in pack_ids:
        names += ["%s.db.gz" % pid, "%s.json" % pid]
    return names


def prepare_release(gh, tag, manifest_tags, body, dry_run=False, protected=None):
    """Make sure a fresh draft for `tag` can be used. Returns
    (release or None, decision) where decision is 'build' or a reason not to.

    * a published release the manifest names  -> 'already-published'
      (it can never be replaced, I2);
    * a published release `protected` holds    -> its reason (e.g.
      'rolled-back'): an edition an editions log records; never deleted
      here (the caller passes no `protected` only when forced);
    * a published release nobody records      -> orphan of a failed commit:
      deleted (allowed — nothing references it), then rebuilt;
    * a draft                                  -> stale draft: deleted.
    """
    protected = protected or {}
    existing = gh.find_release(tag)
    if existing is not None and not existing.get("draft"):
        if tag in manifest_tags:
            return None, "already-published"
        if tag in protected:
            return None, protected[tag]
        log("release %s is published but no manifest names it: deleting the orphan" % tag)
        if not dry_run:
            gh.delete_release(existing)
    elif existing is not None:
        log("deleting stale draft %s (id %s)" % (tag, existing["id"]))
        if not dry_run:
            gh.delete_release(existing)
    if dry_run:
        return None, "build"
    return gh.create_draft(tag, tag, body), "build"


def upload_pack(gh, release_id, out_dir, pid):
    gh.upload_asset(release_id, os.path.join(out_dir, "%s.db.gz" % pid), "%s.db.gz" % pid,
                    "application/gzip")
    gh.upload_asset(release_id, os.path.join(out_dir, "%s.json" % pid), "%s.json" % pid,
                    "application/json")


def verify_release(gh, release, pack_ids, cfg, editions_dir, force=False):
    """Verify every pack asset of `release`. Returns (reports, verified set).

    Raises PublishError on ANY missing/mismatching asset or failed gate.
    """
    assets = {a["name"]: a for a in gh.list_assets(release["id"])}
    missing = [name for name in expected_assets(pack_ids) if name not in assets]
    if missing:
        raise PublishError("release %s is missing assets: %s" % (release["tag_name"], missing))
    reports = []
    verified = set()
    for pid in pack_ids:
        report = json.loads(gh.download_asset_text(assets["%s.json" % pid]))
        if report.get("pack_id") != pid or report.get("tag") != release["tag_name"]:
            raise PublishError("report for %s names %s/%s" % (pid, report.get("pack_id"),
                                                             report.get("tag")))
        gz = assets[report["asset"]]
        if gz.get("state", "uploaded") != "uploaded":
            raise PublishError("%s is in state %s" % (gz["name"], gz.get("state")))
        if gz["size"] != report["download_bytes"]:
            raise PublishError("%s size %d != report %d" % (gz["name"], gz["size"],
                                                           report["download_bytes"]))
        digest, how = gh.asset_sha256(gz)
        if digest != report["sha256"]:
            raise PublishError("%s sha256 mismatch (%s)" % (gz["name"], how))
        lang_cfg, first = _first_edition(cfg, report)
        gate = sanity.evaluate(report["article_count"], report["installed_bytes"],
                               report.get("golden", []), report.get("validation_errors", ["missing"]),
                               editions.previous_stats(editions_dir, report["family"], pid),
                               first, cfg["sanity"]["tolerance"], force)
        if not gate["passed"]:
            raise PublishError("%s: %s" % (pid, sanity.summarize(gate)))
        report["gate"] = gate
        log("verified %s (%d bytes, sha256 via %s; %s)" % (gz["name"], gz["size"], how,
                                                         sanity.summarize(gate)))
        verified.add((release["tag_name"], report["asset"]))
        reports.append(report)
    return reports, verified


def _first_edition(cfg, report):
    if report["kind"] == "wiki":
        lang_cfg = cfg["wiki"]["languages"][report["language"]]
    else:
        lang_cfg = cfg["dict"]["languages"][report["language"]]
    return lang_cfg, lang_cfg.get("first_edition", {}).get(report["tier"])


def wait_public(gh, url, tries=5, backoff_s=5.0, sleep=time.sleep):
    """True once `url` answers the public Range GET, trying up to `tries`
    times with doubling backoff (5+10+20+40 s by default). A just-published
    asset can take a moment to propagate, and one failed request must not
    throw away a verified edition. Bounded: after `tries` it gives up and
    the caller raises (no retry without a give-up)."""
    for attempt in range(1, tries + 1):
        if gh.public_url_ok(url):
            return True
        if attempt < tries:
            sleep(backoff_s * (2 ** (attempt - 1)))
    return False


def publish_release(gh, release, reports, verified, cfg, manifest_path, editions_dir,
                    action="publish", check_public=True, sleep=time.sleep, log_extra=None):
    """Undraft (if needed), confirm public URLs, then write the new manifest
    and append the editions logs. Returns the new manifest.

    `verified` is the set `verify_release` returned; any report outside it
    is refused BEFORE the release is touched (it is never recomputed from
    the reports, which would make the check pass by construction)."""
    unverified = [r["pack_id"] for r in reports
                  if (r.get("tag"), r.get("asset")) not in verified
                  or r.get("tag") != release["tag_name"]]
    if unverified or not reports:
        raise PublishError("refusing to publish %s: unverified reports %s"
                           % (release["tag_name"], unverified or "(none given)"))
    if release.get("draft"):
        release = gh.publish_release(release["id"])
    if check_public:
        public = cfg.get("github", {}).get("public_check", {})
        for report in reports:
            url = asset_url(cfg, release["tag_name"], report["asset"])
            if not wait_public(gh, url, public.get("tries", 5), public.get("backoff_s", 5.0), sleep):
                raise PublishError("public URL not reachable: %s (the published release is "
                                   "kept; re-run the failed job to finish)" % url)
    families = sorted({r["family"] for r in reports})
    current = manifest.load(manifest_path)
    new = manifest.build_manifest(current, families, reports, verified,
                                  lambda tag, asset: asset_url(cfg, tag, asset))
    manifest.write(manifest_path, new)
    for family in families:
        packs = {r["pack_id"]: editions.stats_from_report(r) for r in reports if r["family"] == family}
        edition = next(r["edition"] for r in reports if r["family"] == family)
        forced = any(r["gate"].get("forced") for r in reports if r["family"] == family)
        extra = dict((log_extra or {}).get(family, {}))
        if forced:
            extra["forced"] = True
        editions.append(editions_dir, family, action, release["tag_name"], edition, packs,
                        extra or None)
    return new


def rollback(gh, tag, families, cfg, manifest_path, editions_dir, dry_run=False):
    """Re-point `families` at the kept, published release `tag`.

    The editions line records `from_tags` — the tags each family was served
    before — so resolve never rebuilds the abandoned edition (DC2-03).

    WHAT A ROLLBACK REACHES (OPS4-02): it re-points the manifest, so every
    entry's `version` goes DOWN to the target's. An app that only takes a
    HIGHER version ("same or older: nothing changes", today's import rule)
    keeps the abandoned edition; a rollback then fixes only new installs.
    The contract (SCHEMA.md "Updates and rollbacks") is that the edition the
    manifest names IS the one to have: a device whose installed
    `installed_sha256` differs from the entry's replaces it, whichever way
    `version` moved. Each lowered version is reported as a warning
    annotation so the operator sees the reach of what they did. (The
    alternative — republishing the target's bytes under a new tag with a
    higher version — would make the version disagree with the pack's own
    meta and needs a new tag family; not taken.)
    dry_run (OPS2-01): verify the target exactly as a real rollback does
    (assets, sizes, digests, public URLs — all reads), log the manifest
    change, and write NOTHING: no manifest, no editions line, no release
    call. Returns the manifest the rollback would write."""
    release = gh.find_release(tag)
    if release is None or release.get("draft"):
        raise PublishError("rollback target %s is not a published release" % tag)
    names = [a["name"] for a in gh.list_assets(release["id"])]
    pack_ids = sorted(n[:-len(".db.gz")] for n in names if n.endswith(".db.gz")
                      and _family_of(n[:-len(".db.gz")]) in families)
    if not pack_ids:
        raise PublishError("release %s holds no packs of %s" % (tag, families))
    # A rollback re-verifies the assets; the gate is not re-run against the
    # newer edition (rolling back is the deliberate answer to a bad one).
    reports, verified = verify_release(gh, release, pack_ids, cfg, editions_dir, force=True)
    current = manifest.load(manifest_path)
    from_tags = {family: sorted({e.get("tag") for e in current.get("packs", [])
                                 if e.get("family") == family and e.get("tag")
                                 and e.get("tag") != tag})
                 for family in families}
    if dry_run:
        for report in reports:
            url = asset_url(cfg, tag, report["asset"])
            if not gh.public_url_ok(url):
                raise PublishError("dry run: rollback target URL not reachable: %s" % url)
        new = manifest.build_manifest(current, sorted({r["family"] for r in reports}), reports,
                                      verified, lambda t, asset: asset_url(cfg, t, asset))
        before = {e["id"]: e.get("tag") for e in current.get("packs", [])}
        for entry in new["packs"]:
            if before.get(entry["id"]) != entry["tag"]:
                log("dry run: %s: %s -> %s" % (entry["id"], before.get(entry["id"]), entry["tag"]))
        _warn_lowered_versions(current, new)
        log("dry run: nothing written (manifest, editions log and releases untouched)")
        return new
    new = publish_release(gh, release, reports, verified, cfg, manifest_path, editions_dir,
                          action="rollback",
                          log_extra={f: {"from_tags": t} for f, t in from_tags.items() if t})
    _warn_lowered_versions(current, new)
    return new


def _warn_lowered_versions(before, after):
    """Warn for every entry whose version a rollback LOWERED (OPS4-02)."""
    old = {e.get("id"): e.get("version") for e in before.get("packs", [])}
    lowered = []
    for entry in after.get("packs", []):
        was = old.get(entry["id"])
        if isinstance(was, int) and entry["version"] < was:
            warning_annotation(
                "rollback: %s version %d -> %d. Only apps that replace an installed edition "
                "whose installed_sha256 differs from the manifest's take it; an app that "
                "only takes a HIGHER version keeps %d (SCHEMA.md 'Updates and rollbacks')"
                % (entry["id"], was, entry["version"], was))
            lowered.append(entry["id"])
    return lowered


def _family_of(pid):
    parts = pid.split("-")
    return "-".join(parts[:2])


def cleanup_run(gh, release_id, manifest_on_main):
    """Called when a run failed or was cancelled. Deletes the run's release
    only if it is still a DRAFT. A published release is never deleted here:
    if the manifest on the remote branch names it, it is live; if not, it is
    a verified edition whose publish died after the undraft (see module doc
    point 5) — kept for "Re-run failed jobs". Who clears it if nobody re-runs:
    the next run for the same tag (`prepare_release` deletes the unnamed
    orphan and rebuilds), or retention once 3 newer wiki editions exist (an
    unnamed release is never protected)."""
    try:
        release = gh.get_release(release_id)
    except Exception as error:  # noqa: BLE001 — nothing to clean
        log("cleanup: release %s not found (%s)" % (release_id, error))
        return "absent"
    named = manifest.named_tags(manifest_on_main)
    if release.get("draft"):
        gh.delete_release(release)
        return "deleted-draft"
    if release["tag_name"] in named:
        return "kept-named"
    log("cleanup: %s is published but no manifest names it — kept so 'Re-run failed jobs' can "
        "finish the publish; the next run deletes it if it is still unnamed" % release["tag_name"])
    return "kept-published-unnamed"


def prune(gh, manifest_tags, wiki_keep, keep_draft_id=None, dry_run=False,
          dict_keep_previous=0, recorded_tags=()):
    releases = gh.list_releases()
    doomed = retention.select_prunable(releases, manifest_tags, wiki_keep, keep_draft_id,
                                       dict_keep_previous, recorded_tags)
    for release in doomed:
        log("prune %s release %s" % ("draft" if release.get("draft") else "published",
                                     release["tag_name"]))
        if not dry_run:
            gh.delete_release(release)
    return [r["tag_name"] for r in doomed]
