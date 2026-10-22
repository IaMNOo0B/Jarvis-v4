"""Keys serve the NEXT turn — no restart (the owner filed a
key, kept talking, and met the stub, because the chain was
built once at startup). Plus the owner's idle rule: the
state word earns Standby only after 120s of silence."""

from jarvis.chat import Activity, ChatApp


def _provider_names(app):
    inner = getattr(app.chain, "inner", None)
    providers = getattr(inner, "providers", []) or []
    return [getattr(p, "name", "") for p in providers]


def test_set_provider_rebuilds_the_chain_live(tmp_path):
    app = ChatApp(tmp_path)
    assert "gemini" not in _provider_names(app)
    out = app.set_provider("gemini", "AIzaSy" + "x" * 30)
    assert out["ok"] is True
    assert "restart nahi" in out["note"]
    assert "gemini" in _provider_names(app)
    # The brain and every parts reader see the same new chain.
    assert app.brain.chain is app.chain
    assert app.parts.chain is app.chain


def test_key_command_rebuilds_too(tmp_path):
    app = ChatApp(tmp_path)
    reply = app.handle_command("/key gemini " + "AIzaSy" + "y" * 30)
    assert "restart nahi chahiye" in reply
    assert "gemini" in _provider_names(app)


def test_injected_chain_is_never_rebuilt(tmp_path):
    class FakeChain:
        def complete(self, messages, tools=None):
            return {"content": "fake", "tool_calls": []}

    fake = FakeChain()
    app = ChatApp(tmp_path, chain=fake)
    assert app._rebuild_chain() is False
    assert app.chain.inner is fake


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_standby_is_earned_by_two_minutes_of_silence():
    clock = _Clock()
    act = Activity(clock=clock)
    assert act.state == "standby"
    act.set("listening")
    assert act.state == "listening"
    clock.now += 119
    assert act.state == "listening"
    clock.now += 2
    assert act.state == "standby"
    # A fresh input revives the word at once.
    act.set("thinking")
    assert act.state == "thinking"
    # An explicit standby (a real stop) lands immediately.
    act.set("standby")
    assert act.state == "standby"
