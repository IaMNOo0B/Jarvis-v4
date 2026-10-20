"""Tests for jarvis.vad — the energy -> neural cascade state machine.

Synthetic streams only: loud frames are constant-amplitude sample
lists (RMS 0.4, far above the -40 dBFS energy gate), quiet frames are
silence (energy gate fails, neural tier never consulted). The neural
tier is a scripted fake — the tests judge the DECISION LOGIC, and the
no-fake-probabilities rule is itself under test (NullNeuralVAD and
the unloaded Silero adapter must raise, never guess).

Frame clock: the default VADConfig frame_ms=20, so 250 ms min-speech
= 13 steady frames, 650 ms hangover = 33 silent frames.

Run:  python -m unittest tests.test_vad -v
      python -m pytest tests/test_vad.py -q     (if pytest is present)
"""

import unittest

from jarvis.vad import (
    NeuralVADUnavailable,
    NullNeuralVAD,
    SileroShapedVAD,
    VADConfig,
    VADStream,
    dbfs,
    rms,
)


def loud(n=320, amp=0.4):
    return [amp] * n


def quiet(n=320):
    return [0.0] * n


class ScriptedNeural:
    """Fake neural tier: constant prob, counts its consultations."""

    def __init__(self, p):
        self.p = p
        self.calls = 0

    def prob(self, frame):
        self.calls += 1
        return self.p


def feed(stream, frames):
    events = []
    for frame in frames:
        events.extend(stream.process(frame))
    return events


class TestLevels(unittest.TestCase):
    def test_rms_and_dbfs(self):
        self.assertAlmostEqual(rms([0.5] * 4), 0.5)
        self.assertEqual(rms([]), 0.0)
        self.assertAlmostEqual(dbfs([1.0]), 0.0)
        self.assertEqual(dbfs([0.0] * 8), float("-inf"))

    def test_energy_gate_threshold_is_minus_40_dbfs(self):
        # -40 dBFS in linear RMS is exactly 0.01.
        self.assertAlmostEqual(VADConfig().energy_gate_rms, 0.01)


class TestNoFakeProbabilities(unittest.TestCase):
    def test_null_neural_raises(self):
        with self.assertRaises(NeuralVADUnavailable):
            NullNeuralVAD().prob(loud())

    def test_default_stream_raises_on_loud_frame(self):
        # Energy gate passes -> neural tier consulted -> no model -> raise.
        stream = VADStream()
        with self.assertRaises(NeuralVADUnavailable):
            stream.process(loud())

    def test_default_stream_stays_quiet_on_silence(self):
        # Silence never reaches the neural tier, so no raise.
        stream = VADStream()
        self.assertEqual(feed(stream, [quiet()] * 10), [])

    def test_silero_stub_raises_without_model(self):
        with self.assertRaises(NeuralVADUnavailable):
            SileroShapedVAD().prob([0.1] * 512)

    def test_silero_adapter_uses_injected_model(self):
        seen = {}

        def fake_silero(window):
            seen["len"] = len(window)
            return 0.77

        vad = SileroShapedVAD(model=fake_silero)
        self.assertAlmostEqual(vad.prob([0.1] * 600), 0.77)
        self.assertEqual(seen["len"], 512)  # the Silero window shape
        with self.assertRaises(ValueError):
            vad.prob([0.1] * 100)  # shorter than one window


