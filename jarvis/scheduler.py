"""Scheduler — JARVIS v4, Wave 4 (Dodaios's routines, Thoth's
scheduled workflows, Nova's scheduled goals — one mechanism).

Three names, one shape: a named piece of work, a schedule, and
an executor that runs it when it falls due and reports back.

- A ROUTINE is a list of steps run as one delegated work order.
- A GOAL is a standing work order that runs on its schedule
  until the owner marks it done — its runs accumulate in its
  record.
- A WORKFLOW is a routine whose steps were always meant to chain;
  the executor states them as one numbered order, so the working
  brain sequences them itself.

Schedules: {"type": "daily", "at": "08:30"} | {"type":
"interval", "hours": 6} | {"type": "once", "at": <epoch>}.
Discipline (Sentient's): a run missed while the PC was off fires
ONCE when noticed, never in a burst; a "once" item retires after
its run. Execution always rides the app's delegated-work runner
— the scheduler owns no brain of its own.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

MAX_ITEMS = 50


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------

def _load(data_dir, name: str) -> list:
    path = Path(data_dir) / name
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _save(data_dir, name: str, rows: list) -> None:
    try:
        (Path(data_dir) / name).write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def load_routines(data_dir) -> list:
    return _load(data_dir, "routines.json")


def load_goals(data_dir) -> list:
    return _load(data_dir, "scheduled_goals.json")


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

def compute_next(schedule: dict, now: float,
                 last_run: Optional[float] = None) -> Optional[float]:
    """The next epoch a schedule fires after `now`, or None when
    the schedule has run its course. Garbage in the schedule
    (a hand-edited "25:99") means None — never an exception
    (audit 2026-10-22: replace(hour=25) raised ValueError and
    took the whole tick down with it)."""
    if not isinstance(schedule, dict):
        return None
    kind = str(schedule.get("type", "daily"))
    if kind == "interval":
        try:
            hours = max(0.25, float(schedule.get("hours", 24)))
        except (TypeError, ValueError):
            return None
        base = last_run if last_run else now
        nxt = base + hours * 3600
        return nxt if nxt > now else now + 60.0
    if kind == "once":
        at = schedule.get("at")
        try:
            at = float(at)
        except (TypeError, ValueError):
            return None
        if last_run:
            return None
        return at
    if kind == "weekly":
        # {"type": "weekly", "weekday": 0-6 (Mon=0),
        #  "at": "HH:MM"} — added for the Task Parser
        # (Spec 7); daily/once/interval are untouched.
        try:
            weekday = int(schedule.get("weekday", -1))
        except (TypeError, ValueError):
            return None
        if not 0 <= weekday <= 6:
            return None
        at = str(schedule.get("at", "09:00"))
        try:
            hour, minute = (int(x) for x in at.split(":", 1))
        except Exception:
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        today = datetime.fromtimestamp(now).replace(
            hour=hour, minute=minute, second=0,
            microsecond=0)
        delta = (weekday - today.weekday()) % 7
        nxt = (today + timedelta(days=delta)).timestamp()
        if nxt <= now:
            nxt = (today + timedelta(
                days=delta + 7)).timestamp()
        return nxt
    # daily at HH:MM (local time)
    at = str(schedule.get("at", "09:00"))
    try:
        hour, minute = (int(x) for x in at.split(":", 1))
    except Exception:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    today = datetime.fromtimestamp(now).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    nxt = today.timestamp()
    if nxt <= now:
        nxt = (today + timedelta(days=1)).timestamp()
    return nxt


def add_item(data_dir, name: str, filename: str, **fields) -> dict:
    rows = _load(data_dir, filename)
    if len(rows) >= MAX_ITEMS:
        return {"ok": False,
                "error": f"bahut zyada items ho gaye, sir "
                         f"(limit {MAX_ITEMS})"}
    now = time.time()
    row = {"id": uuid.uuid4().hex[:8], "name": name,
           "created": now, "last_run": None, "runs": 0, **fields}
    row["next_run"] = compute_next(row.get("schedule", {}), now)
    rows.append(row)
    _save(data_dir, filename, rows)
    return {"ok": True, "item": row}


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

def tick(data_dir, runner: Callable[[str], str],
         notify: Optional[Callable] = None,
         now: Optional[float] = None,
         journal=None,
         local_runner: Optional[Callable] = None) -> dict:
    """Fire everything due, once each. `runner(work_order)` does
    the work and returns a summary; `notify(title, body)` reaches
    the owner. A row marked `local` runs on `local_runner` when
    one is given (the job scheduler's local-model road: small
    jobs never spend the cloud). Never raises."""
    now = time.time() if now is None else now
    report = {"routines": 0, "goals": 0, "errors": 0}
    for filename, kind in (("routines.json", "routines"),
                           ("scheduled_goals.json", "goals")):
        rows = _load(data_dir, filename)
        changed = False
        for row in rows:
            if not row.get("enabled", True):
                continue
            if kind == "goals" and row.get("status") == "done":
                continue
            nxt = row.get("next_run")
            if nxt is None:
                nxt = compute_next(row.get("schedule", {}), now,
                                   row.get("last_run"))
                row["next_run"] = nxt
                changed = True
            if nxt is None or nxt > now:
                continue
            work = _work_order(row, kind)
            run = runner
            if row.get("local") and local_runner is not None:
                run = local_runner
            try:
                summary = run(work)
            except Exception as exc:
                summary = f"failed: {type(exc).__name__}: {exc}"
                report["errors"] += 1
            row["last_run"] = now
            row["runs"] = int(row.get("runs", 0)) + 1
            row["last_summary"] = str(summary)[:300]
            row["next_run"] = compute_next(
                row.get("schedule", {}), now, last_run=now)
            if row["next_run"] is None:
                if kind == "goals":
                    row["status"] = "done"
                else:
                    row["enabled"] = False
            changed = True
            report[kind] += 1
            # Persist BEFORE notify, against a FRESH load
            # (audit 2026-10-22): the old order saved once at
            # the end and notified first — a raising notify
            # crashed the tick ("never raises" was a lie),
            # the run's bookkeeping never landed, and the
            # routine fired AGAIN next tick. A long runner
            # also made the in-memory rows stale: another
            # writer's updates were overwritten by the final
            # save. Now each fire merges into a fresh copy
            # and saves immediately.
            fresh_rows = _load(data_dir, filename)
            for fresh in fresh_rows:
                if fresh.get("id") == row.get("id"):
                    for key in ("last_run", "runs", "last_summary",
                                "next_run", "status", "enabled"):
                        if key in row:
                            fresh[key] = row[key]
                    break
            _save(data_dir, filename, fresh_rows)
            rows = fresh_rows
            if notify is not None:
                label = "Routine" if kind == "routines" else "Goal"
                try:
                    notify(f"{label}: {row.get('name', '')}",
                           str(summary)[:900])
                except Exception:
                    pass    # told or not, the run is recorded
            if journal is not None:
                try:
                    journal.append(f"scheduler.{kind[:-1]}_ran",
                                   {"id": row.get("id"),
                                    "name": row.get("name")})
                except Exception:
                    pass
        if changed:
            _save(data_dir, filename, rows)
    return report


def fire_row(data_dir, filename: str, row_id: str,
             runner: Callable[[str], str],
             notify: Optional[Callable] = None,
             journal=None,
             local_runner: Optional[Callable] = None
             ) -> Optional[dict]:
    """Fire ONE stored item right now (the manual '/routines run'
    path) with the same bookkeeping a tick applies. Returns the
    updated row, or None when the id is unknown."""
    kind = "goals" if filename == "scheduled_goals.json" \
        else "routines"
    rows = _load(data_dir, filename)
    for row in rows:
        if row.get("id") != row_id:
            continue
        # The manual path respects the same switches the tick
        # does (audit 2026-10-22: fire_row ran DISABLED
        # routines and DONE goals).
        if not row.get("enabled", True):
            return row
        if kind == "goals" and row.get("status") == "done":
            return row
        now = time.time()
        run = runner
        if row.get("local") and local_runner is not None:
            run = local_runner
        try:
            summary = run(_work_order(row, kind))
        except Exception as exc:
            summary = f"failed: {type(exc).__name__}: {exc}"
        row["last_run"] = now
        row["runs"] = int(row.get("runs", 0)) + 1
        row["last_summary"] = str(summary)[:300]
        row["next_run"] = compute_next(row.get("schedule", {}),
                                       now, last_run=now)
        if row["next_run"] is None:
            if kind == "goals":
                row["status"] = "done"
            else:
                row["enabled"] = False
        _save(data_dir, filename, rows)
        if notify is not None:
            label = "Routine" if kind == "routines" else "Goal"
            notify(f"{label}: {row.get('name', '')}",
                   str(summary)[:900])
        if journal is not None:
            try:
                journal.append(f"scheduler.{kind[:-1]}_ran",
                               {"id": row.get("id"),
                                "name": row.get("name"),
                                "manual": True})
            except Exception:
                pass
        return row
    return None


def _work_order(row: dict, kind: str) -> str:
    if kind == "goals":
        return (f"Scheduled goal '{row.get('name', '')}': "
                f"{row.get('prompt', '')}\n"
                "Ye is goal ka is baar ka run hai — jo hissa abhi "
                "ho sakta hai woh pura karo, aur aakhir me saaf "
                "summary do ki kya hua aur kya bacha.")
    steps = row.get("steps") or []
    numbered = "\n".join(f"{i + 1}. {step}"
                         for i, step in enumerate(steps))
    return (f"Routine '{row.get('name', '')}' — ye kadam isi "
            f"tarateeb se karo:\n{numbered}")
