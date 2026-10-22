"""Triggers — JARVIS v4: kaam jo ghatna par khud chalta hai.

Sentient's triggered-task shape, this house's discipline:
the owner defines a trigger ONCE — an event, a filter, and
the work — and creating it IS the approval of its plan. From
then on JARVIS watches quietly and, when the event truly
happens, runs the work through the same delegated worker and
gate as everything else, and tells the owner.

Two event sources, both honestly pollable in code:
* file_appears — a file whose name contains `contains`
  appears (or changes) in `folder`.
* tracker_row — a row in tracker table `table` reaches
  `status`.

Idempotency is the load-bearing rule: every firing is keyed
(file: name+size+mtime; row: id+status). The same key never
fires twice — a trigger that re-ran its work every minute
would be a bug with a schedule. A trigger whose worker fails
is noted, not retried in a loop.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

EVENTS = ("file_appears", "tracker_row")
MAX_TRIGGERS = 30


class TriggerBook:
    def __init__(self, data_dir):
        self._path = Path(data_dir) / "triggers.json"
        self._lock = threading.Lock()

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

    def add(self, name: str, event: str, match: dict,
            action: str) -> dict:
        event = str(event).strip()
        if event not in EVENTS:
            raise ValueError(f"event galat hai: {event} "
                             f"(hain: {', '.join(EVENTS)})")
        if not str(action).strip():
            raise ValueError("kaam (action) khaali hai")
        with self._lock:
            book = self._load()
            if len(book) >= MAX_TRIGGERS:
                raise ValueError("triggers ki had puri hai")
            row = {"id": uuid.uuid4().hex[:8],
                   "name": str(name)[:60] or event,
                   "event": event,
                   "match": dict(match or {}),
                   "action": str(action)[:600],
                   "state": "armed",
                   "fired": {},          # key -> ts
                   "created_ts": time.time()}
            book[row["id"]] = row
            self._save(book)
            return dict(row)

    def list(self) -> list:
        rows = [dict(r) for r in self._load().values()]
        rows.sort(key=lambda r: r.get("created_ts", 0))
        return rows

    def set_state(self, trigger_id: str, state: str) -> bool:
        with self._lock:
            book = self._load()
            row = book.get(str(trigger_id))
            if row is None:
                return False
            row["state"] = str(state)
            self._save(book)
            return True

    def mark_fired(self, trigger_id: str, key: str) -> None:
        with self._lock:
            book = self._load()
            row = book.get(str(trigger_id))
            if row is not None:
                row.setdefault("fired", {})[key] = time.time()
                self._save(book)


def _current_keys(data_dir, row: dict) -> list:
    """[(key, context_line)] — what the event looks like RIGHT
    NOW for this trigger. Empty when nothing matches."""
    match = row.get("match") or {}
    out = []
    if row["event"] == "file_appears":
        folder = Path(str(match.get("folder", ""))).expanduser()
        contains = str(match.get("contains", "")).lower()
        if not folder.is_dir():
            return out
        for path in sorted(folder.iterdir()):
            if not path.is_file():
                continue
            if contains and contains not in path.name.lower():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            key = f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}"
            out.append((key, f"File: {path}"))
    elif row["event"] == "tracker_row":
        from .tracker import list_rows
        table = str(match.get("table", ""))
        status = str(match.get("status", "")).lower()
        if not table:
            return out
        for item in list_rows(data_dir, table, limit=200):
            if status and str(item.get("status", "")).lower() \
                    != status:
                continue
            key = f"{item.get('id')}:{item.get('status')}"
            label = (item.get("name") or item.get("lead")
                     or item.get("id"))
            out.append((key, f"Tracker {table}: {label} "
                             f"({item.get('status')})"))
    return out


def tick(data_dir, runner, notify=None, journal=None) -> int:
    """One watch pass over every armed trigger. Fires each
    NEW key once, through `runner(work_text) -> summary`.
    Returns how many firings happened. Never raises."""
    book = TriggerBook(data_dir)
    fired_count = 0
    for row in book.list():
        if row.get("state") != "armed":
            continue
        try:
            keys = _current_keys(data_dir, row)
        except Exception:
            continue
        already = row.get("fired") or {}
        for key, context in keys:
            if key in already:
                continue                      # idempotent: once
            work = (f"Trigger '{row['name']}' ki ghatna ghati "
                    f"hai.\n{context}\nKaam: {row['action']}")
            try:
                summary = runner(work)
            except Exception as exc:
                summary = f"fail: {type(exc).__name__}"
            book.mark_fired(row["id"], key)
            fired_count += 1
            if journal is not None:
                try:
                    journal.append("trigger.fired", {
                        "id": row["id"], "key": key[:80],
                        "summary": str(summary)[:200]})
                except Exception:
                    pass
            if notify is not None:
                notify(f"Trigger: {row['name']}",
                       f"{context}\n{str(summary)[:300]}")
    return fired_count
