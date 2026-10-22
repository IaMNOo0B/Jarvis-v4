"""Batch-4 audit regressions: tracker / accounts / scheduler
(2026-10-22). Each test names the defect it pins."""

import json
import threading
import time

import pytest

from jarvis import scheduler, tracker
from jarvis.accounts import AccountBook


# -- tracker -----------------------------------------------------------

def test_tracker_max_tables_checked_before_create(tmp_path):
    for i in range(tracker.MAX_TABLES):
        assert tracker.add(tmp_path, f"t{i}", {"n": i})["ok"]
    refused = tracker.add(tmp_path, "one-too-many", {"n": 1})
    assert refused["ok"] is False
    # Existing tables still accept rows.
    assert tracker.add(tmp_path, "t0", {"n": 99})["ok"]


def test_tracker_due_followups_open_status_only(tmp_path):
    past = time.time() - 60
    for status in ("open", "OPEN", "cancelled", "bounced",
                   "Contacted", "done"):
        out = tracker.add(tmp_path, "leads",
                          {"name": status, "follow_up_due": past})
        assert out["ok"]
        if status != "open":
            tracker.set_status(tmp_path, "leads",
                               out["row"]["id"], status)
    due = tracker.due_followups(tmp_path, now_ts=time.time())
    labels = {d["label"] for d in due}
    assert labels == {"open", "OPEN"}


def test_tracker_concurrent_adds_lose_nothing(tmp_path):
    def add_many(n):
        for i in range(n):
            tracker.add(tmp_path, "leads", {"n": i})
    threads = [threading.Thread(target=add_many, args=(25,))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert tracker.tables(tmp_path)["leads"] == 100


def test_tracker_save_failure_is_reported(tmp_path, monkeypatch):
    real_replace = tracker.os.replace

    def broken(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(tracker.os, "replace", broken)
    out = tracker.add(tmp_path, "leads", {"n": 1})
    assert out["ok"] is False and "save" in out["error"]
    monkeypatch.setattr(tracker.os, "replace", real_replace)


# -- accounts ------------------------------------------------------------

def test_accounts_cap_rechecked_at_record(tmp_path):
    book = AccountBook(tmp_path)
    row = book.add("instagram", "ig", "@x", cap=2)
    book.record_send(row["id"])
    book.record_send(row["id"])
    third = book.record_send(row["id"])
    assert third.get("capped") is True
    assert book.get(row["id"])["sent_count"] == 2


def test_accounts_duplicate_address_refused(tmp_path):
    book = AccountBook(tmp_path)
    book.add("email", "main", "a@b.c")
    with pytest.raises(ValueError):
        book.add("email", "again", "A@b.c ")
    # Same address on a DIFFERENT channel is a different book
    # entry and stays allowed.
    book.add("threads", "t", "a@b.c")


def test_accounts_shared_lock_across_instances(tmp_path):
    one = AccountBook(tmp_path)
    two = AccountBook(tmp_path)
    assert one._lock is two._lock


def test_accounts_corrupt_file_keeps_last_good(tmp_path):
    book = AccountBook(tmp_path)
    row = book.add("email", "main", "a@b.c")
    assert book._load()  # primes last-good
    (tmp_path / "accounts.json").write_text("{torn", "utf-8")
    loaded = book._load()
    assert row["id"] in loaded


def test_accounts_rollover_clears_cap_rest(tmp_path):
    book = AccountBook(tmp_path)
    row = book.add("email", "main", "a@b.c", cap=1)
    book.record_send(row["id"])
    rested = book.get(row["id"])
    assert rested["rest_reason"] == "roz ki had puri"
    raw = json.loads((tmp_path / "accounts.json").read_text())
    raw[row["id"]]["sent_day"] = "1999-01-01"
    (tmp_path / "accounts.json").write_text(json.dumps(raw))
    fresh = AccountBook(tmp_path).get(row["id"])
    assert fresh["sent_count"] == 0
    assert fresh["rest_reason"] == ""
    assert fresh["rest_until"] == 0.0


# -- scheduler -----------------------------------------------------------

def test_compute_next_garbage_returns_none():
    now = time.time()
    assert scheduler.compute_next({"type": "daily",
                                   "at": "25:99"}, now) is None
    assert scheduler.compute_next({"type": "daily",
                                   "at": "dinner"}, now) is None
    assert scheduler.compute_next({"type": "interval",
                                   "hours": "soon"}, now) is None
    assert scheduler.compute_next("not-a-dict", now) is None
    assert scheduler.compute_next({"type": "daily",
                                   "at": "08:30"}, now) is not None


def test_tick_notify_raise_never_refires(tmp_path):
    scheduler.add_item(tmp_path, "r1", "routines.json",
                       schedule={"type": "interval", "hours": 1},
                       steps=["x"])
    rows = scheduler.load_routines(tmp_path)
    rows[0]["next_run"] = time.time() - 5
    (tmp_path / "routines.json").write_text(json.dumps(rows))
    calls = []

    def runner(work):
        calls.append(work)
        return "done"

    def bad_notify(title, body):
        raise RuntimeError("notify channel dead")

    report = scheduler.tick(tmp_path, runner, notify=bad_notify)
    assert report["routines"] == 1 and len(calls) == 1
    # Progress was saved before the raising notify: a second
    # tick must NOT fire the same run again.
    scheduler.tick(tmp_path, runner, notify=bad_notify)
    assert len(calls) == 1


def test_fire_row_respects_enabled_and_done(tmp_path):
    out = scheduler.add_item(tmp_path, "off", "routines.json",
                             schedule={"type": "daily",
                                       "at": "08:00"}, steps=["x"])
    rows = scheduler.load_routines(tmp_path)
    rows[0]["enabled"] = False
    (tmp_path / "routines.json").write_text(json.dumps(rows))
    ran = []
    scheduler.fire_row(tmp_path, "routines.json",
                       out["item"]["id"],
                       lambda w: ran.append(w) or "ok")
    assert ran == []
    gout = scheduler.add_item(tmp_path, "g", "scheduled_goals.json",
                              schedule={"type": "daily",
                                        "at": "08:00"}, prompt="p")
    grows = scheduler.load_goals(tmp_path)
    grows[0]["status"] = "done"
    (tmp_path / "scheduled_goals.json").write_text(
        json.dumps(grows))
    scheduler.fire_row(tmp_path, "scheduled_goals.json",
                       gout["item"]["id"],
                       lambda w: ran.append(w) or "ok")
    assert ran == []
