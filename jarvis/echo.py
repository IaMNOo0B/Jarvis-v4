"""EchoGuard — telling the owner's voice from JARVIS's own echo.

Adapted from Mark-LVI's core/echo.py (studied 2026-10-21), with one
substitution: their band energies come from a numpy rFFT; v4 is
stdlib-only, so the same 8 log-spaced speech bands are measured
with Goertzel filters (one per band centre — O(n) per band, exact
for this job). The logic is theirs, because it is sound:

- What is being played is remembered for 1.5 s as band energies.
- A mic block is projected onto those remembered slices; as much
  energy as the echo explains is subtracted. What remains is the
  residual ratio.
- The user threshold is LEARNED, not tuned: the 97th percentile of
  residuals observed from definite echo, x1.15, floored at 0.15.
  The room calibrates itself (a running gain estimate folds each
  definite echo back in at a 0.08 learning rate).
- Doubt resolves to "echo" — a false negative costs a repeated
  sentence; a false positive would let JARVIS interrupt itself.

Used by the post-speech echo tail: for a short window after each
reply, mic blocks pass only if this guard calls them user speech.
(Voice barge-in during speech stays OFF, exactly as in the repo —
it waits for real-hardware trials.)
"""

from __future__ import annotations

import math
import struct
import time
from collections import deque
from typing import Callable, List, Optional

BAND_EDGES = (200, 400, 700, 1100, 1700, 2600, 3800, 5200, 7000)
BAND_CENTRES = tuple(
    math.sqrt(BAND_EDGES[i] * BAND_EDGES[i + 1])
    for i in range(len(BAND_EDGES) - 1))
_HISTORY_S = 1.5
_MIN_USER = 0.15
_BAD_ROOM_FLOOR = 0.22


def _samples(pcm: bytes) -> List[float]:
    n = len(pcm) // 2
    if n == 0:
        return []
    return [v / 32768.0 for v in struct.unpack(f"<{n}h", pcm[:n * 2])]


def _goertzel_energy(samples: List[float], freq: float,
                     sample_rate: int) -> float:
    """Power at one frequency over the block (Goertzel)."""
    if not samples:
        return 0.0
    w = 2.0 * math.pi * freq / sample_rate
    coeff = 2.0 * math.cos(w)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
    return max(s1 * s1 + s2 * s2 - coeff * s1 * s2, 0.0) / len(samples)


def band_energies(pcm: bytes, sample_rate: int) -> List[float]:
    """Energies in the 8 speech bands — sample-rate agnostic, so a
    16 kHz mic block and a 24 kHz playback block compare directly."""
    samples = _samples(pcm)
    if not samples:
        return [0.0] * len(BAND_CENTRES)
    # A gentle Hann taper cuts band leakage between neighbours.
    n = len(samples)
    windowed = [x * (0.5 - 0.5 * math.cos(2 * math.pi * i / max(n - 1, 1)))
                for i, x in enumerate(samples)]
    return [_goertzel_energy(windowed, f, sample_rate)
            for f in BAND_CENTRES]


def pcm_level(pcm: bytes) -> float:
    """RMS level mapped 60..2600 -> 0..1 (the repo's HUD mapping)."""
    samples = _samples(pcm)
    if not samples:
        return 0.0
    rms = math.sqrt(sum(x * x for x in samples) / len(samples)) * 32768.0
    return max(0.0, min(1.0, (rms - 60.0) / (2600.0 - 60.0)))


class EchoGuard:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._history: deque = deque()      # (t, bands)
        self._echo_residuals: deque = deque(maxlen=200)
        self._gain = 1.0

    # -- what is being played -------------------------------------------------

    def note_output(self, pcm: bytes, sample_rate: int):
        try:
            bands = band_energies(pcm, sample_rate)
        except Exception:
            return
        now = self._clock()
        self._history.append((now, bands))
        while self._history and now - self._history[0][0] > _HISTORY_S:
            self._history.popleft()

    # -- the verdict ------------------------------------------------------------

    def _threshold(self) -> float:
        if len(self._echo_residuals) < 8:
            return _MIN_USER
        ordered = sorted(self._echo_residuals)
        p97 = ordered[min(len(ordered) - 1,
                           int(0.97 * len(ordered)))]
        return max(_MIN_USER, p97 * 1.15)

    @property
    def bad_room(self) -> bool:
        return self._threshold() >= _BAD_ROOM_FLOOR

    def is_user_speech(self, pcm: bytes, sample_rate: int,
                       level: Optional[float] = None) -> bool:
        """True only when the mic block carries energy the known
        output cannot explain. Any failure answers False — never
        interrupt on doubt."""
        try:
            return self._verdict(pcm, sample_rate)
        except Exception:
            return False

    def _verdict(self, pcm: bytes, sample_rate: int) -> bool:
        mic = band_energies(pcm, sample_rate)
        mic_total = sum(mic)
        if mic_total <= 1e-9 or not self._history:
            return False
        now = self._clock()
        while self._history and now - self._history[0][0] > _HISTORY_S:
            self._history.popleft()
        if not self._history:
            return False
        best_ratio = None
        best_ref_total = 0.0
        for _t, ref in self._history:
            residual = sum(
                max(m - self._gain * r, 0.0)
                for m, r in zip(mic, ref))
            ratio = residual / mic_total
            if best_ratio is None or ratio < best_ratio:
                best_ratio = ratio
                best_ref_total = sum(ref)
        if best_ratio is None:
            return False
        if best_ratio <= _MIN_USER:
            # Definite echo: learn from it (threshold + gain).
            self._echo_residuals.append(best_ratio)
            if best_ref_total > 1e-9:
                observed = mic_total / best_ref_total
                self._gain += 0.08 * (observed - self._gain)
            return False
        return best_ratio > self._threshold()
