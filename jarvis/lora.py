"""LoRA adapter lifecycle registry — JARVIS v4, Steps 08/09 data layer.

Design (ai-eng study, Part D Phase 08 L08 + Part E Phase 10 L06/L08):
- LoRA: W' = W + alpha * B A on a FROZEN base model, rank r = 4-16 typical,
  B zero-initialised — an adapter is a small, removable personalisation delta
  (~20-200 MB), never a new model. This module is the bookkeeping layer for
  those deltas: manifests, lifecycle, eval-gated activation. No torch, no
  training — the data/management layer only.
- The ratchet rule: an adapter may go live ONLY with a recorded eval delta
  against the behaviour suite. No delta, no activation — no exceptions. A
  persona delta that was never measured never reaches the owner.
- Exactly one adapter is active per base model. Activating a candidate
  retires the previously active adapter for that base (superseded).
- Every transition is appended to the hash-chained journal (jarvis.journal)
  when a journal is attached: the log is the audit record, never a
  self-report (study Part I, Phase 18).

Layout on disk:
    <root>/<adapter-name>/adapter.json

Manifest keys: name, base_model, rank, alpha, created_from (dataset hash),
status (candidate|active|retired), eval_delta (latest recorded eval, or
None), eval_history, history (transition log inside the manifest).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:  # the journal module lives in this repo; stay importable without it
    from jarvis.journal import Journal
except Exception:  # pragma: no cover
    Journal = None  # type: ignore[assignment]

MANIFEST_NAME = "adapter.json"
STATUSES = ("candidate", "active", "retired")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class AdapterError(Exception):
    """Raised for registry misuse: bad manifests, unknown adapters, ..."""


class ActivationRefused(AdapterError):
    """Raised when activation violates the ratchet rule or lifecycle."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _copy(manifest: dict) -> dict:
    return json.loads(json.dumps(manifest, ensure_ascii=False))


