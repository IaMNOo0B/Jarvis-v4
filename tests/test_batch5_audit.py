"""Batch-5 audit regressions: builds / team / autopilot /
forge / mailer / graph / fanout / tornet (2026-10-22)."""

import json
import time
from pathlib import Path

import pytest

from jarvis import (autopilot, builds, fanout, forge, graph,
                    mailer, team, tornet)


# -- builds ---------------------------------------------------------------

def test_safe_relpath_rejects_windows_drives():
    assert builds._safe_relpath("src/a.py")
    assert builds._safe_relpath("./src/a.py")
    assert not builds._safe_relpath("C:/evil.py")
    assert not builds._safe_relpath("C:\\evil.py")
    assert not builds._safe_relpath("D:evil.py")
    assert not builds._safe_relpath("../evil.py")
    assert not builds._safe_relpath("/abs.py")


def test_validate_plan_checks_every_item():
    plan = {"goal": "g", "steps": ["s"],
            "verification": [{"kind": "check", "detail": "d"}],
            "changes": [{"action": "NEW", "path": f"f{i}.py",
                         "what": "w"} for i in range(45)]}
    plan["changes"][44] = {"action": "NEW", "path": "../evil.py",
                            "what": "w"}
    problems = builds.validate_plan(plan)
    assert any("45" in p for p in problems)


def test_binding_deleted_never_counts_as_done():
    plan = {"changes": [{"action": "NEW", "path": "a.py"},
                        {"action": "DELETE", "path": "b.py"}]}
    diff = {"created": [], "modified": [], "deleted": ["a.py"]}
    report = builds.binding_report(plan, diff)
    assert report["done"] == []
    assert len(report["missing"]) == 2


def test_confidence_diluted_by_extras():
    binding = {"done": ["[NEW] a"], "missing": [],
               "extras": [f"x{i}" for i in range(50)]}
    score, why = builds.computed_confidence(
        [{"ok": True}], binding)
    assert score < 0.1
    assert "bina-plan" in why


def test_list_builds_latest_first_by_mtime(tmp_path):
    store = builds.BuildStore(tmp_path)
    plan = {"goal": "g", "steps": ["s"], "changes": [],
            "verification": []}
    store.create("aaa-old", "old", plan, "planning", "")
    store.create("zzz-new", "new", plan, "planning", "")
    future = time.time() + 100
    import os
    os.utime(tmp_path / "builds" / "zzz-new" / "state.json",
             (future, future))
    listed = store.list_builds()
    assert listed[0]["id"] == "zzz-new"


def test_build_claim_is_atomic(tmp_path):
    store = builds.BuildStore(tmp_path)
    plan = {"goal": "g", "steps": ["s"], "changes": [],
            "verification": []}
    store.create("b1", "g", plan, "planning", "")
    assert store.claim("b1", True)["status"] == "executing"
    assert store.claim("b1", True) is None     # second tap loses
    assert store.claim("../escape", True) is None


def test_snapshot_truncated_flag(tmp_path, monkeypatch):
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x")
    monkeypatch.setattr(builds, "MAX_SNAPSHOT_FILES", 3)
    snap, truncated = builds.snapshot_tree_full(tmp_path)
    assert truncated is True and len(snap) == 3


# -- team -------------------------------------------------------------------

def _planner(subtasks):
    return lambda task: {"subtasks": subtasks}


def test_team_plan_rejects_escape_paths():
    subtasks, err = team.validate_plan({"subtasks": [
        {"title": "a", "files": ["../secret.txt"], "brief": ""}]})
    assert subtasks is None and "surakshit" in err


def test_team_overlap_normalized():
    subtasks, err = team.validate_plan({"subtasks": [
        {"title": "a", "files": ["./x.py"], "brief": ""},
        {"title": "b", "files": ["x.py"], "brief": ""}]})
    assert subtasks is None and "do hisson" in err


