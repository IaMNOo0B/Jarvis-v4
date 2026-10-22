"""Learned thresholds: clamp(base - 0.05*score, 0.40, 0.95), fed
by watcher-notification outcomes (read = approval, 48h unread =
dismissal), each counted exactly once."""

import json
import time

from jarvis.chat import _notify, _watcher_feedback
from jarvis.thresholds import LearnedThresholds


def test_formula_and_clamps(tmp_path):
    th = LearnedThresholds(tmp_path)
    assert th.threshold("w1") == 0.60
    for _ in range(4):
        th.feedback("w1", approved=True)
    assert th.threshold("w1") == 0.40 + 0.0 or abs(
        th.threshold("w1") - 0.40) < 1e-9     # 0.60 - 4*0.05, clamped
    for _ in range(20):
        th.feedback("w1", approved=False)
    assert th.threshold("w1") == 0.95          # ceiling holds
    assert th.allows("w2", 0.61) is True
    assert th.allows("w2", 0.59) is False
    again = LearnedThresholds(tmp_path)        # persisted
    assert again.score("w1") == th.score("w1")


def test_watcher_feedback_counts_once(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "watchers.json").write_text(json.dumps([
        {"id": "w9", "name": "Prices",
         "url": "https://x.example/prices", "condition": "noteworthy",
         "last_hash": "h", "last_checked": 0, "excerpt": ""}]),
        encoding="utf-8")
    row = _notify(data, "Watcher: Prices",
                  "https://x.example/prices par kuch badla hai, sir.",
                  kind="watcher")
    # mark it read, as the owner opening it would
    path = data / "notifications.jsonl"
    rows = [json.loads(x) for x in
            path.read_text(encoding="utf-8").splitlines()]
    rows[0]["read"] = True
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                    encoding="utf-8")
    _watcher_feedback(data)
    th = LearnedThresholds(data)
    assert th.score("w9") == 1
    _watcher_feedback(data)                    # never counted twice
    assert LearnedThresholds(data).score("w9") == 1
    # an ancient UNREAD one is a dismissal
    old = {"id": "zz", "ts": time.time() - 72 * 3600,
           "title": "Watcher: Prices",
           "body": "https://x.example/prices par kuch badla hai.",
           "kind": "watcher", "read": False}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(old) + "\n")
    _watcher_feedback(data)
    assert LearnedThresholds(data).score("w9") == 0
