"""Configuration for JARVIS v4 — data_dir/config.json, atomically written.

Spine contract (shared with providers/brain/gate/skills/memory/chat):

    from jarvis.config import load
    cfg = load(data_dir)                      # -> Config
    cfg.get("brain", "model", "qwen2.5:7b")   # section, key, default

Design rules (lessons carried from v1/v2, not re-learned):
- Atomic writes only: a config write goes to a temp file in the same
  directory, is flushed + fsynced, then os.replace()d over the target.
  A crash mid-save can never leave a torn config.json behind (the v1
  settings-corruption failure class).
- A corrupt config.json is LOUD: load() raises ConfigError and never
  silently resets the owner's settings to defaults.
- Missing file is fine: first run means "all defaults" until the first
  set() creates the file.
- API keys do NOT live here. Secrets belong in jarvis.secrets; config
  holds plain settings only.
- When a Journal is attached, every successful set() appends a
  "config.changed" event carrying the section and key — never the value
  (the journal is an audit of WHAT changed, readable by anyone with the
  data dir; values stay out of it on principle).

Stdlib only.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

CONFIG_NAME = "config.json"

_TRUE_WORDS = {"true", "1", "yes", "on"}
_FALSE_WORDS = {"false", "0", "no", "off"}


class ConfigError(Exception):
    """Raised when config on disk is unreadable or a value is invalid."""


def _atomic_write_json(path: Path, obj) -> None:
    """Write obj as JSON to path atomically (tmp file + fsync + replace)."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


class Config:
    """Sectioned settings backed by <data_dir>/config.json."""

    def __init__(self, data_dir, journal=None):
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / CONFIG_NAME
        self._journal = journal
        self._lock = threading.Lock()
        self._data = self._read()

    # -- loading ---------------------------------------------------------

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ConfigError(
                f"config file unreadable: {self._path} ({exc}); "
                "fix or remove it — settings were NOT reset"
            ) from exc
        if not isinstance(raw, dict):
            raise ConfigError(
                f"config file must hold a JSON object of sections: {self._path}"
            )
        return raw

    @property
    def path(self) -> Path:
        return self._path

    @property
    def data_dir(self) -> Path:
        return self._dir

    # -- journal ---------------------------------------------------------

    def attach_journal(self, journal) -> None:
        """Attach (or replace) the journal that receives config.changed."""
        self._journal = journal

    # -- reads -----------------------------------------------------------

    def get(self, section: str, key: str, default=None):
        """Return config[section][key], or default when absent."""
        sec = self._data.get(section)
        if not isinstance(sec, dict):
            return default
        return sec.get(key, default)

    def getint(self, section: str, key: str, default=None):
        value = self.get(section, key, None)
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return default
        return default

    def getfloat(self, section: str, key: str, default=None):
        value = self.get(section, key, None)
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return default
        return default

    def getbool(self, section: str, key: str, default=None):
        value = self.get(section, key, None)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            word = value.strip().lower()
            if word in _TRUE_WORDS:
                return True
            if word in _FALSE_WORDS:
                return False
        return default

    def sections(self) -> list:
        """Sorted section names currently present."""
        return sorted(self._data.keys())

    def keys(self, section: str) -> list:
        """Sorted keys inside one section (empty list when absent)."""
        sec = self._data.get(section)
        if not isinstance(sec, dict):
            return []
        return sorted(sec.keys())

    def as_dict(self) -> dict:
        """Deep copy of all settings (mutating it changes nothing here)."""
        return json.loads(json.dumps(self._data))

    # -- writes ----------------------------------------------------------

    def set(self, section: str, key: str, value) -> None:
        """Set one value, persist atomically, journal the change."""
        if not section or not isinstance(section, str):
            raise ConfigError("section must be a non-empty string")
        if not key or not isinstance(key, str):
            raise ConfigError("key must be a non-empty string")
        try:
            json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"value for {section}.{key} is not JSON-serializable"
            ) from exc
        with self._lock:
            new_data = self.as_dict()
            sec = new_data.get(section)
            if not isinstance(sec, dict):
                sec = {}
                new_data[section] = sec
            sec[key] = value
            _atomic_write_json(self._path, new_data)
            self._data = new_data
        if self._journal is not None:
            # Section + key only — never the value (see module docstring).
            self._journal.append(
                "config.changed", {"section": section, "key": key}
            )

    def reload(self) -> None:
        """Re-read config.json from disk (picks up external edits)."""
        with self._lock:
            self._data = self._read()

    def __repr__(self) -> str:
        return f"Config(path={str(self._path)!r}, sections={len(self._data)})"


def load(data_dir, journal=None) -> Config:
    """Spine-contract entry point: load(data_dir) -> Config."""
    return Config(data_dir, journal=journal)