def test_team_critic_string_false_is_not_a_pass(tmp_path):
    def worker(task, sub, ws, issues):
        (ws / sub["files"][0]).write_text("x = 1")
        return "done"

    out = team.run_team(
        tmp_path, "t",
        _planner([{"title": "a", "files": ["a.py"],
                   "brief": ""}]),
        worker, lambda *a: {"ok": "false", "issues": "nahi"})
    assert out["ok"] is False
    assert out["subtasks"][0]["ok"] is False


def test_team_worker_crash_is_reported_not_raised(tmp_path):
    def worker(task, sub, ws, issues):
        raise RuntimeError("worker boom")

    out = team.run_team(
        tmp_path, "t",
        _planner([{"title": "a", "files": ["a.py"],
                   "brief": ""}]),
        worker, lambda *a: {"ok": True})
    assert out["ok"] is False
    assert "worker crash" in out["subtasks"][0]["issues"]


def test_team_missing_file_fails_despite_happy_critic(tmp_path):
    out = team.run_team(
        tmp_path, "t",
        _planner([{"title": "a", "files": ["ghost.py"],
                   "brief": ""}]),
        lambda *a: "sab ho gaya", lambda *a: {"ok": True})
    assert out["ok"] is False
    assert "bani hi nahi" in out["subtasks"][0]["issues"]


# -- autopilot ----------------------------------------------------------------

def test_mission_judge_crash_becomes_stuck():
    def step(goal, log, n):
        return "kadam"

    def judge(goal, log):
        raise RuntimeError("judge dead")

    out = autopilot.run_mission("g", step, judge, max_steps=3)
    assert out["state"] == "stuck"
    assert "judge fail" in out["note"]


def test_mission_unknown_verdict_fails_closed():
    out = autopilot.run_mission(
        "g", lambda g, l, n: "s",
        lambda g, l: {"state": "banana"}, max_steps=3)
    assert out["state"] == "stuck"
    assert "banana" in out["note"]


def test_mission_steps_taken_counts_real_steps():
    calls = {"n": 0}

    def step(goal, log, n):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("pehla crash")
        return "dusra kadam"

    verdicts = iter([{"state": "done"}])
    out = autopilot.run_mission(
        "g", step, lambda g, l: next(verdicts),
        repair_fn=lambda g, l, f: {"strategy": "retry",
                                   "root_cause": "x"},
        max_steps=3)
    assert out["state"] == "done"
    assert out["steps_taken"] == 2        # crash + success
    assert out["repairs_made"] == 1


def test_mission_repair_caps_clamped():
    out = autopilot.run_mission(
        "g", lambda g, l, n: (_ for _ in ()).throw(
            RuntimeError("crash")),
        lambda g, l: {"state": "continue"},
        repair_fn=lambda g, l, f: {"strategy": "retry"},
        max_steps=2, max_repairs_per_step=99,
        max_repairs_total=99)
    assert out["repairs_made"] <= autopilot.MAX_REPAIRS_TOTAL


# -- mailer -------------------------------------------------------------------

class _FakeSMTP:
    def __init__(self, host, port, timeout=None):
        self.refuse = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        return self.refuse


def test_send_email_surfaces_refusals():
    fake = _FakeSMTP

    def factory(host, port, timeout=None):
        inst = fake(host, port)
        inst.refuse = {"bad@x.com": (550, b"no")}
        return inst

    out = mailer.send_email("me@gmail.com", "pw",
                            "good@x.com, bad@x.com", "s", "b",
                            smtp_factory=factory)
    assert out["refused"] == ["bad@x.com"]
    assert out["recipients"] == ["good@x.com", "bad@x.com"]


def test_send_email_total_refusal_raises():
    def factory(host, port, timeout=None):
        inst = _FakeSMTP(host, port)
        inst.refuse = {"bad@x.com": (550, b"no")}
        return inst

    with pytest.raises(RuntimeError):
        mailer.send_email("me@gmail.com", "pw", "bad@x.com",
                          "s", "b", smtp_factory=factory)


