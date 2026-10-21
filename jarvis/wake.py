"""Wake-word gate — Mark-LVI's semantics over openWakeWord.

- OFF by default (the repo's default, and the owner's machine is
  his to open): with the gate off, every mic block streams.
- ON and asleep: mic audio NEVER leaves the machine — blocks go
  only to the local detector. Detection wakes for one conversation;
  120 s without user speech puts it back to sleep.
- The detector's inference runs on its OWN daemon thread (a queue
  between the audio callback and the model) — the callback thread
  must never wait on inference (its one-time import alone cost the
  repo a measured 2.1 s).
- openWakeWord is an opt-in install (setup offers it); enabling the
  gate without it answers honestly instead of pretending to listen.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

WAKE_MODEL = "hey_jarvis"
DEFAULT_THRESHOLD = 0.5
SLEEP_TIMEOUT_S = 120.0


class WakeUnavailable(Exception):
    pass


class WakeGate:
    def __init__(self, enabled: bool = False, detector=None,
                 clock: Callable[[], float] = time.monotonic,
                 on_wake: Optional[Callable[[], None]] = None):
        self.enabled = bool(enabled)
        self._detector = detector
        self._clock = clock
        self._on_wake = on_wake
        self.awake = not self.enabled
        self._last_speech = 0.0

    def feed(self, pcm: bytes) -> bool:
        """One mic block. True = stream it to the session;
        False = it stays on this machine (detector food only)."""
        if not self.enabled:
            return True
        if not self.awake:
            if self._detector is not None:
                self._detector.feed(pcm)
            return False
        return True

    def note_user_speech(self):
        self._last_speech = self._clock()

    def wake(self):
        if not self.awake:
            self.awake = True
            self._last_speech = self._clock()
            if self._on_wake:
                self._on_wake()

    def sleep(self):
        if self.enabled:
            self.awake = False

    def check_sleep(self) -> bool:
        """Auto-sleep after SLEEP_TIMEOUT_S without user speech.
        Returns True when this call put the gate to sleep."""
        if self.enabled and self.awake and self._last_speech \
                and self._clock() - self._last_speech > SLEEP_TIMEOUT_S:
            self.awake = False
            return True
        return False


class OpenWakeWordDetector:
    """The real detector: openWakeWord's hey_jarvis ONNX model,
    inference on a daemon thread fed by a bounded queue."""

    def __init__(self, on_detect: Callable[[], None],
                 threshold: float = DEFAULT_THRESHOLD):
        try:
            from openwakeword.model import Model
        except Exception as e:
            raise WakeUnavailable(
                "openwakeword is not installed — the voice pack in "
                f"setup.bat installs it ({e})") from e
        self._model = Model(wakeword_models=[WAKE_MODEL],
                            inference_framework="onnx")
        self._threshold = float(threshold)
        self._on_detect = on_detect
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._thread = threading.Thread(target=self._loop,
                                        daemon=True,
                                        name="jarvis-wakeword")
        self._thread.start()

    def feed(self, pcm: bytes):
        try:
            self._queue.put_nowait(pcm)
        except queue.Full:
            pass    # frames drop when backed up — never block audio

    def _loop(self):
        import numpy as np
        while True:
            pcm = self._queue.get()
            try:
                frame = np.frombuffer(pcm, dtype=np.int16)
                scores = self._model.predict(frame)
            except Exception:
                continue
            if any("jarvis" in str(k).lower()
                   and float(v) >= self._threshold
                   for k, v in scores.items()):
                # Drain so one utterance cannot double-fire.
                while not self._queue.empty():
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        break
                self._on_detect()
