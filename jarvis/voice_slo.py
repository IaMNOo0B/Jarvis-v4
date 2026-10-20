"""Voice-loop latency SLOs — the written budgets as code.

Frame Step 06 (Speech & Audio), from the ai-engineering-from-scratch
study notes (part-C-nlp-speech.md, Phase 06 Lessons 11/12/14):

    The full conversational loop budget is ~400 ms. The study's split
    (mic 20 / VAD 10 / streaming ASR 150 / LLM first token 100 / TTS
    first chunk 100 / render->speaker 20) is kept here as JARVIS v4's
    own stage table, with the capture+VAD front folded into one
    ``vad_gate`` bucket:

        vad_gate 20 · stt 150 · brain_first_token 100
        tts_first_chunk 100 · playback 20 · misc 10    = 400 ms

    Two hard deadlines sit on top of the stage table:
    - first TTS audio < 800 ms after end-of-utterance (Lesson 12
      target for the integrated pipeline), and
    - a barge-in must cancel the in-flight LLM generation within
      150 ms (Lessons 11/12: the VAD cancels the LLM, not just the
      TTS; JARVIS budgets the top of the 100-150 ms band).

This module is pure bookkeeping: it owns the numbers and judges
recorded latencies against them. The Step 06 saboot — measuring the
real loop on the owner's PC against these verdicts — is PC-side work;
nothing here pretends a measurement happened.

Python stdlib only.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Dict, Iterator, Optional

__all__ = [
    "STAGE_BUDGETS_MS",
    "TURN_BUDGET_MS",
    "FIRST_AUDIO_DEADLINE_MS",
    "BARGE_IN_CANCEL_DEADLINE_MS",
    "LatencyLedger",
]

# Stage budgets in milliseconds. Insertion order is pipeline order.
# The sum is exactly TURN_BUDGET_MS — a test pins both facts.
STAGE_BUDGETS_MS: Dict[str, int] = {
    "vad_gate": 20,
    "stt": 150,
    "brain_first_token": 100,
    "tts_first_chunk": 100,
    "playback": 20,
    "misc": 10,
}

TURN_BUDGET_MS = 400
FIRST_AUDIO_DEADLINE_MS = 800
BARGE_IN_CANCEL_DEADLINE_MS = 150


class LatencyLedger:
    """Per-turn latency bookkeeping judged against the SLO table.

    One ledger per turn. ``record`` accumulates: a stage measured in
    several chunks (e.g. STT across partials) sums into one actual.
    Verdicts are three-valued per stage — "within", "over", "missing"
    (never recorded) — because an unmeasured stage is not a passed
    stage (fail-closed bookkeeping, same doctrine as the gate).
    """

    def __init__(self) -> None:
        self._actual: Dict[str, float] = {}
        self._counts: Dict[str, int] = {}

    # -- recording ------------------------------------------------------

    def record(self, stage: str, duration_ms: float) -> float:
        """Add one measured duration for ``stage``; returns its total."""
        if stage not in STAGE_BUDGETS_MS:
            raise ValueError(
                f"unknown stage {stage!r}; known stages: "
                f"{', '.join(STAGE_BUDGETS_MS)}"
            )
        if duration_ms < 0:
            raise ValueError(f"duration_ms must be >= 0, got {duration_ms!r}")
        self._actual[stage] = self._actual.get(stage, 0.0) + float(duration_ms)
        self._counts[stage] = self._counts.get(stage, 0) + 1
        return self._actual[stage]

    @contextmanager
    def time_stage(self, stage: str) -> Iterator[None]:
        """Context manager: record the wrapped block's wall time."""
        if stage not in STAGE_BUDGETS_MS:
            raise ValueError(
                f"unknown stage {stage!r}; known stages: "
                f"{', '.join(STAGE_BUDGETS_MS)}"
            )
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(stage, (time.perf_counter() - start) * 1000.0)

    # -- reading --------------------------------------------------------

    def recorded(self, stage: str) -> Optional[float]:
        """Total recorded ms for ``stage``, or None if never recorded."""
        if stage not in STAGE_BUDGETS_MS:
            raise ValueError(
                f"unknown stage {stage!r}; known stages: "
                f"{', '.join(STAGE_BUDGETS_MS)}"
            )
        return self._actual.get(stage)

    @property
    def total_ms(self) -> float:
        """Sum of recorded stage durations (unrecorded stages add 0)."""
        return sum(self._actual.values())

    def verdicts(self) -> Dict[str, Dict[str, object]]:
        """Per-stage verdicts: budget, actual, within/over/missing."""
        out: Dict[str, Dict[str, object]] = {}
        for stage, budget in STAGE_BUDGETS_MS.items():
            actual = self._actual.get(stage)
            if actual is None:
                status = "missing"
            elif actual <= budget:
                status = "within"
            else:
                status = "over"
            out[stage] = {
                "budget_ms": budget,
                "actual_ms": actual,
                "status": status,
            }
        return out

    def turn_verdict(self) -> Dict[str, object]:
        """The whole turn judged: within / over / incomplete.

        "over" if any recorded stage busts its budget OR the recorded
        total busts TURN_BUDGET_MS. "incomplete" if nothing is over
        but at least one stage was never recorded — an unmeasured turn
        is not a passed turn. Otherwise "within".
        """
        stages = self.verdicts()
        total = self.total_ms
        if any(v["status"] == "over" for v in stages.values()) or (
            total > TURN_BUDGET_MS
        ):
            status = "over"
        elif any(v["status"] == "missing" for v in stages.values()):
            status = "incomplete"
        else:
            status = "within"
        return {
            "status": status,
            "total_ms": total,
            "total_budget_ms": TURN_BUDGET_MS,
            "stages": stages,
        }

    # -- one-shot deadline checks ---------------------------------------

    @staticmethod
    def first_audio_verdict(delay_ms: float) -> Dict[str, object]:
        """First-audio delay after end-of-utterance vs the 800 ms line."""
        if delay_ms < 0:
            raise ValueError(f"delay_ms must be >= 0, got {delay_ms!r}")
        return {
            "deadline_ms": FIRST_AUDIO_DEADLINE_MS,
            "actual_ms": float(delay_ms),
            "status": "within" if delay_ms <= FIRST_AUDIO_DEADLINE_MS else "over",
        }

    @staticmethod
    def barge_in_verdict(delay_ms: float) -> Dict[str, object]:
        """Barge-in -> LLM-cancel delay vs the 150 ms line."""
        if delay_ms < 0:
            raise ValueError(f"delay_ms must be >= 0, got {delay_ms!r}")
        return {
            "deadline_ms": BARGE_IN_CANCEL_DEADLINE_MS,
            "actual_ms": float(delay_ms),
            "status": (
                "within" if delay_ms <= BARGE_IN_CANCEL_DEADLINE_MS else "over"
            ),
        }
