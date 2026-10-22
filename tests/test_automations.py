"""Automations: a repeated job is composed ONCE into a saved
route of real tool steps, then run on command through the
same dispatch everything else uses — gate, ledger, and
confirmation cards included. A card pauses the route; the
run resumes from that step after approval."""

from jarvis.automations import (AutomationBook, placeholders_in,
                                render, run_steps, validate_steps)
from jarvis.chat import ChatApp


def test_placeholders_and_render():
    args = {"text": "lead {{name}} ko {{count}} clips",
            "nested": {"x": ["{{name}}"]}}
    assert placeholders_in(args) == {"name", "count"}
    out = render(args, {"name": "Ravi", "count": 3})
    assert out["text"] == "lead Ravi ko 3 clips"
    assert out["nested"]["x"] == ["Ravi"]


def test_book_rules(tmp_path):
    book = AutomationBook(tmp_path)
    row, err = book.create("Lead Scrape", "leads",
                           [{"tool": "web_search",
                             "args": {"query": "{{topic}}"}}],
                           ["topic"])
    assert err is None and row["name"] == "lead-scrape"
    dup, err = book.create("lead-scrape", "x",
                           [{"tool": "time_now", "args": {}}], [])
    assert dup is None and "pehle se hai" in err
    bad, err = book.create("bad", "x",
                           [{"tool": "web_search",
                             "args": {"query": "{{gone}}"}}], [])
    assert bad is None and "elaan nahi" in err
    assert validate_steps([{"tool": "nope", "args": {}}],
                            {"time_now"}) is not None
    assert validate_steps([], {"time_now"}) is not None


def test_create_and_run_end_to_end(tmp_path):
    app = ChatApp(tmp_path)
    out = app.execute_tool("create_automation", {
        "name": "subah-check", "title": "Subah ki check",
        "steps": [
            {"tool": "time_now", "args": {}},
            {"tool": "track_add",
             "args": {"table": "auto-log",
                      "record": {"note": "{{msg}}"}}}],
        "params": ["msg"]})
    assert out["ok"] is True
    run = app.execute_tool("run_automation",
                           {"name": "subah-check",
                            "params": {"msg": "pehli daud"}})
    assert run["status"] == "done" and len(run["completed"]) == 2
    listed = app.execute_tool("list_automations", {})
    row = listed["automations"][0]
    assert row["runs"] == 1 and row["last_status"] == "done"
    missing = app.execute_tool("run_automation",
                               {"name": "subah-check",
                                "params": {}})
    assert missing["status"] == "missing_params"


def test_route_pauses_on_a_card_and_resumes(tmp_path):
    app = ChatApp(tmp_path)
    out = app.execute_tool("create_automation", {
        "name": "temiz", "title": "Safai",
        "steps": [
            {"tool": "time_now", "args": {}},
            {"tool": "process_kill",
             "args": {"target": "notepad"}}]})
    assert out["ok"] is True
    run = app.execute_tool("run_automation", {"name": "temiz"})
    assert run["status"] == "waiting_approval"
    assert run["stopped_at_step"] == 2
    assert run["card"]["id"]
    # Approve the card: the kill step runs through the card
    # door (off-Windows the driver refuses honestly) ...
    reply = app.confirm_card(run["card"]["id"], approve=True)
    assert "chal gaya" in reply or "chal nahi paya" in reply
    # ... and the route itself can be re-run from that step.
    again = app.execute_tool(
        "run_automation", {"name": "temiz", "start_step": 1})
    assert again["status"] in ("waiting_approval", "done",
                               "failed")


def test_delete_rides_a_card(tmp_path):
    app = ChatApp(tmp_path)
    app.execute_tool("create_automation", {
        "name": "mitne-wala", "title": "x",
        "steps": [{"tool": "time_now", "args": {}}]})
    out = app.execute_tool("delete_automation",
                           {"name": "mitne-wala"})
    assert out.get("needs_approval") is True
    reply = app.confirm_card(out["card"]["id"], approve=True)
    assert "Ho gaya" in reply
    assert app.execute_tool("list_automations",
                            {})["automations"] == []
