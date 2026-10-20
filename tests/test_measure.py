"""Tests for jarvis.measure — every expected number is hand-computable.

Run:  python -m unittest tests.test_measure -v
      python -m pytest tests/test_measure.py -q     (if pytest is present)
"""

import json
import math
import os
import tempfile
import unittest

from jarvis.measure import (
    TestSetConsumedError,
    compare_runs,
    false_alarm_audit,
    paired_t,
    posterior,
    psi,
    psi_band,
)


class TestPosterior(unittest.TestCase):
    def test_textbook_base_rate(self):
        # Phase 01 L07 worked example: prevalence 1/10000, 99%/99% test.
        # Hand: TP rate = .99*.0001 = .000099; FP rate = .01*.9999 = .009999
        # posterior = .000099 / (.000099 + .009999) = .000099/.010098 = .009804
        self.assertAlmostEqual(posterior(0.0001, 0.99, 0.99), 0.0098, places=4)

    def test_perfect_detector_common_event(self):
        self.assertEqual(posterior(0.5, 1.0, 1.0), 1.0)

    def test_zero_prevalence(self):
        self.assertEqual(posterior(0.0, 0.99, 0.99), 0.0)

    def test_alarm_can_never_fire(self):
        # prevalence 0 AND specificity 1 -> denominator 0 -> defined as 0.0
        self.assertEqual(posterior(0.0, 0.99, 1.0), 0.0)

    def test_higher_prevalence_raises_posterior(self):
        rare = posterior(0.001, 0.9, 0.9)
        common = posterior(0.2, 0.9, 0.9)
        self.assertLess(rare, common)

    def test_out_of_range_rejected(self):
        for bad in (-0.1, 1.1):
            with self.assertRaises(ValueError):
                posterior(bad, 0.9, 0.9)
            with self.assertRaises(ValueError):
                posterior(0.1, bad, 0.9)
            with self.assertRaises(ValueError):
                posterior(0.1, 0.9, bad)


class TestFalseAlarmAudit(unittest.TestCase):
    def test_textbook_alert_is_almost_all_false(self):
        [row] = false_alarm_audit(
            [
                {
                    "name": "miracle-detector",
                    "prevalence": 0.0001,
                    "sensitivity": 0.99,
                    "specificity": 0.99,
                }
            ]
        )
        self.assertAlmostEqual(row["posterior"], 0.0098, places=4)
        self.assertAlmostEqual(row["false_alarm_rate"], 0.9902, places=4)
        self.assertIn("Almost all false", row["verdict"])

    def test_strong_probe_alert_is_ok(self):
        # Provider-down style: rare, but a very specific probe.
        # posterior = .99*.01 / (.0099 + .001*.99) = .0099/.01089 = .9091
        [row] = false_alarm_audit(
            [
                {
                    "name": "provider-down",
                    "prevalence": 0.01,
                    "sensitivity": 0.99,
                    "specificity": 0.999,
                }
            ]
        )
        self.assertAlmostEqual(row["posterior"], 0.9091, places=4)
        self.assertTrue(row["verdict"].startswith("OK"))

    def test_order_and_names_preserved(self):
        rows = false_alarm_audit(
            [
                {"name": "a", "prevalence": 0.3, "sensitivity": 0.9,
                 "specificity": 0.85},
                {"name": "b", "prevalence": 0.02, "sensitivity": 0.8,
                 "specificity": 0.95},
            ]
        )
        self.assertEqual([r["name"] for r in rows], ["a", "b"])
        for row in rows:
            self.assertAlmostEqual(
                row["posterior"] + row["false_alarm_rate"], 1.0, places=12
            )


class TestPsi(unittest.TestCase):
    def test_identical_distributions_are_zero(self):
        counts = [12, 30, 7, 99, 41, 3, 18, 55, 26, 9]
        self.assertEqual(psi(counts, counts), 0.0)
        self.assertEqual(psi([50, 50], [50, 50]), 0.0)

    def test_two_bin_hand_computed(self):
        # shares ref [.5,.5], cur [.9,.1]:
        # (.9-.5)*ln(1.8) + (.1-.5)*ln(0.2) = .235115 + .643775 = .878890
        value = psi([50, 50], [90, 10])
        self.assertAlmostEqual(value, 0.8789, places=3)
        self.assertEqual(psi_band(value), "significant")

    def test_symmetric(self):
        self.assertAlmostEqual(
            psi([50, 50], [90, 10]), psi([90, 10], [50, 50]), places=12
        )

    def test_decile_stable_case(self):
        # ref deciles uniform; cur shares
        # [.14,.12,.11,.10,.10,.10,.09,.09,.08,.07] -> PSI ~= 0.0353 (hand-sum)
        expected = [100] * 10
        actual = [140, 120, 110, 100, 100, 100, 90, 90, 80, 70]
        value = psi(expected, actual)
        self.assertAlmostEqual(value, 0.0353, places=3)
        self.assertEqual(psi_band(value), "stable")

    def test_moderate_band(self):
        # (.7-.5)*ln(1.4) + (.3-.5)*ln(0.6) = .067294 + .102165 = .169460
        value = psi([50, 50], [70, 30])
        self.assertAlmostEqual(value, 0.1695, places=3)
        self.assertEqual(psi_band(value), "moderate")

    def test_band_boundaries(self):
        self.assertEqual(psi_band(0.099), "stable")
        self.assertEqual(psi_band(0.1), "moderate")
        self.assertEqual(psi_band(0.25), "moderate")
        self.assertEqual(psi_band(0.251), "significant")

    def test_empty_bins_do_not_explode(self):
        # A bin empty on both sides contributes 0; empty-vs-full is finite.
        self.assertEqual(psi([10, 0, 10], [10, 0, 10]), 0.0)
        self.assertTrue(math.isfinite(psi([10, 0, 10], [10, 5, 5])))

    def test_validation(self):
        with self.assertRaises(ValueError):
            psi([1, 2, 3], [1, 2])          # bin mismatch
        with self.assertRaises(ValueError):
            psi([1, -2], [1, 2])            # negative count
        with self.assertRaises(ValueError):
            psi([0, 0], [1, 2])             # zero total
        with self.assertRaises(ValueError):
            psi([], [1])                    # empty


