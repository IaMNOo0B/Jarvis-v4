"""Measurement core — the math JARVIS uses to not fool itself.

Frame Steps 01 (Math Foundations) and 02 (ML Fundamentals), from the
ai-engineering-from-scratch study notes (part-A-foundations.md):

Step 01 — base-rate discipline (Phase 01, Lesson 07, Bayes):
    A detector for a RARE event is mostly wrong even when it is "99%
    accurate", because the prior dominates. ``posterior`` is the exact
    computation; ``false_alarm_audit`` turns it into a per-alert verdict
    for JARVIS's proactive alerts (see docs/false-alarm-audit.md).

Step 02 — drift + eval kanoon (Phase 02, Lessons 20 and 09/15):
    ``psi`` is the Population Stability Index (symmetric KL over bins,
    shares clipped at 1e-4; bands <0.1 stable, 0.1-0.25 moderate,
    >0.25 significant) for watching golden pairs / persona / provider
    behaviour drift over time.
    ``paired_t`` + ``compare_runs`` are the evaluation law: a test set is
    touched ONCE for a final verdict; two versions are compared with a
    paired t-test on per-case score differences, never by vibes.
    ``compare_runs`` mechanically refuses to run a comparison on a test
    set that a prior ``final=True`` evaluation already consumed.

Python stdlib only. No numpy.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone

__all__ = [
    "posterior",
    "false_alarm_audit",
    "psi",
    "psi_band",
    "paired_t",
    "compare_runs",
    "TestSetConsumedError",
]

# Shares below this are floored before PSI's log — Phase 02 L20 convention,
# so an empty bin yields a large finite penalty instead of log(0).
_PSI_SHARE_FLOOR = 1e-4

# Two-tailed t critical values at alpha = 0.05, by degrees of freedom.
# df=4 -> 2.776 (the study notes quote it rounded as 2.78).
_T_CRIT_05 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
    7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179,
    13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101,
    19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064,
    25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}
_T_CRIT_05_LARGE_DF = 1.96  # normal limit for df > 30


class TestSetConsumedError(RuntimeError):
    """Raised when a comparison is attempted on an already-consumed test set.

    Eval kanoon (Phase 02 L09): the test set is touched exactly once for a
    final evaluation. Reusing it turns it into a second validation set and
    every later verdict is quietly optimistic. This error fails closed.
    """


# ---------------------------------------------------------------------------
# Step 01 — base rates
# ---------------------------------------------------------------------------

def _check_probability(name: str, value: float) -> float:
    v = float(value)
    if not 0.0 <= v <= 1.0:
        raise ValueError(f"{name} must be a probability in [0, 1], got {value!r}")
    return v


def posterior(prevalence: float, sensitivity: float, specificity: float) -> float:
    """P(event is real | detector fired) — Bayes with the base rate kept in.

        posterior = sens * prev / (sens * prev + (1 - spec) * (1 - prev))

    Textbook check (Phase 01 L07): prevalence 1/10000 with a 99%-sensitive,
    99%-specific test gives ~0.0098 — over 99% of its alarms are false.

    Edge cases: prevalence 0 -> 0.0 (nothing real to find). If the alarm can
    never fire at all (denominator 0, e.g. prevalence 0 AND specificity 1),
    the question is empty and 0.0 is returned.
    """
    prev = _check_probability("prevalence", prevalence)
    sens = _check_probability("sensitivity", sensitivity)
    spec = _check_probability("specificity", specificity)
    true_alarms = sens * prev
    false_alarms = (1.0 - spec) * (1.0 - prev)
    total = true_alarms + false_alarms
    if total == 0.0:
        return 0.0
    return true_alarms / total


def _false_alarm_verdict(false_alarm_rate: float) -> str:
    """One plain-language verdict line for a given expected false-alarm rate."""
    if false_alarm_rate < 0.25:
        return "OK - most alarms are real; safe to surface normally."
    if false_alarm_rate < 0.50:
        return "Noisy - surface softly (silent ring / digest), do not interrupt."
    if false_alarm_rate < 0.90:
        return "Mostly false - log it for later; do not interrupt the owner."
    return (
        "Almost all false - never raise this as an alert; fix the detector "
        "or raise its threshold first."
    )


def false_alarm_audit(alerts: list[dict]) -> list[dict]:
    """Audit proactive alert types against base-rate math.

    ``alerts`` is a list of dicts, each with keys ``name``, ``prevalence``,
    ``sensitivity``, ``specificity``. Returns one dict per alert, in order:
    the inputs echoed back, plus ``posterior`` (fraction of alarms that are
    real), ``false_alarm_rate`` (1 - posterior) and a ``verdict`` line.
    """
    results = []
    for alert in alerts:
        post = posterior(
            alert["prevalence"], alert["sensitivity"], alert["specificity"]
        )
        far = 1.0 - post
        results.append(
            {
                "name": alert["name"],
                "prevalence": float(alert["prevalence"]),
                "sensitivity": float(alert["sensitivity"]),
                "specificity": float(alert["specificity"]),
                "posterior": post,
                "false_alarm_rate": far,
                "verdict": _false_alarm_verdict(far),
            }
        )
    return results


# ---------------------------------------------------------------------------
# Step 02 — drift (PSI)
# ---------------------------------------------------------------------------

def _shares(counts, label: str) -> list[float]:
    values = [float(c) for c in counts]
    if not values:
        raise ValueError(f"{label} must contain at least one bin")
    if any(c < 0 for c in values):
        raise ValueError(f"{label} counts must be non-negative")
    total = sum(values)
    if total <= 0:
        raise ValueError(f"{label} counts must sum to a positive total")
    return [max(c / total, _PSI_SHARE_FLOOR) for c in values]


def psi(expected_counts, actual_counts) -> float:
    """Population Stability Index between a reference and a current window.

        PSI = sum over bins of (cur - ref) * ln(cur / ref)

    where ref/cur are the two distributions' shares (counts are normalised
    internally, so raw counts or shares both work), each share floored at
    1e-4 per the Phase 02 L20 method. PSI is symmetric KL, so argument order
    does not change the value. Identical distributions give exactly 0.

    Bands (see ``psi_band``): <0.1 stable, 0.1-0.25 moderate, >0.25
    significant. The bands are a convention, not a statistical test.
    """
    ref = _shares(expected_counts, "expected_counts")
    cur = _shares(actual_counts, "actual_counts")
    if len(ref) != len(cur):
        raise ValueError(
            f"bin count mismatch: expected {len(ref)} bins, got {len(cur)}"
        )
    return sum((c - r) * math.log(c / r) for r, c in zip(ref, cur))


def psi_band(value: float) -> str:
    """Interpret a PSI value: 'stable' | 'moderate' | 'significant'."""
    v = float(value)
    if v < 0.1:
        return "stable"
    if v <= 0.25:
        return "moderate"
    return "significant"


# ---------------------------------------------------------------------------
# Step 02 — paired comparison + eval kanoon
# ---------------------------------------------------------------------------

def _t_critical(df: int) -> float:
    return _T_CRIT_05.get(df, _T_CRIT_05_LARGE_DF)


def paired_t(diffs) -> dict:
    """Paired t-test on per-case score differences (Phase 02 L09/L15).

    ``diffs`` are paired differences (e.g. new_score - baseline_score on the
    SAME cases/folds). Returns a dict: ``n``, ``df`` (= n-1), ``mean``,
    ``std`` (sample std, Bessel-corrected), ``t`` and ``significant``
    (two-tailed, alpha 0.05; at df=4 the bar is |t| > 2.776 ~= 2.78).

    Degenerate spreads: all diffs equal and non-zero -> the difference is
    perfectly consistent, so t is infinite and significant; all diffs zero
    -> t = 0.0, not significant.
    """
    values = [float(d) for d in diffs]
    n = len(values)
    if n < 2:
        raise ValueError(f"paired_t needs at least 2 differences, got {n}")
    mean = sum(values) / n
    ss = sum((v - mean) ** 2 for v in values)
    std = math.sqrt(ss / (n - 1))
    df = n - 1
    if std == 0.0:
        t = 0.0 if mean == 0.0 else math.copysign(math.inf, mean)
    else:
        t = mean / (std / math.sqrt(n))
    crit = _t_critical(df)
    return {
        "n": n,
        "df": df,
        "mean": mean,
        "std": std,
        "t": t,
        "critical": crit,
        "significant": abs(t) > crit,
    }


def _load_consumed(state_path: str) -> dict:
    """Read the consumption registry; fail closed on a corrupt file."""
    if not os.path.exists(state_path):
        return {}
    with open(state_path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"eval state file {state_path!r} is corrupt ({exc}); "
                "refusing to compare — a broken registry must not silently "
                "re-open a consumed test set."
            ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("sets", {}), dict):
        raise ValueError(
            f"eval state file {state_path!r} has an unexpected shape; "
            "refusing to compare."
        )
    return data.get("sets", {})


def _save_consumed(state_path: str, sets: dict, set_id: str, n: int) -> None:
    sets[set_id] = {
        "consumed_at": datetime.now(timezone.utc).isoformat(),
        "n": n,
        "final": True,
    }
    parent = os.path.dirname(os.path.abspath(state_path))
    os.makedirs(parent, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "sets": sets}, fh, indent=2, sort_keys=True)


def compare_runs(
    baseline_scores,
    new_scores,
    state_path,
    *,
    set_id: str = "default",
    final: bool = False,
) -> dict:
    """Compare two versions on the SAME cases, under the eval kanoon.

    Computes per-case differences (new - baseline) and runs ``paired_t`` on
    them. ``state_path`` is a small JSON registry of consumed test sets:

    - If ``set_id`` was already consumed by a prior ``final=True`` run,
      raises :class:`TestSetConsumedError` — no comparison happens.
    - With ``final=True``, this run IS the one allowed final evaluation:
      the set is marked consumed in the registry before returning.
    - Non-final runs are exploratory (validation-style peeking during
      development is what validation sets are for) and do not consume.

    ``state_path`` must be a real path (``None`` is refused): the guard only
    works if consumption is written down somewhere durable.

    Returns the ``paired_t`` dict plus ``set_id``, ``baseline_mean``,
    ``new_mean``, ``mean_diff``, ``final`` and ``consumed`` (True once this
    set is in the registry — i.e. after a final run, including this one).
    """
    if state_path is None:
        raise ValueError(
            "state_path is required: eval-kanoon consumption must be tracked "
            "in a durable state file, not remembered."
        )
    baseline = [float(s) for s in baseline_scores]
    new = [float(s) for s in new_scores]
    if len(baseline) != len(new):
        raise ValueError(
            f"paired comparison needs equal-length score lists, got "
            f"{len(baseline)} baseline vs {len(new)} new"
        )
    if len(baseline) < 2:
        raise ValueError("paired comparison needs at least 2 cases")

    sets = _load_consumed(state_path)
    if set_id in sets:
        raise TestSetConsumedError(
            f"test set {set_id!r} was already consumed by a final evaluation "
            f"at {sets[set_id].get('consumed_at', 'unknown time')}. The test "
            "set is touched once; build a fresh set for the next verdict."
        )

    diffs = [n_ - b for b, n_ in zip(baseline, new)]
    result = paired_t(diffs)
    result.update(
        {
            "set_id": set_id,
            "baseline_mean": sum(baseline) / len(baseline),
            "new_mean": sum(new) / len(new),
            "mean_diff": result["mean"],
            "final": bool(final),
            "consumed": bool(final),
        }
    )
    if final:
        _save_consumed(state_path, sets, set_id, len(baseline))
    return result
