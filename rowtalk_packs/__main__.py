"""Command line entry point: python -m rowtalk_packs <command> ...

Exit codes: 0 ok / nothing to do; 1 error; 2 a sanity gate failed — the
pack that failed was not uploaded or published (build-wiki: nothing of that
language was; dict-run: other languages may still have been published, see
its `published`/`skipped` outputs).
"""

import argparse
import datetime
import json
import os
import shutil
import sys
import tempfile
import urllib.request

from . import artifacts, dictbuild, dumpsource, editions, manifest, publish, wikibuild
from .cirrus import LangRules
from .config import REPO_ROOT, load_config
from .github import GitHubClient
from .log import error_annotation, log, warning_annotation
from .staging import Staging

GATE_FAILED = 2


def _output(path, **values):
    """Write step outputs (GITHUB_OUTPUT format) and echo them."""
    for key, value in values.items():
        text = value if isinstance(value, str) else json.dumps(value)
        log("output %s=%s" % (key, text))
        if path:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("%s=%s\n" % (key, text))


def _paths(args):
    root = args.repo_root or REPO_ROOT
    return os.path.join(root, "manifest.json"), os.path.join(root, "editions")


def _gh(cfg, args):
    return GitHubClient(args.repository or cfg["github"]["repository"], user_agent=cfg["user_agent"])


def _csv(text):
    return [item.strip() for item in (text or "").split(",") if item.strip()]


def _main_manifest(cfg, gh, dry_run):
    """manifest.json as committed on main — what the app is actually served.

    The local checkout may be ANY ref (a workflow_dispatch from a feature
    branch checks that branch out), so decisions that delete or skip
    releases must never rest on the local file alone (OPS-2). A failed read
    raises, except in a dry run, which deletes nothing and may fall back to
    the local file (logged)."""
    try:
        text = gh.file_on_branch(cfg["github"]["manifest_path"], "main")
    except Exception as error:  # noqa: BLE001
        if not dry_run:
            raise
        log("dry run: cannot read main's manifest (%s); using the local file only" % error)
        return None
    return json.loads(text) if text else manifest.empty()


def _views(cfg, args, gh, manifest_path):
    """(named tags, manifest whose entries say what is served).

    named = tags the local manifest OR main's manifest names: a release
    either one names is never treated as an orphan (never deleted) and never
    rebuilt. served = main's view when readable (the authority), else local."""
    local = manifest.load(manifest_path)
    remote = _main_manifest(cfg, gh, args.dry_run)
    named = manifest.named_tags(local) | (manifest.named_tags(remote) if remote else set())
    return named, (remote if remote is not None else local)


def _recorded(cfg, args, gh, families):
    """{tag: reason} for every tag an editions log records as published
    (editions.recorded_tags), from main's logs UNION the local ones — the
    safe direction: more recorded tags means fewer deletions. Like
    `_main_manifest`, a failed read of main raises except in a dry run."""
    _, editions_dir = _paths(args)
    out = {}
    for family in families:
        local = editions.read(editions_dir, family)
        remote = []
        path = "%s/%s.jsonl" % (cfg["github"]["editions_dir"], family)
        try:
            remote = editions.parse(gh.file_on_branch(path, "main"))
        except Exception as error:  # noqa: BLE001
            if not args.dry_run:
                raise
            log("dry run: cannot read main's %s (%s); using the local log only" % (path, error))
        for tag, reason in editions.recorded_tags(local + remote).items():
            if out.get(tag) != "rolled-back":
                out[tag] = reason
    return out


