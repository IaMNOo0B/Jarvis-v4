"""The self-healing law: a mission that hits an error must
diagnose it (what, where from, root cause), repair, and run
again toward the goal — not die on the first failure.
Bounded: per-step and per-mission repair caps; an abort from
the doctor ends the mission with the root cause named; and
without a repair_fn the old behaviour is untouched."""

from jarvis.autopilot import run_mission
from jarvis.chat import ChatApp, _mission_repair


def _doctor(strategy="retry", root="galat tool chuna tha"):
    return lambda g, log, f: {
        "error": "nakami", "origin": "kadam 1",
        "root_cause": root, "strategy": strategy,
        "instruction": "is baar dusra raasta lo"}


def test_crash_is_repaired_and_the_mission_lands():
    calls = {"n": 0}

    def step(g, log, n):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("pehli koshish crash")
        return "kadam ho gaya"

    out = run_mission("goal", step,
                      lambda g, log: {"state": "done"},
                      repair_fn=_doctor())
    assert out["state"] == "done"
    assert out["repairs_made"] == 1
    assert out["repairs"][0]["root_cause"] == "galat tool chuna tha"


def test_the_retry_reads_the_diagnosis():
    seen_logs = []

    def step(g, log, n):
        seen_logs.append([e["summary"] for e in log])
        if len(seen_logs) == 1:
            raise ValueError("boom")
        return "theek ho gaya"

    out = run_mission("goal", step,
                      lambda g, log: {"state": "done"},
                      repair_fn=_doctor())
    assert out["state"] == "done"
    assert any("root cause" in s and "galat tool" in s
               for s in seen_logs[1])


def test_judge_stuck_also_goes_to_the_doctor():
    verdicts = iter([{"state": "stuck", "note": "raasta band"},
                     {"state": "done", "note": ""}])
    out = run_mission("goal", lambda g, log, n: "koshish",
                      lambda g, log: next(verdicts),
                      repair_fn=_doctor())
    assert out["state"] == "done" and out["repairs_made"] == 1


def test_abort_ends_with_the_root_cause_named():
    def step(g, log, n):
        raise RuntimeError("hamesha crash")

    out = run_mission("goal", step,
                      lambda g, log: {"state": "continue"},
                      repair_fn=_doctor(
                          strategy="abort",
                          root="server hi band hai"))
    assert out["state"] == "stuck"
    assert "server hi band hai" in out["note"]


def test_repair_caps_hold_and_no_doctor_means_old_behaviour():
    def step(g, log, n):
        raise RuntimeError("hamesha crash")

    out = run_mission("goal", step,
                      lambda g, log: {"state": "continue"},
                      repair_fn=_doctor())
    assert out["state"] == "stuck" and out["repairs_made"] == 2
    out2 = run_mission("goal", step,
                       lambda g, log: {"state": "continue"})
    assert out2["state"] == "stuck" and out2["repairs_made"] == 0


def test_skip_abandons_the_step_and_walks_on():
    def step(g, log, n):
        if n == 1:
            raise RuntimeError("ye kadam hi kharab hai")
        return f"kadam {n} ho gaya"

    verdicts = iter([{"state": "done"}])
    out = run_mission("goal", step, lambda g, log: next(verdicts),
                      repair_fn=_doctor(strategy="skip"))
    assert out["state"] == "done" and out["repairs_made"] == 1


def test_doctor_parses_the_brains_json(tmp_path):
    class Chain:
        def complete(self, messages, tools=None):
            return {"content":
                    '{"error": "timeout", "origin": "web_fetch", '
                    '"root_cause": "site slow hai", "strategy": '
                    '"retry", "instruction": '
                    '"scrape wala raasta lo"}'}

    app = ChatApp(tmp_path, chain=Chain())
    fix = _mission_repair(app.parts, "goal", [], "kadam fail")
    assert fix["strategy"] == "retry"
    assert fix["root_cause"] == "site slow hai"
    assert "scrape" in fix["instruction"]
