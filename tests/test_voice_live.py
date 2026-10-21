"""Gemini Live voice session — the Mark-LVI blueprint, tested
against a scripted fake WebSocket (no network, no audio hardware):
setup shape, transcription flushing with the repeat-tail guard,
50 ms audio slicing, tool calls mid-conversation, the hard-drain
interrupt, resumption, and the two-rung ladder policy."""
import base64
import json
import socket
import threading
import time

import pytest

from jarvis.live_ws import WSClosed
from jarvis.voice import (LIVE_MODELS, LiveSession, VoiceEvents,
                          build_setup, live_model, note_live_failure)


class FakeWS:
    def __init__(self, script):
        self.script = list(script)
        self.sent = []
        self.closed = False

    def set_recv_timeout(self, _s):
        pass

    def send_text(self, text):
        self.sent.append(json.loads(text))

    def recv(self):
        if self.closed:
            raise WSClosed(1000, "test close")
        if not self.script:
            raise socket.timeout()
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return 1, json.dumps(item).encode()

    def close(self):
        self.closed = True


class Sink(VoiceEvents):
    def __init__(self):
        self.audio = []
        self.transcripts = []
        self.sessions = []
        self.audio_ends = 0
        self.interrupts = 0

    def on_audio(self, pcm):
        self.audio.append(pcm)

    def on_turn_audio_end(self):
        self.audio_ends += 1

    def on_transcript(self, role, text):
        self.transcripts.append((role, text))

    def on_session(self, connected, detail=""):
        self.sessions.append((connected, detail))

    def on_interrupted(self):
        self.interrupts += 1


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def make_session(script, **kw):
    sink = Sink()
    sockets = []

    def factory(_url):
        ws = FakeWS(script if not sockets else [])
        sockets.append(ws)
        return ws

    session = LiveSession(api_key="AIzaTEST",
                          dispatch=kw.pop("dispatch",
                                            lambda n, a: {"ok": True}),
                          events=sink, ws_factory=factory, **kw)
    return session, sink, sockets


# -- setup & ladder policy ---------------------------------------------------

def test_setup_shape_and_voice_validation():
    setup = build_setup(
        model="models/x", system_instruction="Be JARVIS.",
        tools=[{"name": "open_app",
                "schema": {"type": "object",
                           "properties": {"n": {"type": "string"}}}}],
        voice="NotAVoice", resume_handle="h-9", proactive=True)
    s = setup["setup"]
    assert s["model"] == "models/x"
    assert s["generationConfig"]["responseModalities"] == ["AUDIO"]
    voice = s["generationConfig"]["speechConfig"]["voiceConfig"]
    assert voice["prebuiltVoiceConfig"]["voiceName"] == "Charon"
    decl = s["tools"][0]["functionDeclarations"][0]
    assert decl["parameters"]["type"] == "OBJECT"
    assert s["sessionResumption"]["handle"] == "h-9"
    assert s["proactivity"] == {"proactiveAudio": True}
    assert s["systemInstruction"]["parts"][0]["text"] == "Be JARVIS."


def test_ladder_only_steps_down_on_quota_or_gone():
    model = "models/test-rung-never-used-elsewhere"
    assert note_live_failure(model, "network unreachable") is False
    assert note_live_failure(model, "503 unavailable") is False
    assert note_live_failure(model, "429 RESOURCE_EXHAUSTED") is True
    assert live_model((model, "models/other")) == "models/other"
    gone = "models/test-gone-rung"
    assert note_live_failure(gone, "404 not found") is True


# -- session flow ---------------------------------------------------------------