# ---------------------------------------------------------------- wiki ----
def cmd_resolve_wiki(cfg, args):
    manifest_path, _ = _paths(args)
    langs = _csv(args.langs) or sorted(cfg["wiki"]["languages"])
    wikis = [cfg["wiki"]["languages"][lang]["wiki"] for lang in langs]
    base = args.dump_base or cfg["dump"]["base_url"]
    date, listings = dumpsource.resolve_latest(base, wikis, cfg["user_agent"],
                                               cfg["dump"]["socket_timeout_s"],
                                               cfg["dump"]["dates_to_scan"], args.date or None,
                                               retry=cfg["dump"].get("listing_retry"))
    if date is None:
        log("no complete dump for %s" % wikis)
        _output(args.github_output, build="false", reason="no-complete-dump")
        return 0
    tag = wikibuild.tag_for_date(date)
    gh = _gh(cfg, args)
    named, current = _views(cfg, args, gh, manifest_path)
    if tag in named and not args.force:
        log("%s already published and named by the manifest — nothing new" % tag)
        _output(args.github_output, build="false", reason="already-published", tag=tag, date=date)
        return 0
    # A tag an editions log records was served once (DC2-03/OPS2-02): most
    # importantly the edition a rollback abandoned. Rebuilding it would undo
    # the rollback with no warning, so only --force does. A wiki tag is
    # SHARED by every wiki family, so the logs read are those of EVERY
    # configured language, whatever --langs says (OPS3-01): a dispatch with
    # langs=en must not delete the edition a wiki-pt rollback abandoned.
    recorded = {} if args.force else _recorded(
        cfg, args, gh, ["wiki-%s" % lang for lang in sorted(cfg["wiki"]["languages"])])
    if tag in recorded:
        log("%s was published before (%s) — not rebuilding it without --force" % (tag, recorded[tag]))
        _output(args.github_output, build="false", reason=recorded[tag], tag=tag, date=date)
        return 0
    # Never move the manifest BACKWARDS by accident: a dump date older than
    # (or equal to) what the manifest already serves for these languages is
    # not "new" (e.g. the newest dump was withdrawn). --force allows it.
    served = [e.get("source", {}).get("dump_date", "") for e in current.get("packs", [])
              if e.get("family") in {"wiki-%s" % lang for lang in langs}]
    if served and date <= min(served) and not args.force:
        log("dump %s is not newer than the served edition(s) %s — nothing new" % (date, sorted(set(served))))
        _output(args.github_output, build="false", reason="not-newer", tag=tag, date=date)
        return 0
    shards = {lang: len(listings[cfg["wiki"]["languages"][lang]["wiki"]].shards) for lang in langs}
    log("dump %s complete: %s" % (date, shards))
    release_id = ""
    if not args.dry_run:
        body = ("Rowtalk wiki packs, Wikipedia CirrusSearch dump %s (%s). Text: Wikipedia "
                "contributors, CC BY-SA 4.0. See DATA-LICENSES.md." % (date, ", ".join(wikis)))
        release, decision = publish.prepare_release(gh, tag, named, body, protected=recorded)
        if decision != "build":
            log("%s: %s — an edition can never be replaced; pick another dump_date" % (tag, decision))
            _output(args.github_output, build="false", reason=decision, tag=tag, date=date)
            return 0
        release_id = str(release["id"])
    _output(args.github_output, build="true", date=date, tag=tag, langs=langs,
            release_id=release_id)
    return 0


def _require_draft(gh, release_id):
    """Fail BEFORE any download when the target draft is gone or already
    published (OPS-7): "Re-run failed jobs" reuses the resolve job's
    release_id, which the failed run's cleanup already deleted, and would
    otherwise rebuild for hours and then 404 on upload."""
    try:
        release = gh.get_release(release_id)
    except Exception as error:  # noqa: BLE001
        raise SystemExit("draft release %s is gone (%s): its run was cleaned up — re-run ALL "
                         "jobs, not only the failed ones" % (release_id, error))
    if not release.get("draft"):
        raise SystemExit("release %s (%s) is no longer a draft — re-run ALL jobs"
                         % (release_id, release.get("tag_name")))
    return release


