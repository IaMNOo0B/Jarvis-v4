"""Process supervisor: clean exits are final, crashes restart on
a bounded budget, a stale heartbeat marks a hung app."""

import time

from jarvis.supervisor import (RestartPolicy, heartbeat_age,
                               run_supervisor, write_heartbeat)


def test_policy_budget_is_bounded():
    policy = RestartPolicy(max_restarts=3, window_s=600)
    now = 1000.0
    assert policy.note_crash(now) is True
    assert policy.note_crash(now) is True
    assert policy.note_crash(now) is True
    assert policy.note_crash(now) is False       # 4th crash: stop
    assert policy.note_crash(now + 601) is True  # window slid past


def test_heartbeat_age(tmp_path):
    assert heartbeat_age(tmp_path) is None       # no file: not a hang
    write_heartbeat(tmp_path)
    age = heartbeat_age(tmp_path)
    assert age is not None and age < 5


class _Child:
    def __init__(self, script):
        self.script = list(script)   # poll() results; last repeats
        self.terminated = False

    def poll(self):
        if len(self.script) > 1:
            return self.script.pop(0)
        return self.script[0]

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return -9


def _run(children, tmp_path, **kw):
    spawned = []

    def spawn(command):
        child = children[len(spawned)]
        spawned.append(child)
        return child

    code = run_supervisor(tmp_path, ["app"], spawn=spawn,
                          sleep=lambda s: None,
                          clock=lambda: 5000.0, poll_s=0, **kw)
    return code, spawned


def test_clean_exit_is_final(tmp_path):
    code, spawned = _run([_Child([0])], tmp_path)
    assert code == 0 and len(spawned) == 1


def test_crashes_restart_until_the_budget_ends(tmp_path):
    kids = [_Child([1]) for _ in range(6)]
    code, spawned = _run(kids, tmp_path)
    # 1 initial + 3 restarts, then the budget says stop
    assert len(spawned) == 4
    assert code == 1
    log = (tmp_path / "supervisor.log").read_text(encoding="utf-8")
    assert "budget exhausted" in log


def test_hung_app_is_terminated_and_restarted(tmp_path):
    write_heartbeat(tmp_path)                    # already stale soon
    stale = _Child([None])                       # never exits
    good = _Child([0])
    now = [time.time() + 1000]                   # heartbeat is ancient
    spawned = []

    def spawn(command):
        child = [stale, good][len(spawned)]
        spawned.append(child)
        return child

    code = run_supervisor(tmp_path, ["app"], spawn=spawn,
                          sleep=lambda s: None,
                          clock=lambda: now[0], poll_s=0)
    assert stale.terminated is True
    assert code == 0 and len(spawned) == 2
