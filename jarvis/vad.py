"""VAD tier cascade — energy gate -> neural tier -> turn decisions.

Frame Step 06 (Speech & Audio), from the ai-engineering-from-scratch
study notes (part-C-nlp-speech.md, Phase 06 Lesson 14, capstone
Lesson 12):

    The cascade: energy gate (RMS > -40 dBFS; free, fires on any
    noise) -> Silero VAD (1M params, MIT, ~1 ms per 30 ms chunk on one
    CPU thread) -> endpointing decision. The lesson's rule: NEVER ship
    energy-only VAD. So this module refuses to invent neural
    probabilities — with no real model injected, the neural tier
    raises :class:`NeuralVADUnavailable` instead of returning a
    made-up number. The energy tier alone can only ever say "quiet";
    the moment audio is loud, a real (or test-injected) neural tier
    must answer.

    Decision numbers (Lesson 14 / capstone Lesson 12):
    - onset threshold 0.3 — sensitive trigger, fewer first-word clips
    - steady (confirm) threshold 0.5 — the default speech decision
    - minimum speech 250 ms — rejects coughs and chair-noise blips
    - silence hangover 500-800 ms — the conversational sweet spot
      (default 650 ms until the owner's learned endpointing tunes it
      inside this band)
    - pre-roll 300-500 ms — the wake phrase's first syllable is never
      lost (default 400 ms)

    Flush trick (Kyutai, Lesson 14): streaming STT carries a
    look-ahead delay (500 ms for STT-1B, 2.5 s for STT-2.6B). When
    the VAD declares end-of-speech, the pipeline flushes — the
    pending partial transcript is emitted immediately instead of
    waiting out the look-ahead. ``push_partial`` / ``flush_partial``
    are that bookkeeping, and a speech_end event auto-flushes any
    pending partial.

Frames are plain sequences of float samples in [-1, 1]. Python
stdlib only.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Protocol, Sequence

__all__ = [
    "NeuralVADUnavailable",
    "NeuralVAD",
    "NullNeuralVAD",
    "SileroShapedVAD",
    "VADConfig",
    "Segment",
    "FlushRecord",
    "VADEvent",
    "VADStream",
    "rms",
    "dbfs",
]


class NeuralVADUnavailable(RuntimeError):
    """The neural VAD tier was consulted but no real model backs it."""


class NeuralVAD(Protocol):
    """Anything that can score one frame's speech probability."""

    def prob(self, frame: Sequence[float]) -> float:
        """Speech probability in [0, 1] for this frame."""
        ...


class NullNeuralVAD:
    """The 'no neural tier configured' placeholder.

    ``prob`` raises instead of guessing: a fabricated probability
    would silently turn the cascade into energy-only VAD with extra
    steps — exactly what the study says never to ship.
    """

    def prob(self, frame: Sequence[float]) -> float:
        raise NeuralVADUnavailable(
            "no neural VAD tier configured (NullNeuralVAD): inject a "
            "real backend (e.g. SileroShapedVAD with a loaded model) "
            "or a test fake — refusing to invent a speech probability"
        )


class SileroShapedVAD:
    """Adapter with the real Silero call shape, minus the model.

    Silero scores fixed 512-sample windows at 16 kHz. ``model`` is a
    callable taking a list of 512 floats and returning a probability
    — wire it to the owner's loaded silero-vad ONNX/torch callable.
    With ``model=None`` (the shipped default) ``prob`` raises
    :class:`NeuralVADUnavailable`: the adapter knows the shape of the
    answer, never the answer itself.
    """

    WINDOW = 512  # samples per Silero call at 16 kHz
    SAMPLE_RATE = 16000

    def __init__(self, model=None, sample_rate: int = SAMPLE_RATE) -> None:
        self._model = model
        self.sample_rate = sample_rate

    def prob(self, frame: Sequence[float]) -> float:
        if self._model is None:
            raise NeuralVADUnavailable(
                "Silero VAD model not loaded: construct "
                "SileroShapedVAD(model=<loaded silero callable>) — "
                "refusing to invent a speech probability"
            )
        if len(frame) < self.WINDOW:
            raise ValueError(
                f"Silero window needs >= {self.WINDOW} samples, "
                f"got {len(frame)}"
            )
        window = [float(s) for s in frame[-self.WINDOW:]]
        p = float(self._model(window))
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"neural VAD returned out-of-range prob {p!r}")
        return p


def rms(frame: Sequence[float]) -> float:
    """Root-mean-square amplitude of a frame (0.0 for an empty frame)."""
    if not frame:
        return 0.0
    return math.sqrt(sum(float(s) * float(s) for s in frame) / len(frame))


def dbfs(frame: Sequence[float]) -> float:
    """RMS level in dBFS (-inf for silence)."""
    level = rms(frame)
    if level <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(level)


