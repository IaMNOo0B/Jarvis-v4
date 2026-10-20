"""Spend-velocity trip — JARVIS v4's Denial-of-Wallet brake (Step 15).

Design (from the ai-eng study, P15-L13 cost governors):
- Of the 12 cost-governor layers, the one that catches a runaway loop
  fastest is the FINANCIAL VELOCITY LIMIT: more than $X inside a rolling
  10-minute window means something is wrong, whatever the cause.
- Tripping LATCHES. A statistical detector can be waited out; a latched
  trip cannot — only an explicit owner reset re-opens the tap, on a
  separate path from the code that spends.
- A daily cap sits beside it for the slow-leak failure mode. The daily
  cap does not latch; a new day re-opens it by itself.

Stdlib only. State optionally persists to a JSON file so a restart does
not quietly forgive a trip.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

DEFAULT_WINDOW_SECONDS = 600.0  # 10 minutes — the study's velocity window


class SpendError(Exception):
    """Raised on invalid spend operations (bad amounts, bad config)."""


class SpendGuard:
    """Rolling-window velocity trip + daily cap over a spend ledger."""

    def __init__(self,
                 velocity_limit: float,
                 window_seconds: float = DEFAULT_WINDOW_SECONDS,
                 daily_cap: Optional[float] = None,
                 state_path: Optional[str] = None,
                 clock: Callable[[], float] = time.time):
        if velocity_limit <= 0:
            raise SpendError("velocity_limit must be > 0")
        if window_seconds <= 0:
            raise SpendError("window_seconds must be > 0")
        if daily_cap is not None and daily_cap <= 0:
            raise SpendError("daily_cap must be > 0 when set")
        self.velocity_limit = float(velocity_limit)
        self.window_seconds = float(window_seconds)
        self.daily_cap = None if daily_cap is None else float(daily_cap)
        self.state_path = state_path
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: list[dict] = []  # {"ts": float, "amount": float}
        self._tripped = False
        self._trip_reason: Optional[str] = None
        self._tripped_at: Optional[float] = None
        if state_path and os.path.exists(state_path):
            self._load()

    # -- persistence -----------------------------------------------------

    def _save(self) -> None:
        if not self.state_path:
            return
        state = {
            "entries": self._entries,
            "tripped": self._tripped,
            "trip_reason": self._trip_reason,
            "tripped_at": self._tripped_at,
        }
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    def _load(self) -> None:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except ValueError as exc:
            raise SpendError(f"spend state unreadable: {exc}") from exc
        self._entries = [{"ts": float(e["ts"]), "amount": float(e["amount"])}
                         for e in state.get("entries", [])]
        self._tripped = bool(state.get("tripped", False))
        self._trip_reason = state.get("trip_reason")
        self._tripped_at = state.get("tripped_at")

    # -- internals --------------------------------------------------------

    def _window_total(self, now: float) -> float:
        cutoff = now - self.window_seconds
        return sum(e["amount"] for e in self._entries if e["ts"] > cutoff)

    def _today_total(self, now: float) -> float:
        today = datetime.fromtimestamp(now, timezone.utc).date()
        total = 0.0
        for e in self._entries:
            if datetime.fromtimestamp(e["ts"], timezone.utc).date() == today:
                total += e["amount"]
        return total

    def _trip(self, reason: str, now: float) -> None:
        self._tripped = True
        self._trip_reason = reason
        self._tripped_at = now

    # -- public API ---------------------------------------------------------

    def allow(self, amount: float) -> bool:
        """Would spending `amount` right now be permitted?

        False when: tripped (latched), the amount would breach the
        velocity limit inside the rolling window, or it would breach the
        daily cap. Pure check — never mutates state, never trips.
        """
        if amount < 0:
            raise SpendError("amount must be >= 0")
        with self._lock:
            if self._tripped:
                return False
            now = self._clock()
            if self._window_total(now) + amount > self.velocity_limit:
                return False
            if (self.daily_cap is not None
                    and self._today_total(now) + amount > self.daily_cap):
                return False
            return True

    def record(self, amount: float) -> bool:
        """Record an actual spend. Returns True if it went through.

        A spend that would breach the velocity limit TRIPS the guard
        (latched) and is refused — the refused amount is NOT added to the
        ledger. A spend refused only by the daily cap does not trip.
        """
        if amount <= 0:
            raise SpendError("amount must be > 0 to record a spend")
        with self._lock:
            if self._tripped:
                return False
            now = self._clock()
            if self._window_total(now) + amount > self.velocity_limit:
                self._trip(
                    f"velocity: >{self.velocity_limit} per "
                    f"{self.window_seconds:.0f}s window", now)
                self._save()
                return False
            if (self.daily_cap is not None
                    and self._today_total(now) + amount > self.daily_cap):
                return False
            self._entries.append({"ts": now, "amount": float(amount)})
            self._save()
            return True

    @property
    def tripped(self) -> bool:
        return self._tripped

    def reset(self, by: str = "owner") -> None:
        """Explicit owner action: clear the latch.

        This is the ONLY way a trip clears — time passing never un-trips.
        Ledger history is kept (the window ages entries out on its own).
        """
        if by != "owner":
            raise SpendError("only an explicit owner action may reset a trip")
        with self._lock:
            self._tripped = False
            self._trip_reason = None
            self._tripped_at = None
            self._save()

    def status(self) -> dict:
        with self._lock:
            now = self._clock()
            return {
                "tripped": self._tripped,
                "trip_reason": self._trip_reason,
                "tripped_at": self._tripped_at,
                "velocity_limit": self.velocity_limit,
                "window_seconds": self.window_seconds,
                "window_total": self._window_total(now),
                "daily_cap": self.daily_cap,
                "today_total": self._today_total(now),
                "entries": len(self._entries),
            }