class AdapterRegistry:
    """Directory-of-adapters registry; adapter.json on disk is the truth."""

    def __init__(
        self,
        root: str | Path,
        journal: Optional["Journal"] = None,
        journal_path: str | Path | None = None,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._journal = journal
        if self._journal is None and journal_path is not None and Journal is not None:
            self._journal = Journal(str(journal_path))

    # -- disk helpers ---------------------------------------------------

    def _adapter_dir(self, name: str) -> Path:
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise AdapterError(f"unsafe adapter name: {name!r}")
        return self.root / name

    def _load(self, name: str) -> dict:
        path = self._adapter_dir(name) / MANIFEST_NAME
        if not path.exists():
            raise AdapterError(f"unknown adapter: {name!r}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise AdapterError(f"corrupt manifest for {name!r}: {exc}") from exc

    def _save(self, manifest: dict) -> None:
        manifest["updated_at"] = _now()
        path = self._adapter_dir(manifest["name"]) / MANIFEST_NAME
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _all(self) -> list[dict]:
        out: list[dict] = []
        for child in sorted(self.root.iterdir()):
            manifest_path = child / MANIFEST_NAME
            if child.is_dir() and manifest_path.exists():
                try:
                    out.append(json.loads(manifest_path.read_text(encoding="utf-8")))
                except ValueError:
                    continue  # corrupt manifests are invisible, never fatal
        return out

    def _emit(self, kind: str, payload: dict) -> None:
        if self._journal is not None:
            self._journal.append(kind, payload)

    # -- reads ------------------------------------------------------------

    def get(self, name: str) -> dict:
        return _copy(self._load(name))

    def list(self, base_model: str | None = None, status: str | None = None) -> list[dict]:
        items = self._all()
        if base_model is not None:
            items = [m for m in items if m.get("base_model") == base_model]
        if status is not None:
            items = [m for m in items if m.get("status") == status]
        return [_copy(m) for m in items]

    def active_for(self, base_model: str) -> Optional[dict]:
        for m in self._all():
            if m.get("base_model") == base_model and m.get("status") == "active":
                return _copy(m)
        return None

    # -- lifecycle --------------------------------------------------------

    def register(
        self,
        *,
        name: str,
        base_model: str,
        rank: int,
        alpha: float,
        created_from: str,
        notes: str | None = None,
    ) -> dict:
        """Create a new adapter in `candidate` status.

        `created_from` is the hash of the dataset the adapter was trained
        from (see jarvis.dpo_data manifest) — provenance is mandatory.
        """
        self._adapter_dir(name)  # validates the name
        if not isinstance(base_model, str) or not base_model.strip():
            raise AdapterError("base_model must be a non-empty string")
        if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
            raise AdapterError(f"rank must be a positive int, got {rank!r}")
        if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or alpha <= 0:
            raise AdapterError(f"alpha must be a positive number, got {alpha!r}")
        if not isinstance(created_from, str) or not created_from.strip():
            raise AdapterError("created_from must name the dataset hash")
        if (self.root / name).exists():
            raise AdapterError(f"adapter already registered: {name!r}")
        (self.root / name).mkdir(parents=True)
        manifest: dict[str, Any] = {
            "name": name,
            "base_model": base_model,
            "rank": rank,
            "alpha": alpha,
            "created_from": created_from,
            "status": "candidate",
            "eval_delta": None,
            "eval_history": [],
            "notes": notes,
            "created_at": _now(),
            "updated_at": _now(),
            "history": [{"event": "registered", "ts": _now(), "status": "candidate"}],
        }
        self._save(manifest)
        self._emit(
            "adapter.registered",
            {"name": name, "base_model": base_model, "rank": rank, "alpha": alpha,
             "created_from": created_from},
        )
        return _copy(manifest)

    def record_eval(
        self,
        name: str,
        delta: float,
        *,
        suite: str = "behaviour",
        notes: str | None = None,
    ) -> dict:
        """Record a measured eval delta for an adapter.

        `delta` is the behaviour-suite score change vs the bare base model
        (e.g. +0.07 = 7 points better). Activation reads the LATEST record.
        """
        if isinstance(delta, bool) or not isinstance(delta, (int, float)):
            raise AdapterError(f"eval delta must be a number, got {delta!r}")
        manifest = self._load(name)
        if manifest["status"] == "retired":
            raise AdapterError(f"cannot record eval for retired adapter {name!r}")
        entry = {"delta": float(delta), "suite": suite, "notes": notes, "ts": _now()}
        manifest.setdefault("eval_history", []).append(entry)
        manifest["eval_delta"] = entry
        manifest["history"].append(
            {"event": "eval_recorded", "ts": _now(), "delta": float(delta), "suite": suite}
        )
        self._save(manifest)
        self._emit(
            "adapter.eval_recorded",
            {"name": name, "base_model": manifest["base_model"],
             "delta": float(delta), "suite": suite},
        )
        return _copy(manifest)

    def activate(self, name: str) -> dict:
        """Promote a candidate to active for its base model.

        Ratchet rule: status must be `candidate` AND an eval delta must be
        recorded. The previously active adapter for the same base model (if
        any) is retired as superseded, keeping exactly one active per base.
        """
        manifest = self._load(name)
        if manifest["status"] != "candidate":
            raise ActivationRefused(
                f"only a candidate can be activated; {name!r} is {manifest['status']!r}"
            )
        if not manifest.get("eval_delta"):
            raise ActivationRefused(
                f"{name!r} has no recorded eval delta — eval first, activation second"
            )
        base = manifest["base_model"]
        for other in self._all():
            if (
                other["name"] != name
                and other.get("base_model") == base
                and other.get("status") == "active"
            ):
                other["status"] = "retired"
                other["history"].append(
                    {"event": "retired", "ts": _now(), "reason": f"superseded_by:{name}"}
                )
                self._save(other)
                self._emit(
                    "adapter.retired",
                    {"name": other["name"], "base_model": base,
                     "reason": f"superseded_by:{name}"},
                )
        manifest["status"] = "active"
        manifest["history"].append(
            {"event": "activated", "ts": _now(),
             "eval_delta": manifest["eval_delta"]["delta"]}
        )
        self._save(manifest)
        self._emit(
            "adapter.activated",
            {"name": name, "base_model": base,
             "eval_delta": manifest["eval_delta"]["delta"]},
        )
        return _copy(manifest)

    def retire(self, name: str, *, reason: str | None = None) -> dict:
        """Move a candidate or active adapter to `retired` (terminal)."""
        manifest = self._load(name)
        if manifest["status"] == "retired":
            raise AdapterError(f"adapter {name!r} is already retired")
        manifest["status"] = "retired"
        manifest["history"].append({"event": "retired", "ts": _now(), "reason": reason})
        self._save(manifest)
        self._emit(
            "adapter.retired",
            {"name": name, "base_model": manifest["base_model"], "reason": reason},
        )
        return _copy(manifest)
