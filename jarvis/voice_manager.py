"""VoiceManager — mic, speaker, gates and the Live session, wired
the way Mark-LVI wires them (and for the reasons it learned):

- the mic callback applies the gates IN ORDER and does nothing
  else: muted -> wake gate (asleep audio never leaves the machine)
  -> speaking (while JARVIS speaks, the mic is NOT streamed — the
  model must never hear itself) -> echo tail (for a short window
  after each reply, a block passes only when EchoGuard calls it a
  real voice) -> the session.
- playback batches queued ~50 ms slices into <=200 ms writes off a
  dedicated thread, so interrupt() can bite within one slice; the
  speaking flag (and the UI's state word) follows the PLAY queue,
  not the network — the UI says "speaking" only while sound is
  actually coming out.
- session end arms the echo tail: measured output latency + 0.25 s.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

from .audio_io import SPEAKER_RATE, AudioUnavailable, SoundDeviceIO
from .echo import EchoGuard, pcm_level
from .voice import LiveSession, VoiceEvents
from .wake import OpenWakeWordDetector, WakeGate, WakeUnavailable

TAIL_MARGIN_S = 0.25
PLAY_BATCH_MAX = 9600          # ~200 ms of 24 kHz int16


class VoiceManager(VoiceEvents):
    def __init__(self, *, session_factory: Callable,
                 io_factory: Optional[Callable] = None,
                 activity=None,
                 on_transcript: Optional[Callable] = None,
                 notify: Optional[Callable[[str], None]] = None,
                 config: Optional[dict] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._session_factory = session_factory
        self._io_factory = io_factory or (
            lambda cfg: SoundDeviceIO(
                input_name=cfg.get("input_device"),
                output_name=cfg.get("output_device")))
        self.activity = activity
        self._on_transcript = on_transcript
        self._notify = notify or (lambda _t: None)
        self.config = dict(config or {})
        self._clock = clock

        self.session: Optional[LiveSession] = None
        self.io = None
        self.gate: Optional[WakeGate] = None
        self.guard: Optional[EchoGuard] = None
        self.muted = False
        self.speaking = False
        self.active = False
        self._play_q: queue.Queue = queue.Queue()
        self._turn_sent = 0        # bytes handed to playback this turn
        self._turn_played = 0      # bytes actually written to the speaker
        self.level = 0.0           # last measured PCM level (mic
                                   # while heard, speaker while
                                   # speaking) — the HUD dances to
                                   # the REAL voice, not a script
        self._turn_end = False
        self._tail_until = 0.0
        self._threads: list = []
        self._ptt_held = False     # push-to-talk hold: while
                                   # held, mic blocks pass the
                                   # wake gate regardless of its
                                   # sleep state

    # -- lifecycle ------------------------------------------------------------

    def start(self):
        """Returns (ok, message). Every failure is a plain sentence,
        never a half-started voice loop."""
        if self.active:
            return True, "Voice is already on, sir."
        cfg = self.config
        try:
            self.io = self._io_factory(cfg)
        except AudioUnavailable as e:
            return False, str(e)
        except Exception as e:
            return False, f"Audio could not start: {e}"
        # Wake gate (opt-in): a missing detector degrades honestly —
        # voice still works, ungated, and the owner is told.
        wake_on = bool(cfg.get("wake_word"))
        detector = None
        if wake_on:
            try:
                detector = OpenWakeWordDetector(
                    on_detect=lambda: self.gate and self.gate.wake(),
                    model=str(cfg.get("wake_model", "") or ""))
            except WakeUnavailable as e:
                self._notify(f"Wake word unavailable: {e}")
                wake_on = False
        self.gate = WakeGate(enabled=wake_on, detector=detector,
                             clock=self._clock)
        self.guard = EchoGuard(clock=self._clock)
        try:
            self.session = self._session_factory(self)
        except Exception as e:
            self._close_io()
            return False, str(e)
        try:
            self.io.start_output()
            self.io.start_input(self._mic_block)
        except Exception as e:
            self._close_io()
            return False, f"Audio could not start: {e}"
        self.active = True
        self._threads = [
            threading.Thread(target=self._play_loop, daemon=True,
                             name="jarvis-playback"),
            threading.Thread(target=self._watch_loop, daemon=True,
                             name="jarvis-voice-watch"),
        ]
        for t in self._threads:
            t.start()
        try:
            self.session.start()
        except Exception as e:
            # A session that fails to start must not leave
            # threads, io and a phantom "active" behind
            # (audit 2026-10-22: the raise escaped with
            # everything half-started and no handle).
            self.active = False
            try:
                self.session.stop()
            except Exception:
                pass
            self.session = None
            self._close_io()
            return False, f"Voice session start nahi hua: {e}"
        # Voice on = JARVIS is listening (for the wake word if
        # not awake, for speech if awake). Standby comes from
        # the Activity idle rule or from stop(), never early.
        self._set_activity("listening")
        return True, "Voice is on, sir."

    def stop(self):
        if not self.active:
            return
        self.active = False
        # The state word must not lie after a stop (audit
        # 2026-10-22: speaking/level stayed set forever when
        # voice stopped mid-sentence, and queued audio could
        # still play out).
        self.speaking = False
        self.level = 0.0
        self._drain_play_queue()
        self._turn_sent = 0
        self._turn_played = 0
        self._turn_end = False
        self._ptt_held = False
        if self.gate is not None:
            self.gate.stop()      # the wake detector's thread
        if self.session is not None:
            try:
                self.session.stop()
            except Exception:
                pass
            self.session = None
        self._close_io()
        self._set_activity("standby")

    def _close_io(self):
        if self.io is not None:
            try:
                self.io.close()
            except Exception:
                pass
            self.io = None

    # -- owner controls ---------------------------------------------------------

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        return self.muted

    def ptt_hold(self, held: bool) -> bool:
        """Push-to-talk: while held, the mic streams even when
        the wake gate is asleep — the owner's hand IS the wake
        signal. Releasing restores the gate exactly as it was."""
        self._ptt_held = bool(held)
        if held and self.gate is not None:
            self.gate.note_user_speech()
        return self._ptt_held

    def interrupt(self):
        if self.session is not None:
            self.session.interrupt()

    def send_text(self, text: str) -> bool:
        if self.session is None or not self.active:
            return False
        self.session.send_text(text)
        return True

    def status(self) -> dict:
        return {
            "active": self.active,
            "connected": bool(self.session and self.session.connected),
            "speaking": self.speaking,
            "level": round(float(self.level), 3),
            "muted": self.muted,
            "ptt_held": self._ptt_held,
            "awake": bool(self.gate and self.gate.awake),
            "wake_word": bool(self.gate and self.gate.enabled),
            "voice": self.config.get("voice_name", "Charon"),
        }

    # -- mic path -----------------------------------------------------------------

    def _mic_block(self, pcm: bytes):
        if not self.active or self.muted or self.session is None:
            return
        if self.gate is not None and not self._ptt_held \
                and not self.gate.feed(pcm):
            return                      # asleep: stays on this machine
        if self.speaking:
            return                      # never stream our own voice
        if self._clock() < self._tail_until:
            if self.guard is not None and self.guard.is_user_speech(
                    pcm, 16000):
                self._tail_until = 0.0  # a real voice ends the tail
            else:
                return
        if self.gate is not None and pcm_level(pcm) > 0.05:
            self.gate.note_user_speech()
        self.level = pcm_level(pcm)   # heard audio only — the
        self.session.send_audio(pcm)  # HUD's honest mic level

    # -- session events (playback side) ------------------------------------------

    def on_audio(self, pcm: bytes):
        self._turn_sent += len(pcm)
        self._play_q.put(pcm)

    def heard_fraction(self):
        """Played / sent for the current spoken turn — the measure
        LiveSession.interrupt() truncates the remembered answer by.
        None when nothing was sent (no honest measure exists)."""
        if self._turn_sent <= 0:
            return None
        return max(0.0, min(1.0, self._turn_played / self._turn_sent))

    def on_turn_audio_end(self):
        self._turn_end = True

    def on_interrupted(self):
        self._drain_play_queue()
        self._turn_end = False
        self._turn_sent = 0
        self._turn_played = 0
        if self.speaking:
            self.speaking = False
            self._set_activity("listening")

    def on_transcript(self, role: str, text: str):
        if role == "user" and self.gate is not None:
            self.gate.note_user_speech()
        if role == "user":
            # The scrub gate (jarvis/scrub.py): a secret's
            # SHAPE in the owner's own speech never reaches
            # history or memory — the words are replaced, the
            # owner is told, and anything playing drains.
            from .scrub import REDACTED, find_secret_shapes
            shapes = find_secret_shapes(text)
            if shapes:
                self._drain_play_queue()
                self._notify(
                    "Suraksha: aapki baat me "
                    + "/".join(shapes)
                    + " ki shakal mili — woh hisaab me NAHI "
                      "likha gaya. Aisi cheezein bol kar mat "
                      "diya karo, sir; keys Settings se dalti "
                      "hain.")
                text = REDACTED
        if self._on_transcript:
            self._on_transcript(role, text)

    def on_session(self, connected: bool, detail: str = ""):
        if connected:
            self._notify("Voice connected.")
        elif detail == "bad key":
            self._notify("Voice stopped: the Gemini key was rejected. "
                         "File a fresh key with /key and restart "
                         "voice.")

    # -- playback + watch threads ---------------------------------------------------

    def _drain_play_queue(self):
        while True:
            try:
                self._play_q.get_nowait()
            except queue.Empty:
                return

    def _play_loop(self):
        while self.active:
            try:
                first = self._play_q.get(timeout=0.1)
            except queue.Empty:
                if self._turn_end and self.speaking:
                    self._finish_speaking()
                continue
            batch = bytearray(first)
            while len(batch) < PLAY_BATCH_MAX:
                try:
                    batch += self._play_q.get_nowait()
                except queue.Empty:
                    break
            if not self.speaking:
                self.speaking = True
                self._set_activity("speaking")
            try:
                self.io.write(bytes(batch))
                self._turn_played += len(batch)
                self.level = pcm_level(bytes(batch))
            except Exception:
                pass
            if self.guard is not None:
                self.guard.note_output(bytes(batch), SPEAKER_RATE)
            if self._turn_end and self._play_q.empty():
                self._finish_speaking()

    def _finish_speaking(self):
        self.speaking = False
        self.level = 0.0
        self._turn_end = False
        self._turn_sent = 0
        self._turn_played = 0
        # Playback has caught up with generation: the session
        # may now commit this turn's transcript WHOLE (the
        # heard-truncation contract — before this signal, an
        # interrupt can still cut it to what was played).
        hook = getattr(self.session, "on_playback_drained",
                       None)
        if callable(hook):
            try:
                hook()
            except Exception:
                pass
        latency = self.io.output_latency if self.io else 0.20
        self._tail_until = self._clock() + latency + TAIL_MARGIN_S
        # A spoken turn ends into listening, awake or not —
        # the mic is still JARVIS's ear. The word decays to
        # standby only through the Activity idle rule.
        self._set_activity("listening")

    def _watch_loop(self):
        while self.active:
            time.sleep(2.0)
            if self.gate is not None:
                # The gate's own sleep bookkeeping; the state
                # WORD is the Activity idle rule's business
                # now (standby after 120s of silence), so the
                # watch loop no longer forces it early.
                self.gate.check_sleep()

    def _set_activity(self, state: str):
        if self.activity is not None:
            try:
                self.activity.set(state)
            except Exception:
                pass
