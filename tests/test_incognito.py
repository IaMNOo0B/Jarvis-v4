"""Incognito mode (Sesame's pattern): the agent may READ prior
memory, but the current session is never written to the permanent
record — no history, no memory notes, no lessons, no golden pairs.
Journal entries keep the audit chain intact with content redacted."""

from jarvis.chat import ChatApp


class _Chain:
    def __init__(self, script=()):
        self.script = list(script)

    def complete(self, messages, tools=None):
        if self.script:
            return self.script.pop(0)
        return {"content": "Understood, sir."}


def _app(tmp_path):
    app = ChatApp(tmp_path)
    app.chain = _Chain()
    app.brain.chain = _Chain()
    return app


def test_toggle_replies(tmp_path):
    app = _app(tmp_path)
    assert "Incognito on" in app.handle_command("/incognito")
    assert app.incognito is True
    assert "Incognito off" in app.handle_command("/incognito off")
    assert app.incognito is False


def test_incognito_turn_writes_nothing_permanent(tmp_path):
    app = _app(tmp_path)
    app.turn("Hello, sir here.")                    # a normal turn
    assert len(app.history) == 2
    app.handle_command("/incognito")
    reply = app.turn("Tell me a secret plan.")
    assert reply                                     # it still answers
    assert len(app.history) == 2                     # history untouched
    assert len(app._incognito_history) == 2          # session-local only
    entries = [e for e in app.journal.replay()
               if e["kind"] in ("user.message", "assistant.reply")]
    assert entries[-2]["payload"]["text"] == "[incognito]"
    assert entries[-1]["payload"]["text"] == "[incognito]"
    assert not (tmp_path / "golden_pairs.jsonl").exists()


def test_incognito_learn_refusal_and_recovery(tmp_path):
    app = _app(tmp_path)
    app.handle_command("/incognito")
    reply = app.turn("yaad rakho mera pin number 4321 hai")
    assert "incognito" in reply.lower()
    assert app.memory.facts.current_facts() == []    # nothing stored
    app.handle_command("/incognito off")
    assert app._incognito_history == []              # discarded for good
    reply = app.turn("yaad rakho mera sheher Pune hai")
    assert "Yaad rakh liya" in reply
    assert len(app.memory.facts.current_facts()) == 1
    assert all("4321" not in str(m.get("content"))
               for m in app.history)                 # secret never landed


def test_incognito_voice_transcript_is_session_local(tmp_path):
    app = _app(tmp_path)
    app.handle_command("/incognito")
    app._voice_transcript("user", "private baat")
    assert app.history == []
    assert len(app._incognito_history) == 1
    entries = [e for e in app.journal.replay()
               if e["kind"] == "voice.turn"]
    assert entries[-1]["payload"]["text"] == "[incognito]"
