"""Dictation Polisher (Sentient Spec 8): deterministic
tidy always (fillers out, spacing/punctuation fixed),
and a local-model polish for longer utterances that is
admitted ONLY when every non-filler input word survives
in order and no new word appears. Voice never blocks:
any model failure falls back to the tidy — and Roman
Hinglish passes through with its words intact."""

import math
import struct
import time

from jarvis import dictation
from jarvis.local_voice import LocalPipelineSession, VoiceEvents


def _block(freq=440, amp=0.5, rate=16000, samples=1024):
    return struct.pack(
        f"<{samples}h",
        *[int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))
          for i in range(samples)])


_SILENCE = b"\x01\x00" * 1024


def test_tidy_removes_fillers_and_fixes_spacing():
    assert dictation.tidy("um kal  mujhe , uh jaana hai") \
        == "Kal mujhe, jaana hai"
    assert dictation.tidy("hello . world") == "Hello. world"


def test_short_utterance_never_waits_for_a_model():
    calls = []
    out = dictation.polish(
        "um haan theek", brain_call=lambda p: calls.append(p))
    assert out == "Haan theek"
    assert calls == []


def test_model_polish_admitted_when_words_preserved():
    out = dictation.polish(
        "um kal mujhe daftar jaana hai",
        brain_call=lambda p: "Kal mujhe daftar jaana hai.")
    assert out == "Kal mujhe daftar jaana hai."


def test_model_that_drops_or_adds_words_is_rejected():
    dropped = dictation.polish(
        "kal mujhe daftar jaana hai",
        brain_call=lambda p: "Kal mujhe jaana hai.")
    assert dropped == "Kal mujhe daftar jaana hai"
    added = dictation.polish(
        "kal mujhe daftar jaana hai",
        brain_call=lambda p: "Kal mujhe daftar jaana hai "
                             "please.")
    assert added == "Kal mujhe daftar jaana hai"
    translated = dictation.polish(
        "kal mujhe daftar jaana hai",
        brain_call=lambda p: "Tomorrow I have to go to "
                             "the office.")
    assert translated == "Kal mujhe daftar jaana hai"


def test_roman_hinglish_passes_through():
    out = dictation.polish(
        "uh sir kal wali meeting cancel kar do",
        brain_call=lambda p: "Sir kal wali meeting cancel "
                             "kar do.")
    assert out == "Sir kal wali meeting cancel kar do."


def test_model_crash_falls_back_to_tidy():
    def boom(prompt):
        raise RuntimeError("ollama down")

    out = dictation.polish("um kal mujhe jaana hai zaroor",
                           brain_call=boom)
    assert out == "Kal mujhe jaana hai zaroor"


class _STT:
    def warmup(self):
        pass

    def transcribe(self, pcm):
        return ("um kal mujhe jaana hai zaroor", "hi")


class _TTS:
    def speak(self, text):
        return b"\x00\x00" * 100


def test_session_polishes_audio_but_not_typed_text():
    heard = []

    def respond(text):
        heard.append(text)
        return "Ho gaya, sir."

    session = LocalPipelineSession(
        VoiceEvents(), stt=_STT(), tts=_TTS(),
        respond=respond, ack_delay=0,
        polish=lambda t: dictation.tidy(t))
    session.start()
    try:
        for _ in range(4):              # speech: turn opens
            session.send_audio(_block())
        for _ in range(11):             # silence: turn closes
            session.send_audio(_SILENCE)
        deadline = time.time() + 3
        while not heard and time.time() < deadline:
            time.sleep(0.05)
        assert heard == ["Kal mujhe jaana hai zaroor"]
        session.send_text("um typed bilkul aisa hi rakho")
        deadline = time.time() + 3
        while len(heard) < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert heard[1] == "um typed bilkul aisa hi rakho"
    finally:
        session.stop()
