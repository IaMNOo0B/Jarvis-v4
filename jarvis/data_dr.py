"""Data-dir disaster discipline for JARVIS v4 (Step 00 — Setup & Tooling).

Why this exists: the owner's data folder was once deleted and every key,
memory and setting was lost. From now on the data folder carries a
manifest (what files exist, how big, a fingerprint of each) and a backup
routine whose copy is PROVEN byte-for-byte before anyone trusts it.

Safety rules:
- File CONTENTS are never written into a manifest or printed. Secrets are
  fingerprinted (SHA-256) only — the digest proves identity without
  exposing the secret.
- Files larger than the hash limit are recorded by size only (their
  fingerprint field is null) so a manifest stays fast on model files.
- Backups and restores verify themselves and RAISE on any mismatch —
  a bad backup is never silently "successful".

CLI:
    python -m jarvis.data_dr snapshot <data_dir>
    python -m jarvis.data_dr verify   <data_dir> <manifest.json>
    python -m jarvis.data_dr backup   <data_dir> <backup_dir>
    python -m jarvis.data_dr restore  <backup_dir> <data_dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = "jarvis-data-manifest"
MANIFEST_VERSION = 1
HASH_LIMIT_BYTES = 8 * 1024 * 1024  # files above this are size-only in manifests
_CHUNK = 1024 * 1024


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------
def _sha256_of(path):
    """SHA-256 hex digest of a file's bytes (streamed; contents never stored)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _iter_files(data_dir):
    """Yield (relative_posix_path, absolute_path) for every real file, sorted.

    Symlinks are NOT followed (a link could point anywhere on the disk);
    they are reported separately by the caller via _iter_symlinks.
    """
    data_dir = Path(data_dir)
    found = []
    for path in data_dir.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_file():
            found.append((path.relative_to(data_dir).as_posix(), path))
    found.sort(key=lambda item: item[0])
    return found


def _iter_symlinks(data_dir):
    data_dir = Path(data_dir)
    links = []
    for path in data_dir.rglob("*"):
        if path.is_symlink():
            links.append(path.relative_to(data_dir).as_posix())
    links.sort()
    return links


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def snapshot_manifest(data_dir, *, hash_limit=HASH_LIMIT_BYTES):
    """Fingerprint a data folder into a manifest dict. Contents never included."""
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise ValueError(f"data dir does not exist: {data_dir}")
    files = {}
    unreadable = []
    total_bytes = 0
    for rel, path in _iter_files(data_dir):
        try:
            size = path.stat().st_size
            digest = _sha256_of(path) if size <= hash_limit else None
        except OSError:
            unreadable.append(rel)
            continue
        files[rel] = {"size": size, "sha256": digest, "hashed": digest is not None}
        total_bytes += size
    return {
        "format": MANIFEST_FORMAT,
        "version": MANIFEST_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "skipped_symlinks": _iter_symlinks(data_dir),
        "unreadable": unreadable,
        "total_files": len(files),
        "total_bytes": total_bytes,
    }


def verify_manifest(data_dir, manifest):
    """Compare a folder against a manifest. Returns a report dict.

    Report keys: ok, checked, missing, extra, changed (list of
    {path, reason}), unreadable. `ok` is True only when the folder
    matches the manifest exactly (no missing, no changed, no unreadable;
    `extra` files are reported but do not fail verification — a live
    data folder may legitimately grow new files after a backup).
    """
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        return {
            "ok": False, "checked": 0,
            "missing": sorted(manifest.get("files", {})),
            "extra": [], "changed": [],
            "unreadable": [], "error": f"data dir does not exist: {data_dir}",
        }
    expected = manifest.get("files", {})
    on_disk = dict(_iter_files(data_dir))
    missing, changed, unreadable = [], [], []
    for rel, meta in sorted(expected.items()):
        path = on_disk.get(rel)
        if path is None:
            missing.append(rel)
            continue
        try:
            size = path.stat().st_size
            if size != meta["size"]:
                changed.append({"path": rel, "reason": "size"})
                continue
            if meta.get("hashed"):
                if _sha256_of(path) != meta["sha256"]:
                    changed.append({"path": rel, "reason": "sha256"})
        except OSError:
            unreadable.append(rel)
    extra = sorted(set(on_disk) - set(expected))
    ok = not missing and not changed and not unreadable
    return {
        "ok": ok,
        "checked": len(expected),
        "missing": missing,
        "extra": extra,
        "changed": changed,
        "unreadable": unreadable,
    }


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------
def _guard_disjoint(data_dir, other_dir, other_label):
    data_res = Path(data_dir).resolve()
    other_res = Path(other_dir).resolve()
    if data_res == other_res:
        raise ValueError(f"{other_label} must differ from the data dir")
    if other_res in data_res.parents or data_res in other_res.parents:
        raise ValueError(
            f"{other_label} ({other_dir}) and data dir ({data_dir}) "
            "must not contain each other"
        )