def cmd_build_wiki(cfg, args):
    _, editions_dir = _paths(args)
    if args.release_id:
        _require_draft(_gh(cfg, args), args.release_id)
    lang = args.lang
    lang_cfg = cfg["wiki"]["languages"][lang]
    tiers = _csv(args.tiers) or sorted(lang_cfg["tiers"], key=lambda t: lang_cfg["tiers"][t])
    # I3: refuse an over-cap connection count before the first dump request.
    connections = args.connections or cfg["dump"]["connections_per_job"]
    if connections > cfg["dump"]["max_connections_per_run"]:
        raise SystemExit("--connections %d exceeds dump.max_connections_per_run %d"
                         % (connections, cfg["dump"]["max_connections_per_run"]))
    base = args.dump_base or cfg["dump"]["base_url"]
    listing = dumpsource.list_index(base, args.date, lang_cfg["wiki"], cfg["user_agent"],
                                    cfg["dump"]["socket_timeout_s"],
                                    retry=cfg["dump"].get("listing_retry"))
    if not listing.complete:
        raise SystemExit("dump %s %s is not complete" % (args.date, lang_cfg["wiki"]))
    urls = listing.shard_urls()
    if args.max_shards:
        urls = urls[:args.max_shards]
    os.makedirs(args.out, exist_ok=True)
    work = args.work or args.out
    os.makedirs(work, exist_ok=True)
    keep = max(lang_cfg["tiers"][t] for t in tiers)
    rules = LangRules(lang_cfg, cfg["abstract"], cfg["aliases"])
    stats = wikibuild.BuildStats()
    staging = Staging(os.path.join(work, "staging-%s.db" % lang), keep,
                      cfg["staging"]["prune_every"], cfg["staging"]["batch"])
    try:
        wikibuild.stage_shards(urls, rules, staging, cfg["user_agent"], cfg["dump"], stats,
                               os.path.join(work, "shards"), workers=args.workers or None,
                               connections=connections)
        log("%s: staged %d shards with %d connections and %d decompressors: %.0f MB bz2 in "
            "%.0fs of network, "
            "%.0f MB decompressed in %.0fs of decompressor time, %.0fs parsing; resumes=%d "
            "restarts=%d corrupt_rereads=%d" % (
                lang, len(urls), connections, stats.workers, stats.compressed_bytes / 1e6,
                stats.net_seconds,
                stats.decompressed_bytes / 1e6, stats.decompress_seconds, stats.parse_seconds,
                stats.resumes, stats.restarts, stats.corrupt_rereads))
        totals = staging.totals()
        ranking = staging.ranking(cfg["wiki"]["min_popularity_presence"])
        log("%s: %d candidates (popularity on %d, links on %d, duplicate page ids %d) -> "
            "ranking %s; excluded %s; lines %s" % (
                lang, totals["candidates"], totals["with_popularity"], totals["with_links"],
                totals["duplicates"], ranking, dict(stats.excluded), stats.lines.as_dict()))
        source = {"wiki": lang_cfg["wiki"], "dump_date": args.date, "url": listing.url,
                  "site": lang_cfg["site"]}
        reports = wikibuild.build_tiers(cfg, lang, args.date, tiers, staging, ranking, args.out,
                                        editions_dir, source, args.force)
        # The download/parse record travels with every report (TH-08), so a
        # run's resumes/restarts/re-downloads are inspectable after the fact.
        build = {"shards": len(urls), "connections": connections, "workers": stats.workers,
                 "compressed_bytes": stats.compressed_bytes, "resumes": stats.resumes,
                 "restarts": stats.restarts, "corrupt_rereads": stats.corrupt_rereads,
                 "duplicate_page_ids": totals["duplicates"], "excluded": dict(stats.excluded),
                 "lines": stats.lines.as_dict()}
        for report in reports:
            report["build"] = build
            artifacts.write_json(os.path.join(args.out, "%s.json" % report["pack_id"]), report)
    finally:
        staging.close()
    for tier in tiers:
        db = os.path.join(args.out, "%s.db" % wikibuild.pack_id(lang, tier))
        if os.path.exists(db) and not args.keep_db:
            os.remove(db)
    if not all(r["gate"]["passed"] for r in reports):
        log("sanity gate failed: nothing uploaded")
        return GATE_FAILED
    if args.release_id:
        gh = _gh(cfg, args)
        for report in reports:
            publish.upload_pack(gh, args.release_id, args.out, report["pack_id"])
    return 0


def cmd_publish_wiki(cfg, args):
    """Publish every language whose packs are ALL present and verified.

    A language is all-or-nothing (both tiers, every asset, every check), but
    languages are independent (OPS-3): an enwiki build that timed out must
    not throw away a verified ptwiki edition, month after month. Skipped
    languages are written to the `skipped` output; the workflow fails the
    job AFTER committing what was published, so the run is still red. If no
    language qualifies, nothing is published and this raises (cleanup then
    deletes the draft)."""
    manifest_path, editions_dir = _paths(args)
    gh = _gh(cfg, args)
    release = gh.get_release(args.release_id)
    present = {asset["name"] for asset in gh.list_assets(release["id"])}
    reports, verified, published, skipped = [], set(), [], []
    for lang in _csv(args.langs):
        lang_cfg = cfg["wiki"]["languages"][lang]
        tiers = _csv(args.tiers) or sorted(lang_cfg["tiers"])
        pack_ids = [wikibuild.pack_id(lang, tier) for tier in tiers]
        missing = [name for name in publish.expected_assets(pack_ids) if name not in present]
        if missing:
            error_annotation("%s: not publishing — its build did not upload %s" % (lang, missing))
            skipped.append(lang)
            continue
        try:
            lang_reports, lang_verified = publish.verify_release(gh, release, pack_ids, cfg,
                                                                 editions_dir, args.force)
        except publish.PublishError as error:
            error_annotation("%s: not publishing — %s" % (lang, error))
            skipped.append(lang)
            continue
        reports += lang_reports
        verified |= lang_verified
        published += pack_ids
    if not reports:
        raise publish.PublishError("nothing to publish in %s (skipped: %s)"
                                   % (release["tag_name"], skipped))
    publish.publish_release(gh, release, reports, verified, cfg, manifest_path, editions_dir)
    log("published %s: %s%s" % (release["tag_name"], published,
                                "; SKIPPED %s" % skipped if skipped else ""))
    _output(args.github_output, skipped=",".join(skipped))
    return 0


