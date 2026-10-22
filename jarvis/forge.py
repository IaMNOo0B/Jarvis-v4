"""Forge — JARVIS v4, Wave 5b: JARVIS khud feature jodta hai.

The owner's standing order: told "ye feature add karo", JARVIS
researches repositories, extracts the pattern, implements it
into itself, tests it — and lands it ONLY through this gate:

1. STAGE. The new/changed files are written into an isolated
   folder (data/forge/<slug>/), never into the live tree.
   Payload paths are code-checked: relative, no escapes, and
   only under jarvis/, tests/ or knowledge/.
2. VERIFY. A sandbox copy of the package + the payload runs
   the feature's own test plus an import smoke of the whole
   app. Red here means the live tree was never touched.
3. PROMOTE. Before anything lands, every file it would
   overwrite is backed up inside the forge entry (and new
   files are recorded). The payload lands; the feature test
   runs AGAIN in the live tree.
4. ROLLBACK. If the live run fails, promotion undoes itself
   from the backup — automatically. The owner can also roll
   back any promoted feature by hand (/forge rollback).

The quality gate is the safeguard the owner accepted for his
no-restriction self-upgrade order: nothing unproven lands,
and everything landed can be taken back.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ALLOWED_PREFIXES = ("jarvis/", "tests/", "knowledge/")
FORGE_DIR = "forge"


def _slug(name: str) -> str:
    keep = [c if c.isalnum() else "-"
            for c in str(name).lower()]
    slug = "".join(keep).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:40] or "feature"


def _check_paths(files: dict) -> str:
    for rel in files:
        path = str(rel).replace("\\", "/")
        if path.startswith("/") or ".." in path.split("/"):
            return f"raasta mana hai: {rel}"
        if not path.startswith(ALLOWED_PREFIXES):
            return (f"file sirf jarvis/, tests/ ya knowledge/ "
                    f"ke andar ja sakti hai: {rel}")
    return ""


def _run_tests(cwd: Path, test_rel: str) -> tuple:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", test_rel],
            cwd=str(cwd), capture_output=True, text=True,
            timeout=180)
        tail = (proc.stdout + proc.stderr).strip()[-500:]
        return proc.returncode == 0, tail
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _smoke_import(cwd: Path) -> tuple:
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import jarvis.chat"],
            cwd=str(cwd), capture_output=True, text=True,
            timeout=60)
        return proc.returncode == 0, \
            (proc.stderr or "").strip()[-300:]
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def stage_feature(data_dir, root, name: str, files: dict,
                  test_code: str, source: str = "") -> dict:
    """Write the payload + its test into data/forge/<slug>/.
    Returns the entry dict (with 'dir' and 'slug')."""
    problem = _check_paths(files)
    if problem:
        return {"ok": False, "error": problem}
    if not str(test_code).strip():
        return {"ok": False,
                "error": "feature ka apna test chahiye — bina "
                         "saboot ke kuch nahi lagega"}
    slug = _slug(name) + "-" + uuid.uuid4().hex[:6]
    entry = Path(data_dir) / FORGE_DIR / slug
    payload = entry / "payload"
    for rel, content in files.items():
        target = payload / str(rel).replace("\\", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    test_rel = (f"tests/test_forge_"
                f"{_slug(name).replace('-', '_')}.py")
    (entry / "test_file.py").write_text(str(test_code),
                                       encoding="utf-8")
    manifest = {"slug": slug, "name": str(name)[:80],
                "ts": time.time(),
                "files": sorted(str(r) for r in files),
                "test_rel": test_rel,
                "source": str(source)[:500],
                "status": "staged"}
    (entry / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return {"ok": True, "slug": slug, "dir": str(entry),
            **manifest}


def _state(entry: Path, manifest: dict) -> None:
    (entry / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1),
        encoding="utf-8")


def verify_staged(root, entry_dir) -> dict:
    """Sandbox: copy the package, apply the payload, run the
    feature test + an import smoke. The live tree is only
    READ (for the copy), never written."""
    entry = Path(entry_dir)
    manifest = json.loads((entry / "MANIFEST.json").read_text(
        encoding="utf-8"))
    sandbox = entry / "sandbox"
    if sandbox.exists():
        shutil.rmtree(sandbox, ignore_errors=True)
    sandbox.mkdir(parents=True)
    root = Path(root)
    shutil.copytree(root / "jarvis", sandbox / "jarvis")
    payload = entry / "payload"
    for path in payload.rglob("*"):
        if path.is_file():
            dest = sandbox / path.relative_to(payload)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
    test_target = sandbox / manifest["test_rel"]
    test_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(entry / "test_file.py", test_target)
    ok, tail = _run_tests(sandbox, manifest["test_rel"])
    if not ok:
        manifest["status"] = "verify-failed"
        manifest["verify_tail"] = tail
        _state(entry, manifest)
        return {"ok": False, "stage": "tests", "detail": tail}
    ok, tail = _smoke_import(sandbox)
    if not ok:
        manifest["status"] = "verify-failed"
        manifest["verify_tail"] = tail
        _state(entry, manifest)
        return {"ok": False, "stage": "import", "detail": tail}
    manifest["status"] = "verified"
    _state(entry, manifest)
    return {"ok": True}


def promote(root, entry_dir, runner=None) -> dict:
    """Land a VERIFIED entry: back up what it overwrites,
    apply, re-run its test live, and roll back automatically
    if the live run fails. `runner(cwd, test_rel) -> (ok,
    tail)` is injectable for tests."""
    entry = Path(entry_dir)
    manifest = json.loads((entry / "MANIFEST.json").read_text(
        encoding="utf-8"))
    if manifest.get("status") != "verified":
        return {"ok": False,
                "error": "pehle verify hona chahiye — ye entry "
                         f"{manifest.get('status')} hai"}
    root = Path(root)
    backup = entry / "backup"
    backup.mkdir(parents=True, exist_ok=True)
    run = runner or _run_tests
    payload = entry / "payload"
    landed = []
    try:
        for path in sorted(payload.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(payload)
            dest = root / rel
            if dest.exists():
                keep = backup / rel
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(dest, keep)
            else:
                (backup / (str(rel) + ".NEW")).parent.mkdir(
                    parents=True, exist_ok=True)
                (backup / (str(rel) + ".NEW")).write_text(
                    "", encoding="utf-8")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
            landed.append(rel)
        live_test = root / manifest["test_rel"]
        live_test.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(entry / "test_file.py", live_test)
        ok, tail = run(root, manifest["test_rel"])
        if not ok:
            _restore(root, entry, manifest)
            manifest["status"] = "rolled-back"
            manifest["live_tail"] = tail
            _state(entry, manifest)
            return {"ok": False, "stage": "live-tests",
                    "detail": tail,
                    "note": "live test fail — sab kuch khud "
                            "wapas purana kar diya gaya"}
    except Exception as exc:
        _restore(root, entry, manifest)
        manifest["status"] = "rolled-back"
        _state(entry, manifest)
        return {"ok": False, "stage": "apply",
                "detail": f"{type(exc).__name__}: {exc}",
                "note": "gadbad hui — rollback ho gaya"}
    manifest["status"] = "promoted"
    manifest["promoted_ts"] = time.time()
    _state(entry, manifest)
    return {"ok": True, "files": [str(r) for r in landed]}


def _restore(root, entry: Path, manifest: dict) -> None:
    root = Path(root)
    backup = entry / "backup"
    if backup.exists():
        for path in sorted(backup.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(backup)
            if str(rel).endswith(".NEW"):
                target = root / str(rel)[:-4]
                if target.exists():
                    target.unlink()
                continue
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
    live_test = root / manifest["test_rel"]
    if live_test.exists():
        live_test.unlink()


def rollback(root, data_dir, slug: str) -> dict:
    """The owner's manual undo for a promoted feature."""
    entry = Path(data_dir) / FORGE_DIR / slug
    if not entry.exists():
        return {"ok": False, "error": "woh forge entry nahi "
                                      "mili, sir"}
    manifest = json.loads((entry / "MANIFEST.json").read_text(
        encoding="utf-8"))
    if manifest.get("status") != "promoted":
        return {"ok": False,
                "error": f"ye feature live hi nahi hai "
                         f"({manifest.get('status')})"}
    _restore(Path(root), entry, manifest)
    manifest["status"] = "rolled-back"
    _state(entry, manifest)
    return {"ok": True, "slug": slug}


def list_entries(data_dir) -> list:
    base = Path(data_dir) / FORGE_DIR
    if not base.is_dir():
        return []
    out = []
    for entry in sorted(base.iterdir()):
        try:
            out.append(json.loads(
                (entry / "MANIFEST.json").read_text(
                    encoding="utf-8")))
        except Exception:
            continue
    return out
