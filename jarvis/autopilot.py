"""Autopilot — a mission JARVIS carries to its goal itself.

The owner's order: give a task ONCE; JARVIS uses everything it
has, calibrates its decisions, walks step by step to the goal,
and does NOT interrupt the owner with "yeh hua, woh hua" — it
reports at the END, or when it is genuinely stuck. This module
is the loop; the app supplies the two brains (a worker that
takes one real step, a checker that judges done / continue /
stuck with a stated confidence). The loop is bounded by a step
budget and honors the owner's stop — autonomy with a leash of
exactly the length the owner set.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

DEFAULT_MAX_STEPS = 10
MAX_STEPS = 25


def run_mission(goal: str,
                step_fn: Callable[[str, List[Dict], int], str],
                check_fn: Callable[[str, List[Dict]], Dict],
                *,
                max_steps: int = DEFAULT_MAX_STEPS,
                should_stop: Optional[Callable[[], bool]] = None,
                on_step: Optional[Callable[[int, Dict, Dict],
                                           None]] = None,
                ) -> Dict[str, Any]:
    """Walk `goal` one verified step at a time.

    step_fn(goal, log, n) does ONE real step and returns its
    summary. check_fn(goal, log) returns {state, note,
    confidence} with state in done | continue | stuck. Ends on
    done, stuck, the step budget, a stop, or a checker verdict
    of "owner" (JARVIS itself judges the owner must decide —
    the one interruption that is allowed)."""
    max_steps = max(1, min(MAX_STEPS, int(max_steps)))
    log: List[Dict] = []
    state, note = "stuck", "koi kadam hi nahi chala"
    for n in range(1, max_steps + 1):
        if should_stop is not None and should_stop():
            state, note = "stopped", "owner ne rok diya"
            break
        try:
            summary = str(step_fn(goal, log, n))[:600]
        except Exception as exc:  # a crashed step is a stuck one
            summary = f"kadam crash: {type(exc).__name__}: {exc}"
            log.append({"step": n, "summary": summary})
            state, note = "stuck", summary
            break
        log.append({"step": n, "summary": summary})
        verdict = check_fn(goal, log) or {}
        if on_step is not None:
            on_step(n, verdict, log[-1])
        state = str(verdict.get("state", "continue"))
        note = str(verdict.get("note", ""))[:300]
        if state in ("done", "stuck", "owner"):
            break
    else:
        state = "budget"
        note = (f"kadam ki had ({max_steps}) puri — goal abhi "
                "baki hai; chaaho to mission dobara chalao, "
                "woh wahin se aage badhega jahan log chhoda tha")
    return {"goal": goal, "state": state, "note": note,
            "steps": log, "steps_taken": len(log)}
