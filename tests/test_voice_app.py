"""Voice wiring — VoiceManager gates/playback over fake audio, and
the ChatApp surface (/voice, transcripts joining history, the UI's
/api/voice + /api/state). No hardware, no network."""
import json
import math
import struct
import threading
import time
import urllib.request

import pytest

from jarvis.voice_manager import VoiceManager


def tone(freq, seconds=0.064, rate=16000, amp=0.5):
    n = int(rate * seconds)
    return struct.pack(
        f"<{n}h",
        *[int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))
          for i in range(n)])


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeIO:
    output_latency = 0.9

    def __init__(self):
        self.writes = []
        self.mic_cb = None
        self.closed = False

    def start_output(self):
        pass

    def start_input(self, cb):
        self.mic_cb = cb

    def write(self, pcm):
        self.writes.append(pcm)

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, events=None, **kw):
        # The app factory passes LiveSession's kwargs (api_key,
        # dispatch, tools…); the manager passes its events sink.
        self.events = events if events is not None \
            else kw.get("events")
        self.connected = True
        self.audio_in = []
        self.started = False
        self.stopped = False
        self.interrupt_calls = 0

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def send_audio(self, pcm):
        self.audio_in.append(pcm)

    def send_text(self, text):
        pass

    def interrupt(self):
        self.interrupt_calls += 1
        self.events.on_interrupted()


class FakeActivity:
    def __init__(self):
        self.state = "standby"
        self.tool = None

    def set(self, state, tool=None):
        self.state = state
        self.tool = tool


def make_manager(**kw):
    io = FakeIO()
    holder = {}

    def factory(events):
        holder["session"] = FakeSession(events)
        return holder["session"]

    manager = VoiceManager(
        session_factory=factory,
        io_factory=lambda cfg: io,
        activity=kw.pop("activity", FakeActivity()),
        on_transcript=kw.pop("on_transcript", None),
        notify=kw.pop("notify", None),
        config=kw.pop("config", {}))
    return manager, io, holder


# -- manager gates ----------------------------------------------------------------

def test_mic_streams_when_awake_and_idle():
    manager, io, holder = make_manager()
    ok, _msg = manager.start()
    assert ok
    io.mic_cb(tone(440))
    assert len(holder["session"].audio_in) == 1
    manager.stop()
    assert holder["session"].stopped and io.closed


def test_mic_never_streams_while_speaking():
    manager, io, holder = make_manager()
    manager.start()
    events = holder["session"].events
    events.on_audio(tone(440, rate=24000))
    assert wait_for(lambda: manager.speaking)
    io.mic_cb(tone(440))                    # would be our own echo
    assert holder["session"].audio_in == []
    events.on_turn_audio_end()
    assert wait_for(lambda: not manager.speaking)
    manager.stop()


def test_echo_tail_drops_echo_and_passes_a_real_voice():
    manager, io, holder = make_manager()
    manager.start()
    events = holder["session"].events
    said = tone(440, rate=24000)
    events.on_audio(said)
    assert wait_for(lambda: manager.speaking)
    events.on_turn_audio_end()
    assert wait_for(lambda: not manager.speaking)
    io.mic_cb(tone(440))                    # the tail of our own voice
    assert holder["session"].audio_in == []
    io.mic_cb(tone(2000, amp=0.9))          # a real voice ends the tail
    assert len(holder["session"].audio_in) == 1
    manager.stop()


def test_interrupt_drains_the_play_queue():
    manager, io, holder = make_manager()
    manager.start()
    events = holder["session"].events
    for _ in range(5):
        events.on_audio(tone(440, rate=24000))
    assert wait_for(lambda: manager.speaking)
    manager.interrupt()
    assert holder["session"].interrupt_calls == 1
    assert manager.speaking is False
    written = len(io.writes)
    time.sleep(0.3)
    assert len(io.writes) <= written + 1    # at most the in-flight batch
    manager.stop()


def test_mute_and_status():
    manager, io, holder = make_manager()
    manager.start()
    assert manager.toggle_mute() is True
    io.mic_cb(tone(440))
    assert holder["session"].audio_in == []
    status = manager.status()
    assert status["active"] and status["muted"] and status["awake"]
    manager.stop()


def test_wake_word_unavailable_degrades_honestly():
    notes = []
    manager, io, holder = make_manager(
        notify=notes.append, config={"wake_word": True})
    ok, _ = manager.start()
    assert ok                                # voice still works…
    assert any("Wake word unavailable" in n for n in notes)
    assert manager.status()["wake_word"] is False   # …but ungated
    io.mic_cb(tone(440))
    assert len(holder["session"].audio_in) == 1
    manager.stop()


# -- app + UI wiring ---------------------------------------------------------------

@pytest.fixture()
def voice_app(tmp_path, monkeypatch):
    import jarvis.chat as chat_mod
    import jarvis.voice as voice_mod
    import jarvis.voice_manager as vm_mod

    monkeypatch.setattr(vm_mod, "SoundDeviceIO", lambda **kw: FakeIO())
    monkeypatch.setattr(voice_mod, "LiveSession", FakeSession)
    app = chat_mod.ChatApp(tmp_path)
    app.handle_command("/key " + "AIza" + "x" * 35)
    return app


def test_app_voice_commands_and_transcript_history(voice_app):
    app = voice_app
    assert app.handle_command("/voice") == "Voice is on, sir."
    assert app.handle_command("/voice") == \
        "Voice is already on, sir."
    status = app.voice_status()
    assert status["active"] is True
    session = app.voice_manager.session
    session.events.on_transcript("user", "jarvis, open chrome")
    session.events.on_transcript("assistant", "Opening Chrome, sir.")
    assert app.history[-2] == {"role": "user",
                               "content": "jarvis, open chrome"}
    assert app.history[-1]["role"] == "assistant"
    assert app.handle_command("/voice mute") == \
        "Microphone muted, sir."
    assert app.handle_command("/voice off") == "Voice is off, sir."
    assert app.voice_status()["active"] is False


def test_app_voice_without_a_key_says_so(voice_app):
    import jarvis.chat as chat_mod
    app = chat_mod.ChatApp(voice_app.data_dir.parent / "nokey")
    msg = app.handle_command("/voice")
    assert "Gemini key" in msg and "/key" in msg


def test_ui_voice_endpoints(voice_app):
    from jarvis.ui import build_server
    app = voice_app
    server = build_server(app, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state") as r:
            state = json.loads(r.read())
        assert state["voice"]["active"] is False
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/voice",
            data=json.dumps({"action": "toggle"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req) as r:
            out = json.loads(r.read())
        assert out["voice"]["active"] is True
        assert "Voice is on" in out["message"]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state") as r:
            state = json.loads(r.read())
        assert state["voice"]["active"] is True
    finally:
        app.stop_voice()
        server.shutdown()
