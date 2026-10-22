"""Batch-8 audit regressions: voice manager lifecycle, PTT,
heard-truncation, wake/UI truthfulness (2026-10-22)."""

import json
import re
import threading
import urllib.request

import pytest

from jarvis import ui as ui_mod
from jarvis.chat import ChatApp
from jarvis.overlay import buddy_text
from jarvis.ui import build_server
from jarvis.voice import LiveSession, VoiceEvents
from jarvis.voice_manager import VoiceManager
from jarvis.wake import WakeGate


# -- chat voice_status ---------------------------------------------------

def test_voice_status_no_manager_is_not_awake(tmp_path):
    app = ChatApp(tmp_path)
    app.voice_manager = None
    status = app.voice_status()
    assert status["active"] is False
    assert status["awake"] is False


# -- manager lifecycle -----------------------------------------------------

class FakeIO:
    def __init__(self):
        self.closed = False
        self.output_latency = 0.0
        self.mic_cb = None

    def start_output(self):
        pass

    def start_input(self, cb):
        self.mic_cb = cb

    def write(self, pcm):
        pass

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, events):
        self.events = events
        self.audio_in = []
        self.stopped = False
        self.connected = True

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def send_audio(self, pcm):
        self.audio_in.append(pcm)

    def send_text(self, text):
        pass

    def interrupt(self):
        pass


def _manager(session_factory=None, config=None):
    io = FakeIO()
    holder = {}

    def factory(events):
        session = (session_factory or FakeSession)(events)
        holder["session"] = session
        return session

    manager = VoiceManager(session_factory=factory,
                           io_factory=lambda cfg: io,
                           config=config or {})
    return manager, io, holder


def test_manager_stop_resets_speaking_and_drains():
    manager, io, holder = _manager()
    ok, _ = manager.start()
    assert ok
    manager.on_audio(b"\x01\x00" * 4800)
    manager.speaking = True
    manager.level = 0.7
    manager.stop()
    assert manager.speaking is False
    assert manager.level == 0.0
    assert manager._play_q.empty()
    assert holder["session"].stopped and io.closed


def test_manager_start_tears_down_when_session_start_raises():
    class BadSession(FakeSession):
        def start(self):
            raise RuntimeError("ws connect fail")

    manager, io, holder = _manager(session_factory=BadSession)
    ok, message = manager.start()
    assert ok is False and "start nahi hua" in message
    assert manager.active is False
    assert manager.session is None
    assert io.closed


def test_ptt_hold_passes_asleep_gate():
    manager, io, holder = _manager(config={"wake_word": True})
    # No openwakeword on this VM: force the gate by hand.
    ok, _ = manager.start()
    assert ok
    manager.gate = WakeGate(enabled=True, detector=None)
    manager.gate.awake = False
    pcm = b"\x10\x00" * 1600
    io.mic_cb(pcm)
    assert holder["session"].audio_in == []     # asleep: held back
    manager.ptt_hold(True)
    io.mic_cb(pcm)
    assert len(holder["session"].audio_in) == 1  # held: streams
    manager.ptt_hold(False)
    io.mic_cb(pcm)
    assert len(holder["session"].audio_in) == 1  # released: gated
    manager.stop()


def test_wake_gate_stop_stops_detector():
    stopped = []

    class Detector:
        def stop(self):
            stopped.append(True)

    gate = WakeGate(enabled=True, detector=Detector())
    gate.stop()
    assert stopped == [True]


# -- LiveSession: deferred transcript commit -------------------------------

class Sink(VoiceEvents):
    def __init__(self, fraction=None):
        self.transcripts = []
        self.sessions = []
        self._fraction = fraction

    def on_transcript(self, role, text):
        self.transcripts.append((role, text))

    def on_session(self, connected, detail=""):
        self.sessions.append((connected, detail))

    def on_interrupted(self):
        pass

    def heard_fraction(self):
        return self._fraction


def _session(sink):
    return LiveSession(api_key="k", dispatch=lambda n, a: {},
                       events=sink, ws_factory=None)


def _say(session, text):
    session._handle_server_content({
        "outputTranscription": {"text": text},
        "turnComplete": True})


