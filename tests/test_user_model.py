"""User model: dialectic confidence, dispute -> question, and
the owner's own insights are never auto-changed."""

from jarvis.chat import ChatApp
from jarvis.user_model import UserModel


def test_support_and_contradict_move_confidence(tmp_path):
    model = UserModel(tmp_path)
    ins = model.observe("khana", "Owner ko filter coffee pasand hai",
                        "subah coffee pi thi")
    assert ins["confidence"] == 0.5 and ins["status"] == "active"
    ins = model.observe("khana", "Owner ko filter coffee pasand hai",
                        "phir coffee mangi", relation="support")
    assert ins["confidence"] == 0.6
    assert len(ins["evidence"]) == 2


def test_contradictions_dispute_and_queue_a_question(tmp_path):
    model = UserModel(tmp_path)
    model.observe("khana", "Owner ko filter coffee pasand hai", "e1")
    for i in range(3):
        ins = model.observe("khana",
                            "Owner ko filter coffee pasand hai",
                            f"chai pi thi {i}", relation="contradict")
    # 0.5 - 3*0.15 = 0.05 < 0.35: disputed, and a question waits
    assert ins["status"] == "disputed"
    questions = model.pending_questions()
    assert questions and "filter coffee" in questions[0]["question"]
    # disputed insights leave the prompt surface
    assert model.context_lines("filter coffee") == []


def test_owner_insights_are_never_auto_changed(tmp_path):
    model = UserModel(tmp_path)
    mine = model.observe("kaam", "Owner raat ko kaam karta hai",
                         "owner words", origin="owner")
    assert mine["confidence"] == 0.9
    after = model.observe("kaam", "Owner raat ko kaam karta hai",
                          "din me so raha tha", relation="contradict")
    assert after["confidence"] == 0.9 and after["status"] == "active"
    assert model.pending_questions()        # but a question IS queued


def test_app_wiring(tmp_path):
    app = ChatApp(tmp_path)
    out = app.handle_command(
        "/model add khana: mujhe filter coffee pasand hai")
    assert "Samajh me jod liya" in out
    listing = app.handle_command("/model")
    assert "filter coffee" in listing
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert "note_insight" in names
    lines = app.memory.excerpts("filter coffee pasand", k=5)
    assert any("Samajh" in line for line in lines)
