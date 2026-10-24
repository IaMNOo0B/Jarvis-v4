"""Voice honesty + voice speaking (2026-10-24): the brain had no
way to check voice state, so it improvised "text-only" excuses;
and chat replies were never spoken, so voice-on looked like
"listens but never speaks". Audio itself cannot be heard on
this VM — the tests prove the wiring with fakes, never the
sound."""

import time

from jarvis.chat import ChatApp
from jarvis.prompt import PERSONA
from jarvis.voice_manager import VoiceManager


class _FakeTTS:
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)
        return b"\x00\x00" * 2400  # 50 ms of silence at 24 kHz


def test_persona_names_voice_and_forbids_text_only_excuse():
    assert "voice_status" in PERSONA
    assert "text-only" in PERSONA


def test_voice_status_tool_is_advertised_and_reports_off(tmp_path):
    app = ChatApp(tmp_path)
    names = [t["name"] for t in
             app.parts.registry.advertised_tools(app.allowances)]
    assert "voice_status" in names
    out = app.execute_tool("voice_status")
    assert out.get("active") is False


def test_speak_text_queues_audio_and_ends_turn():
    manager = VoiceManager(session_factory=lambda events: None)
    manager.active = True
    manager.io = object()  # only checked for None by speak_text
    tts = _FakeTTS()
    assert manager.speak_text("Hello sir.", tts=tts) is True
    assert tts.spoken == ["Hello sir."]
    assert not manager._play_q.empty()
    assert manager._turn_end is True


def test_speak_text_refuses_when_voice_off():
    manager = VoiceManager(session_factory=lambda events: None)
    assert manager.speak_text("Hello sir.", tts=_FakeTTS()) is False


def test_speak_chat_reply_guards(tmp_path):
    app = ChatApp(tmp_path)
    assert app.speak_chat_reply("hi") is False  # no manager at all

    class _FakeManager:
        def __init__(self):
            self.active = True
            self.session = None
            self.spoken = []

        def speak_text(self, text):
            self.spoken.append(text)
            return True

    fake = _FakeManager()
    app.voice_manager = fake
    assert app.speak_chat_reply("  ") is False  # empty reply
    assert app.speak_chat_reply("Hello sir.") is True
    for _ in range(100):
        if fake.spoken:
            break
        time.sleep(0.01)
    assert fake.spoken == ["Hello sir."]

    # A live session owns speech: chat replies must not double it.
    from jarvis.voice import LiveSession
    fake.session = object.__new__(LiveSession)
    assert app.speak_chat_reply("Hello again.") is False
