"""Backup — JARVIS v4: the profile, packed and restorable.

The owner's JARVIS is his memory, his rules, his learned
library, his calibrations and trackers — all of it lives in
one data/ folder. One bad disk and it is gone. /backup packs
that folder into a dated zip under data/backups/; a restore
unpacks it into a fresh install. Two hard rules:

* SECRETS NEVER TRAVEL. The secrets file (and any keyring
  spill) is excluded by name — a backup is safe to keep on a
  drive, a stick, anywhere. Keys are re-filed by the owner in
  Settings after a restore; everything else comes back.
* RESTORE NEVER OVERWRITES BLINDLY. Files land only inside
  the target data dir, path-checked per entry.
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

EXCLUDED_NAMES = {"secrets.json", "secrets.json.bak"}
EXCLUDED_DIRS = {"backups", "team"}


def export_profile(data_dir) -> dict:
    """Zip the data folder (minus secrets, minus old backups
    and team workspaces). Returns {ok, path, files}."""
    data_dir = Path(data_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"profile-{stamp}.zip"
    files = 0
    with zipfile.ZipFile(out_path, "w",
                         zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(data_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(data_dir)
            if rel.parts[0] in EXCLUDED_DIRS:
                continue
            if path.name in EXCLUDED_NAMES:
                continue
            zf.write(path, rel.as_posix())
            files += 1
    return {"ok": True, "path": str(out_path), "files": files}


def restore_profile(data_dir, zip_path) -> dict:
    """Unpack a profile zip INTO data_dir, entry by entry,
    refusing anything that would land outside it. Secrets are
    skipped even if a foreign zip carries them."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    restored = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            if Path(name).name in EXCLUDED_NAMES:
                continue
            target = (data_dir / name).resolve()
            if not str(target).startswith(
                    str(data_dir.resolve())):
                continue                     # path escape: refuse
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            restored += 1
    return {"ok": True, "restored": restored}
