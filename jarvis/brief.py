"""Daily Brief — JARVIS v4, Wave 2.

The pattern, from two sources that converged: Google's Gemini
Daily Brief and Sentient's brief.py. A short digest with a FIXED
triage structure — Top of mind / FYI / Looking ahead — assembled
deterministically from data the app already holds (reminders,
notifications, remembered facts). No model call: the brief is
assembled, not generated, so it can never invent an item. It is
spoken-friendly plain text, because its natural surface is the
owner asking "what's my day look like" — in chat or by voice.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional


def _load_reminders(data_dir) -> list:
    path = Path(data_dir) / "reminders.json"
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _load_notifications(data_dir, limit: int = 100) -> list:
    path = Path(data_dir) / "notifications.jsonl"
    rows = []
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        except Exception:
            return []
    return rows[-limit:]


def _fmt_due(due: datetime, now: datetime) -> str:
    local = due.astimezone()
    if due.date() == now.date():
        return f"today at {local.strftime('%I:%M %p')}"
    if due.date() == (now + timedelta(days=1)).date():
        return f"tomorrow at {local.strftime('%I:%M %p')}"
    return local.strftime("%d %b, %I:%M %p")


def build_brief(data_dir, facts=(), now: Optional[datetime] = None) -> dict:
    """Assemble the brief. `facts` is an iterable of fact dicts
    (Memory's current_facts shape: claim + ts). Returns the three
    sections as lists plus a ready-to-say `text`."""
    now = now or datetime.now(timezone.utc)
    top: List[str] = []
    ahead: List[str] = []
    for row in _load_reminders(data_dir):
        if row.get("done"):
            continue
        try:
            due = datetime.fromisoformat(str(row["due"]))
        except Exception:
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        text = str(row.get("text", "")).strip()
        if not text:
            continue
        if due <= now:
            top.append(f"Reminder due: {text}")
        elif due <= now + timedelta(hours=24):
            ahead.append(f"{text} — {_fmt_due(due, now)}")
    notes = _load_notifications(data_dir)
    for row in notes:
        if not row.get("read"):
            title = str(row.get("title", "")).strip()
            body = str(row.get("body", "")).strip()
            item = f"{title}: {body}" if body else title
            if item:
                top.append(item)
    fyi: List[str] = []
    horizon = now.timestamp() - 24 * 3600
    for row in notes:
        if row.get("read") and float(row.get("ts", 0)) >= horizon:
            title = str(row.get("title", "")).strip()
            if title:
                fyi.append(title)
    recent = sorted((f for f in facts if f.get("ts")),
                    key=lambda f: str(f["ts"]), reverse=True)[:3]
    for fact in recent:
        claim = str(fact.get("claim", "")).strip()
        if claim:
            fyi.append(f"Remembered: {claim}")
    # The chase list and the waiting proposals — small numbers
    # the owner acts on, surfaced where he already looks.
    try:
        from .tracker import due_followups
        due = due_followups(data_dir)
        if due:
            names = ", ".join(d["label"] for d in due[:3])
            fyi.append(f"{len(due)} follow-up(s) due: {names}")
    except Exception:
        pass
    try:
        from .candidates import pending as _pending_rules
        waiting = [r for r in _pending_rules(data_dir)
                   if r.get("kind") != "skill"]
        if waiting:
            fyi.append(f"{len(waiting)} rule proposal(s) waiting "
                       "— /rules candidates")
    except Exception:
        pass
    try:
        from .followups import FollowupStore
        drafts = FollowupStore(data_dir).drafts()
        if drafts:
            fyi.append(f"{len(drafts)} follow-up draft(s) "
                       "ready — /followups")
    except Exception:
        pass
    try:
        from .candidates import pending as _pending_all
        skills_waiting = [r for r in _pending_all(data_dir)
                          if r.get("kind") == "skill"]
        if skills_waiting:
            fyi.append(f"{len(skills_waiting)} skill "
                       "proposal(s) waiting — /skills")
    except Exception:
        pass

    lines = ["Here's your brief, sir."]
    for name, items in (("Top of mind", top[:5]),
                        ("FYI", fyi[:5]),
                        ("Looking ahead", ahead[:5])):
        lines.append(f"{name}:")
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append("- Nothing there.")
    return {"top_of_mind": top[:5], "fyi": fyi[:5],
            "looking_ahead": ahead[:5], "text": "\n".join(lines)}
