"""Append-only event journal — JARVIS v4's audit spine.

Design (from the ai-eng study, P14-L34 + P15 + P18):
- The event log is the audit record, never the model's self-report.
- Hash-chained records: editing history breaks the chain and is detectable.
- Snapshots are records too, so state can be rebuilt by replay.

Stdlib only. One JSON object per line (JSONL). Thread-safe appends.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

GENESIS_HASH = "0" * 64
SNAPSHOT_KIND = "__snapshot__"


def _canonical(seq: int, ts: str, kind: str, payload: dict) -> str:
    return json.dumps(
        {"seq": seq, "ts": ts, "kind": kind, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _record_hash(prev_hash: str, seq: int, ts: str, kind: str, payload: dict) -> str:
    h = hashlib.sha256()
    h.update(prev_hash.encode("ascii"))
    h.update(_canonical(seq, ts, kind, payload).encode("utf-8"))
    return h.hexdigest()


class JournalError(Exception):
    """Raised when the journal on disk is structurally unreadable."""


class Journal:
    """Hash-chained append-only journal backed by a JSONL file."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._seq = 0
        self._last_hash = GENESIS_HASH
        if os.path.exists(path):
            self._resume()

    # -- internals ------------------------------------------------------

    def _resume(self) -> None:
        """Load the tail state (seq + last hash) from an existing file."""
        last = None
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last = line
        if last is None:
            return
        try:
            rec = json.loads(last)
            self._seq = int(rec["seq"])
            self._last_hash = str(rec["hash"])
        except (ValueError, KeyError, TypeError) as exc:
            raise JournalError(f"journal tail unreadable in {self.path}: {exc}") from exc

    # -- writing --------------------------------------------------------

    def append(self, kind: str, payload: Optional[dict] = None) -> dict:
        """Append one event; returns the stored record (with seq + hash)."""
        if not kind or kind == SNAPSHOT_KIND:
            raise ValueError("kind must be a non-empty event kind")
        payload = payload or {}
        with self._lock:
            seq = self._seq + 1
            ts = datetime.now(timezone.utc).isoformat()
            digest = _record_hash(self._last_hash, seq, ts, kind, payload)
            rec = {
                "seq": seq,
                "ts": ts,
                "kind": kind,
                "payload": payload,
                "prev": self._last_hash,
                "hash": digest,
            }
            line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._seq = seq
            self._last_hash = digest
            return rec

    def snapshot(self, state: dict) -> dict:
        """Store a state snapshot as a chained record (for fast rebuilds)."""
        with self._lock:
            seq = self._seq + 1
            ts = datetime.now(timezone.utc).isoformat()
            payload = {"state": state}
            digest = _record_hash(self._last_hash, seq, ts, SNAPSHOT_KIND, payload)
            rec = {
                "seq": seq,
                "ts": ts,
                "kind": SNAPSHOT_KIND,
                "payload": payload,
                "prev": self._last_hash,
                "hash": digest,
            }
            line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._seq = seq
            self._last_hash = digest
            return rec

    # -- reading --------------------------------------------------------

    def replay(self, upto_seq: Optional[int] = None) -> Iterator[dict]:
        """Yield records in order, optionally stopping at a seq (inclusive)."""
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError as exc:
                    raise JournalError(f"corrupt journal line: {exc}") from exc
                if upto_seq is not None and int(rec["seq"]) > upto_seq:
                    return
                yield rec

    def latest_snapshot(self) -> Optional[tuple[int, dict]]:
        """Return (seq, state) of the newest snapshot, or None."""
        found: Optional[tuple[int, dict]] = None
        for rec in self.replay():
            if rec["kind"] == SNAPSHOT_KIND:
                found = (int(rec["seq"]), rec["payload"]["state"])
        return found

    def verify(self) -> tuple[bool, Optional[int]]:
        """Recompute the whole chain. Returns (ok, first_bad_seq | None)."""
        prev = GENESIS_HASH
        expected_seq = 1
        for rec in self.replay():
            seq = int(rec["seq"])
            if seq != expected_seq:
                return (False, seq)
            if rec["prev"] != prev:
                return (False, seq)
            digest = _record_hash(prev, seq, rec["ts"], rec["kind"], rec["payload"])
            if digest != rec["hash"]:
                return (False, seq)
            prev = rec["hash"]
            expected_seq += 1
        return (True, None)

    # -- state ----------------------------------------------------------

    @property
    def seq(self) -> int:
        return self._seq

    def __len__(self) -> int:
        return self._seq
