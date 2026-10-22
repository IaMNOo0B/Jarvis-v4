"""Task Parser (Sentient Spec 7): one plain sentence ->
one validated structured job. The schedule must survive
the job system's REAL vocabulary (compute_next for time
kinds — including the new weekly kind — and real
trigger events for event kinds), the run-time vs
event-time distinction is the charter, and the created
job lands in the same stores the brain's own tools use."""

import json
import time
from types import SimpleNamespace

from jarvis import scheduler, taskparse, triggers

NOW = time.mktime((2026, 10, 22, 10, 0, 0, 0, 0, -1))  # Thu


class _Brain:
    def __init__(self, reply):
        self._reply = reply
        self.prompts = []

    def respond(self, history, text):
        self.prompts.append(text)
        return SimpleNamespace(reply=self._reply)


def _factory(reply):
    brain = _Brain(reply)
    return (lambda: brain), brain


def test_parse_weekly_job_and_create(tmp_path):
    reply = json.dumps({
        "name": "invoice friday",
        "description": "Friday shaam ko invoice bhej do",
        "priority": "high",
        "schedule": {"type": "weekly", "weekday": 4,
                     "at": "18:00"}})
    factory, brain = _factory(reply)
    parsed, problems = taskparse.parse_task(
        factory, "har Friday shaam ko invoice bhejna hai",
        tz_name="IST", now=NOW)
    assert problems == [] and parsed is not None
    assert parsed["priority"] == "high"
    # The charter line is in the prompt.
    assert "alag hain" in brain.prompts[0]
    ok, detail = taskparse.create_from_parsed(tmp_path,
                                              parsed)
    assert ok
    rows = scheduler._load(tmp_path, "routines.json")
    assert len(rows) == 1
    assert rows[0]["steps"] == [
        "Friday shaam ko invoice bhej do"]
    assert rows[0]["next_run"] > NOW


def test_parse_once_in_the_past_is_rejected(tmp_path):
    reply = json.dumps({
        "name": "x", "description": "y", "priority": "low",
        "schedule": {"type": "once", "at": NOW - 3600}})
    factory, _b = _factory(reply)
    parsed, problems = taskparse.parse_task(
        factory, "ek ghanta pehle yaad dilana tha",
        tz_name="IST", now=NOW)
    assert parsed is None and problems


def test_parse_garbage_schedule_is_rejected():
    reply = json.dumps({
        "name": "x", "description": "y",
        "priority": "normal",
        "schedule": {"type": "weekly", "weekday": 9,
                     "at": "25:99"}})
    factory, _b = _factory(reply)
    parsed, problems = taskparse.parse_task(
        factory, "kabhi", tz_name="IST", now=NOW)
    assert parsed is None and problems


def test_parse_event_job_creates_a_trigger(tmp_path):
    reply = json.dumps({
        "name": "naya lead",
        "description": "tracker me naya lead aaye to "
                       "batao",
        "priority": "normal",
        "schedule": {"type": "event",
                     "event": "tracker_row",
                     "match": {"table": "leads"}}})
    factory, _b = _factory(reply)
    parsed, problems = taskparse.parse_task(
        factory, "naya lead aaye to batao", tz_name="IST",
        now=NOW)
    assert parsed is not None
    ok, _detail = taskparse.create_from_parsed(tmp_path,
                                               parsed)
    assert ok
    book = triggers.TriggerBook(tmp_path).list()
    assert len(book) == 1
    assert book[0]["event"] == "tracker_row"


def test_weekly_compute_next():
    # Thursday 10:00 now; Friday (4) 18:00 -> tomorrow.
    nxt = scheduler.compute_next(
        {"type": "weekly", "weekday": 4, "at": "18:00"},
        NOW)
    assert nxt is not None
    friday = time.mktime((2026, 10, 23, 18, 0, 0, 0, 0, -1))
    assert abs(nxt - friday) < 2
    # Same weekday, time already passed -> next week.
    nxt = scheduler.compute_next(
        {"type": "weekly", "weekday": 3, "at": "09:00"},
        NOW)
    next_thu = time.mktime((2026, 10, 29, 9, 0, 0, 0, 0,
                            -1))
    assert abs(nxt - next_thu) < 2
    # Garbage never raises, just None.
    assert scheduler.compute_next(
        {"type": "weekly", "weekday": 9, "at": "18:00"},
        NOW) is None
    assert scheduler.compute_next(
        {"type": "weekly", "weekday": 2, "at": "99:99"},
        NOW) is None


def test_task_command_end_to_end(tmp_path):
    from jarvis.chat import ChatApp

    reply = json.dumps({
        "name": "paani yaad",
        "description": "Paani peene ki yaad dilao",
        "priority": "normal",
        "schedule": {"type": "daily", "at": "07:30"}})

    class _Chain:
        def complete(self, messages, tools=None):
            return {"content": reply}

    app = ChatApp(tmp_path, chain=_Chain())
    out = app.handle_command(
        "/task roz subah 7:30 paani ki yaad dilao")
    assert "Samajh gaya" in out
    assert "roz 07:30" in out
    rows = scheduler._load(tmp_path, "routines.json")
    assert len(rows) == 1
