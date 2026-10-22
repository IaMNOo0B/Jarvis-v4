"""The autonomy core (the owner's order: sab automatic,
dimag khud decide kare — kaunsi cheez, kab, kitni — aur
probability calibrate karke): every dispatch outcome is
learned into the Calibrator under its domain; autonomous
automation runs pass the act/verify/ask gate; the turn's
effort budget is sized by the brain itself; run_shell
gives JARVIS a real terminal; and the Linux pack is in
its knowledge."""

from jarvis.chat import ChatApp, _effort_for


def test_dispatch_outcomes_are_learned(tmp_path):
    app = ChatApp(tmp_path)
    assert app.calibrator.stats("core.time")["resolved"] == 0
    out = app.execute_tool("time_now", {})
    assert "iso" in out  # a bare payload, no "ok" key — the
    # learner must still read it as success (via _result_ok)
    stats = app.calibrator.stats("core.time")
    assert stats["resolved"] == 1 and stats["accuracy"] == 1.0
    out = app.execute_tool("run_code",
                           {"code": "raise SystemExit(3)"})
    assert out["ok"] is False
    stats = app.calibrator.stats("core.code")
    assert stats["resolved"] == 1 and stats["accuracy"] == 0.0


def test_gate_decisions_are_never_learned(tmp_path):
    app = ChatApp(tmp_path)
    out = app.execute_tool("process_kill", {"target": "x.exe"})
    assert out.get("needs_approval")
    assert app.calibrator.stats("core.pc")["resolved"] == 0


def test_effort_budget_sizes_itself():
    assert _effort_for("salam, kya haal hai") == ("light", 2, 8)
    level, rounds, calls = _effort_for(
        "pehle repo download karo, phir uske tests chalao, "
        "aur phir jo errors aayein unhe ek ek karke theek "
        "karke poori report banao aur sab files check karo")
    assert (level, rounds, calls) == ("heavy", 5, 20)
    assert _effort_for("mujhe batao kal ka mausam kaisa "
                       "rahega aur meri meeting kab hai, dono "
                       "check karke batao")[0] == "normal"


def test_automation_gate_asks_when_reliability_is_low(tmp_path):
    app = ChatApp(tmp_path)
    out = app.execute_tool("create_automation", {
        "name": "waqt-batao", "title": "Waqt batao route",
        "steps": [{"tool": "time_now", "args": {}}],
        "params": []})
    assert out["ok"] is True
    # A PROVEN-healthy domain self-runs: seed its record into
    # the act band first. (An unproven domain sits in the
    # verify band since the batch-7 fix — the brain runs the
    # route via run_automation and watches, it does not
    # self-run mechanically.)
    for _ in range(4):
        row = app.calibrator.record("core.automations",
                                    "route chalega", 0.7)
        app.calibrator.resolve(row["id"], True)
    reply, hint = app._automation_pre_brain("waqt batao route")
    assert reply is not None and hint is None
    # Poison the domain's lived record: 8 resolved failures.
    for _ in range(8):
        row = app.calibrator.record("core.automations",
                                    "route chalega", 0.7)
        app.calibrator.resolve(row["id"], False)
    reply, hint = app._automation_pre_brain("waqt batao route")
    assert reply is None
    assert hint is not None and "reliability kam hai" in hint


def test_run_shell_is_a_real_terminal(tmp_path):
    app = ChatApp(tmp_path)
    out = app.execute_tool("run_shell",
                           {"command": "echo namaste-shell"})
    assert out["ok"] is True and out["exit_code"] == 0
    assert "namaste-shell" in out["stdout"]
    out = app.execute_tool("run_shell", {"command": "exit 3"})
    assert out["ok"] is False and out["exit_code"] == 3


def test_linux_pack_is_in_the_knowledge(tmp_path):
    app = ChatApp(tmp_path)
    out = app.execute_tool(
        "knowledge_search",
        {"query": "chmod permissions tar archive shell"})
    text = str(out)
    assert "chmod" in text and "tar" in text
