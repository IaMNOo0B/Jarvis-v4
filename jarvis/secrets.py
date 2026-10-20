"""Secret storage for JARVIS v4 — values never logged, never printed.

Spine contract (shared with providers/brain/gate/skills/memory/chat):

    from jarvis.secrets import SecretStore
    store = SecretStore(data_dir)
    store.set("llm:openrouter:1", "...")   # value goes in
    store.get("llm:openrouter:1")          # value comes out, here only
    store.names()                          # names only — NEVER values

Honest backend story (the v2 lesson, kept):
- The RIGHT home for secrets is the OS keyring. On the owner's Windows PC
  that is the Credential Manager, reached via ctypes. That wiring is
  verified on the PC (it cannot be exercised from a Linux build box), so
  _WindowsCredBackend below is a structured PLUG POINT: it reports itself
  available only on win32 and is never selected on this platform.
- Everywhere else the store falls back to a local file, secrets.json,
  written atomically with 0600 permissions, and it SAYS SO: backend_name
  is "local-file (less secure)" and a warning is printed to stderr once
  per secrets file per process. No silent downgrade.
- Nothing in this module ever prints, logs, reprs or raises WITH a secret
  value inside. Exceptions name the file and the failure, never contents.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

SECRETS_NAME = "secrets.json"

_WARNING = (
    "JARVIS: secrets are stored in a local file (less secure — "
    "no OS keyring available on this platform). File: {path}"
)
_warned_paths: set = set()
_warned_lock = threading.Lock()


class SecretError(Exception):
    """Raised when the secrets store is unreadable or misused.

    Messages never contain secret values.
    """


def _warn_once(path: Path) -> None:
    key = str(path)
    with _warned_lock:
        if key in _warned_paths:
            return
        _warned_paths.add(key)
    print(_WARNING.format(path=path), file=sys.stderr)


def _atomic_write_secrets(path: Path, obj) -> None:
    """Write obj as JSON to path atomically, 0600 from birth to replace."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
class _LocalFileBackend:
    """secrets.json in the data dir, 0600, atomic writes."""

    name = "local-file (less secure)"

    def __init__(self, data_dir: Path):
        self.path = data_dir / SECRETS_NAME

    @classmethod
    def available(cls) -> bool:
        return True

    def read_all(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise SecretError(
                f"secrets file unreadable: {self.path} ({type(exc).__name__}); "
                "fix or remove it — no secrets were touched"
            ) from exc
        if not isinstance(raw, dict):
            raise SecretError(
                f"secrets file must hold a JSON object: {self.path}"
            )
        return raw

    def write_all(self, data: dict) -> None:
        _atomic_write_secrets(self.path, data)


class _WindowsCredBackend:
    """Plug point for the Windows Credential Manager (owner's PC).

    The ctypes CredReadW/CredWriteW wiring is verified on the owner's
    Windows machine (Step 00 Windows checks) — it cannot run on this
    Linux build platform, so available() is honest: win32 AND the PC-side
    wiring flag. Until then the local-file backend serves, loudly.
    """

    name = "windows-credential-manager"
    _pc_wiring_verified = False  # flipped by the PC-verified ctypes module

    @classmethod
    def available(cls) -> bool:
        return sys.platform == "win32" and cls._pc_wiring_verified

    def __init__(self, data_dir: Path):  # pragma: no cover - PC only
        raise SecretError(
            "Windows Credential Manager backend is not wired on this "
            "platform; the local-file backend should have been selected"
        )


_BACKENDS = (_WindowsCredBackend, _LocalFileBackend)


def _select_backend(data_dir: Path):
    for cls in _BACKENDS:
        if cls.available():
            return cls(data_dir)
    raise SecretError("no secrets backend available")  # pragma: no cover


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class SecretStore:
    """Names + values in, names only out (except an explicit .get)."""

    def __init__(self, data_dir):
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._backend = _select_backend(self._dir)
        if isinstance(self._backend, _LocalFileBackend):
            _warn_once(self._backend.path)
        self._lock = threading.Lock()

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def data_dir(self) -> Path:
        return self._dir

    def get(self, name: str):
        """Return the secret stored under name, or None when absent."""
        return self._backend.read_all().get(name)

    def set(self, name: str, value: str) -> None:
        if not name or not isinstance(name, str):
            raise SecretError("secret name must be a non-empty string")
        if not isinstance(value, str) or value == "":
            raise SecretError(f"secret {name!r} must be a non-empty string")
        with self._lock:
            data = self._backend.read_all()
            data[name] = value
            self._backend.write_all(data)

    def delete(self, name: str) -> bool:
        """Remove name. Returns True when something was removed."""
        with self._lock:
            data = self._backend.read_all()
            if name not in data:
                return False
            del data[name]
            self._backend.write_all(data)
            return True

    def names(self) -> list:
        """Sorted secret NAMES. Values are never part of this output."""
        return sorted(self._backend.read_all().keys())

    def __contains__(self, name: str) -> bool:
        return name in self._backend.read_all()

    def __len__(self) -> int:
        return len(self._backend.read_all())

    def __repr__(self) -> str:
        # Count only — a repr must be safe to log.
        return (
            f"SecretStore(backend={self.backend_name!r}, "
            f"count={len(self)})"
        )