def test_audio_slicing_and_transcript_flush():
    pcm = bytes(range(256)) * 12          # 3072 bytes
    b64 = base64.b64encode(pcm).decode()
    script = [
        {"setupComplete": {}},
        {"serverContent": {
            "inputTranscription": {"text": "hello "},
            "modelTurn": {"parts": [
                {"inlineData": {"mimeType": "audio/pcm;rate=24000",
                                "data": b64}}]}}},
        {"serverContent": {
            "inputTranscription": {"text": "hello "},   # repeat tail
            "outputTranscription": {"text": "Hi sir."}}},
        {"serverContent": {
            "inputTranscription": {"text": "world"},
            "turnComplete": True}},
    ]
    session, sink, sockets = make_session(script)
    session.start()
    try:
        assert wait_for(lambda: len(sink.transcripts) == 2)
        assert sink.transcripts[0] == ("user", "hello world")
        assert sink.transcripts[1] == ("assistant", "Hi sir.")
        sizes = [len(s) for s in sink.audio]
        assert sizes == [2400, 672]       # ~50 ms slices
        assert sink.sessions and sink.sessions[0][0] is True
        sent0 = sockets[0].sent[0]
        assert sent0["setup"]["model"] == LIVE_MODELS[0]
    finally:
        session.stop()


def test_tool_call_runs_through_dispatch_and_answers():
    seen = []

    def dispatch(name, args):
        seen.append((name, args))
        return {"opened": "chrome"}

    script = [
        {"setupComplete": {}},
        {"toolCall": {"functionCalls": [
            {"id": "c1", "name": "open_app",
             "args": {"name": "chrome"}}]}},
    ]
    session, sink, sockets = make_session(script,
                                          dispatch=dispatch)
    session.start()
    try:
        assert wait_for(lambda: len(sockets[0].sent) >= 2)
        assert seen == [("open_app", {"name": "chrome"})]
        resp = sockets[0].sent[1]["toolResponse"]["functionResponses"]
        assert resp[0]["name"] == "open_app"
        assert "chrome" in resp[0]["response"]["result"]
    finally:
        session.stop()


def test_interrupt_discards_audio_and_the_cut_answer():
    pcm = base64.b64encode(b"x" * 4800).decode()
    audio_part = {"inlineData": {"data": pcm}}
    script = [
        {"setupComplete": {}},
        {"serverContent": {
            "modelTurn": {"parts": [audio_part]},
            "outputTranscription": {"text": "a long answer"}}},
        {"serverContent": {
            "modelTurn": {"parts": [audio_part]},
            "turnComplete": True}},
    ]

    class CutSink(Sink):
        session_ref = None

        def on_audio(self, pcm_bytes):
            super().on_audio(pcm_bytes)
            if len(self.audio) == 1 and self.session_ref:
                self.session_ref.interrupt()

    sink = CutSink()
    sockets = []

    def factory(_url):
        ws = FakeWS(script if not sockets else [])
        sockets.append(ws)
        return ws

    session = LiveSession(api_key="k",
                          dispatch=lambda n, a: "ok", events=sink,
                          ws_factory=factory)
    sink.session_ref = session
    session.start()
    try:
        assert wait_for(lambda: sink.audio_ends >= 1)
        assert len(sink.audio) == 2       # only the first turn's slices
        assert sink.transcripts == []     # cut answer never logged
        assert sink.interrupts == 1
    finally:
        session.stop()


def test_resumption_handle_is_carried_to_the_next_connect():
    script = [
        {"setupComplete": {}},
        {"sessionResumptionUpdate": {"resumable": True,
                                     "newHandle": "handle-42"}},
        WSClosed(1006, "dropped"),
    ]
    session, sink, sockets = make_session(script)
    session.start()
    try:
        assert wait_for(lambda: len(sockets) >= 2)
        setup2 = sockets[1].sent[0]["setup"]
        assert setup2["sessionResumption"]["handle"] == "handle-42"
    finally:
        session.stop()


def test_send_text_shape():
    session, sink, sockets = make_session([{"setupComplete": {}}])
    session.start()
    try:
        assert wait_for(lambda: len(sockets) == 1)
        session.send_text("good morning")
        assert wait_for(lambda: len(sockets[0].sent) >= 2)
        msg = sockets[0].sent[1]["clientContent"]
        assert msg["turnComplete"] is True
        assert msg["turns"][0]["parts"][0]["text"] == "good morning"
    finally:
        session.stop()
