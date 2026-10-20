"""Visual-token budgeting for the screen watcher — JARVIS v4, Step 12.

Design (from the ai-eng study, part-F, Phase 12 L06 — working budgets):
- An image costs one visual token per non-overlapping patch (ViT-style,
  patch 28 by default: Qwen2-VL-style 14px patch + 2x2 merge = /28 grid).
- Every content type has a working token budget (sweet-spot range):
      ui          729 - 1024     (screens, charts, app windows)
      photo       256 -  576
      ocr        1024 - 4096     (text-heavy screens, documents)
      document   1024 - 4096
      video_frame  64 -  128
- Pooling merges f x f patches into one token; plan_glance picks the
  smallest pooling factor that brings the glance inside the budget.
- A rolling per-minute cap (GlanceBudget) approves or DEFERS glances.
  A deferral always carries a reason — a glance is never silently dropped.

Stdlib only. The clock is injectable so the rolling window is testable.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Optional, Union

DEFAULT_PATCH = 28

# content type -> (min_tokens, max_tokens): the working sweet-spot range.
CONTENT_BUDGETS: dict[str, tuple[int, int]] = {
    "ui": (729, 1024),
    "photo": (256, 576),
    "ocr": (1024, 4096),
    "document": (1024, 4096),
    "video_frame": (64, 128),
}


def patch_grid(image_w: int, image_h: int, patch: int = DEFAULT_PATCH) -> tuple[int, int]:
    """Patch grid dimensions (cols, rows) for an image at a patch size."""
    if image_w <= 0 or image_h <= 0:
        raise ValueError("image dimensions must be positive")
    if patch <= 0:
        raise ValueError("patch must be positive")
    return (math.ceil(image_w / patch), math.ceil(image_h / patch))


def patch_tokens(image_w: int, image_h: int, patch: int = DEFAULT_PATCH) -> int:
    """Raw visual-token cost of an image with no pooling."""
    gw, gh = patch_grid(image_w, image_h, patch)
    return gw * gh


def _pooled_tokens(grid_w: int, grid_h: int, pooling: int) -> int:
    return math.ceil(grid_w / pooling) * math.ceil(grid_h / pooling)


def plan_glance(
    content_type: str,
    image_w: int,
    image_h: int,
    patch: int = DEFAULT_PATCH,
) -> dict:
    """Plan one glance: raw patch cost + pooling needed to fit the budget.

    Returns a dict with the full arithmetic so the caller can log WHY a
    glance costs what it costs:
        content_type, image_w, image_h, patch, grid_w, grid_h, raw_tokens,
        budget_min, budget_max, pooling, tokens, fits, note
    `tokens` is the planned (post-pooling) cost; `fits` is tokens <= budget_max.
    """
    if content_type not in CONTENT_BUDGETS:
        raise ValueError(
            f"unknown content_type {content_type!r}; known: {sorted(CONTENT_BUDGETS)}"
        )
    budget_min, budget_max = CONTENT_BUDGETS[content_type]
    grid_w, grid_h = patch_grid(image_w, image_h, patch)
    raw = grid_w * grid_h

    pooling = 1
    tokens = raw
    while tokens > budget_max:
        pooling += 1
        tokens = _pooled_tokens(grid_w, grid_h, pooling)

    if pooling == 1:
        note = "native resolution fits the budget"
    elif tokens < budget_min:
        note = (
            f"pooled {pooling}x{pooling} to fit; below the {content_type} "
            f"sweet-spot minimum ({budget_min}) — detail will be coarse"
        )
    else:
        note = f"pooled {pooling}x{pooling} to fit the {content_type} budget"

    return {
        "content_type": content_type,
        "image_w": image_w,
        "image_h": image_h,
        "patch": patch,
        "grid_w": grid_w,
        "grid_h": grid_h,
        "raw_tokens": raw,
        "budget_min": budget_min,
        "budget_max": budget_max,
        "pooling": pooling,
        "tokens": tokens,
        "fits": tokens <= budget_max,
        "note": note,
    }


class GlanceBudget:
    """Rolling per-minute token budget for watcher glances.

    approve() either records the glance's spend or DEFERS it with a reason
    (window full / glance alone exceeds the cap). Nothing is silently
    dropped: every decision is returned and kept in `self.decisions`.
    """

    WINDOW_SECONDS = 60.0

    def __init__(
        self,
        max_tokens_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        if max_tokens_per_minute <= 0:
            raise ValueError("max_tokens_per_minute must be positive")
        self.cap = int(max_tokens_per_minute)
        self._clock = clock
        self._spend: list[tuple[float, int]] = []  # (timestamp, tokens)
        self.decisions: list[dict] = []

    # -- internals ------------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - self.WINDOW_SECONDS
        self._spend = [(t, n) for (t, n) in self._spend if t > cutoff]

    @staticmethod
    def _cost_of(plan_or_tokens: Union[dict, int]) -> int:
        if isinstance(plan_or_tokens, dict):
            return int(plan_or_tokens["tokens"])
        return int(plan_or_tokens)

    # -- queries --------------------------------------------------------

    def spent(self, now: Optional[float] = None) -> int:
        """Tokens spent inside the current rolling window."""
        now = self._clock() if now is None else now
        self._prune(now)
        return sum(n for _, n in self._spend)

    def next_available_in(
        self, tokens: int, now: Optional[float] = None
    ) -> Optional[float]:
        """Seconds until `tokens` would fit, or None if it can never fit."""
        if tokens > self.cap:
            return None
        now = self._clock() if now is None else now
        self._prune(now)
        spent = sum(n for _, n in self._spend)
        if spent + tokens <= self.cap:
            return 0.0
        # Walk oldest-first: budget frees as entries age out of the window.
        freed = spent
        for ts, n in sorted(self._spend):
            freed -= n
            if freed + tokens <= self.cap:
                return max(0.0, ts + self.WINDOW_SECONDS - now)
        return self.WINDOW_SECONDS  # unreachable in practice; safe fallback

    # -- decisions ------------------------------------------------------

    def approve(
        self, plan_or_tokens: Union[dict, int], now: Optional[float] = None
    ) -> dict:
        """Approve (and record) a glance, or defer it with a reason.

        Returns {approved, tokens, spent_in_window, cap, reason}.
        On approval spent_in_window is the total AFTER recording this glance;
        on deferral it is the current window total (glance NOT recorded).
        """
        now = self._clock() if now is None else now
        cost = self._cost_of(plan_or_tokens)
        if cost <= 0:
            raise ValueError("glance cost must be positive")
        spent = self.spent(now)

        if cost > self.cap:
            decision = {
                "approved": False,
                "tokens": cost,
                "spent_in_window": spent,
                "cap": self.cap,
                "reason": (
                    f"glance costs {cost} tokens, above the per-minute cap "
                    f"{self.cap} by itself — pool it further or raise the cap"
                ),
            }
        elif spent + cost > self.cap:
            wait = self.next_available_in(cost, now)
            decision = {
                "approved": False,
                "tokens": cost,
                "spent_in_window": spent,
                "cap": self.cap,
                "reason": (
                    f"rolling minute budget: {spent}/{self.cap} tokens already "
                    f"spent; this glance needs {cost}; retry in ~{wait:.1f}s"
                ),
            }
        else:
            self._spend.append((now, cost))
            decision = {
                "approved": True,
                "tokens": cost,
                "spent_in_window": spent + cost,
                "cap": self.cap,
                "reason": "within rolling minute budget",
            }
        self.decisions.append(decision)
        return decision

    def deferrals(self) -> list[dict]:
        """All deferred decisions so far (each carries its reason)."""
        return [d for d in self.decisions if not d["approved"]]
