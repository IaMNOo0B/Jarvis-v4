"""Accounts — JARVIS v4: the owner's many accounts, one book.

The owner's order (2026-10-22): "50 accounts bhi diya to handle
kar sakta — email ho, Instagram ho, Threads ho; messages bhej
sakta, pura kaam large scale pe." One account is a toy; the
campaign runs on MANY. This book is the discipline that makes
many accounts safe to use:

* Every account (any channel: email / instagram / threads /
  facebook) is registered ONCE, here — data/accounts.json.
  Secrets never live in this file: each row names a secret in
  the vault (`account-<id>`), nothing more.
* Rotation: sending picks the least-used eligible account, so
  the load spreads instead of hammering one identity.
* Per-account daily caps: an account that reaches its cap
  RESTS until tomorrow — the 429 lesson from the owner's own
  campaign, built into the book itself: no sender here can
  push a rested or capped account.
* A hard rest (provider warning / rate limit) benches an
  account for hours with the reason written down.

The book keeps the hisaab; the channel tools do the sending.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

CHANNELS = ("email", "instagram", "threads", "facebook")
DEFAULT_CAPS = {"email": 200, "instagram": 80,
                "threads": 20, "facebook": 30}


class AccountBook:
    def __init__(self, data_dir):
        self._path = Path(data_dir) / "accounts.json"
        self._lock = threading.Lock()

    # -- storage ------------------------------------------------------
    def _load(self) -> dict:
        try:
            data = json.loads(self._path.read_text(
                encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save(self, book: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(book, ensure_ascii=False, indent=1),
            encoding="utf-8")

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def _fresh(self, row: dict) -> dict:
        """Day rollover: yesterday's count never haunts today."""
        if row.get("sent_day") != self._today():
            row["sent_day"] = self._today()
            row["sent_count"] = 0
        return row

    # -- CRUD -----------------------------------------------------------
    def add(self, channel: str, label: str, address: str,
            cap: int = 0) -> dict:
        channel = str(channel).strip().lower()
        if channel not in CHANNELS:
            raise ValueError(f"channel galat hai: {channel}")
        row = {"id": uuid.uuid4().hex[:8],
               "channel": channel,
               "label": str(label)[:60] or str(address)[:60],
               "address": str(address)[:120],
               "cap": int(cap) if int(cap or 0) > 0
                      else DEFAULT_CAPS[channel],
               "enabled": True, "sent_day": self._today(),
               "sent_count": 0, "rest_until": 0.0,
               "rest_reason": "", "added_ts": time.time()}
        with self._lock:
            book = self._load()
            book[row["id"]] = row
            self._save(book)
        return dict(row)

    def remove(self, account_id: str) -> bool:
        with self._lock:
            book = self._load()
            gone = book.pop(str(account_id), None)
            if gone is not None:
                self._save(book)
            return gone is not None

    def set_enabled(self, account_id: str, enabled: bool) -> bool:
        with self._lock:
            book = self._load()
            row = book.get(str(account_id))
            if row is None:
                return False
            row["enabled"] = bool(enabled)
            self._save(book)
            return True

    def list(self, channel: str = "") -> list:
        book = self._load()
        rows = [self._fresh(dict(r)) for r in book.values()
                if not channel or r.get("channel") == channel]
        rows.sort(key=lambda r: (r["channel"], r["added_ts"]))
        return rows

    def get(self, account_id: str):
        row = self._load().get(str(account_id))
        return self._fresh(dict(row)) if row else None

    # -- rotation + caps --------------------------------------------------
    def eligible(self, row: dict, now: float = None) -> bool:
        now = time.time() if now is None else now
        return (bool(row.get("enabled"))
                and float(row.get("rest_until", 0)) <= now
                and int(row.get("sent_count", 0))
                < int(row.get("cap", 0)))

    def pick(self, channel: str):
        """The least-used eligible account, or None — the caller
        must tell the owner honestly when the whole stable is
        capped or resting."""
        candidates = [r for r in self.list(channel)
                      if self.eligible(r)]
        if not candidates:
            return None
        candidates.sort(key=lambda r: (int(r["sent_count"]),
                                       r["added_ts"]))
        return candidates[0]

    def record_send(self, account_id: str) -> dict:
        with self._lock:
            book = self._load()
            row = book.get(str(account_id))
            if row is None:
                return {}
            self._fresh(row)
            row["sent_count"] = int(row.get("sent_count", 0)) + 1
            if row["sent_count"] >= int(row.get("cap", 0)):
                tomorrow = time.localtime(time.time() + 86400)
                row["rest_until"] = time.mktime(time.struct_time(
                    (tomorrow.tm_year, tomorrow.tm_mon,
                     tomorrow.tm_mday, 0, 5, 0, 0, 0, -1)))
                row["rest_reason"] = "roz ki had puri"
            self._save(book)
            return dict(row)

    def rest(self, account_id: str, seconds: float,
             reason: str = "") -> bool:
        with self._lock:
            book = self._load()
            row = book.get(str(account_id))
            if row is None:
                return False
            row["rest_until"] = time.time() + max(0.0, seconds)
            row["rest_reason"] = str(reason)[:120]
            self._save(book)
            return True

    def overview(self) -> list:
        """Secret-free rows for the Settings panel."""
        out = []
        for row in self.list():
            out.append({
                "id": row["id"], "channel": row["channel"],
                "label": row["label"], "address": row["address"],
                "cap": row["cap"],
                "sent_today": int(row.get("sent_count", 0)),
                "enabled": bool(row.get("enabled")),
                "eligible": self.eligible(row),
                "rest_reason": row.get("rest_reason", "")})
        return out
