"""The local voice pipeline — Mark-LVI's second engine, adapted.

Theirs impersonates the Live session behind the same interface so
the rest of the app cannot tell which engine is speaking; v4 keeps
that contract exactly (start/stop/send_audio/send_text/interrupt +
the VoiceEvents sink). The differences that matter:

- Turn-taking is a hand-rolled energy VAD with the repo's numbers:
  the noise floor tracks down fast and up slow; speech is RMS >=
  max(floor x 3.5, 0.006); 250 ms of speech opens a turn, 700 ms of
  silence closes it; the last 0.3 s before speech rides along so
  the first syllable is never clipped; a turn caps at 30 s.
- STT is faster-whisper, CPU int8 BY DEFAULT as a stated promise
  (their words), language detected per utterance. The transcript
  goes to the app's OWN chat brain (ChatApp.turn) — one brain, one
  memory; the pipeline deliberately emits no on_transcript of its
  own, because turn() already records the exchange.
- TTS is the OS voice (Windows SAPI via PowerShell) resampled to
  the speaker's 24 kHz before queueing — a rate mismatch presents
  as a bad voice, not an error, so the conversion is not optional.
- warmup() loads the whisper model on a thread at start: their
  measured cold first sentence was 8.9 s against 1.2 s warm.
"""

from __future__ import annotations

import importlib.util
import queue
import struct
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import Callable, Optional

from .audio_io import SPEAKER_RATE, chunk_pcm, resample_pcm
from .voice import VoiceEvents

BLOCK_MS = 64                    # 1024 samples @ 16 kHz
SPEECH_START_MS = 250
SILENCE_END_MS = 700
PRE_SPEECH_MS = 300
MAX_TURN_MS = 30_000
MIN_SPEECH_RMS = 0.006
FLOOR_FACTOR = 3.5


class LocalVoiceUnavailable(Exception):
    pass


def _rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if not n:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm[:n * 2])
    return (sum(s * s for s in samples) / n) ** 0.5 / 32768.0


class EnergyVAD:
    """Block-by-block turn segmenter. feed() returns the finished
    utterance's PCM exactly once, at the silence that ends it."""

    def __init__(self):
        self.floor = 0.002
        self._pre: list = []
        self._turn: Optional[list] = None
        self._speech_ms = 0
        self._silence_ms = 0
        self._turn_ms = 0

    def feed(self, pcm: bytes) -> Optional[bytes]:
        level = _rms(pcm)
        is_speech = level >= max(self.floor * FLOOR_FACTOR,
                                 MIN_SPEECH_RMS)
        if not is_speech:
            # Floor tracks down fast, up slow.
            if level < self.floor:
                self.floor = max(level, 0.0005)
            else:
                self.floor += 0.01 * (level - self.floor)
        if self._turn is None:
            self._pre.append(pcm)
            keep = max(1, PRE_SPEECH_MS // BLOCK_MS)
            self._pre = self._pre[-keep:]
            if is_speech:
                self._speech_ms += BLOCK_MS
                if self._speech_ms >= SPEECH_START_MS:
                    self._turn = list(self._pre)
                    self._turn_ms = self._speech_ms
                    self._silence_ms = 0
            else:
                self._speech_ms = 0
            return None
        self._turn.append(pcm)
        self._turn_ms += BLOCK_MS
        if is_speech:
            self._silence_ms = 0
        else:
            self._silence_ms += BLOCK_MS
        if self._silence_ms >= SILENCE_END_MS \
                or self._turn_ms >= MAX_TURN_MS:
            utterance = b"".join(self._turn)
            self._turn = None
            self._speech_ms = 0
            self._silence_ms = 0
            self._turn_ms = 0
            return utterance
        return None


class WhisperSTT:
    """faster-whisper, loaded lazily; numpy arrives with the
    package, so audio goes in as an array — never a file, so no
    ffmpeg anywhere (the repo's rule)."""

    def __init__(self, model: str = "base", device: str = "cpu",
                 model_factory: Optional[Callable] = None):
        if model_factory is None and \
                importlib.util.find_spec("faster_whisper") is None:
            raise LocalVoiceUnavailable(
                "faster-whisper is not installed — the local voice "
                "needs it (setup.bat installs the voice pack), or "
                "use the live voice with a Gemini key.")
        self._model_name = model
        self._device = device
        self._factory = model_factory
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    if self._factory is not None:
                        self._model = self._factory()
                    else:
                        from faster_whisper import WhisperModel
                        compute = "int8" if self._device == "cpu" \
                            else "float16"
                        self._model = WhisperModel(
                            self._model_name, device=self._device,
                            compute_type=compute)
        return self._model

    def warmup(self):
        threading.Thread(target=self._load_safely, daemon=True,
                         name="jarvis-stt-warmup").start()

    def _load_safely(self):
        try:
            self._load()
        except Exception:
            pass

    def transcribe(self, pcm: bytes, rate: int = 16000):
        """Returns (text, detected_language)."""
        import numpy as np
        model = self._load()
        audio = np.frombuffer(pcm, dtype=np.int16) \
            .astype(np.float32) / 32768.0
        segments, info = model.transcribe(audio, language=None)
        text = "".join(seg.text for seg in segments).strip()
        return text, getattr(info, "language", "") or ""


class SystemTTS:
    """Zero-install OS voice. Windows: SAPI5 through PowerShell to
    a WAV, normalised by _wav_to_pcm24k. Other platforms answer
    honestly rather than pretending to speak."""

    def __init__(self, runner: Optional[Callable] = None):
        self._runner = runner or subprocess.run

    def speak(self, text: str) -> bytes:
        import platform
        if platform.system() != "Windows":
            raise LocalVoiceUnavailable(
                "The local voice's speech is Windows SAPI — this "
                "machine is not Windows. The live voice (Gemini) "
                "works anywhere.")
        safe = text.replace("'", "''")
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "speech.wav")
            script = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis."
                "SpeechSynthesizer; "
                f"$s.SetOutputToWaveFile('{out}'); "
                f"$s.Speak('{safe}'); $s.Dispose()")
            self._runner(["powershell", "-NoProfile", "-Command",
                          script], check=True, timeout=60,
                         capture_output=True)
            return _wav_to_pcm24k(out)