class TestPairedT(unittest.TestCase):
    def test_hand_computed(self):
        # diffs [2,3,1,4,2]: mean 2.4; squared devs .16+.36+1.96+2.56+.16=5.2
        # sample var = 5.2/4 = 1.3; s = 1.140175; SE = s/sqrt(5) = 0.509902
        # t = 2.4/0.509902 = 4.7068; df = 4; bar 2.776 -> significant
        r = paired_t([2, 3, 1, 4, 2])
        self.assertEqual(r["n"], 5)
        self.assertEqual(r["df"], 4)
        self.assertAlmostEqual(r["mean"], 2.4, places=10)
        self.assertAlmostEqual(r["std"], 1.1402, places=4)
        self.assertAlmostEqual(r["t"], 4.7068, places=3)
        self.assertAlmostEqual(r["critical"], 2.776, places=3)
        self.assertTrue(r["significant"])

    def test_zero_differences_not_significant(self):
        r = paired_t([0, 0, 0, 0, 0])
        self.assertEqual(r["t"], 0.0)
        self.assertFalse(r["significant"])

    def test_cancelling_differences(self):
        r = paired_t([1, -1, 1, -1, 0])
        self.assertEqual(r["mean"], 0.0)
        self.assertEqual(r["t"], 0.0)
        self.assertFalse(r["significant"])

    def test_constant_nonzero_diffs_are_infinite_t(self):
        r = paired_t([1.5, 1.5, 1.5, 1.5, 1.5])
        self.assertEqual(r["std"], 0.0)
        self.assertTrue(math.isinf(r["t"]))
        self.assertTrue(r["significant"])

    def test_too_few_differences(self):
        with self.assertRaises(ValueError):
            paired_t([1.0])
        with self.assertRaises(ValueError):
            paired_t([])


class TestCompareRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "eval-state.json")
        # Baseline vs new: diffs are exactly [2,3,1,4,2] (see TestPairedT).
        self.baseline = [10, 10, 10, 10, 10]
        self.new = [12, 13, 11, 14, 12]

    def tearDown(self):
        self.tmp.cleanup()

    def test_basic_comparison(self):
        r = compare_runs(self.baseline, self.new, self.state, set_id="s1")
        self.assertEqual(r["set_id"], "s1")
        self.assertAlmostEqual(r["baseline_mean"], 10.0)
        self.assertAlmostEqual(r["new_mean"], 12.4)
        self.assertAlmostEqual(r["mean_diff"], 2.4)
        self.assertAlmostEqual(r["t"], 4.7068, places=3)
        self.assertTrue(r["significant"])
        self.assertFalse(r["final"])
        self.assertFalse(r["consumed"])
        # A non-final run must NOT write a consumption record.
        self.assertFalse(os.path.exists(self.state))

    def test_final_marks_consumed_and_refusal_fires(self):
        r = compare_runs(
            self.baseline, self.new, self.state, set_id="golden", final=True
        )
        self.assertTrue(r["consumed"])
        with open(self.state, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertIn("golden", saved["sets"])
        self.assertEqual(saved["sets"]["golden"]["n"], 5)
        # Any later comparison on the same set is refused — final or not.
        with self.assertRaises(TestSetConsumedError):
            compare_runs(self.baseline, self.new, self.state, set_id="golden")
        with self.assertRaises(TestSetConsumedError):
            compare_runs(
                self.baseline, self.new, self.state, set_id="golden", final=True
            )

    def test_other_sets_unaffected(self):
        compare_runs(self.baseline, self.new, self.state, set_id="golden",
                     final=True)
        r = compare_runs(self.baseline, self.new, self.state, set_id="fresh")
        self.assertEqual(r["set_id"], "fresh")

    def test_default_set_id(self):
        compare_runs(self.baseline, self.new, self.state, final=True)
        with self.assertRaises(TestSetConsumedError):
            compare_runs(self.baseline, self.new, self.state)

    def test_length_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            compare_runs([1, 2, 3], [1, 2], self.state)

    def test_state_path_required(self):
        with self.assertRaises(ValueError):
            compare_runs(self.baseline, self.new, None)

    def test_corrupt_state_fails_closed(self):
        with open(self.state, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(ValueError):
            compare_runs(self.baseline, self.new, self.state)


if __name__ == "__main__":
    unittest.main()
