"""Idempotency ledger for side effects — JARVIS v4, Step 13.

Design (from the ai-eng study, part-F L29 + part-G L34):
- Every side effect (send / write / payment-class action) gets a business
  key + an argument fingerprint, committed to the journal atomically with
  the effect's outcome.
- The PENDING record is written BEFORE the effect runs. If the process
  dies mid-effect, the key is left PENDING — on recovery that is an
  UNKNOWN OUTCOME: the effect may or may not have happened, so it is
  NEVER auto-retried. It is listed for a human/owner decision.
- Replay is safe: same key + same args after commit returns the recorded
  result without re-executing. Same key + DIFFERENT args is a hard error —
  a key names ONE specific action, forever.

Built on jarvis.journal (hash-chained, append-only). The ledger's state is
derived purely by replaying the journal, so a restart rebuilds it exactly.
Stdlib only.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable, Optional, Union

from .journal import Journal

KIND_BEGIN = "idem.begin"
KIND_COMMIT = "idem.commit"
KIND_FAIL = "idem.fail"

STATUS_PENDING = "pending"
STATUS_COMMITTED = "committed"
STATUS_FAILED = "failed"


class IdempotencyError(Exception):
    """Base class for ledger errors."""


class IdempotencyConflictError(IdempotencyError):
    """Same business key presented with different arguments."""


class UnknownOutcomeError(IdempotencyError):
    """Key is PENDING from a previous run — the effect's fate is unknown."""


class EffectFailedError(IdempotencyError):
    """The recorded effect failed; re-running under the same key is refused."""


class LedgerStateError(IdempotencyError):
    """commit/fail called for a key that is not PENDING."""