@dataclass(frozen=True)
class VADConfig:
    """Turn-decision numbers, defaults straight from the study.

    Bands the study gives as ranges keep their range in the comments;
    the defaults sit mid-band until the owner's learned endpointing
    tunes them per-user inside the band.
    """

    onset_threshold: float = 0.3  # sensitive trigger (fewer clipped words)
    steady_threshold: float = 0.5  # confirm/maintain decision
    min_speech_ms: float = 250.0  # shorter than this = blip, rejected
    hangover_ms: float = 650.0  # study band 500-800
    pre_roll_ms: float = 400.0  # study band 300-500
    energy_gate_dbfs: float = -40.0  # RMS gate (Lesson 14)
    lookahead_ms: float = 500.0  # streaming-STT look-ahead (flush trick)
    frame_ms: float = 20.0  # duration of one frame fed to process()
    max_candidate_ms: float = 1000.0  # hover-noise abort for candidates

    def __post_init__(self) -> None:
        if not 0.0 <= self.onset_threshold <= self.steady_threshold <= 1.0:
            raise ValueError(
                "need 0 <= onset_threshold <= steady_threshold <= 1, got "
                f"onset={self.onset_threshold!r} steady={self.steady_threshold!r}"
            )
        if self.min_speech_ms <= 0 or self.hangover_ms <= 0:
            raise ValueError("min_speech_ms and hangover_ms must be > 0")
        if self.pre_roll_ms < 0 or self.lookahead_ms < 0:
            raise ValueError("pre_roll_ms and lookahead_ms must be >= 0")
        if self.frame_ms <= 0 or self.max_candidate_ms <= 0:
            raise ValueError("frame_ms and max_candidate_ms must be > 0")

    @property
    def energy_gate_rms(self) -> float:
        """Linear RMS corresponding to ``energy_gate_dbfs``."""
        return 10.0 ** (self.energy_gate_dbfs / 20.0)

    @property
    def pre_roll_frames(self) -> int:
        return int(math.ceil(self.pre_roll_ms / self.frame_ms))

    @property
    def max_candidate_frames(self) -> int:
        return int(math.ceil(self.max_candidate_ms / self.frame_ms))


@dataclass
class Segment:
    """One confirmed utterance, as the cascade saw it.

    ``frames`` includes the pre-roll, the speech, any mid-utterance
    pauses, and (at close) the trailing hangover silence — use
    ``audio_frames()`` for the frames up to the last speech frame.
    All counts are in frames; ``frame_ms`` converts them.
    """

    frames: List[Sequence[float]] = field(default_factory=list)
    pre_roll_frames: int = 0
    speech_frames: int = 0  # frames the neural tier called speech
    trailing_silence_frames: int = 0
    frame_ms: float = 20.0
    started_at_ms: float = 0.0  # stream clock at first pre-roll frame
    ended_at_ms: Optional[float] = None  # stream clock at close

    @property
    def speech_ms(self) -> float:
        return self.speech_frames * self.frame_ms

    @property
    def duration_ms(self) -> float:
        return len(self.frames) * self.frame_ms

    def audio_frames(self) -> List[Sequence[float]]:
        """Frames without the trailing hangover silence."""
        if self.trailing_silence_frames:
            return self.frames[: -self.trailing_silence_frames]
        return list(self.frames)


@dataclass
class FlushRecord:
    """A partial transcript emitted early by the flush trick."""

    text: str
    lookahead_skipped_ms: float  # look-ahead NOT waited out
    flushed_at_ms: float  # stream clock when the flush happened


@dataclass
class VADEvent:
    """Emitted by ``VADStream.process``: speech_start / speech_end."""

    kind: str
    segment: Segment
    flush: Optional[FlushRecord] = None  # set on speech_end only


