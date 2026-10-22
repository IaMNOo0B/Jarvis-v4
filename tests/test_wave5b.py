"""Wave 5, second slice: proposed rules stay proposals until
the owner accepts one into the ONE rules file; follow-ups come
due on their date; push-to-talk persists in config; the brief
counts what waits."""

import time
from pathlib import Path

from jarvis import candidates
from jarvis.chat import ChatApp
from jarvis.tracker import add as track_add_row
from jarvis.tracker import due_followups


def test_proposal_lifecycle_and_owner_acceptance(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert "propose_rule" in names
    out = app.execute_tool("propose_rule", {
        "text": "Shaam 7 ke baad notifications band rakho",
        "why": "malik shaam ko aaram karta hai"})
    cid = out["candidate_id"]
    waiting = candidates.pending(tmp_path)
    assert len(waiting) == 1
    # The rules file is untouched until the OWNER accepts.
    rules_copy = tmp_path / "JARVIS-RULES.txt"
    rules_copy.write_text(
        "# RULES\n\n## TAREEKA\n- pehla rule\n\n## AWAAZ\n"
        "- dheere bolo\n", encoding="utf-8")
    app.rules_source = str(rules_copy)
    listing = app.handle_command("/rules candidates")
    assert cid in listing
    reply = app.handle_command(f"/rules accept {cid}")
    assert "lag gaya" in reply
    body = rules_copy.read_text(encoding="utf-8")
    assert "Shaam 7 ke baad" in body
    assert body.index("Shaam 7") < body.index("## AWAAZ")
    assert candidates.pending(tmp_path) == []
    brief = app.handle_command("/brief")
    assert "rule proposal" not in brief   # nothing waiting now


def test_followups_come_due_on_their_date(tmp_path):
    past = time.time() - 3600
    track_add_row(tmp_path, "leads", {
        "name": "Purana Lead", "status": "open",
        "follow_up_due": past})
    band = track_add_row(tmp_path, "leads", {
        "name": "Band Lead", "follow_up_due": past})
    from jarvis.tracker import set_status
    set_status(tmp_path, "leads", band["row"]["id"], "replied")
    track_add_row(tmp_path, "leads", {
        "name": "Kal Wala", "status": "open",
        "follow_up_due": time.time() + 86400})
    due = due_followups(tmp_path)
    assert [d["label"] for d in due] == ["Purana Lead"]
    app = ChatApp(tmp_path)
    out = app.execute_tool("followups_due", {})
    assert out["ok"] is True and len(out["due"]) == 1


def test_push_to_talk_setting_roundtrips(tmp_path):
    app = ChatApp(tmp_path)
    assert app.push_to_talk() is False
    out = app.set_push_to_talk(True)
    assert out["ok"] is True
    assert app.push_to_talk() is True
    fresh = ChatApp(tmp_path)
    assert fresh.push_to_talk() is True    # config persisted
