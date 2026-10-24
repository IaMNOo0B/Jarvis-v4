"""Process supervisor — JARVIS v4, Wave 3.

Dodaios's warning #4 against v4, verbatim in spirit: readiness was
launch-time only — if the UI process died, nothing restarted it
and nothing even reported it. This module is the watcher that
lives OUTSIDE the app process:

    python -m jarvis.supervisor [--data-dir DIR] [ui args...]

It launches `python -m jarvis.ui`, and then:
- a clean exit (code 0 — the owner closed it) is final;
- a crash (nonzero exit) restarts the app, with backoff, up to a
  bounded budget (3 restarts per 10 minutes) — past the budget it
  stops restarting and leaves the evidence in
  data/supervisor.log instead of crash-looping forever;
- a HUNG app (process alive but its heartbeat file —
  data/heartbeat.json, refreshed by the UI every 10 seconds —
  goes stale past 90 seconds) is terminated and restarted on the
  same budget. A heartbeat is only trusted when it was written
  AFTER this child spawned: a stale file left by a dead run must
  never kill a newborn (that exact trap killed every restart on
  the owner's PC from 2026-10-22 on — the file's timestamp sat
  at one old moment while its reported age kept growing).

Running `python -m jarvis.ui` directly still works; it is simply
unsupervised.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

HEARTBEAT_NAME = "heartbeat.json"
HEARTBEAT_STALE_S = 90.0


class RestartPolicy:
    """Bounded crash recovery: at most `max_restarts` restarts in
    any `window_s` span. The bound is the feature — an app that
    crashes on launch must not be relaunched forever."""

    def __init__(self, max_restarts: int = 3, window_s: float = 600.0):
        self.max_restarts = int(max_restarts)
        self.window_s = float(window_s)
        self._crashes: list = []

    def note_crash(self, now: float) -> bool:
        """Record a crash; True when a restart is still allowed."""
        self._crashes = [t for t in self._crashes
                         if now - t < self.window_s]
        self._crashes.append(now)
        return len(self._crashes) <= self.max_restarts


def write_heartbeat(data_dir, pid: Optional[int] = None) -> None:
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        (Path(data_dir) / HEARTBEAT_NAME).write_text(
            json.dumps({"pid": pid or os.getpid(),
                        "ts": time.time()}), encoding="utf-8")
    except Exception:
        pass


def heartbeat_age(data_dir, now: Optional[float] = None) -> Optional[float]:
    """Seconds since the app's last heartbeat, or None when no
    heartbeat file exists (the app may predate the supervisor —
    absence is not a hang)."""
    ts = heartbeat_ts(data_dir)
    if ts is None:
        return None
    return (time.time() if now is None else now) - ts


def heartbeat_ts(data_dir) -> Optional[float]:
    """The timestamp inside the heartbeat file, or None when the
    file is missing or unreadable. The spawn rule in
    run_supervisor compares this against the spawn moment."""
    path = Path(data_dir) / HEARTBEAT_NAME
    if not path.exists():
        return None
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
        return float(row["ts"])
    except Exception:
        return None


def _log(data_dir, line: str) -> None:
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        with (Path(data_dir) / "supervisor.log").open(
                "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"{line}\n")
    except Exception:
        pass


def run_supervisor(data_dir, command: list, *,
                   policy: Optional[RestartPolicy] = None,
                   spawn: Callable = subprocess.Popen,
                   sleep: Callable = time.sleep,
                   clock: Callable = time.time,
                   poll_s: float = 1.0,
                   backoff_s: float = 2.0,
                   heartbeat_timeout_s: float = HEARTBEAT_STALE_S) -> int:
    """Supervise `command` until a clean exit or an exhausted
    restart budget. Returns the final exit code."""
    policy = policy or RestartPolicy()
    while True:
        _log(data_dir, f"starting: {' '.join(map(str, command))}")
        # The spawn moment is the line between runs: only a
        # heartbeat written after it belongs to this child. A
        # file left by a dead run is ignored, however old — it
        # must never kill a newborn on the first poll.
        spawn_ts = clock()
        child = spawn(command)
        hung = False
        fresh_seen = False
        while True:
            code = child.poll()
            if code is not None:
                break
            hb_ts = heartbeat_ts(data_dir)
            now = clock()
            if hb_ts is not None and hb_ts >= spawn_ts:
                fresh_seen = True
                if now - hb_ts > heartbeat_timeout_s:
                    _log(data_dir, f"heartbeat stale "
                                   f"({now - hb_ts:.0f}s) — "
                                   f"terminating hung app")
                    hung = True
                    try:
                        child.terminate()
                    except Exception:
                        pass
                    try:
                        code = child.wait(timeout=5)
                    except Exception:
                        code = -1
                    break
            elif not fresh_seen and \
                    now - spawn_ts > heartbeat_timeout_s + 60.0:
                # No heartbeat ever arrived: the child hung
                # before its heartbeat thread started.
                _log(data_dir, "no heartbeat since spawn — "
                               "terminating hung app")
                hung = True
                try:
                    child.terminate()
                except Exception:
                    pass
                try:
                    code = child.wait(timeout=5)
                except Exception:
                    code = -1
                break
            sleep(poll_s)
        if code == 0 and not hung:
            _log(data_dir, "app exited cleanly — supervisor done")
            return 0
        _log(data_dir, f"app exited with code {code}"
                       + (" (hung)" if hung else ""))
        if not policy.note_crash(clock()):
            _log(data_dir, "restart budget exhausted — NOT "
                           "restarting; see supervisor.log")
            return int(code) if isinstance(code, int) and code \
                else 1
        sleep(backoff_s)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    data_dir = None
    if "--data-dir" in argv:
        idx = argv.index("--data-dir")
        if idx + 1 < len(argv):
            data_dir = argv[idx + 1]
    if data_dir is None:
        from .ui import DEFAULT_DATA_DIR
        data_dir = str(DEFAULT_DATA_DIR)
    command = [sys.executable, "-m", "jarvis.ui"] + argv
    return run_supervisor(data_dir, command)


if __name__ == "__main__":
    raise SystemExit(main())