class TestStateMachine(unittest.TestCase):
    def test_energy_gate_never_consults_neural_on_quiet(self):
        neural = ScriptedNeural(1.0)  # would scream "speech" if asked
        stream = VADStream(neural=neural)
        self.assertEqual(feed(stream, [quiet()] * 20), [])
        self.assertEqual(neural.calls, 0)

    def test_too_short_blip_rejected_by_min_speech(self):
        stream = VADStream(neural=ScriptedNeural(0.9))
        # 5 loud frames = 100 ms of "speech" < 250 ms minimum, then silence.
        events = feed(stream, [loud()] * 5 + [quiet()])
        self.assertEqual(events, [])
        self.assertEqual(stream.state, "idle")
        self.assertEqual(stream.stats["rejected_blips"], 1)

    def test_onset_alone_never_confirms(self):
        # prob 0.4 sits in [onset 0.3, steady 0.5): it can OPEN a
        # candidate but never confirm one — and the hover aborts.
        # One candidate lifecycle = 51 frames (1000 ms max at 20 ms,
        # aborted on the frame that crosses it); 102 frames = two
        # full lifecycles, each rejected, no segment ever opened.
        stream = VADStream(neural=ScriptedNeural(0.4))
        events = feed(stream, [loud()] * 102)
        self.assertEqual(events, [])
        self.assertEqual(stream.state, "idle")
        self.assertEqual(stream.stats["rejected_blips"], 2)

    def test_segment_opens_with_pre_roll(self):
        stream = VADStream(neural=ScriptedNeural(0.9))
        feed(stream, [quiet()] * 10)  # 200 ms of pre-speech room tone
        events = feed(stream, [loud()] * 13)  # 260 ms >= 250 ms minimum
        starts = [e for e in events if e.kind == "speech_start"]
        self.assertEqual(len(starts), 1)
        segment = starts[0].segment
        # Config wants 20 pre-roll frames; only 10 exist — take what is there.
        self.assertEqual(segment.pre_roll_frames, 10)
        self.assertEqual(len(segment.frames), 10 + 13)
        self.assertEqual(segment.speech_frames, 13)
        self.assertAlmostEqual(segment.speech_ms, 260.0)
        self.assertEqual(stream.state, "speech")

    def test_hangover_keeps_segment_open_then_closes(self):
        stream = VADStream(neural=ScriptedNeural(0.9))
        events = feed(stream, [loud()] * 13)  # segment opens
        # 300 ms pause (< 650 ms hangover): mid-sentence, NOT an ending.
        events += feed(stream, [quiet()] * 15)
        self.assertEqual([e.kind for e in events], ["speech_start"])
        self.assertEqual(stream.state, "speech")
        # Speech resumes inside the same segment.
        events += feed(stream, [loud()] * 5)
        self.assertEqual([e.kind for e in events], ["speech_start"])
        # 660 ms of silence >= hangover: the segment finally closes.
        events += feed(stream, [quiet()] * 33)
        self.assertEqual(
            [e.kind for e in events], ["speech_start", "speech_end"]
        )
        segment = events[-1].segment
        self.assertEqual(segment.trailing_silence_frames, 33)
        self.assertEqual(segment.speech_frames, 13 + 5)
        # Audio without the hangover tail: 13 + 15 pause + 5 resumed.
        self.assertEqual(len(segment.audio_frames()), 13 + 15 + 5)
        self.assertEqual(stream.stats["segments"], 1)
        self.assertEqual(stream.state, "idle")


class TestFlushTrick(unittest.TestCase):
    def test_flush_partial_skips_lookahead(self):
        stream = VADStream(neural=ScriptedNeural(0.0))
        feed(stream, [quiet()] * 5)  # clock: 100 ms
        stream.push_partial("namaste")
        feed(stream, [quiet()] * 5)  # clock: 200 ms; partial waited 100 ms
        record = stream.flush_partial()
        self.assertIsNotNone(record)
        self.assertEqual(record.text, "namaste")
        # 500 ms look-ahead, 100 ms already waited -> 400 ms skipped.
        self.assertAlmostEqual(record.lookahead_skipped_ms, 400.0)
        self.assertIsNone(stream.flush_partial())  # nothing left pending

    def test_flush_with_nothing_pending_is_none(self):
        stream = VADStream(neural=ScriptedNeural(0.0))
        self.assertIsNone(stream.flush_partial())

    def test_speech_end_auto_flushes_pending_partial(self):
        stream = VADStream(neural=ScriptedNeural(0.9))
        feed(stream, [loud()] * 13)  # segment opens; clock 260 ms
        feed(stream, [quiet()] * 30)  # 600 ms into the hangover
        stream.push_partial("chrome kholo")  # partial lands late
        events = feed(stream, [quiet()] * 3)  # hangover expires -> end
        ends = [e for e in events if e.kind == "speech_end"]
        self.assertEqual(len(ends), 1)
        self.assertIsNotNone(ends[0].flush)
        self.assertEqual(ends[0].flush.text, "chrome kholo")
        # Partial waited 60 ms of its 500 ms look-ahead.
        self.assertAlmostEqual(ends[0].flush.lookahead_skipped_ms, 440.0)


if __name__ == "__main__":
    unittest.main()
