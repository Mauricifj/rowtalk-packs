"""Delivery artifacts: gzip (-9, stdlib), digests, and the per-pack report.

The report (`<pack-id>.json`, uploaded next to `<pack-id>.db.gz`) is what the
publish job reads to verify the asset and to write the manifest entry. It is
small and never carries article text.
"""

import gzip
import hashlib
import json
import os
import shutil


def sha256_file(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def gzip_file(src, dst):
    """gzip -9 with mtime=0 and no embedded file name (stable bytes for the
    same input)."""
    tmp = dst + ".tmp"
    with open(src, "rb") as fin, open(tmp, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=9, fileobj=raw, mtime=0) as out:
            shutil.copyfileobj(fin, out, 1 << 20)
    os.replace(tmp, dst)
    return dst


def gunzip_file(src, dst):
    with gzip.open(src, "rb") as fin, open(dst, "wb") as out:
        shutil.copyfileobj(fin, out, 1 << 20)
    return dst


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def pack_report(pack_id, family, kind, language, tier, edition, version, tag, db_path, gz_path,
                meta, validation_errors, golden, gate, source):
    return {
        "report_version": 1,
        "pack_id": pack_id,
        "family": family,
        "kind": kind,
        "language": language,
        "tier": tier,
        "edition": edition,
        "version": version,
        "tag": tag,
        "asset": os.path.basename(gz_path),
        "download_bytes": os.path.getsize(gz_path),
        "sha256": sha256_file(gz_path),
        "installed_bytes": os.path.getsize(db_path),
        "installed_sha256": sha256_file(db_path),
        "article_count": int(meta["article_count"]),
        "key_count": int(meta["key_count"]),
        "schema_version": int(meta["schema_version"]),
        "fold_version": int(meta["fold_version"]),
        "ranking": meta["ranking"],
        "license": meta["license"],
        "license_url": meta["license_url"],
        "attribution": meta["attribution"],
        "source": source,
        "validation_errors": validation_errors,
        "golden": golden,
        "gate": gate,
    }
