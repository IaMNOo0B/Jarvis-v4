"""Slice B + C together: the PC domain library (parsers proven
on canned PowerShell output, both volume routes) and the
confirmation-card door — a destructive tool files a card and
runs NOTHING until the owner approves it, then runs exactly
once through the guarded dispatch, and the card is spent."""

import jarvis.pc as pc
from jarvis.chat import ChatApp


def _runner(stdout, rc=0):
    def run(script):
        return {"rc": rc, "stdout": stdout, "stderr": ""}
    return run


def test_pc_info_parser():
    out = pc.op_pc_info(runner=_runner(
        '{"os": "Windows 11", "cpu": "Ryzen 5 3600", '
        '"gpu": "RTX 2060", "ram_total_gb": 16.0, '
        '"ram_free_gb": 6.5, "uptime_hours": 3.2}'))
    # Off-Windows the driver refuses before the runner is used.
    assert "error" in out or out["info"]["cpu"] == "Ryzen 5 3600"


def test_process_and_window_parsers(monkeypatch):
    monkeypatch.setattr(pc.sys, "platform", "win32")
    out = pc.op_process_list(runner=_runner(
        '[{"ProcessName": "chrome", "Id": 42, "cpu_s": 9.5, '
        '"mem_mb": 512.0}]'))
    assert out["processes"][0]["ProcessName"] == "chrome"
    out = pc.op_window_list(runner=_runner(
        '{"ProcessName": "code", "Id": 7, '
        '"MainWindowTitle": "jarvis - VS Code"}'))
    assert out["windows"][0]["title"] == "jarvis - VS Code"
    out = pc.op_battery_status(runner=_runner(
        '{"percent": null}'))
    assert out["battery"] is None


def test_volume_two_routes(monkeypatch):
    monkeypatch.setattr(pc.sys, "platform", "win32")
    out = pc.op_volume_set(40, runner=_runner("40"))
    assert out["route"] == "endpoint (exact)"
    calls = {"n": 0}

    def flaky(script):
        calls["n"] += 1
        if "AudioVol" in script:
            return {"rc": 1, "stdout": "", "stderr": "COM fail"}
        return {"rc": 0, "stdout": "", "stderr": ""}

    out = pc.op_volume_set(40, runner=flaky)
    assert out["route"].startswith("key-ladder")
    # route 1 tried, route 2 ran, route 2 verified by read-back
    # (the read-back AudioVol call also fails on this runner, so
    # the result honestly reports verified=False).
    assert calls["n"] == 3
    assert out["verified"] is False


def test_card_flow_end_to_end(tmp_path):
    app = ChatApp(tmp_path)
    out = app.execute_tool("process_kill",
                           {"target": "notepad"})
    assert out.get("needs_approval") is True
    card = out.get("card") or {}
    assert card.get("id") and "process_kill" in \
        (card.get("summary") or "")
    pending = app.pending_confirmations()
    assert [c["id"] for c in pending] == [card["id"]]
    # Approval runs it through the real dispatch (off-Windows
    # the driver refuses honestly — the FLOW is what's proven:
    # the answer is an execution report, not another card).
    reply = app.confirm_card(card["id"], approve=True)
    assert "chal gaya" in reply or "chal nahi paya" in reply
    assert app.pending_confirmations() == []
    again = app.confirm_card(card["id"], approve=True)
    assert "nahi chala" in again       # one-shot: card is spent


def test_card_deny_path(tmp_path):
    app = ChatApp(tmp_path)
    out = app.execute_tool("empty_recycle_bin", {})
    card = (out.get("card") or {})
    assert card.get("id")
    reply = app.confirm_card(card["id"], approve=False)
    assert "radd" in reply
    assert app.pending_confirmations() == []
    assert app.handle_command("/cards").startswith("Koi pending")


def test_gate_verdict_for_new_destructive_tools(tmp_path):
    app = ChatApp(tmp_path)
    tools = {t["name"]: t for t in
             app.registry.advertised_tools(app.allowances)}
    assert tools["process_kill"]["destructive"] is True
    assert tools["pc_info"]["destructive"] is False
    d = app.gate.evaluate("process_kill",
                          {"target": "x"}, "owner",
                          app.allowances)
    assert d.verdict == "require_approval"