def _canonical(obj: Any) -> str:
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _fingerprint(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def business_key(thread_id: str, action_signature: Any) -> str:
    """Derive the business key: sha256 of canonical JSON.

    action_signature should fully describe the action (tool + arguments).
    Key derivation is canonical — dict ordering never changes the key.
    """
    return _fingerprint({"thread": thread_id, "action": action_signature})


class IdempotencyLedger:
    """Side-effect ledger over a hash-chained Journal.

    Accepts a Journal instance or a journal file path.
    """

    def __init__(self, journal: Union[Journal, str]):
        self.journal = journal if isinstance(journal, Journal) else Journal(journal)
        self._records: dict[str, dict] = {}
        self._unknown_at_open: set[str] = set()
        self._scan()
        # Anything still PENDING at open was left behind by a previous
        # process (or a crash): its outcome is unknown by definition.
        self._unknown_at_open = {
            key
            for key, rec in self._records.items()
            if rec["status"] == STATUS_PENDING
        }

    # -- state derivation -----------------------------------------------

    def _scan(self) -> None:
        for rec in self.journal.replay():
            kind, payload = rec["kind"], rec["payload"]
            key = payload.get("key")
            if key is None:
                continue
            if kind == KIND_BEGIN:
                if key not in self._records:  # first begin wins; key is forever
                    self._records[key] = {
                        "key": key,
                        "status": STATUS_PENDING,
                        "args_fingerprint": payload["args_fingerprint"],
                        "payload": payload["payload"],
                        "result": None,
                        "error": None,
                        "begun_seq": rec["seq"],
                        "updated_seq": rec["seq"],
                    }
            elif kind == KIND_COMMIT and key in self._records:
                r = self._records[key]
                r["status"] = STATUS_COMMITTED
                r["result"] = payload["result"]
                r["updated_seq"] = rec["seq"]
            elif kind == KIND_FAIL and key in self._records:
                r = self._records[key]
                r["status"] = STATUS_FAILED
                r["error"] = payload["error"]
                r["updated_seq"] = rec["seq"]

    def _view(self, key: str) -> dict:
        """A defensive-copy public view of one record."""
        rec = self._records[key]
        out = copy.deepcopy(rec)
        out["unknown_outcome"] = (
            key in self._unknown_at_open and rec["status"] == STATUS_PENDING
        )
        return out

    # -- core operations --------------------------------------------------

    def begin(self, key: str, payload: Any) -> dict:
        """Write the PENDING record BEFORE any effect runs.

        If the key already exists: same args -> the existing record is
        returned (no second begin, no second effect); different args ->
        IdempotencyConflictError. A key names one action, forever.
        """
        fp = _fingerprint(payload)
        existing = self._records.get(key)
        if existing is not None:
            if existing["args_fingerprint"] != fp:
                raise IdempotencyConflictError(
                    f"business key {key} was begun with different arguments"
                )
            return self._view(key)
        rec = self.journal.append(
            KIND_BEGIN,
            {"key": key, "args_fingerprint": fp, "payload": payload},
        )
        self._records[key] = {
            "key": key,
            "status": STATUS_PENDING,
            "args_fingerprint": fp,
            "payload": copy.deepcopy(payload),
            "result": None,
            "error": None,
            "begun_seq": rec["seq"],
            "updated_seq": rec["seq"],
        }
        return self._view(key)

    def commit(self, key: str, result: Any) -> dict:
        """Record the effect's result. Key must be PENDING."""
        rec = self._records.get(key)
        if rec is None or rec["status"] != STATUS_PENDING:
            raise LedgerStateError(f"cannot commit key {key}: not pending")
        stored = self.journal.append(KIND_COMMIT, {"key": key, "result": result})
        rec["status"] = STATUS_COMMITTED
        rec["result"] = copy.deepcopy(result)
        rec["updated_seq"] = stored["seq"]
        self._unknown_at_open.discard(key)
        return self._view(key)

    def fail(self, key: str, error: str) -> dict:
        """Record that the effect failed. Key must be PENDING."""
        rec = self._records.get(key)
        if rec is None or rec["status"] != STATUS_PENDING:
            raise LedgerStateError(f"cannot fail key {key}: not pending")
        stored = self.journal.append(KIND_FAIL, {"key": key, "error": str(error)})
        rec["status"] = STATUS_FAILED
        rec["error"] = str(error)
        rec["updated_seq"] = stored["seq"]
        self._unknown_at_open.discard(key)
        return self._view(key)

    def lookup(self, key: str, payload: Any = None) -> Optional[dict]:
        """Return the record for a key (or None if the key is unknown).

        With payload given, the arguments are verified: same key +
        different args raises IdempotencyConflictError instead of
        returning a result that belongs to a different action.
        """
        rec = self._records.get(key)
        if rec is None:
            return None
        if payload is not None and _fingerprint(payload) != rec["args_fingerprint"]:
            raise IdempotencyConflictError(
                f"business key {key} was begun with different arguments"
            )
        return self._view(key)

    # -- the executor -----------------------------------------------------

    def run(self, key: str, payload: Any, effect: Callable[[Any], Any]) -> Any:
        """Execute `effect(payload)` exactly once for this key, ever.

        - COMMITTED key -> recorded result returned, effect NOT called.
        - PENDING key (left by a crash / concurrent run) ->
          UnknownOutcomeError; the effect is never auto-retried.
        - FAILED key -> EffectFailedError with the recorded error.
        - New key -> begin, run effect, commit (or fail on exception).
        If the process dies inside the effect, the key stays PENDING and
        the next open reports it via unknown_outcomes().
        """
        rec = self.lookup(key, payload)  # also enforces args identity
        if rec is not None:
            if rec["status"] == STATUS_COMMITTED:
                return copy.deepcopy(rec["result"])
            if rec["status"] == STATUS_FAILED:
                raise EffectFailedError(
                    f"effect for key {key} already failed: {rec['error']}"
                )
            raise UnknownOutcomeError(
                f"key {key} is pending — a previous attempt may or may not "
                "have completed; refusing to auto-retry"
            )
        self.begin(key, payload)
        try:
            result = effect(payload)
        except Exception as exc:
            self.fail(key, f"{type(exc).__name__}: {exc}")
            raise
        self.commit(key, result)
        return copy.deepcopy(result)

    # -- recovery ---------------------------------------------------------

    def unknown_outcomes(self) -> list[dict]:
        """Keys left PENDING by a previous process — outcomes unknown.

        These need an owner decision (verify externally, then commit/fail
        by hand). They are never auto-retried by run().
        """
        return [
            self._view(key)
            for key in sorted(self._unknown_at_open)
            if self._records[key]["status"] == STATUS_PENDING
        ]

    def __contains__(self, key: str) -> bool:
        return key in self._records

    def __len__(self) -> int:
        return len(self._records)