def cmd_rollback(cfg, args):
    manifest_path, editions_dir = _paths(args)
    publish.rollback(_gh(cfg, args), args.tag, _csv(args.families), cfg, manifest_path,
                     editions_dir, dry_run=args.dry_run)
    return 0


def cmd_cleanup(cfg, args):
    gh = _gh(cfg, args)
    text = gh.file_on_branch(cfg["github"]["manifest_path"], args.branch)
    on_main = json.loads(text) if text else manifest.empty()
    for release_id in _csv(args.release_ids):
        log("cleanup %s: %s" % (release_id, publish.cleanup_run(gh, release_id, on_main)))
    return 0


def cmd_prune(cfg, args):
    manifest_path, _ = _paths(args)
    gh = _gh(cfg, args)
    text = gh.file_on_branch(cfg["github"]["manifest_path"], args.branch)
    remote = json.loads(text) if text else manifest.empty()
    protected = manifest.named_tags(manifest.load(manifest_path)) | manifest.named_tags(remote)
    # DC4-03: each dict family keeps one RECORDED unnamed edition (the
    # rollback target) — the newest one a rollback did not abandon and a
    # rebuild did not replace (retention's INVARIANT, DC5-01: it reads the
    # reasons). Recorded = main's logs UNION the local ones (the safe
    # direction: more recorded, fewer deletions); a failed read of main
    # raises, so a prune that cannot see the logs deletes nothing.
    recorded = _recorded(cfg, args, gh, ["dict-%s" % lang
                                         for lang in sorted(cfg["dict"]["languages"])])
    pruned = publish.prune(gh, protected, cfg["retention"]["wiki_keep_editions"],
                           dry_run=args.dry_run,
                           dict_keep_previous=cfg["retention"].get("dict_keep_previous", 1),
                           recorded_tags=recorded)
    log("pruned: %s" % (pruned or "nothing"))
    return 0