class VADStream:
    """Streaming turn detector over the energy -> neural cascade.

    Feed one frame per ``process`` call; it returns the events that
    frame caused (usually none). State machine:

        idle --prob>=onset--> candidate --speech_ms>=min_speech--> speech
        candidate --prob<onset, or hover>max_candidate--> idle (blip)
        speech --silence>=hangover--> idle (segment closed)

    While in candidate/speech, frames whose prob sits in
    [onset, steady) HOLD the state without confirming or killing it
    (hysteresis — the two thresholds are doing different jobs).
    """

    def __init__(
        self,
        neural: Optional[NeuralVAD] = None,
        config: Optional[VADConfig] = None,
    ) -> None:
        self._neural: NeuralVAD = neural if neural is not None else NullNeuralVAD()
        self._cfg = config if config is not None else VADConfig()
        self._clock_ms = 0.0
        self._frame_index = 0  # absolute count of frames processed
        hist_len = (
            self._cfg.pre_roll_frames + self._cfg.max_candidate_frames + 2
        )
        self._history: Deque[Sequence[float]] = deque(maxlen=hist_len)
        self._state = "idle"
        self._cand_start_index = 0
        self._cand_frames = 0
        self._cand_speech_ms = 0.0
        self._segment: Optional[Segment] = None
        self._silence_run_frames = 0
        self._pending_partial: Optional[tuple] = None  # (text, at_ms)
        self.stats = {"rejected_blips": 0, "segments": 0}

    # -- introspection --------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def clock_ms(self) -> float:
        return self._clock_ms

    # -- flush-trick bookkeeping ----------------------------------------

    def push_partial(self, text: str) -> None:
        """Register the streaming STT's latest partial transcript."""
        self._pending_partial = (text, self._clock_ms)

    def flush_partial(self) -> Optional[FlushRecord]:
        """Emit the pending partial NOW instead of after the look-ahead.

        Returns None when no partial is pending. The record states how
        much of the STT look-ahead was skipped — the trick's whole
        point (Lesson 14: ~125 ms to transcript instead of waiting out
        500 ms-2.5 s of look-ahead).
        """
        if self._pending_partial is None:
            return None
        text, at_ms = self._pending_partial
        self._pending_partial = None
        waited = self._clock_ms - at_ms
        skipped = max(0.0, self._cfg.lookahead_ms - waited)
        return FlushRecord(
            text=text,
            lookahead_skipped_ms=skipped,
            flushed_at_ms=self._clock_ms,
        )

    # -- main entry point -----------------------------------------------

    def process(self, frame: Sequence[float]) -> List[VADEvent]:
        cfg = self._cfg
        self._clock_ms += cfg.frame_ms
        self._frame_index += 1
        self._history.append(frame)

        # Tier 1: energy gate. Failing it means non-speech WITHOUT
        # consulting the neural tier (the cascade's compute saving).
        if rms(frame) > cfg.energy_gate_rms:
            prob = float(self._neural.prob(frame))
            if not 0.0 <= prob <= 1.0:
                raise ValueError(
                    f"neural VAD returned out-of-range prob {prob!r}"
                )
        else:
            prob = 0.0

        if self._state == "idle":
            return self._idle(frame, prob)
        if self._state == "candidate":
            return self._candidate(frame, prob)
        return self._speech(frame, prob)

    # -- states -----------------------------------------------------------

    def _idle(self, frame: Sequence[float], prob: float) -> List[VADEvent]:
        if prob >= self._cfg.onset_threshold:
            self._state = "candidate"
            self._cand_start_index = self._frame_index
            self._cand_frames = 1
            self._cand_speech_ms = (
                self._cfg.frame_ms if prob >= self._cfg.steady_threshold else 0.0
            )
        return []

    def _candidate(self, frame: Sequence[float], prob: float) -> List[VADEvent]:
        cfg = self._cfg
        self._cand_frames += 1
        if prob >= cfg.steady_threshold:
            self._cand_speech_ms += cfg.frame_ms
        elif prob >= cfg.onset_threshold:
            pass  # hysteresis hold: neither confirms nor kills
        else:
            return self._abort_candidate()
        if self._cand_speech_ms >= cfg.min_speech_ms:
            return [self._confirm_candidate()]
        if self._cand_frames * cfg.frame_ms > cfg.max_candidate_ms:
            return self._abort_candidate()
        return []

    def _abort_candidate(self) -> List[VADEvent]:
        self._state = "idle"
        self._cand_frames = 0
        self._cand_speech_ms = 0.0
        self.stats["rejected_blips"] += 1
        return []

    def _confirm_candidate(self) -> VADEvent:
        cfg = self._cfg
        history = list(self._history)
        history_start = self._frame_index - len(history) + 1
        desired_start = self._cand_start_index - cfg.pre_roll_frames
        start = max(desired_start, history_start)
        pre_roll_actual = self._cand_start_index - start
        segment = Segment(
            frames=history[start - history_start:],
            pre_roll_frames=pre_roll_actual,
            speech_frames=int(round(self._cand_speech_ms / cfg.frame_ms)),
            frame_ms=cfg.frame_ms,
            started_at_ms=(start - 1) * cfg.frame_ms,
        )
        self._segment = segment
        self._state = "speech"
        self._silence_run_frames = 0
        return VADEvent(kind="speech_start", segment=segment)

    def _speech(self, frame: Sequence[float], prob: float) -> List[VADEvent]:
        cfg = self._cfg
        segment = self._segment
        assert segment is not None  # state invariant
        segment.frames.append(frame)
        if prob >= cfg.onset_threshold:
            segment.speech_frames += 1
            self._silence_run_frames = 0
            return []
        self._silence_run_frames += 1
        if self._silence_run_frames * cfg.frame_ms < cfg.hangover_ms:
            return []
        # Hangover expired: the utterance is over.
        segment.trailing_silence_frames = self._silence_run_frames
        segment.ended_at_ms = self._clock_ms
        self._segment = None
        self._state = "idle"
        self._silence_run_frames = 0
        self.stats["segments"] += 1
        flush = self.flush_partial()  # the flush trick fires on endpoint
        return [VADEvent(kind="speech_end", segment=segment, flush=flush)]
