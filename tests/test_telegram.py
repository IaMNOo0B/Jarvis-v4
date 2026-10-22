"""Telegram channel: pairing takes the code or silence, only the
bound chat is served, the token is never echoed, and the send
tool is honest until everything is in place."""

from jarvis.chat import ChatApp
from jarvis.telegram import TelegramBot, TelegramChannel


class FakeTransport:
    def __init__(self, updates):
        self.updates = updates
        self.sent = []

    def __call__(self, token, method, params):
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return {"result": out}
        if method == "sendMessage":
            self.sent.append((params["chat_id"], params["text"]))
            return {"ok": True}
        return {"ok": True}


def _update(uid, chat_id, text, name="Owais"):
    return {"update_id": uid,
            "message": {"chat": {"id": chat_id, "first_name": name},
                        "text": text}}


def _channel(tmp_path, transport, code="482916"):
    return TelegramChannel(
        TelegramBot("tok", transport=transport), tmp_path,
        handle=lambda t: f"jawab: {t}",
        pair_code=lambda: code)


def test_pairing_binds_only_with_the_code(tmp_path):
    transport = FakeTransport([
        _update(1, 777, "hello"),            # no code -> silence
        _update(2, 777, "482916"),           # the code -> binds
        _update(3, 999, "482916"),           # too late, and not his
        _update(4, 777, "waqt kya hai"),     # bound chat is served
        _update(5, 999, "mujhe bhi batao"),  # stranger: silence
    ])
    channel = _channel(tmp_path, transport)
    handled = channel.poll_once()
    texts = [text for _, text in transport.sent]
    assert any("Jud gaya" in t for t in texts)
    assert any("jawab: waqt kya hai" in t for t in texts)
    assert not any("mujhe bhi" in t for t in texts)
    assert handled == 2


def test_app_commands_and_tool_honesty(tmp_path):
    app = ChatApp(tmp_path)
    out = app.execute_tool("telegram_send", {"text": "hi"})
    assert out["ok"] is False and "token" in out["error"]
    reply = app.handle_command("/telegram token 123456:ABC-secret")
    assert "set ho gaya" in reply
    assert "ABC-secret" not in reply       # never echoed
    status = app.handle_command("/telegram")
    assert "set hai" in status and "juda nahi" in status
    pair = app.handle_command("/telegram pair")
    code = app._telegram_pair[0]
    assert code in pair
    out2 = app.execute_tool("telegram_send", {"text": "hi"})
    assert out2["ok"] is False and "pair" in out2["error"]


def test_real_handle_adapter_speaks_both_roads(tmp_path):
    """Regression (found by the 2026-10-22 full scan): turn()
    returns a plain string — the adapter once called .get() on
    it, which would have killed every non-slash phone reply
    while slash commands kept working."""
    from jarvis.chat import _telegram_handle
    from jarvis.providers import FallbackChain, StubProvider
    app = ChatApp(tmp_path,
                  chain=FallbackChain([StubProvider()]))
    reply = _telegram_handle(app, "what time is it right now?")
    assert isinstance(reply, str) and ":" in reply
    status = _telegram_handle(app, "/telegram")
    assert "Telegram" in status
