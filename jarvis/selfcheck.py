"""Self-grounding loop: JARVIS asks itself four questions, on a loop.

Who am I? Where am I? Where did I come from? What must I do?

This is a PROGRAM self-check, never a claim of self-awareness:
every answer comes from a fact — the configured persona, the data
dir, the build stamp, the open builds — never from a feeling.
The loop runs on the scheduler tick (every minute) but records
only when an answer CHANGED, so asking again and again never
becomes spam: it asks every minute and speaks only when something
is different. Doubt stays honest: a question with no factual
answer records "pata nahi" plus its name in `unknown`, never an
invented answer.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .version import BUILD_STAMP

QUESTIONS = ("who", "where", "origin", "task")
_PENDING_STATES = ("awaiting_proceed", "planned", "running", "queued")


def _persona_name(app) -> str:
    brain = getattr(app, "brain", None)
    persona = getattr(brain, "persona", None) if brain else None
    name = getattr(persona, "name", None) if persona else None
    return name or "JARVIS"


def _pending_build(app):
    try:
        builds = app.list_builds()
    except Exception:
        return None, False
    if not isinstance(builds, list):
        return None, False
    for entry in builds:
        if isinstance(entry, dict) and entry.get("state") in \
                _PENDING_STATES:
            return entry.get("id", "?"), True
    return None, True


def gather(app) -> dict:
    """Answer the four questions from facts. Never raises."""
    unknown = []
    who = "%s, the owner's program assistant" % _persona_name(app)
    where = "%s on %s" % (getattr(app, "data_dir", "?"),
                           sys.platform)
    origin = "Jarvis-v4 build %s" % BUILD_STAMP
    pending, builds_ok = _pending_build(app)
    if pending:
        task = "build %s ka faisla/kaam pending hai" % pending
    elif builds_ok:
        task = "koi pending build nahi"
    else:
        task = "pata nahi"
        unknown.append("task")
    return {"who": who, "where": where, "origin": origin,
            "task": task, "unknown": unknown}


def changed(prev, record) -> bool:
    if not isinstance(prev, dict):
        return True
    return any(prev.get(q) != record.get(q) for q in QUESTIONS)


def _state_path(data_dir) -> Path:
    return Path(data_dir) / "selfcheck.json"


def _load_prev(data_dir):
    try:
        data = json.loads(_state_path(data_dir).read_text(
            encoding="utf-8"))
        return data.get("last") if isinstance(data, dict) else None
    except Exception:
        return None


def _save(data_dir, record) -> None:
    path = _state_path(data_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last": record}, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def maybe_self_check(app):
    """One loop turn: ask, compare, record only on change.
    Returns the record when it was recorded, else None. Never
    raises — a self-check must never break the scheduler tick."""
    try:
        record = gather(app)
        data_dir = getattr(app, "data_dir", None)
        if data_dir is None:
            return None
        if not changed(_load_prev(data_dir), record):
            return None
        _save(data_dir, record)
        journal = getattr(app, "journal", None)
        if journal is not None:
            try:
                journal.append("self.grounding", record)
            except Exception:
                pass
        return record
    except Exception:
        return None
