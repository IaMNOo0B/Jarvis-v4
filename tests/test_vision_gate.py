"""Step 04 tests: CMER gate, float crops, RoIAlign (hand-computed)."""

import pytest

from jarvis.vision_gate import (
    clip_box,
    cmer_rate,
    cmer_verdict,
    pixel_span,
    roi_align_sample,
    snap_free_crop,
)

# ---------------------------------------------------------------------------
# CMER do-not-click rule: block iff text_conf > 0.8 AND cosine < 0.25
# ---------------------------------------------------------------------------


def test_cmer_blocks_confident_but_ungrounded():
    d = cmer_verdict(0.92, 0.18)
    assert d.allow_click is False
    assert d.verdict == "DO_NOT_CLICK"
    assert "CMER" in d.reason


def test_cmer_allows_confident_and_grounded():
    d = cmer_verdict(0.92, 0.61)
    assert d.allow_click is True
    assert d.verdict == "CLICK"


def test_cmer_allows_ungrounded_but_unconfident():
    # Low text confidence alone is not the hallucination pattern.
    d = cmer_verdict(0.55, 0.10)
    assert d.allow_click is True


def test_cmer_thresholds_are_strict():
    assert cmer_verdict(0.8, 0.10).allow_click is True  # conf not > 0.8
    assert cmer_verdict(0.95, 0.25).allow_click is True  # cosine not < 0.25
    assert cmer_verdict(0.81, 0.24).allow_click is False


def test_cmer_rate():
    claims = [(0.92, 0.18), (0.92, 0.61), (0.55, 0.10)]
    assert cmer_rate(claims) == pytest.approx(1 / 3)
    assert cmer_rate([]) == 0.0


# ---------------------------------------------------------------------------
# Float-coordinate crop discipline
# ---------------------------------------------------------------------------


def test_snap_free_crop_keeps_floats_at_125_percent_dpi():
    out = snap_free_crop((10.5, 20.25, 30.75, 40.5), 1.25)
    assert out == (13.125, 25.3125, 38.4375, 50.625)
    assert all(isinstance(v, float) for v in out)


def test_pixel_span_is_the_only_rounding_step():
    box = snap_free_crop((10.5, 20.25, 30.75, 40.5), 1.25)
    assert pixel_span(box) == (13, 25, 39, 51)  # floor mins, ceil maxes


def test_clip_box_clamps_and_stays_float():
    assert clip_box((-5, 10, 250, 90), 200, 100) == (0.0, 10.0, 200.0, 90.0)


def test_clip_box_reorders_inverted_box():
    assert clip_box((50, 50, 10, 10), 100, 100) == (10.0, 10.0, 50.0, 50.0)


# ---------------------------------------------------------------------------
# RoIAlign - all expectations computed by hand.
#
# Plane grid: value(row, col) = 10*row + col, so bilinear interpolation
# is exact everywhere: f(x, y) = x + 10*y  (x = column, y = row).
# ---------------------------------------------------------------------------

PLANE = [[10 * r + c for c in range(4)] for r in range(4)]


def test_roi_align_bin_centres_on_plane():
    # Box (0.5,0.5)-(2.5,2.5), out 2x2: bin size 1.0 -> 1 sample per
    # bin at its centre: (1,1)->11, (2,1)->12, (1,2)->21, (2,2)->22.
    out = roi_align_sample(PLANE, (0.5, 0.5, 2.5, 2.5), 2)
    assert out == [[11.0, 12.0], [21.0, 22.0]]


def test_roi_align_two_samples_per_bin_on_plane():
    # Full-map box in the aligned convention: (-0.5,-0.5)-(3.5,3.5)
    # covers the 4x4 grid exactly.  Out 2x2 -> bin size 2.0 -> 2x2
    # samples per bin at offsets 0.5/1.5 inside each bin.
    # Bin (0,0) samples (0,0),(1,0),(0,1),(1,1) -> {0, 1, 10, 11} -> 5.5
    # Bin (0,1): {2, 3, 12, 13} -> 7.5 ; Bin (1,0): {20,21,30,31} -> 25.5
    # Bin (1,1): {22, 23, 32, 33} -> 27.5
    out = roi_align_sample(PLANE, (-0.5, -0.5, 3.5, 3.5), 2)
    assert out == [[5.5, 7.5], [25.5, 27.5]]


def test_roi_align_single_output_averages_four_samples():
    # Box (1,1)-(3,3), out 1: samples (1.5,1.5),(2.5,1.5),(1.5,2.5),
    # (2.5,2.5) -> 16.5, 17.5, 26.5, 27.5 -> mean 22.0.
    out = roi_align_sample(PLANE, (1, 1, 3, 3), 1)
    assert out == [[22.0]]


def test_roi_align_true_bilinear_not_corner_snap():
    # Only the bottom-right cell is hot; the centre of box (0,0)-(1,1)
    # must bilinearly average all four cells: 0.25 * 100 = 25.  A
    # snapping (RoIPool-style) sampler would return 0 or 100.
    grid = [[0.0, 0.0], [0.0, 100.0]]
    out = roi_align_sample(grid, (0, 0, 1, 1), 1)
    assert out == [[25.0]]


def test_roi_align_fractional_box_differs_from_snapped_box():
    # The fractional ROI's centre value (11) is NOT what the snapped
    # integer box (0,0,2,2) would give (its bin samples average 5.5 in
    # the first bin) - float coordinates carry real information.
    frac = roi_align_sample(PLANE, (0.5, 0.5, 2.5, 2.5), 2)
    snapped = roi_align_sample(PLANE, (0, 0, 2, 2), 2)
    assert frac[0][0] == 11.0
    assert snapped[0][0] != frac[0][0]


def test_roi_align_degenerate_box_is_zeros():
    assert roi_align_sample(PLANE, (1, 1, 1, 3), 2) == [[0.0, 0.0], [0.0, 0.0]]
    assert roi_align_sample([], (0, 0, 1, 1), 2) == [[0.0, 0.0], [0.0, 0.0]]


def test_roi_align_rectangular_output():
    out = roi_align_sample(PLANE, (0.5, 0.5, 2.5, 2.5), (1, 2))
    assert len(out) == 1 and len(out[0]) == 2
