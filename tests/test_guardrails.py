"""Guardrails (ECC Hookify, JARVIS-bound): owner-stated
prohibitions are MINED into candidates that enforce
nothing; only an accepted rule stops a call — in dispatch,
before the gate, with the rule's id on the denial."""

from jarvis.guardrails import GuardrailBook, mine_guardrail


# -- mining -----------------------------------------------------------

def test_miner_catches_hinglish_prohibition():
    found = mine_guardrail("kabhi meri photo delete mat karna")
    assert found is not None
    tool, match, form = found
    assert tool == "*" and match == "photo" and form == "deny"


def test_miner_catches_english_prohibition():
    found = mine_guardrail(
        "never send an email without asking me first")
    assert found is not None
    assert found[1] == "email"


def test_miner_catches_always_first_form():
    found = mine_guardrail("hamesha pehle backup lena")
    assert found is not None
    assert found[2] == "hamesha-pehle"
    assert found[1]


def test_miner_ignores_ordinary_turns():
    assert mine_guardrail("aaj mausam kaisa hai") is None
    assert mine_guardrail("ek email bhejo mere dost ko") is None
    assert mine_guardrail("photo dikhao meri") is None
    assert mine_guardrail("") is None


# -- the book -----------------------------------------------------------

def test_candidate_never_enforces_until_accepted(tmp_path):
    book = GuardrailBook(tmp_path)
    row, created = book.add_candidate(
        "*", "photo", "kabhi meri photo delete mat karna")
    assert created
    assert book.match_for("write_file",
                          {"path": "photo.jpg"}) is None
    book.accept(row["id"])
    hit = book.match_for("write_file", {"path": "photo.jpg"})
    assert hit is not None and hit["id"] == row["id"]
    # A call without the match sails through.
    assert book.match_for("write_file",
                          {"path": "notes.txt"}) is None


def test_candidate_dedupes_and_drop_removes(tmp_path):
    book = GuardrailBook(tmp_path)
    row, created = book.add_candidate("*", "photo", "ek")
    assert created
    again, created = book.add_candidate("*", "photo", "do")
    assert not created and again["id"] == row["id"]
    assert book.drop(row["id"]) is True
    assert book.list() == []
    assert book.drop(row["id"]) is False


def test_tool_scoped_rule_only_fires_for_that_tool(tmp_path):
    book = GuardrailBook(tmp_path)
    row, _ = book.add_candidate("email_send", "boss",
                                "boss ko kabhi mat bhejna")
    book.accept(row["id"])
    assert book.match_for("email_send",
                          {"to": "boss@x.com"}) is not None
    assert book.match_for("write_file",
                          {"path": "boss.txt"}) is None


# -- dispatch + app wiring ----------------------------------------------

class _ScriptedChain:
    def __init__(self, script=()):
        self.script = list(script)

    def complete(self, messages, tools=None):
        if self.script:
            return self.script.pop(0)
        return {"content": "Understood, sir."}


def test_turn_mines_candidate_then_accept_enforces(tmp_path):
    from jarvis.chat import ChatApp

    app = ChatApp(tmp_path)
    app.chain = _ScriptedChain()
    app.brain.chain = _ScriptedChain()
    reply = app.turn("kabhi meri photo delete mat karna")
    assert "candidate" in reply
    rows = app.guardrails.list()
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    assert rows[0]["match"] == "photo"
    gid = rows[0]["id"]

    # Pending: the same call is NOT stopped.
    res = app.execute_tool("time_now", {"note": "photo file"})
    assert not res.get("guardrail_id")

    out = app.handle_command(f"/guardrails accept {gid}")
    assert "laagu" in out
    res = app.execute_tool("time_now", {"note": "photo file"})
    assert res["ok"] is False
    assert res["guardrail_id"] == gid
    assert res["gate_denied"] is True

    out = app.handle_command(f"/guardrails drop {gid}")
    assert "hata diya" in out
    res = app.execute_tool("time_now", {"note": "photo file"})
    assert not res.get("guardrail_id")


def test_guardrails_command_lists_states(tmp_path):
    from jarvis.chat import ChatApp

    app = ChatApp(tmp_path)
    assert "Koi guardrail nahi" in app.handle_command(
        "/guardrails")
    row, _ = app.guardrails.add_candidate(
        "*", "email", "never send email")
    out = app.handle_command("/guardrails")
    assert "candidate" in out and row["id"] in out
    app.guardrails.accept(row["id"])
    out = app.handle_command("/guardrails")
    assert "LAAGU" in out