class _FakeIMAP:
    status = "OK"
    seen_timeout = None

    def __init__(self, host, port, timeout=None):
        _FakeIMAP.seen_timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        pass

    def append(self, folder, flags, when, raw):
        return _FakeIMAP.status, [b""]


def test_draft_email_append_no_is_failure():
    _FakeIMAP.status = "NO"
    with pytest.raises(RuntimeError):
        mailer.draft_email("me@gmail.com", "pw", "t@x.com",
                           "s", "b", imap_factory=_FakeIMAP)
    _FakeIMAP.status = "OK"
    out = mailer.draft_email("me@gmail.com", "pw", "t@x.com",
                             "s", "b", imap_factory=_FakeIMAP)
    assert out["ok"] is True
    assert _FakeIMAP.seen_timeout == 30


# -- graph ----------------------------------------------------------------------

def test_graph_one_bad_line_keeps_the_rest(tmp_path):
    path = tmp_path / "graph.jsonl"
    good = {"fact_id": "f1", "subject": "Aisha",
            "predicate": "is", "object": "teacher"}
    path.write_text(json.dumps(good) + "\n{torn line\n",
                    encoding="utf-8")
    g = graph.KnowledgeGraph(tmp_path)
    assert len(g) == 1 and g.skipped_lines == 1
    # A rebuild must not duplicate the surviving edge.
    g.build_from_facts([{"id": "f1", "claim": "Aisha is a "
                         "teacher", "ts": 1}])
    assert len(g) == 1


def test_graph_about_matches_whole_tokens(tmp_path):
    g = graph.KnowledgeGraph(tmp_path)
    g.add_fact({"id": "f1", "claim": "Coffee is hot", "ts": 1})
    # 'fee' is a substring of 'coffee' but not a token of it.
    assert g.about("fee")["edges"] == []
    assert len(g.about("coffee")["edges"]) == 1


def test_graph_no_fabricated_subject(tmp_path):
    edges = graph.extract_edges(
        "Aisha is a teacher who lives in Pune")
    subjects = {s for s, _p, _o in edges}
    assert subjects == {"Aisha"}


def test_graph_neighbours_discard_normalized(tmp_path):
    g = graph.KnowledgeGraph(tmp_path)
    g.add_fact({"id": "f1", "claim": "aisha lives in Pune",
                "ts": 1})
    out = g.about("Aisha")
    assert "aisha" not in [n.lower() for n in out["neighbours"]]


# -- fanout ---------------------------------------------------------------------

def test_merge_hits_normalizes_urls():
    merged = fanout.merge_hits([
        ("bing", [{"url": "https://www.Example.com/page/"
                          "?utm_source=x#frag",
                   "title": "", "snippet": ""}]),
        ("ddg", [{"url": "https://example.com/page",
                  "title": "Real Title", "snippet": "snip"}]),
    ])
    assert len(merged) == 1
    assert merged[0]["via"] == ["bing", "ddg"]
    assert merged[0]["title"] == "Real Title"
    assert merged[0]["snippet"] == "snip"


def test_run_parallel_none_and_ok_false_are_failures():
    oks, failures = fanout.run_parallel({
        "none-route": lambda: None,
        "false-route": lambda: {"ok": False, "error": "boom"},
        "good": lambda: {"ok": True, "hits": []},
    })
    assert set(oks) == {"good"}
    assert set(failures) == {"none-route", "false-route"}


# -- tornet ---------------------------------------------------------------------

def test_tor_timeout_message_does_not_blame_tor(monkeypatch):
    import socket as _socket

    def slow_connect(host, port, proxy_port=9050, timeout=5,
                     **kw):
        raise _socket.timeout("read timed out")

    monkeypatch.setattr(tornet, "socks5_connect", slow_connect)
    text, err = tornet.fetch_via_tor("https://example.com/",
                                     ports=(9050,))
    assert text is None
    assert "nahi mila" not in err
    assert "sust" in err