def test_transcript_commits_only_when_playback_drains():
    sink = Sink()
    session = _session(sink)
    _say(session, "Poora jawab jo abhi bola ja raha hai")
    # turnComplete alone commits NOTHING now.
    assert sink.transcripts == []
    session.on_playback_drained()
    assert sink.transcripts == [
        ("assistant", "Poora jawab jo abhi bola ja raha hai")]


def test_interrupt_after_turn_complete_cuts_to_heard():
    sink = Sink(fraction=0.6)
    session = _session(sink)
    _say(session, "pehla hissa dusra hissa")
    session.interrupt()
    # 60% of the chars, cut back to the word boundary: only
    # the first half was heard, and only IT is remembered.
    assert sink.transcripts == [("assistant", "pehla hissa")]


def test_send_audio_arms_stall_watchdog():
    sink = Sink()
    session = _session(sink)
    assert session._rx_deadline is None
    session.send_audio(b"\x00\x00" * 100)
    assert session._rx_deadline is not None


def test_interrupted_flag_clears_on_new_user_turn():
    sink = Sink()
    session = _session(sink)
    session._interrupted = True
    session._handle_server_content(
        {"inputTranscription": {"text": "ruk jao"}})
    assert session._interrupted is False


def test_classify_failure_recognizes_bad_key_shapes():
    sink = Sink()
    session = _session(sink)
    session._classify_failure("m", "HTTP 400: API_KEY_INVALID", 0.0)
    assert (False, "bad key") in sink.sessions
    assert session._stop.is_set()


# -- overlay -----------------------------------------------------------------

def test_buddy_follows_activity_not_voice_flags():
    line, _c, _s = buddy_text({
        "activity": "thinking",
        "voice": {"active": True, "speaking": False},
        "notifications": 0, "provider": ""})
    assert "Soch raha" in line
    line, _c, _s = buddy_text({
        "activity": "standby",
        "voice": {"active": True, "speaking": False},
        "notifications": 0, "provider": ""})
    assert "Standby" in line
    line, _c, _s = buddy_text({
        "activity": "listening",
        "voice": {"active": True, "speaking": True},
        "notifications": 0, "provider": ""})
    assert "Bol raha" in line


# -- UI page + endpoints -------------------------------------------------------

def test_page_js_fixed_sites():
    page = ui_mod.PAGE
    assert "Hello, sir. How may I assist you today?" not in page
    assert "sending: 'Sending…'" in page
    assert "writing: 'Writing…'" in page
    assert "addMsg(out.result" in page            # (text, who)
    assert "addMsg('jarvis', out" not in page
    assert "pttHold(true)" in page and "pttHold(false)" in page


@pytest.fixture()
def ui(tmp_path):
    app = ChatApp(tmp_path / "data")
    server = build_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever,
                              daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", app
    server.shutdown()


def _post_raw(url, data: bytes):
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_api_voice_malformed_json_is_400_not_toggle(ui):
    base, app = ui
    status, _body = _post_raw(base + "/api/voice", b"{not json")
    assert status == 400
    assert app.voice_status()["active"] is False   # NOT toggled on


def test_api_builds_proceed_defaults_to_deny(ui):
    base, app = ui
    status, body = _post_raw(
        base + "/api/builds/proceed",
        json.dumps({"id": "nope"}).encode())
    out = json.loads(body)
    assert out["ok"] is False    # unknown build, and no proceed


def test_api_chat_exception_becomes_json_error(ui):
    base, app = ui

    def boom(text):
        raise RuntimeError("brain melted")

    app.turn = boom
    status, body = _post_raw(
        base + "/api/chat", json.dumps({"text": "hi"}).encode())
    assert status == 500
    assert "brain melted" in json.loads(body)["error"]


def test_api_ptt_held_reaches_manager(ui):
    base, app = ui
    status, body = _post_raw(
        base + "/api/ptt", json.dumps({"held": True}).encode())
    # Voice is off in this app: the hold is refused honestly,
    # not silently accepted.
    assert status == 400
    assert json.loads(body)["ok"] is False