def _wav_to_pcm24k(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise LocalVoiceUnavailable(
            f"Unexpected WAV sample width {width}; expected 16-bit.")
    if channels == 2:
        samples = struct.unpack(f"<{len(frames) // 2}h", frames)
        mono = bytes()
        mono_samples = [int((samples[i] + samples[i + 1]) / 2)
                        for i in range(0, len(samples), 2)]
        frames = struct.pack(f"<{len(mono_samples)}h", *mono_samples)
    elif channels != 1:
        raise LocalVoiceUnavailable(
            f"Unexpected WAV channel count {channels}.")
    return resample_pcm(frames, rate, SPEAKER_RATE)


class LocalPipelineSession:
    """The Live-session contract over local parts. `respond` is
    the app's chat turn: STT text in, spoken reply out."""

    def __init__(self, events: Optional[VoiceEvents] = None, *,
                 stt: Optional[WhisperSTT] = None,
                 tts: Optional[SystemTTS] = None,
                 respond: Optional[Callable[[str], str]] = None,
                 vad: Optional[EnergyVAD] = None):
        self.events = events or VoiceEvents()
        self.stt = stt
        self.tts = tts
        self.respond = respond
        self.vad = vad or EnergyVAD()
        self.connected = False
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._drop_pending = threading.Event()

    def start(self):
        if self.stt is None or self.tts is None or self.respond is None:
            raise LocalVoiceUnavailable(
                "The local voice is missing a part (STT, TTS or the "
                "chat brain).")
        self._stop.clear()
        self.connected = True
        self.stt.warmup()
        self._worker = threading.Thread(target=self._loop,
                                        daemon=True,
                                        name="jarvis-local-voice")
        self._worker.start()
        self.events.on_session(True, "local")

    def stop(self):
        self._stop.set()
        self.connected = False
        self._queue.put(None)

    def send_audio(self, pcm: bytes):
        utterance = self.vad.feed(pcm)
        if utterance is not None:
            self._queue.put(("audio", utterance))

    def send_text(self, text: str):
        self._queue.put(("text", text))

    def interrupt(self):
        self._drop_pending.set()
        self.events.on_interrupted()

    def _loop(self):
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            self._drop_pending.clear()
            kind, payload = item
            try:
                if kind == "audio":
                    text, _lang = self.stt.transcribe(payload)
                else:
                    text = str(payload)
                if not text:
                    continue
                reply = self.respond(text)
                if self._drop_pending.is_set() or not reply:
                    continue        # cut: the answer is never spoken
                pcm = self.tts.speak(str(reply))
                if self._drop_pending.is_set():
                    continue
                for piece in chunk_pcm(pcm, ms=50,
                                       rate=SPEAKER_RATE):
                    self.events.on_audio(piece)
                self.events.on_turn_audio_end()
            except Exception:
                # A failed local turn must not kill the pipeline;
                # the chat surface still shows what happened via
                # the brain's own journal.
                continue