def test_tor_http_error_status_is_error_shaped(monkeypatch):
    class FakeResp:
        status = 404

        def read(self):
            return b"nope"

    class FakeConn:
        sock = None

        def __init__(self, *a, **k):
            pass

        def request(self, *a, **k):
            pass

        def getresponse(self):
            return FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(tornet, "socks5_connect",
                        lambda *a, **k: object())
    monkeypatch.setattr(tornet.http.client, "HTTPConnection",
                        FakeConn)
    text, err = tornet.fetch_via_tor("http://example.onion/",
                                     ports=(9050,))
    assert text is None and "HTTP 404" in err


# -- forge -----------------------------------------------------------------

def _forge_root(tmp_path):
    root = tmp_path / "root"
    (root / "jarvis").mkdir(parents=True)
    (root / "jarvis" / "__init__.py").write_text("")
    (root / "jarvis" / "chat.py").write_text("")
    (root / "jarvis" / "thing.py").write_text("X = 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_existing.py").write_text(
        "import jarvis.thing\n\n\ndef test_existing():\n"
        "    assert jarvis.thing.X == 1\n")
    return root


def test_forge_verify_runs_existing_suite(tmp_path):
    root = _forge_root(tmp_path)
    data = tmp_path / "data"
    staged = forge.stage_feature(
        data, root, "thing upgrade",
        {"jarvis/thing.py": "X = 1\ndef broken(:\n"},
        "def test_feature():\n    assert True\n")
    assert staged["ok"]
    out = forge.verify_staged(root, staged["dir"])
    # The payload breaks an EXISTING module its own test
    # never touches — the staged suite run must catch it.
    assert out["ok"] is False and out["stage"] == "tests"


def test_forge_verify_passes_clean_feature(tmp_path):
    root = _forge_root(tmp_path)
    data = tmp_path / "data"
    staged = forge.stage_feature(
        data, root, "thing upgrade",
        {"jarvis/thing.py": "X = 2\n"},
        "import jarvis.thing\n\n\ndef test_feature():\n"
        "    assert jarvis.thing.X == 2\n")
    # The staged existing test asserts X == 1; the payload
    # changes X to 2 — update the sandbox expectation by
    # making the existing test part of the payload too.
    staged2 = forge.stage_feature(
        data, root, "thing upgrade two",
        {"jarvis/thing.py": "X = 2\n",
         "tests/test_existing.py":
             "import jarvis.thing\n\n\ndef test_existing():\n"
             "    assert jarvis.thing.X == 2\n"},
        "import jarvis.thing\n\n\ndef test_feature():\n"
        "    assert jarvis.thing.X == 2\n")
    assert forge.verify_staged(root, staged2["dir"])["ok"]


def test_forge_test_file_backed_up_and_restored(tmp_path):
    root = _forge_root(tmp_path)
    data = tmp_path / "data"
    staged = forge.stage_feature(
        data, root, "solo feature",
        {"jarvis/thing.py": "X = 5\n"},
        "def test_feature():\n    assert True\n")
    assert staged["ok"]
    manifest = json.loads(
        (Path(staged["dir"]) / "MANIFEST.json").read_text())
    live_test = root / manifest["test_rel"]
    live_test.parent.mkdir(parents=True, exist_ok=True)
    live_test.write_text("# ORIGINAL — kisi aur ka test\n")
    manifest["status"] = "verified"
    (Path(staged["dir"]) / "MANIFEST.json").write_text(
        json.dumps(manifest))
    out = forge.promote(root, staged["dir"],
                        runner=lambda cwd, rel: (True, ""))
    assert out["ok"]
    assert "ORIGINAL" not in live_test.read_text()
    back = forge.rollback(root, data, staged["slug"])
    assert back["ok"]
    assert "ORIGINAL" in live_test.read_text()
