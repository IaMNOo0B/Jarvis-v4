"""Script watchers: diff in code (zero model calls while quiet),
one rubric-judged call when a 'noteworthy' page changes."""

import time

from jarvis.chat import ChatApp
from jarvis.watchers import (add_watcher, any_watcher_due,
                             check_watchers, load_watchers)

PAGES = {"https://x.example/prices": "Price: 100 rupees"}


def _fetch(url):
    return {"content": PAGES[url]}


def test_add_takes_a_baseline_and_changed_condition_notifies(tmp_path):
    out = add_watcher(tmp_path, "https://x.example/prices",
                      name="Prices", condition="changed", fetch=_fetch)
    assert out["ok"] is True
    notes = []
    PAGES["https://x.example/prices"] = "Price: 80 rupees"
    report = check_watchers(
        tmp_path, notify=lambda t, b: notes.append((t, b)),
        fetch=_fetch, now=time.time() + 3600)
    assert report == {"checked": 1, "changed": 1, "notified": 1,
                      "errors": 0}
    assert notes and "Prices" in notes[0][0]


def test_quiet_page_costs_nothing_and_is_not_due_early(tmp_path):
    add_watcher(tmp_path, "https://x.example/prices", fetch=_fetch)
    assert any_watcher_due(tmp_path) is False
    report = check_watchers(tmp_path, fetch=_fetch,
                            now=time.time() + 3600)
    assert report["checked"] == 1 and report["changed"] == 0


def test_noteworthy_judge_can_say_no(tmp_path):
    add_watcher(tmp_path, "https://x.example/prices", fetch=_fetch)
    PAGES["https://x.example/prices"] = "Price: 100 rupees (updated!)"
    notes = []
    report = check_watchers(
        tmp_path, judge=lambda old, new: False,
        notify=lambda t, b: notes.append((t, b)),
        fetch=_fetch, now=time.time() + 3600)
    assert report["changed"] == 1 and report["notified"] == 0
    assert notes == []
    # ...and the baseline moved on: the same text is quiet next time
    report2 = check_watchers(tmp_path, fetch=_fetch,
                             now=time.time() + 7200)
    assert report2["changed"] == 0


def test_app_advertises_watch_url(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert "watch_url" in names
    assert "Koi watcher nahi" in app.handle_command("/watchers")
    assert load_watchers(tmp_path) == []
