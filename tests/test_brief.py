"""Daily Brief — the fixed-structure digest (Top of mind / FYI /
Looking ahead), assembled deterministically from the app's own
reminders, notifications and facts. No model call, so nothing in a
brief can be invented."""

import json
from datetime import datetime, timedelta, timezone

from jarvis.brief import build_brief
from jarvis.chat import ChatApp

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)


def _seed(data):
    past = (NOW - timedelta(hours=2)).isoformat()
    soon = (NOW + timedelta(hours=3)).isoformat()
    later = (NOW + timedelta(days=3)).isoformat()
    (data / "reminders.json").write_text(json.dumps([
        {"id": "1", "text": "Doctor ko call karna hai", "due": past,
         "done": False},
        {"id": "2", "text": "Team meeting", "due": soon, "done": False},
        {"id": "3", "text": "Door ki cheez", "due": later, "done": False},
        {"id": "4", "text": "Ho gaya kaam", "due": past, "done": True},
    ]), encoding="utf-8")
    (data / "notifications.jsonl").write_text(
        json.dumps({"id": "n1", "ts": NOW.timestamp(), "title": "Upgrade",
                    "body": "Ek nayi skill mili", "kind": "upgrade",
                    "read": False}) + "\n" +
        json.dumps({"id": "n2", "ts": NOW.timestamp() - 3600,
                    "title": "Purani khabar", "body": "",
                    "kind": "info", "read": True}) + "\n",
        encoding="utf-8")


def test_brief_sections_and_contents(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _seed(data)
    facts = [{"claim": "The owner lives in Pune",
              "ts": NOW.isoformat()}]
    brief = build_brief(data, facts=facts, now=NOW)
    assert any("Doctor ko call" in x for x in brief["top_of_mind"])
    assert any("Upgrade" in x for x in brief["top_of_mind"])
    assert any("Team meeting" in x for x in brief["looking_ahead"])
    assert all("Door ki cheez" not in x
               for x in brief["looking_ahead"])     # beyond 24h: out
    assert all("Ho gaya" not in x
               for x in brief["top_of_mind"])       # done: out
    assert any("Purani khabar" in x for x in brief["fyi"])
    assert any("Pune" in x for x in brief["fyi"])
    text = brief["text"]
    assert text.index("Top of mind") < text.index("FYI") \
        < text.index("Looking ahead")


def test_brief_empty_state_is_honest(tmp_path):
    brief = build_brief(tmp_path / "data", now=NOW)
    assert brief["top_of_mind"] == []
    assert brief["text"].count("Nothing there.") == 3


def test_app_brief_command_and_tool(tmp_path):
    app = ChatApp(tmp_path)
    out = app.handle_command("/brief")
    assert "Top of mind" in out and "Looking ahead" in out
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert "daily_brief" in names
    result = app.execute_tool("daily_brief", {})
    assert result.get("ok") is True
    assert "brief" in result
