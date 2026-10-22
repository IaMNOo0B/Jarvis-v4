"""Script watchers — JARVIS v4, Wave 2 (Sentient's cheapest pattern).

A watcher is a URL JARVIS re-reads on a schedule and diffs in CODE
— no model call at all while nothing changed. When something did
change, the condition decides: "changed" notifies at once;
"noteworthy" spends exactly ONE small model call, a judge with a
written rubric, on the old-vs-new excerpts, and notifies only if
the change is worth the owner's attention. Counters, clocks,
timestamps, ads and rewordings are explicitly not noteworthy.

Store: data/watchers.json. Checks run from the app's existing
background cadence (see _maybe_auto_watch in chat.py).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

DUE_AFTER_S = 1800          # a watcher is due again after 30 minutes
_EXCERPT = 600


def _path(data_dir) -> Path:
    return Path(data_dir) / "watchers.json"


def load_watchers(data_dir) -> list:
    path = _path(data_dir)
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _save(data_dir, rows: list) -> None:
    try:
        _path(data_dir).write_text(json.dumps(rows, ensure_ascii=False),
                                   encoding="utf-8")
    except Exception:
        pass


def _hash(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _content_of(fetched: dict) -> str:
    if not isinstance(fetched, dict) or fetched.get("error"):
        return ""
    return str(fetched.get("content") or fetched.get("text") or "")


def add_watcher(data_dir, url: str, name: str = "",
                condition: str = "noteworthy",
                fetch: Optional[Callable] = None) -> dict:
    """Create a watcher and take its baseline snapshot now — a
    watcher without a baseline could only ever cry wolf."""
    from . import builtin_skills as bs
    fetch = fetch or bs.op_web_fetch
    url = str(url).strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False,
                "error": "watcher URL must start with http:// or "
                         "https://, sir."}
    content = _content_of(fetch(url))
    if not content:
        return {"ok": False,
                "error": "watcher baseline fetch failed — the page "
                         "did not give readable text, sir."}
    row = {"id": uuid.uuid4().hex[:8],
           "name": str(name).strip() or url,
           "url": url,
           "condition": ("changed" if condition == "changed"
                         else "noteworthy"),
           "last_hash": _hash(content),
           "last_checked": time.time(),
           "excerpt": content[:_EXCERPT]}
    rows = load_watchers(data_dir)
    rows.append(row)
    _save(data_dir, rows)
    return {"ok": True, "watcher_id": row["id"], "name": row["name"]}


def any_watcher_due(data_dir, now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    return any(now - float(w.get("last_checked", 0)) >= DUE_AFTER_S
               for w in load_watchers(data_dir))


def check_watchers(data_dir, judge: Optional[Callable] = None,
                   notify: Optional[Callable] = None,
                   fetch: Optional[Callable] = None,
                   now: Optional[float] = None,
                   thresholds=None) -> dict:
    """One check pass over due watchers. `judge(old, new)` is the
    noteworthy rubric call returning a confidence 0..1 (a plain
    bool still works; None abstains and the change is told).
    Without a judge, "noteworthy" degrades honestly to "changed".
    With `thresholds` (jarvis/thresholds.py), the confidence is
    gated by the watcher's LEARNED threshold instead of a fixed
    0.5. `notify(title, body)` reaches the owner. Returns a small
    report; never raises."""
    from . import builtin_skills as bs
    fetch = fetch or bs.op_web_fetch
    now = time.time() if now is None else now
    rows = load_watchers(data_dir)
    report = {"checked": 0, "changed": 0, "notified": 0, "errors": 0}
    for row in rows:
        if now - float(row.get("last_checked", 0)) < DUE_AFTER_S:
            continue
        report["checked"] += 1
        content = _content_of(fetch(str(row["url"])))
        row["last_checked"] = now
        if not content:
            report["errors"] += 1
            continue
        digest = _hash(content)
        if digest == row.get("last_hash"):
            continue
        report["changed"] += 1
        old_excerpt = str(row.get("excerpt", ""))
        row["last_hash"] = digest
        row["excerpt"] = content[:_EXCERPT]
        worth = True
        if row.get("condition") == "noteworthy" and judge is not None:
            try:
                conf = judge(old_excerpt, content[:_EXCERPT])
            except Exception:
                conf = None         # judge down: tell, don't hide
            if isinstance(conf, bool):
                conf = 1.0 if conf else 0.0
            if conf is None:
                worth = True
            elif thresholds is not None:
                worth = thresholds.allows(row["id"], float(conf))
            else:
                worth = float(conf) >= 0.5
        if worth:
            report["notified"] += 1
            if notify is not None:
                notify(f"Watcher: {row.get('name', row['url'])}",
                       f"{row['url']} par kuch badla hai, sir.")
    _save(data_dir, rows)
    return report
