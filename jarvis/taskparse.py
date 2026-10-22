"""Task Parser — JARVIS v4 (Sentient install, Spec 7).

One sentence in plain words — "har Friday shaam ko
invoice bhej diya karo" — becomes one STRUCTURED
reminder/trigger for the job system: name, description,
priority, and a schedule or event spec, validated in
code before anything is created.

The parser is one tool-free model call, but the trust
boundary is deterministic: the returned schedule is
checked against the job system's REAL vocabulary —
compute_next must be able to compute it, event kinds
must be real trigger events — and garbage is rejected
honestly instead of being half-saved. The one charter
line that shapes the prompt: the time a job should RUN
is not the time of the event it is ABOUT ("kal ki flight
se PEHLE yaad dilana" — the run time is before, the
flight is what it is about).

The brain's inline set_routine remains the fallback
route (version coexistence): /task is the precise front
door, not a replacement.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime

from . import scheduler, triggers

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]


def _json_from_reply(text: str):
    cleaned = str(text or "").strip()
    if "```" in cleaned:
        cleaned = cleaned.split("```")[1].strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except Exception:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(cleaned[start:end + 1])
            except Exception:
                return None
    return None


def validate_schedule(schedule, now: float) -> list:
    """Problems with a parsed schedule, in plain words —
    empty list means the job system can really run it."""
    if not isinstance(schedule, dict):
        return ["schedule samajh nahi aaya"]
    kind = str(schedule.get("type", ""))
    if kind in ("once", "daily", "weekly", "interval"):
        if scheduler.compute_next(schedule, now) is None:
            return [f"{kind} schedule ki values theek "
                    "nahi hain"]
        if kind == "once":
            at = schedule.get("at")
            try:
                if float(at) <= now:
                    return ["once ka waqt peeche ka hai"]
            except (TypeError, ValueError):
                return ["once ka waqt samajh nahi aaya"]
        return []
    if kind == "event":
        event = str(schedule.get("event", ""))
        if event not in triggers.EVENTS:
            return [f"event '{event}' asal trigger "
                    "nahi hai"]
        if not isinstance(schedule.get("match"), dict) \
                or not schedule["match"]:
            return ["event ke liye match zaroori hai"]
        return []
    return [f"schedule type '{kind}' nahi chalta"]


def schedule_words(schedule) -> str:
    """The schedule in one plain line — the owner sees
    exactly what was understood before anything runs."""
    if not isinstance(schedule, dict):
        return "?"
    kind = str(schedule.get("type", ""))
    if kind == "daily":
        return f"roz {schedule.get('at', '')} baje"
    if kind == "weekly":
        try:
            day = _WEEKDAYS[int(schedule.get("weekday", 0))]
        except Exception:
            day = "?"
        return f"har {day} {schedule.get('at', '')} baje"
    if kind == "interval":
        return f"har {schedule.get('hours', '?')} ghante me"
    if kind == "once":
        try:
            when = datetime.fromtimestamp(
                float(schedule.get("at"))).strftime(
                "%d %b %H:%M")
        except Exception:
            when = "?"
        return f"ek baar — {when}"
    if kind == "event":
        return f"jab {schedule.get('event', '')} ho"
    return kind or "?"


def parse_task(brain_factory, text: str, *, tz_name: str,
               now: float):
    """(parsed, problems). parsed = {name, description,
    priority, schedule} when the model's answer validates;
    problems names exactly what is wrong otherwise."""
    now_iso = datetime.fromtimestamp(now).strftime(
        "%Y-%m-%d %H:%M (%A)")
    prompt = (
        "Tum Task Parser ho. Owner ka ek jumla ek NAUKRI "
        "(job) hai — use structured banao.\n"
        f"Abhi ka waqt: {now_iso} ({tz_name}).\n"
        "SABSE ZAROORI: job ke CHALNE ka waqt aur us GHATNA "
        "ka waqt alag hain jiske BAARE me job hai. 'Kal ki "
        "flight se pehle yaad dilana' — chalega AAJ/"
        "pehle, flight kal ki hai; schedule me CHALNE ka "
        "waqt aayega.\n"
        "Schedule ki shaklein (sirf yahi):\n"
        '- {"type": "once", "at": <epoch seconds — chalne '
        'ka waqt>}\n'
        '- {"type": "daily", "at": "HH:MM"}\n'
        '- {"type": "weekly", "weekday": 0-6 (Mon=0), '
        '"at": "HH:MM"}\n'
        '- {"type": "interval", "hours": <number>}\n'
        '- {"type": "event", "event": "file_appears" | '
        '"tracker_row", "match": {...}}\n'
        f"Jumla: {str(text)[:600]}\n"
        'Reply ONLY with JSON: {"name": "chhota naam", '
        '"description": "kaam ki tafseel — apne shabdon '
        'me, poori", "priority": "low"|"normal"|"high", '
        '"schedule": {...}}')
    holder: dict = {}

    def _go():
        try:
            brain = brain_factory()
            holder["text"] = str(
                brain.respond([], prompt).reply)
        except Exception as exc:
            holder["error"] = exc

    worker = threading.Thread(target=_go, daemon=True)
    worker.start()
    worker.join(20.0)
    if worker.is_alive() or "text" not in holder:
        return None, ["parser brain jawab nahi de paya"]
    parsed = _json_from_reply(holder["text"])
    if not isinstance(parsed, dict):
        return None, ["parser ka jawab samajh nahi aaya"]
    name = str(parsed.get("name", "")).strip()[:80]
    description = str(
        parsed.get("description", "")).strip()[:600]
    if not name or not description:
        return None, ["naam ya tafseel khaali hai"]
    priority = str(parsed.get("priority", "normal")).lower()
    if priority not in ("low", "normal", "high"):
        priority = "normal"
    schedule = parsed.get("schedule")
    problems = validate_schedule(schedule, now)
    if problems:
        return None, problems
    return {"name": name, "description": description,
            "priority": priority,
            "schedule": schedule}, []


def create_from_parsed(data_dir, parsed: dict,
                       journal=None):
    """Actually create the job from a validated parse:
    a scheduler routine for time kinds, a TriggerBook row
    for event kinds. Returns (ok, detail)."""
    schedule = parsed["schedule"]
    kind = str(schedule.get("type", ""))
    if kind == "event":
        book = triggers.TriggerBook(data_dir)
        row = book.add(parsed["name"], schedule["event"],
                       schedule["match"],
                       action=parsed["description"])
        if journal is not None:
            try:
                journal.append("taskparse.created", {
                    "name": parsed["name"], "kind": "event"})
            except Exception:
                pass
        return True, f"trigger #{row['id']}"
    out = scheduler.add_item(
        data_dir, parsed["name"], "routines.json",
        schedule=schedule, steps=[parsed["description"]],
        priority=parsed["priority"], enabled=True,
        local=False)
    if not out.get("ok"):
        return False, str(out.get("error", ""))
    if journal is not None:
        try:
            journal.append("taskparse.created", {
                "name": parsed["name"], "kind": kind})
        except Exception:
            pass
    return True, f"routine #{out['item']['id']}"
