"""Confirmation cards — the Kodee pattern over v4's gate.

The gate has always been able to say require_approval and mint
an HMAC token for one exact (tool, args); what was missing is
the door the OWNER walks through. A card is that door: when a
destructive-class tool is proposed, dispatch files a card
(tool + args + a plain-language summary) instead of running
anything. The owner approves or denies it — in the app UI, in
chat with /confirm, or from Telegram. Approval re-evaluates
the gate for a FRESH token (cards never store one) and runs
the tool exactly once through the normal guarded dispatch.

Cards live in data/confirmations.json, expire after an hour,
and are one-shot: a used or denied card can never run. The
pre-card behaviour (a plain needs_approval result) remains as
the fallback whenever no card book is wired.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

CARD_TTL_SECONDS = 3600
MAX_CARDS = 100

# One lock per cards file, shared across instances (audit
# 2026-10-22: every caller built a fresh book with its OWN
# lock — the UI thread and the chat thread could take() the
# same card in the same moment and run a destructive tool
# twice).
_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path) -> threading.Lock:
    key = str(Path(path).resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def card_summary(tool: str, args: Dict[str, Any]) -> str:
    """One plain line: what will happen, concretely."""
    bits = []
    for key, value in (args or {}).items():
        if isinstance(value, (str, int, float)) and value != "":
            bits.append(f"{key}={str(value)[:60]}")
        if len(bits) >= 3:
            break
    detail = ", ".join(bits) if bits else "bina tafseel ke"
    return f"{tool} chalega — {detail}"


class ConfirmationBook:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "confirmations.json"
        self._lock = _lock_for(self.path)

    def _rows(self) -> List[Dict]:
        try:
            rows = json.loads(
                self.path.read_text(encoding="utf-8"))
            return rows if isinstance(rows, list) else []
        except Exception:
            return []

    def _save(self, rows: List[Dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(rows[-MAX_CARDS:], ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass

    def add(self, tool: str, args: Dict[str, Any]) -> Dict:
        with self._lock:
            rows = self._rows()
            # An identical pending card (same tool, same
            # args) is the SAME request — return it instead
            # of stacking duplicates the owner must deny
            # one by one.
            now = time.time()
            for existing in rows:
                if existing.get("status") == "pending" \
                        and existing.get("tool") == str(tool) \
                        and existing.get("args") \
                        == dict(args or {}) \
                        and now - float(existing.get("ts", 0)) \
                        < CARD_TTL_SECONDS:
                    return existing
            card = {"id": uuid.uuid4().hex[:8], "ts": now,
                    "tool": str(tool),
                    "args": dict(args or {}),
                    "summary": card_summary(tool, args or {}),
                    "status": "pending"}
            rows.append(card)
            self._save(rows)
            return card

    def peek(self, card_id: str) -> Optional[Dict]:
        """The pending card WITHOUT consuming it — the gate
        is re-evaluated against a peeked card first, and only
        a card that will actually run gets taken."""
        now = time.time()
        for card in self._rows():
            if card.get("id") == card_id \
                    and card.get("status") == "pending" \
                    and now - float(card.get("ts", 0)) \
                    < CARD_TTL_SECONDS:
                return card
        return None

    def pending(self) -> List[Dict]:
        now = time.time()
        return [c for c in self._rows()
                if c.get("status") == "pending"
                and now - float(c.get("ts", 0)) < CARD_TTL_SECONDS]

    def take(self, card_id: str) -> Optional[Dict]:
        """Claim a card for execution: flips it to 'used' so it
        can never run twice. None when missing, stale, or
        already decided."""
        with self._lock:
            rows = self._rows()
            for card in rows:
                if card.get("id") != card_id:
                    continue
                if card.get("status") != "pending":
                    return None
                if time.time() - float(card.get("ts", 0)) \
                        >= CARD_TTL_SECONDS:
                    card["status"] = "expired"
                    self._save(rows)
                    return None
                card["status"] = "used"
                self._save(rows)
                return card
            return None

    def deny(self, card_id: str) -> bool:
        with self._lock:
            rows = self._rows()
            for card in rows:
                if card.get("id") == card_id \
                        and card.get("status") == "pending":
                    card["status"] = "denied"
                    self._save(rows)
                    return True
            return False
