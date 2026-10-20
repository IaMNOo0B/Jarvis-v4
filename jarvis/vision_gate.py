"""Step 04 - Computer Vision: grounding-verification gate primitives.

Three mechanisms, lifted from the ai-engineering-from-scratch study
(Part B, lessons on Mask R-CNN / RoIAlign and on VLM grounding / CMER):

1. CMER do-not-click rule.  CMER flags outputs whose *text* confidence is
   high (> 0.8) while the image-text (CLIP-style) cosine similarity of the
   claimed target is low (< 0.25): the model sounds sure but the pixels do
   not back it up.  Such a claim is a suspected hallucinated grounding -
   the verifier gate must NOT click; it re-observes instead.

2. Float-coordinate crop discipline.  Candidate boxes stay as floats end
   to end; rounding happens exactly once, at the final pixel boundary
   (``pixel_span``).  Snapping boxes to an integer grid early - the
   RoIPool mistake - misaligns crops by up to a full feature pixel, which
   is exactly what breaks on 125%/150% DPI displays.

3. ``roi_align_sample`` - a real, small RoIAlign over a plain 2D list
   grid: bins are computed from the float box with zero quantisation,
   sample points are bilinearly interpolated, and bin values are the
   mean of their samples (torchvision-style adaptive sampling count,
   ``ceil(bin_size)`` samples per axis).

Coordinate convention for (3): the grid is ``grid[row][col]`` and a
continuous point ``(x, y)`` interpolates with ``x`` along columns and
``y`` along rows, such that integer coordinates land exactly on cell
centres.  Sample points are clamped to the grid extent (edge
replication) before interpolation.  A degenerate (zero-area) box
samples to zeros.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# CMER thresholds from the study notes (Part B, VLM lesson): text
# confidence above 0.8 combined with image-text cosine below 0.25 marks
# a suspected hallucinated grounding.
CMER_TEXT_CONF_THRESHOLD = 0.8
CMER_COSINE_THRESHOLD = 0.25


@dataclass(frozen=True)
class CMERDecision:
    """Verdict of the CMER gate for one grounding claim."""

    allow_click: bool
    verdict: str  # "CLICK" or "DO_NOT_CLICK"
    reason: str
    text_conf: float
    image_text_cosine: float

    @property
    def suspect(self) -> bool:
        """True when the claim matches the CMER hallucination pattern."""
        return not self.allow_click


def cmer_verdict(text_conf: float, image_text_cosine: float) -> CMERDecision:
    """Apply the CMER do-not-click rule to one grounding claim.

    CLICK is blocked only by the conjunction: the model is confident
    (``text_conf > 0.8``) AND the image does not agree with the text
    (``image_text_cosine < 0.25``).  A low-confidence claim, or a
    confident claim the pixels support, passes the gate.
    """
    if text_conf > CMER_TEXT_CONF_THRESHOLD and image_text_cosine < CMER_COSINE_THRESHOLD:
        return CMERDecision(
            allow_click=False,
            verdict="DO_NOT_CLICK",
            reason=(
                "CMER hallucination pattern: text confidence "
                f"{text_conf:.2f} > {CMER_TEXT_CONF_THRESHOLD} but image-text "
                f"cosine {image_text_cosine:.2f} < {CMER_COSINE_THRESHOLD} - "
                "the model sounds sure but the pixels disagree; re-observe, "
                "do not click."
            ),
            text_conf=text_conf,
            image_text_cosine=image_text_cosine,
        )
    return CMERDecision(
        allow_click=True,
        verdict="CLICK",
        reason=(
            "No CMER pattern: text confidence "
            f"{text_conf:.2f}, image-text cosine {image_text_cosine:.2f} - "
            "claim is not a confident-but-ungrounded one."
        ),
        text_conf=text_conf,
        image_text_cosine=image_text_cosine,
    )


def cmer_rate(claims) -> float:
    """Fraction of ``(text_conf, cosine)`` claims matching the CMER pattern.

    This is the CMER KPI from the study: monitor it over grounding calls;
    a rising rate means the grounder is hallucinating more, not that any
    single claim is wrong.
    """
    claims = list(claims)
    if not claims:
        return 0.0
    hits = sum(1 for conf, cos in claims if not cmer_verdict(conf, cos).allow_click)
    return hits / len(claims)


# ---------------------------------------------------------------------------
# Float-coordinate crop discipline
# ---------------------------------------------------------------------------

Box = tuple  # (x0, y0, x1, y1) of floats


def snap_free_crop(box, scale: float) -> tuple:
    """Scale a float box by a DPI ``scale`` WITHOUT rounding anything.

    ``box`` is ``(x0, y0, x1, y1)`` in logical (unscaled) coordinates;
    the result is the same box in physical pixels, still floats.  This
    is the anti-RoIPool rule: quantisation is deferred to
    ``pixel_span``, the single final pixel boundary.
    """
    x0, y0, x1, y1 = (float(v) for v in box)
    return (x0 * scale, y0 * scale, x1 * scale, y1 * scale)


def pixel_span(box) -> tuple:
    """The ONLY rounding step: integer pixel coverage of a float box.

    Returns ``(px0, py0, px1, py1)`` ints - floor of the mins, ceil of
    the maxes - i.e. every pixel the float box touches.
    """
    x0, y0, x1, y1 = (float(v) for v in box)
    return (math.floor(x0), math.floor(y0), math.ceil(x1), math.ceil(y1))


def clip_box(box, width: float, height: float) -> tuple:
    """Clamp a float box into ``[0, width] x [0, height]``, staying float.

    Coordinates are clamped independently and the box is re-ordered so
    ``x0 <= x1`` and ``y0 <= y1`` always hold afterwards.
    """
    x0, y0, x1, y1 = (float(v) for v in box)
    w, h = float(width), float(height)
    x0 = min(max(x0, 0.0), w)
    x1 = min(max(x1, 0.0), w)
    y0 = min(max(y0, 0.0), h)
    y1 = min(max(y1, 0.0), h)
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return (x0, y0, x1, y1)


# ---------------------------------------------------------------------------
# RoIAlign on a plain 2D grid
# ---------------------------------------------------------------------------


def _bilinear(grid, height: int, width: int, x: float, y: float) -> float:
    """Bilinear interpolation of ``grid`` at continuous point ``(x, y)``.

    The point is clamped to the grid extent first (edge replication);
    integer coordinates return the exact cell value.
    """
    x = min(max(x, 0.0), width - 1.0)
    y = min(max(y, 0.0), height - 1.0)
    x_lo = int(math.floor(x))
    y_lo = int(math.floor(y))
    x_hi = min(x_lo + 1, width - 1)
    y_hi = min(y_lo + 1, height - 1)
    fx = x - x_lo
    fy = y - y_lo
    top = grid[y_lo][x_lo] * (1.0 - fx) + grid[y_lo][x_hi] * fx
    bottom = grid[y_hi][x_lo] * (1.0 - fx) + grid[y_hi][x_hi] * fx
    return top * (1.0 - fy) + bottom * fy


def roi_align_sample(feature_grid, box, out_size) -> list:
    """Sample a fixed-size patch from ``feature_grid`` inside float ``box``.

    ``feature_grid`` is a list of rows of numbers.  ``box`` is
    ``(x0, y0, x1, y1)`` in the grid's continuous coordinates (see the
    module docstring).  ``out_size`` is an int (square output) or an
    ``(out_h, out_w)`` pair.  Returns ``out_h`` rows of ``out_w`` floats.

    No coordinate is ever rounded: bin edges are float divisions of the
    box, and each bin averages ``ceil(bin_h) x ceil(bin_w)`` bilinear
    samples placed evenly inside the bin (torchvision's adaptive
    sampling-ratio behaviour).  A degenerate box yields zeros.
    """
    if isinstance(out_size, int):
        out_h = out_w = out_size
    else:
        out_h, out_w = int(out_size[0]), int(out_size[1])
    height = len(feature_grid)
    width = len(feature_grid[0]) if height else 0
    zeros = [[0.0] * out_w for _ in range(out_h)]
    if height == 0 or width == 0:
        return zeros
    x0, y0, x1, y1 = (float(v) for v in box)
    roi_w = x1 - x0
    roi_h = y1 - y0
    if roi_w <= 0.0 or roi_h <= 0.0:
        return zeros

    bin_w = roi_w / out_w
    bin_h = roi_h / out_h
    n_x = max(1, math.ceil(bin_w))
    n_y = max(1, math.ceil(bin_h))

    out = []
    for i in range(out_h):
        row = []
        bin_y0 = y0 + i * bin_h
        for j in range(out_w):
            bin_x0 = x0 + j * bin_w
            total = 0.0
            for sy in range(n_y):
                y = bin_y0 + (sy + 0.5) * bin_h / n_y
                for sx in range(n_x):
                    x = bin_x0 + (sx + 0.5) * bin_w / n_x
                    total += _bilinear(feature_grid, height, width, x, y)
            row.append(total / (n_x * n_y))
        out.append(row)
    return out
