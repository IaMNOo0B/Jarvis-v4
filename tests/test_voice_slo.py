"""Tests for jarvis.voice_slo — the SLO table and the ledger verdicts.

Every number here traces to the study split pinned in the module
docstring (part-C Phase 06 L11/L12): stages sum to the ~400 ms turn
budget; first audio < 800 ms after end-of-utterance; barge-in cancels
the LLM within 150 ms.

Run:  python -m unittest tests.test_voice_slo -v
      python -m pytest tests/test_voice_slo.py -q     (if pytest is present)
"""

import unittest

from jarvis.voice_slo import (
    BARGE_IN_CANCEL_DEADLINE_MS,
    FIRST_AUDIO_DEADLINE_MS,
    STAGE_BUDGETS_MS,
    TURN_BUDGET_MS,
    LatencyLedger,
)


class TestSloTable(unittest.TestCase):
    def test_stage_table_is_the_frame_table(self):
        self.assertEqual(
            STAGE_BUDGETS_MS,
            {
                "vad_gate": 20,
                "stt": 150,
                "brain_first_token": 100,
                "tts_first_chunk": 100,
                "playback": 20,
                "misc": 10,
            },
        )

    def test_stages_sum_to_turn_budget(self):
        self.assertEqual(sum(STAGE_BUDGETS_MS.values()), TURN_BUDGET_MS)
        self.assertEqual(TURN_BUDGET_MS, 400)

    def test_deadlines(self):
        self.assertEqual(FIRST_AUDIO_DEADLINE_MS, 800)
        self.assertEqual(BARGE_IN_CANCEL_DEADLINE_MS, 150)


class TestLedgerVerdicts(unittest.TestCase):
    def _full_within_ledger(self):
        ledger = LatencyLedger()
        ledger.record("vad_gate", 12)
        ledger.record("stt", 140)
        ledger.record("brain_first_token", 90)
        ledger.record("tts_first_chunk", 80)
        ledger.record("playback", 15)
        ledger.record("misc", 5)
        return ledger

    def test_all_within_turn_is_within(self):
        verdict = self._full_within_ledger().turn_verdict()
        self.assertEqual(verdict["status"], "within")
        self.assertEqual(verdict["total_ms"], 342)
        self.assertEqual(verdict["total_budget_ms"], 400)
        for stage_verdict in verdict["stages"].values():
            self.assertEqual(stage_verdict["status"], "within")

    def test_one_stage_over_turns_turn_over(self):
        ledger = self._full_within_ledger()
        ledger.record("stt", 20)  # stt total 160 > 150 budget
        verdict = ledger.turn_verdict()
        self.assertEqual(verdict["status"], "over")
        self.assertEqual(verdict["stages"]["stt"]["status"], "over")
        self.assertEqual(verdict["stages"]["stt"]["actual_ms"], 160)

    def test_record_accumulates(self):
        ledger = LatencyLedger()
        self.assertEqual(ledger.record("misc", 5), 5)
        self.assertEqual(ledger.record("misc", 6), 11)
        # misc budget is 10 — two innocent chunks sum to a bust.
        self.assertEqual(ledger.verdicts()["misc"]["status"], "over")

    def test_missing_stage_is_not_a_pass(self):
        ledger = LatencyLedger()
        ledger.record("stt", 100)
        verdicts = ledger.verdicts()
        self.assertEqual(verdicts["vad_gate"]["status"], "missing")
        self.assertIsNone(verdicts["vad_gate"]["actual_ms"])
        # Nothing over, but stages unmeasured -> incomplete, not within.
        self.assertEqual(ledger.turn_verdict()["status"], "incomplete")

    def test_empty_ledger_is_incomplete(self):
        self.assertEqual(LatencyLedger().turn_verdict()["status"], "incomplete")
        self.assertEqual(LatencyLedger().total_ms, 0)

    def test_unknown_stage_and_negative_duration_raise(self):
        ledger = LatencyLedger()
        with self.assertRaises(ValueError):
            ledger.record("thinking", 10)
        with self.assertRaises(ValueError):
            ledger.record("stt", -1)
        with self.assertRaises(ValueError):
            ledger.recorded("thinking")

    def test_time_stage_records(self):
        ledger = LatencyLedger()
        with ledger.time_stage("misc"):
            pass
        self.assertIsNotNone(ledger.recorded("misc"))
        self.assertGreaterEqual(ledger.recorded("misc"), 0.0)
        with self.assertRaises(ValueError):
            with ledger.time_stage("thinking"):
                pass


class TestDeadlineChecks(unittest.TestCase):
    def test_first_audio_deadline(self):
        self.assertEqual(
            LatencyLedger.first_audio_verdict(700)["status"], "within"
        )
        self.assertEqual(
            LatencyLedger.first_audio_verdict(800)["status"], "within"
        )
        verdict = LatencyLedger.first_audio_verdict(900)
        self.assertEqual(verdict["status"], "over")
        self.assertEqual(verdict["deadline_ms"], 800)

    def test_barge_in_deadline(self):
        self.assertEqual(
            LatencyLedger.barge_in_verdict(120)["status"], "within"
        )
        self.assertEqual(
            LatencyLedger.barge_in_verdict(150)["status"], "within"
        )
        verdict = LatencyLedger.barge_in_verdict(200)
        self.assertEqual(verdict["status"], "over")
        self.assertEqual(verdict["deadline_ms"], 150)

    def test_negative_delay_raises(self):
        with self.assertRaises(ValueError):
            LatencyLedger.first_audio_verdict(-5)
        with self.assertRaises(ValueError):
            LatencyLedger.barge_in_verdict(-5)


if __name__ == "__main__":
    unittest.main()
