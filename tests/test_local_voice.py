"""Local voice pipeline — the energy VAD's turn-taking, the
whisper/SAPI adapters' honest edges, and a full local turn over
fakes (STT -> chat brain -> TTS -> sliced audio out)."""
import json
import math
import struct
import time
import wave

import pytest

from jarvis.local_voice import (EnergyVAD, LocalPipelineSession,
                                LocalVoiceUnavailable, SystemTTS,
                                WhisperSTT, _wav_to_pcm24k)
from jarvis.voice import VoiceEvents


def block(freq=440, amp=0.5, rate=16000, samples=1024):
    return struct.pack(
        f"<{samples}h",
        *[int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))
          for i in range(samples)])


SILENCE = b"\x01\x00" * 1024


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# -- VAD turn-taking -------------------------------------------------------------

def test_vad_opens_and_closes_a_turn_with_the_repos_numbers():
    vad = EnergyVAD()
    for _ in range(5):
        assert vad.feed(SILENCE) is None
    for _ in range(4):                     # 256 ms of speech: turn opens
        assert vad.feed(block()) is None
    utterance = None
    for _ in range(11):                    # 704 ms of silence: turn closes
        out = vad.feed(SILENCE)
        if out is not None:
            utterance = out
    assert utterance is not None
    # Pre-speech ride-along + speech + trailing silence all present.
    assert len(utterance) > 4 * 2048
    # And it does not fire again without new speech.
    assert vad.feed(SILENCE) is None


def test_vad_ignores_a_blip():
    vad = EnergyVAD()
    assert vad.feed(block()) is None       # 64 ms < 250 ms start rule
    for _ in range(15):
        assert vad.feed(SILENCE) is None


# -- adapters ----------------------------------------------------------------------

def test_whisper_unavailable_is_honest_without_the_package():
    if __import__("importlib").util.find_spec("faster_whisper"):
        pytest.skip("faster-whisper installed here")
    with pytest.raises(LocalVoiceUnavailable):
        WhisperSTT()


def test_system_tts_refuses_non_windows_honestly():
    import platform
    if platform.system() == "Windows":
        pytest.skip("on Windows it would really speak")
    with pytest.raises(LocalVoiceUnavailable):
        SystemTTS().speak("hello")


def test_wav_normalises_to_24k_mono(tmp_path):
    path = str(tmp_path / "t.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(block(rate=22050, samples=22050))
    out = _wav_to_pcm24k(path)
    assert abs(len(out) - 48000) <= 8      # 1 s at 24 kHz int16


# -- the pipeline turn ---------------------------------------------------------------

class Sink(VoiceEvents):
    def __init__(self):
        self.audio = []
        self.ends = 0
        self.sessions = []
        self.interrupts = 0

    def on_audio(self, pcm):
        self.audio.append(pcm)

    def on_turn_audio_end(self):
        self.ends += 1

    def on_session(self, connected, detail=""):
        self.sessions.append((connected, detail))

    def on_interrupted(self):
        self.interrupts += 1


class FakeSTT:
    def warmup(self):
        pass

    def transcribe(self, pcm, rate=16000):
        return "open chrome", "en"


class FakeTTS:
    def speak(self, text):
        return block(rate=24000, samples=4800)   # 0.2 s at 24 kHz


def test_local_turn_end_to_end():
    sink = Sink()
    heard = []
    session = LocalPipelineSession(
        sink, stt=FakeSTT(), tts=FakeTTS(),
        respond=lambda text: heard.append(text) or "Opening Chrome.")
    session.start()
    try:
        assert sink.sessions == [(True, "local")]
        for _ in range(4):
            session.send_audio(block())
        for _ in range(11):
            session.send_audio(SILENCE)
        assert wait_for(lambda: sink.ends == 1)
        assert heard == ["open chrome"]
        assert sink.audio and all(len(s) <= 2400 for s in sink.audio)
        session.interrupt()
        assert sink.interrupts == 1
    finally:
        session.stop()


def test_app_local_engine_without_whisper_says_so(tmp_path,
                                                  monkeypatch):
    import jarvis.voice_manager as vm_mod
    from jarvis.chat import ChatApp
    from tests.test_voice_app import FakeIO

    if __import__("importlib").util.find_spec("faster_whisper"):
        pytest.skip("faster-whisper installed here")
    (tmp_path / "config.json").write_text(json.dumps(
        {"voice": {"engine": "local"}}))
    monkeypatch.setattr(vm_mod, "SoundDeviceIO", lambda **kw: FakeIO())
    app = ChatApp(tmp_path)
    msg = app.handle_command("/voice")
    assert "faster-whisper" in msg
    assert app.voice_status()["active"] is False


# -- the ACK race (Dodaios Ack-Brain / spoken preambles) -------------------------

class RecordingTTS:
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)
        return block(rate=24000, samples=4800)


def test_ack_suppressed_when_the_reply_is_fast():
    tts = RecordingTTS()
    session = LocalPipelineSession(
        Sink(), tts=tts, respond=lambda text: "Four.",
        ack_delay=0.5)
    assert session._respond_with_ack("2+2?") == "Four."
    assert tts.spoken == []            # no ACK: the gate suppressed it


def test_ack_spoken_when_the_reply_is_slow():
    tts = RecordingTTS()

    def slow(text):
        time.sleep(0.3)
        return "Done, sir."

    session = LocalPipelineSession(
        Sink(), tts=tts, respond=slow, ack_delay=0.05)
    assert session._respond_with_ack("do the long thing") == "Done, sir."
    assert tts.spoken == ["On it, sir."]     # exactly one ACK, first in rotation


def test_ack_disabled_at_zero_delay():
    tts = RecordingTTS()

    def slow(text):
        time.sleep(0.1)
        return "Done."

    session = LocalPipelineSession(
        Sink(), tts=tts, respond=slow, ack_delay=0)
    assert session._respond_with_ack("x") == "Done."
    assert tts.spoken == []


# -- speech cleaning + sentence streaming (Wave 1.5) ------------------------------

from jarvis.local_voice import clean_for_speech, split_sentences


def test_clean_for_speech_strips_what_ears_cannot_use():
    raw = ("**Done**, sir — see [the report](https://x.example/r) "
           "or https://y.example/raw for `details`.\n"
           "```python\nprint('never read this aloud')\n```\n"
           "| A | B |\nAll good 👍")
    out = clean_for_speech(raw)
    assert "http" not in out
    assert "print" not in out
    assert "the report" in out
    assert "**" not in out and "`" not in out
    assert "|" not in out
    assert "👍" not in out


def test_split_sentences_guards_decimals_and_abbreviations():
    assert split_sentences("It is 3.5 km away. Dr. Rao agrees! "
                           "Shall we go?") == [
        "It is 3.5 km away.", "Dr. Rao agrees!", "Shall we go?"]
    assert split_sentences("One only") == ["One only"]
    assert split_sentences("") == []


def test_local_turn_streams_sentence_by_sentence():
    sink = Sink()
    tts = RecordingTTS()
    session = LocalPipelineSession(
        sink, stt=FakeSTT(), tts=tts,
        respond=lambda text: "First part here. Second part follows!",
        ack_delay=0)
    session.start()
    try:
        for _ in range(4):
            session.send_audio(block())
        for _ in range(11):
            session.send_audio(SILENCE)
        assert wait_for(lambda: sink.ends == 1)
        assert tts.spoken == ["First part here.",
                              "Second part follows!"]
    finally:
        session.stop()