def _copy_listed(source_root, dest_root, rel_paths):
    for rel in rel_paths:
        src = Path(source_root) / rel
        dst = Path(dest_root) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def make_backup(data_dir, dest_dir, *, hash_limit=HASH_LIMIT_BYTES):
    """Copy the data folder to `dest_dir`, write its manifest there, and
    PROVE the copy matches the source before returning.

    Returns {backup_dir, manifest_path, files, verification}.
    Raises ValueError on bad arguments, RuntimeError if the finished
    backup fails verification.
    """
    data_dir = Path(data_dir)
    dest_dir = Path(dest_dir)
    if not data_dir.is_dir():
        raise ValueError(f"data dir does not exist: {data_dir}")
    _guard_disjoint(data_dir, dest_dir, "backup dir")
    manifest = snapshot_manifest(data_dir, hash_limit=hash_limit)
    dest_dir.mkdir(parents=True, exist_ok=True)
    _copy_listed(data_dir, dest_dir, sorted(manifest["files"]))
    manifest_path = dest_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    report = verify_manifest(dest_dir, manifest)
    if not report["ok"]:
        raise RuntimeError(f"backup verification failed: {report}")
    return {
        "backup_dir": str(dest_dir),
        "manifest_path": str(manifest_path),
        "files": manifest["total_files"],
        "verification": report,
    }


def restore_backup(backup_dir, data_dir):
    """Restore a backup made by make_backup into `data_dir` and verify it.

    Returns the verification report dict. Raises ValueError when the
    backup has no manifest, RuntimeError if the restored folder fails
    verification against that manifest.
    """
    backup_dir = Path(backup_dir)
    data_dir = Path(data_dir)
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError(f"no {MANIFEST_NAME} in backup dir: {backup_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"backup manifest is unreadable: {exc}") from exc
    if manifest.get("format") != MANIFEST_FORMAT:
        raise ValueError("backup manifest has an unknown format")
    data_dir.mkdir(parents=True, exist_ok=True)
    _copy_listed(backup_dir, data_dir, sorted(manifest.get("files", {})))
    report = verify_manifest(data_dir, manifest)
    if not report["ok"]:
        raise RuntimeError(f"restore verification failed: {report}")
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_json(obj):
    print(json.dumps(obj, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="jarvis.data_dr",
        description="Manifest + verified backup/restore for the JARVIS data folder.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_snap = sub.add_parser("snapshot", help="print a manifest for a data folder")
    p_snap.add_argument("data_dir", type=Path)

    p_ver = sub.add_parser("verify", help="check a folder against a manifest file")
    p_ver.add_argument("data_dir", type=Path)
    p_ver.add_argument("manifest", type=Path)

    p_back = sub.add_parser("backup", help="copy a data folder + manifest, verified")
    p_back.add_argument("data_dir", type=Path)
    p_back.add_argument("backup_dir", type=Path)

    p_rest = sub.add_parser("restore", help="restore a verified backup")
    p_rest.add_argument("backup_dir", type=Path)
    p_rest.add_argument("data_dir", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            _print_json(snapshot_manifest(args.data_dir))
            return 0
        if args.command == "verify":
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            report = verify_manifest(args.data_dir, manifest)
            _print_json(report)
            return 0 if report["ok"] else 1
        if args.command == "backup":
            _print_json(make_backup(args.data_dir, args.backup_dir))
            return 0
        if args.command == "restore":
            _print_json(restore_backup(args.backup_dir, args.data_dir))
            return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
