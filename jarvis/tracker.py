"""Tracker — JARVIS v4 (the owner's long-campaign ask, 2026-10-22:
"lead generation kare, unka track rakhe, follow-up bhi de").

A plain, general register: named tables of records, each with
an id, a status and a timestamp, stored in data/tracker.json.
Leads, applications, orders, follow-ups — anything a long task
must not lose between sessions. What makes a campaign out of it
is composition, not another subsystem: research tools FIND the
rows (web_search, read_fields, deep_research), this store KEEPS
them, reminders and the scheduler CHASE them on time.

Records are the owner's data on the owner's machine; nothing
here sends anything anywhere.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

MAX_TABLES = 20
MAX_ROWS = 2000


def _path(data_dir) -> Path:
    return Path(data_dir) / "tracker.json"


def _load(data_dir) -> dict:
    path = _path(data_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _save(data_dir, data: dict) -> None:
    try:
        _path(data_dir).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def add(data_dir, table: str, record: dict) -> dict:
    data = _load(data_dir)
    rows = data.setdefault(str(table), [])
    if len(data) > MAX_TABLES and str(table) not in data:
        return {"ok": False, "error": "tables ki limit ho gayi, "
                                      "sir"}
    if len(rows) >= MAX_ROWS:
        return {"ok": False,
                "error": f"'{table}' table bhar gayi, sir"}
    row = {"id": uuid.uuid4().hex[:8], "ts": time.time(),
           "status": "open"}
    if isinstance(record, dict):
        row.update({str(k): v for k, v in record.items()
                    if k not in ("id", "ts", "status")})
    rows.append(row)
    _save(data_dir, data)
    return {"ok": True, "row": row}


def list_rows(data_dir, table: str, status: str = "",
              limit: int = 20) -> list:
    rows = _load(data_dir).get(str(table), [])
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return rows[-max(1, int(limit)):]


def set_status(data_dir, table: str, row_id: str,
               status: str) -> Optional[dict]:
    data = _load(data_dir)
    for row in data.get(str(table), []):
        if row.get("id") == row_id:
            row["status"] = str(status)
            row["updated"] = time.time()
            _save(data_dir, data)
            return row
    return None


def tables(data_dir) -> dict:
    return {name: len(rows)
            for name, rows in _load(data_dir).items()}


def due_followups(data_dir, now_ts: float = None) -> list:
    """Rows whose follow-up date has arrived and whose status
    is still open — the chase list the Daily Brief and the
    followups_due tool read. A row marks its date in
    `follow_up_due` (epoch seconds or an ISO date); a status of
    done / replied / closed takes it off the list."""
    import time as _time
    from datetime import datetime as _dt
    now_ts = _time.time() if now_ts is None else now_ts
    out = []
    try:
        book = _load(data_dir)
    except Exception:
        return out
    closed = {"done", "replied", "closed", "complete", "paid"}
    tables = book.get("tables") if isinstance(
        book.get("tables"), dict) else book
    for table, rows in (tables or {}).items():
        if not isinstance(rows, list):
            continue
        for row in rows or []:
            if str(row.get("status", "open")).lower() in closed:
                continue
            raw = row.get("follow_up_due") or \
                row.get("next_follow_up") or row.get("fu_due")
            if raw is None:
                continue
            try:
                due = float(raw)
            except (TypeError, ValueError):
                try:
                    due = _dt.fromisoformat(
                        str(raw)).timestamp()
                except Exception:
                    continue
            if due <= now_ts:
                label = (row.get("name") or row.get("lead")
                         or row.get("title") or row.get("id")
                         or "?")
                out.append({"table": table, "id": row.get("id"),
                            "label": str(label),
                            "status": row.get("status", "open")})
    return out
