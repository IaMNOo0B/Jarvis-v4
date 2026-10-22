"""Stop-Everything: a persisted, model-free halt (Sentient's kill
switch). Turns refuse, voice will not start, the flag survives a
restart — and /resume always works because commands never route
through the gated turn path."""

from jarvis.chat import ChatApp, _is_stopped


def test_stop_refuses_turns_and_resume_restores(tmp_path):
    app = ChatApp(tmp_path)
    assert "band kar diya" in app.handle_command("/stop")
    assert _is_stopped(tmp_path) is True
    assert "Band hoon" in app.turn("hello, kuch kaam hai")
    assert "hazir" in app.handle_command("/resume")
    assert _is_stopped(tmp_path) is False


def test_stop_survives_a_restart(tmp_path):
    app = ChatApp(tmp_path)
    app.handle_command("/stop")
    app2 = ChatApp(tmp_path)          # a fresh app over the same data
    assert "Band hoon" in app2.turn("kuch bhi")
    assert "Voice" not in app2.start_voice()
    assert "Band hoon" in app2.start_voice()
