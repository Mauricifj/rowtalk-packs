"""manifest.json (v2) generation.

INVARIANT (I2): the manifest only ever names assets that exist, are complete
and match the sha256/size it states. This module enforces the part it can:
an entry is built ONLY from a report whose asset was verified against the
release (`verified_assets`), and `build_manifest` refuses to emit an entry
for any asset not in that set.

REPLACEMENT IS BY PACK ID, never by family: a publish replaces exactly the
entries whose `id` it reports and carries every other entry over untouched
(other families, AND the tiers of the same family it did not rebuild — a
`tiers=essential` dispatch must not drop `-extended`). Carried-over entries
were verified when they were published, and retention never deletes a
release the manifest names, so they stay valid. The cost of this rule (the
mirror image of dropping too much): an entry is never removed by a publish.
Retiring a pack id needs a deliberate manifest edit; nothing does it
implicitly.

Every v1 key the app's current tolerant parser reads is kept with its v1
meaning (`sizeBytes`/`sha256` describe the DOWNLOADED bytes; `version` is an
int — a String there would throw in today's parser).
"""

import json
import os
import time

MANIFEST_VERSION = 2


class ManifestError(Exception):
    pass


def empty():
    return {"manifest_version": MANIFEST_VERSION, "packs": []}


def load(path):
    if not os.path.exists(path):
        return empty()
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data.get("packs"), list):
        raise ManifestError("manifest at %s has no packs list" % path)
    return data


def named_tags(manifest):
    return {entry.get("tag") for entry in manifest.get("packs", []) if entry.get("tag")}


def entry_from_report(report, url):
    return {
        # v1 keys (app parser today)
        "id": report["pack_id"],
        "kind": report["kind"],
        "language": report["language"],
        "version": int(report["version"]),
        "sizeBytes": report["download_bytes"],
        "sha256": report["sha256"],
        "url": url,
        # v2 keys
        "family": report["family"],
        "tier": report["tier"],
        "edition": report["edition"],
        "tag": report["tag"],
        "schema_version": report["schema_version"],
        "fold_version": report["fold_version"],
        "encoding": "gzip",
        "installed_bytes": report["installed_bytes"],
        "installed_sha256": report["installed_sha256"],
        "article_count": report["article_count"],
        "license": report["license"],
        "license_url": report["license_url"],
        "attribution": report["attribution"],
        "source": report["source"],
    }


def build_manifest(current, families, reports, verified_assets, url_for):
    """Return a new manifest: the entries whose id a report names are
    replaced by that report; every other entry is kept (see module doc).

    families: the families being published (each report must belong to one).
    verified_assets: set of (tag, asset name) that passed verification.
    url_for(tag, asset) -> absolute URL of the immutable release asset.
    """
    families = set(families)
    replaced = {report["pack_id"] for report in reports}
    kept = [e for e in current.get("packs", []) if e.get("id") not in replaced]
    fresh = []
    for report in reports:
        if report["family"] not in families:
            raise ManifestError("report %s is not in families %s" % (report["pack_id"], families))
        key = (report["tag"], report["asset"])
        if key not in verified_assets:
            raise ManifestError("refusing to name unverified asset %s/%s" % key)
        fresh.append(entry_from_report(report, url_for(report["tag"], report["asset"])))
    ids = [e["id"] for e in kept + fresh]
    if len(ids) != len(set(ids)):
        raise ManifestError("duplicate pack ids in manifest: %s" % ids)
    packs = sorted(kept + fresh, key=lambda e: (e.get("kind", ""), e.get("language", ""),
                                                 e.get("tier") or "", e.get("id", "")))
    return {"manifest_version": MANIFEST_VERSION,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "packs": packs}


def write(path, manifest):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
