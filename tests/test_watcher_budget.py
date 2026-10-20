"""Tests for jarvis.watcher_budget — Step 12 saboot: per-glance cost fits budget."""

import pytest

from jarvis.watcher_budget import (
    CONTENT_BUDGETS,
    GlanceBudget,
    patch_grid,
    patch_tokens,
    plan_glance,
)


def test_patch_grid_4k():
    # 3840/28 = 137.14 -> 138 cols; 2160/28 = 77.14 -> 78 rows
    assert patch_grid(3840, 2160, patch=28) == (138, 78)
    assert patch_tokens(3840, 2160, patch=28) == 138 * 78 == 10764


def test_plan_4k_screenshot_fits_ui_budget_with_pooling_4():
    plan = plan_glance("ui", 3840, 2160)
    assert plan["raw_tokens"] == 10764
    assert plan["budget_max"] == CONTENT_BUDGETS["ui"][1] == 1024
    # f=3 gives ceil(138/3)*ceil(78/3) = 46*26 = 1196 > 1024, so pooling must be 4
    assert plan["pooling"] == 4
    # f=4: ceil(138/4)=35, ceil(78/4)=20 -> 700 tokens
    assert plan["tokens"] == 35 * 20 == 700
    assert plan["tokens"] <= plan["budget_max"]
    assert plan["fits"] is True


def test_plan_photo_exact_fit_needs_no_pooling():
    # 672x672 at patch 28 -> 24x24 = 576 = photo budget max exactly
    plan = plan_glance("photo", 672, 672)
    assert plan["raw_tokens"] == 576
    assert plan["pooling"] == 1
    assert plan["tokens"] == 576
    assert plan["fits"] is True


def test_plan_ocr_uses_the_large_budget():
    plan = plan_glance("ocr", 3840, 2160)
    # f=2: ceil(138/2)*ceil(78/2) = 69*39 = 2691, inside 1024..4096
    assert plan["pooling"] == 2
    assert plan["tokens"] == 2691
    assert plan["budget_min"] <= plan["tokens"] <= plan["budget_max"]


def test_plan_unknown_content_type_rejected():
    with pytest.raises(ValueError):
        plan_glance("hologram", 800, 600)


def test_rolling_window_defers_the_glance_that_would_exceed_the_cap():
    budget = GlanceBudget(max_tokens_per_minute=2000)
    plan = plan_glance("ui", 1920, 1080)  # planned, pooled cost <= 1024
    cost = plan["tokens"]

    d1 = budget.approve(plan, now=1000.0)
    assert d1["approved"] is True
    assert d1["spent_in_window"] == cost

    # second identical glance still fits (2*cost <= 2000 when cost <= 1000)...
    # use raw token counts to force the boundary deterministically:
    budget2 = GlanceBudget(max_tokens_per_minute=2000)
    assert budget2.approve(1024, now=1000.0)["approved"] is True
    d2 = budget2.approve(1024, now=1000.0)
    assert d2["approved"] is False  # 1024 + 1024 = 2048 > 2000
    assert "rolling minute budget" in d2["reason"]
    assert d2["spent_in_window"] == 1024  # deferred glance NOT recorded
    assert len(budget2.deferrals()) == 1

    # after the window rolls (61s later), the same glance is approved
    d3 = budget2.approve(1024, now=1061.0)
    assert d3["approved"] is True
    assert d3["spent_in_window"] == 1024


def test_glance_larger_than_cap_is_deferred_with_reason_never_dropped():
    budget = GlanceBudget(max_tokens_per_minute=500)
    d = budget.approve(700, now=0.0)
    assert d["approved"] is False
    assert "by itself" in d["reason"]
    assert budget.spent(now=0.0) == 0  # nothing recorded, nothing hidden
    assert budget.next_available_in(700, now=0.0) is None


def test_next_available_in_reports_wait():
    budget = GlanceBudget(max_tokens_per_minute=2000)
    budget.approve(1500, now=100.0)
    # needs 600 more; the 1500 entry ages out at t=160
    assert budget.next_available_in(600, now=100.0) == pytest.approx(60.0)
    assert budget.next_available_in(400, now=100.0) == 0.0