def cmd_probe(cfg, args):
    from .probe import run_probe
    lang = next(l for l, c in cfg["wiki"]["languages"].items() if c["wiki"] == args.wiki)
    work = tempfile.mkdtemp(prefix="probe-", dir=args.work or None)
    try:
        text = run_probe(cfg, lang, args.shards, args.date or None, args.dump_base, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    return 0


# ---------------------------------------------------------------- dict ----
def _upstream(gh, lang_cfg):
    import re
    release = gh.latest_release_of(lang_cfg["upstream_repo"])
    pattern = re.compile(lang_cfg["asset_pattern"])
    assets = [a for a in release.get("assets", []) if pattern.match(a.get("name", ""))]
    if not assets:
        raise SystemExit("no asset matching %s in %s %s" % (
            lang_cfg["asset_pattern"], lang_cfg["upstream_repo"], release.get("tag_name")))
    published = (release.get("published_at") or "")[:10]
    return {"upstream_tag": release["tag_name"], "url": assets[0]["browser_download_url"],
            "name": assets[0]["name"], "published": published}


def cmd_dict_run(cfg, args):
    """Resolve upstream versions, build changed dictionaries, publish them.
    Writes the created release ids to --release-log for the cleanup step.

    INVARIANT (OPS2-05): each language is a unit, as in cmd_publish_wiki.
    A failure in one language (upstream lookup, download, build, gate,
    upload, verify, publish) is recorded in `skipped` and never stops the
    others, and the outputs (`published`, `changed`, `skipped`) are ALWAYS
    written before the exit status — so the workflow's commit step (which
    runs on `changed == 'true'` even when this step failed) puts every
    language that DID publish on main. Otherwise a language that keeps
    failing would leave the other's release published-but-unnamed every
    quarter, deleted as an orphan and rebuilt by the next run, forever.
    Exit: 0 when nothing was skipped; GATE_FAILED (2) when a skip was a gate
    failure; 1 for any other skip."""
    manifest_path, editions_dir = _paths(args)
    gh = _gh(cfg, args)
    named, _ = _views(cfg, args, gh, manifest_path)
    work = args.work or tempfile.mkdtemp(prefix="dict-")
    os.makedirs(work, exist_ok=True)
    built, skipped, gate_failed = [], [], False
    for lang in _csv(args.langs) or sorted(cfg["dict"]["languages"]):
        try:
            outcome = _dict_one(cfg, args, gh, lang, named, work, manifest_path, editions_dir)
        except (Exception, SystemExit) as error:  # noqa: BLE001 — one language's failure
            error_annotation("dict %s: not published — %s: %s" % (lang, type(error).__name__,
                                                                  error))
            skipped.append(lang)
            continue
        if outcome == "gate-failed":
            gate_failed = True
            skipped.append(lang)
        elif outcome:
            built.append(outcome)
            named |= manifest.named_tags(manifest.load(manifest_path))
    _output(args.github_output, published=built, changed="true" if built else "false",
            skipped=",".join(skipped))
    if skipped:
        log("dict: published %s; SKIPPED %s" % (built or "nothing", skipped))
        return GATE_FAILED if gate_failed else 1
    return 0


def _dict_one(cfg, args, gh, lang, named, work, manifest_path, editions_dir):
    """Build and publish one dictionary. Returns the published tag, None when
    there is nothing to do, or 'gate-failed'. Raises on any other failure."""
    lang_cfg = cfg["dict"]["languages"][lang]
    upstream = _upstream(gh, lang_cfg)
    base = dictbuild.dict_tag(lang, upstream["upstream_tag"])
    # Every revision of this upstream version that a manifest names or a log
    # records (DC3-03): `--rebuild` mints the next one; otherwise a named
    # revision means "already published", a recorded one "not without force".
    logged = _recorded(cfg, args, gh, ["dict-%s" % lang])
    revisions = {t: n for t in set(named) | set(logged)
                 for n in [dictbuild.revision_of(t, base)] if n}
    named_revisions = sorted((t for t in revisions if t in named), key=revisions.get)
    if args.rebuild:
        revision = max(revisions.values(), default=0) + 1
        tag = dictbuild.revision_tag(base, revision)
        recorded = {} if args.force else logged
        log("%s: rebuild of %s requested — publishing it as revision %d (%s)"
            % (lang, upstream["upstream_tag"], revision, tag))
    else:
        # The newest revision this upstream version has (1 when none): a
        # --force rebuild of a recorded-but-unnamed edition keeps its tag and
        # its version, never an older revision's.
        revision = max(revisions.values(), default=1)
        tag = dictbuild.revision_tag(base, revision)
        if named_revisions:
            if args.force:
                # A named tag is never replaced (I2); force only waives the
                # gate. Say so visibly instead of a silent no-op (DC3-03) —
                # a warning, not a failure: a forced dispatch for one
                # language must not turn red over the unchanged other one.
                warning_annotation(
                    "dict %s: %s is published and named by the manifest; an edition is never "
                    "replaced, so force changes nothing here — dispatch rebuild=true to "
                    "publish a corrected build of %s as a new revision"
                    % (lang, named_revisions[-1], upstream["upstream_tag"]))
                return None
            log("%s: %s already published — nothing new" % (lang, named_revisions[-1]))
            return None
        recorded = {} if args.force else logged
        stale = sorted((t for t in revisions if t in recorded), key=revisions.get)
        if stale:
            log("%s: %s was published before (%s) — not rebuilding it without --force or "
                "--rebuild" % (lang, stale[-1], recorded[stale[-1]]))
            return None
    source_path = os.path.join(work, upstream["name"])
    log("%s: downloading %s" % (lang, upstream["url"]))
    request = urllib.request.Request(upstream["url"], headers={"User-Agent": cfg["user_agent"]})
    with urllib.request.urlopen(request, timeout=300) as response, open(source_path, "wb") as out:
        shutil.copyfileobj(response, out)
    version_int = dictbuild.dict_version(upstream["published"].replace("-", "") or "0",
                                         revision)
    source_meta = {"version": upstream["upstream_tag"], "tag": tag, "url": upstream["url"],
                   "edition": upstream["upstream_tag"], "version_int": version_int}
    report = dictbuild.build_dict_pack(cfg, lang, source_path, source_meta, work, editions_dir,
                                       args.force)
    os.remove(os.path.join(work, "%s.db" % report["pack_id"]))
    if not report["gate"]["passed"]:
        error_annotation("dict %s: sanity gate failed — not published" % lang)
        return "gate-failed"
    if args.dry_run:
        return None
    body = "Rowtalk dictionary pack %s from %s %s (%s). See DATA-LICENSES.md." % (
        report["pack_id"], lang_cfg["label"], upstream["upstream_tag"], lang_cfg["license"])
    release, decision = publish.prepare_release(gh, tag, named, body, protected=recorded)
    if decision != "build":
        log("%s: %s" % (tag, decision))
        return None
    if args.release_log:
        with open(args.release_log, "a", encoding="utf-8") as handle:
            handle.write("%s\n" % release["id"])
    publish.upload_pack(gh, release["id"], work, report["pack_id"])
    reports, verified = publish.verify_release(gh, release, [report["pack_id"]], cfg,
                                               editions_dir, args.force)
    publish.publish_release(gh, release, reports, verified, cfg, manifest_path, editions_dir)
    return tag


def cmd_build_dict_local(cfg, args):
    _, editions_dir = _paths(args)
    os.makedirs(args.out, exist_ok=True)
    source_meta = {"version": args.source_version, "tag": dictbuild.dict_tag(args.lang, args.source_version),
                   "url": args.source_url or "local:%s" % os.path.basename(args.source),
                   "edition": args.source_version,
                   "version_int": dictbuild.dict_version(
                       datetime.date.today().strftime("%Y%m%d"), 1)}
    report = dictbuild.build_dict_pack(cfg, args.lang, args.source, source_meta, args.out,
                                       editions_dir, args.force)
    return 0 if report["gate"]["passed"] else GATE_FAILED


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rowtalk_packs")
    parser.add_argument("--config", default=None)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--repository", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("resolve-wiki")
    p.add_argument("--langs", default="")
    p.add_argument("--date", default="")
    p.add_argument("--dump-base", default="")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    p.set_defaults(func=cmd_resolve_wiki)

    p = sub.add_parser("build-wiki")
    p.add_argument("--lang", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--tiers", default="")
    p.add_argument("--out", required=True)
    p.add_argument("--work", default="")
    p.add_argument("--dump-base", default="")
    p.add_argument("--release-id", default="")
    p.add_argument("--max-shards", type=int, default=0)
    p.add_argument("--workers", type=int, default=0)
    p.add_argument("--connections", type=int, default=0)
    p.add_argument("--force", action="store_true")
    p.add_argument("--keep-db", action="store_true")
    p.set_defaults(func=cmd_build_wiki)

    p = sub.add_parser("publish-wiki")
    p.add_argument("--release-id", required=True)
    p.add_argument("--langs", required=True)
    p.add_argument("--tiers", default="")
    p.add_argument("--force", action="store_true")
    p.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    p.set_defaults(func=cmd_publish_wiki)

    p = sub.add_parser("rollback")
    p.add_argument("--tag", required=True)
    p.add_argument("--families", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("cleanup")
    p.add_argument("--release-ids", required=True)
    p.add_argument("--branch", default="main")
    p.set_defaults(func=cmd_cleanup)

    p = sub.add_parser("prune")
    p.add_argument("--branch", default="main")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("probe")
    p.add_argument("--wiki", required=True)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--date", default="")
    p.add_argument("--dump-base", default="")
    p.add_argument("--work", default="")
    p.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("dict-run")
    p.add_argument("--langs", default="")
    p.add_argument("--work", default="")
    # Two separate inputs (DC3-03): --rebuild publishes a corrected build of
    # an already published upstream version as a NEW revision tag (I6 gate
    # still enforced); --force waives the I6 gate (and, per the recorded-tag
    # rule, may rebuild a recorded-but-unnamed tag).
    p.add_argument("--rebuild", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--release-log", default="")
    p.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    p.set_defaults(func=cmd_dict_run)

    p = sub.add_parser("build-dict-local")
    p.add_argument("--lang", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--source-version", required=True)
    p.add_argument("--source-url", default="")
    p.add_argument("--out", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_build_dict_local)

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
