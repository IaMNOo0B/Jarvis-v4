"""Tests for jarvis.speculative — the draft-and-verify tuning math.

Hand-checked numbers (Phase 07 L16):
- E(alpha=0.8, N=5) = (1 - 0.8^6) / (1 - 0.8)
                   = (1 - 0.262144) / 0.2 = 0.737856 / 0.2 = 3.68928
- speedup(0.8, 5, c=0.05) = 3.68928 / (1 + 5*0.05) = 3.68928 / 1.25
                          = 2.951424
- Raw speedup argmax at alpha=0.8, c=0.05 is N=8 (3.0921x) vs N=7
  (3.0823x) — a 0.3% gap; the knee rule (smallest N within 1% of
  best) returns 7, inside the frame's N~5-7 guidance.

Run:  python -m unittest tests.test_speculative -v
      python -m pytest tests/test_speculative.py -q     (if pytest is present)
"""

import unittest

from jarvis.speculative import (
    draft_modelfile,
    estimate_alpha,
    estimate_alpha_from_rounds,
    expected_tokens,
    optimal_draft_len,
    speedup,
)


class TestExpectedTokens(unittest.TestCase):
    def test_known_value_alpha_08_n_5(self):
        self.assertAlmostEqual(expected_tokens(0.8, 5), 3.68928, places=5)

    def test_known_value_alpha_075_n_5(self):
        # (1 - 0.75^6) / 0.25 = (1 - 0.177978515625) / 0.25
        self.assertAlmostEqual(
            expected_tokens(0.75, 5), 3.2880859375, places=9
        )

    def test_alpha_zero_still_yields_the_resample_token(self):
        self.assertEqual(expected_tokens(0.0, 5), 1.0)

    def test_alpha_one_yields_n_plus_1(self):
        self.assertEqual(expected_tokens(1.0, 5), 6.0)
        self.assertEqual(expected_tokens(1.0, 0), 1.0)

    def test_zero_draft_length(self):
        self.assertEqual(expected_tokens(0.8, 0), 1.0)

    def test_validation(self):
        with self.assertRaises(ValueError):
            expected_tokens(-0.1, 5)
        with self.assertRaises(ValueError):
            expected_tokens(1.1, 5)
        with self.assertRaises(ValueError):
            expected_tokens(0.8, -1)


class TestSpeedupAndOptimum(unittest.TestCase):
    def test_known_speedup(self):
        self.assertAlmostEqual(speedup(0.8, 5, 0.05), 2.951424, places=5)

    def test_optimal_draft_len_in_frame_band(self):
        n = optimal_draft_len(0.8, 0.05)
        self.assertGreaterEqual(n, 5)
        self.assertLessEqual(n, 7)

    def test_optimal_is_at_the_knee_not_past_it(self):
        # The returned N must be within 1% of the best modelled
        # speedup, and N-1 must NOT be (it is the smallest such N).
        n = optimal_draft_len(0.8, 0.05)
        best = max(speedup(0.8, k, 0.05) for k in range(1, 33))
        self.assertGreaterEqual(speedup(0.8, n, 0.05), best * 0.99)
        self.assertLess(speedup(0.8, n - 1, 0.05), best * 0.99)

    def test_pricier_draft_means_shorter_optimum(self):
        self.assertLess(
            optimal_draft_len(0.8, 0.2), optimal_draft_len(0.8, 0.02)
        )

    def test_free_draft_still_bounded_by_search(self):
        n = optimal_draft_len(0.8, 0.0)
        self.assertGreaterEqual(n, 1)
        self.assertLessEqual(n, 32)

    def test_validation(self):
        with self.assertRaises(ValueError):
            speedup(0.8, 5, -0.1)
        with self.assertRaises(ValueError):
            optimal_draft_len(0.8, 0.05, max_n=0)


class TestEstimateAlpha(unittest.TestCase):
    def test_from_outcomes(self):
        self.assertEqual(estimate_alpha([True, True, False, True]), 0.75)
        self.assertAlmostEqual(estimate_alpha([1, 0, 1]), 2 / 3)
        self.assertEqual(estimate_alpha([False, False]), 0.0)

    def test_from_rounds(self):
        # (3 of 4) + (2 of 4) = 5 of 8 = 0.625
        self.assertEqual(estimate_alpha_from_rounds([(3, 4), (2, 4)]), 0.625)

    def test_empty_and_invalid_raise(self):
        with self.assertRaises(ValueError):
            estimate_alpha([])
        with self.assertRaises(ValueError):
            estimate_alpha([True, "yes"])
        with self.assertRaises(ValueError):
            estimate_alpha_from_rounds([])
        with self.assertRaises(ValueError):
            estimate_alpha_from_rounds([(5, 4)])  # accepted > drafted
        with self.assertRaises(ValueError):
            estimate_alpha_from_rounds([(0, 0)])


class TestDraftModelfile(unittest.TestCase):
    def test_house_format(self):
        text = draft_modelfile(
            "qwen2.5:7b-instruct-q4_K_M", "qwen2.5:0.5b-instruct-q4_K_M", 6
        )
        lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(lines[0], "FROM qwen2.5:7b-instruct-q4_K_M")
        self.assertEqual(lines[1], "DRAFT qwen2.5:0.5b-instruct-q4_K_M")
        self.assertEqual(lines[2], "PARAMETER draft_num_predict 6")

    def test_validation(self):
        with self.assertRaises(ValueError):
            draft_modelfile("", "qwen2.5:0.5b", 4)
        with self.assertRaises(ValueError):
            draft_modelfile("qwen2.5:7b", "  ", 4)
        with self.assertRaises(ValueError):
            draft_modelfile("qwen2.5:7b", "qwen2.5:0.5b", -1)


if __name__ == "__main__":
    unittest.main()
