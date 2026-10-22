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


# -- heard-truncation (Wave 1.6: record what was spoken) -------------------------

from jarvis.voice import _TranscriptBuffer


def test_buffer_heard_prefix_math():
    buf = _TranscriptBuffer()
    buf.add("The capital of Australia is Canberra, sir, and it is "
            "a planned city.")
    assert buf.heard(None) == ""                 # no measure: old rule
    buf.add("The capital of Australia is Canberra, sir, and it is "
            "a planned city.")
    assert buf.heard(0.0) == ""
    buf.add("The capital of Australia is Canberra, sir, and it is "
            "a planned city.")
    full = buf.heard(1.0)
    assert full.startswith("The capital") and full.endswith("city.")
    buf.add("The capital of Australia is Canberra, sir, and more.")
    part = buf.heard(0.75)
    assert part and "Canberra" in part
    assert not part.endswith(("is", "and", ","))  # word-boundary cut


def test_interrupt_logs_only_the_heard_prefix():
    sink = Sink()
    session = LiveSession(api_key="key", events=sink,
                          dispatch=lambda n, a: {"ok": True})
    session._out_buf.add("The capital of Australia is Canberra, "
                         "sir, and it is a planned city.")
    session.interrupt(heard_fraction=0.6)
    assert sink.interrupts == 1
    assert len(sink.transcripts) == 1
    role, text = sink.transcripts[0]
    assert role == "assistant"
    assert "Canberra" in text and "planned" not in text
    assert session._turns == [("assistant", text)]


def test_interrupt_without_a_measure_logs_nothing():
    sink = Sink()
    session = LiveSession(api_key="key", events=sink,
                          dispatch=lambda n, a: {"ok": True})
    session._out_buf.add("Something long that was cut early.")
    session.interrupt()
    assert sink.transcripts == []
    assert session._turns == []


def test_interrupt_probes_the_events_sink_for_the_fraction():
    sink = Sink()
    sink.heard_fraction = lambda: 0.3
    session = LiveSession(api_key="key", events=sink,
                          dispatch=lambda n, a: {"ok": True})
    session._out_buf.add("Alpha bravo charlie delta echo foxtrot "
                         "golf hotel india juliet.")
    session.interrupt()
    assert len(sink.transcripts) == 1
    assert "Alpha" in sink.transcripts[0][1]
    assert "juliet" not in sink.transcripts[0][1]


# -- structured responses, parallel exec, thinkingLevel (Wave 3) -----------------

def test_tool_responses_are_structured():
    sink = Sink()
    session = LiveSession(
        api_key="key", events=sink,
        dispatch=lambda n, a: {"ok": True, "value": 7})
    out = session._run_one_tool({"id": "1", "name": "time_now",
                                 "args": {}})
    assert out["response"]["status"] == "ok"
    assert out["response"]["retryable"] is False
    assert "7" in out["response"]["result"]

    failing = LiveSession(
        api_key="key", events=sink,
        dispatch=lambda n, a: {"error": "rate limit 429, try again"})
    out2 = failing._run_one_tool({"id": "2", "name": "web_search",
                                  "args": {}})
    assert out2["response"]["status"] == "error"
    assert out2["response"]["retryable"] is True
    assert out2["response"]["message"]


def test_parallel_batch_runs_together_unless_destructive():
    import threading as _t
    order = []
    lock = _t.Lock()

    def dispatch(name, args):
        with lock:
            order.append(name)
        return {"ok": True}

    sink = Sink()
    session = LiveSession(api_key="key", events=sink,
                          dispatch=dispatch,
                          destructive_tools=frozenset({"wipe"}))
    sent = []
    session._send = lambda payload: sent.append(payload)
    session._handle_tool_call({"functionCalls": [
        {"id": "1", "name": "read_a", "args": {}},
        {"id": "2", "name": "read_b", "args": {}}]})
    responses = sent[0]["toolResponse"]["functionResponses"]
    assert [r["name"] for r in responses] == ["read_a", "read_b"]
    assert sorted(order) == ["read_a", "read_b"]


def test_thinking_level_rides_the_setup():
    setup = build_setup(model="m", system_instruction="x",
                        thinking_level="high")["setup"]
    assert setup["thinkingConfig"] == {"thinkingLevel": "high"}
    setup2 = build_setup(model="m", system_instruction="x")["setup"]
    assert "thinkingConfig" not in setup2
