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

The owner's second order (the self-healing law): an error is
not the end. When a step crashes or the judge calls stuck,
the loop does NOT give up on the spot — a repair_fn (the
doctor) diagnoses WHAT the error is, WHERE it comes from and
its ROOT CAUSE, then prescribes retry (with a concrete
instruction for what to do differently), skip (abandon this
step, keep walking) or abort (the road is truly closed; the
mission ends with the root cause in its report). Repairs are
bounded per step and per mission, and written into the step
log — where the worker brain reads them on the retry, so the
same mistake is not made twice in the dark.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

DEFAULT_MAX_STEPS = 10
MAX_STEPS = 25
MAX_REPAIRS_PER_STEP = 2
MAX_REPAIRS_TOTAL = 6


def run_mission(goal: str,
                step_fn: Callable[[str, List[Dict], int], str],
                check_fn: Callable[[str, List[Dict]], Dict],
                *,
                max_steps: int = DEFAULT_MAX_STEPS,
                should_stop: Optional[Callable[[], bool]] = None,
                on_step: Optional[Callable[[int, Dict, Dict],
                                           None]] = None,
                repair_fn: Optional[Callable[
                    [str, List[Dict], str], Dict]] = None,
                max_repairs_per_step: int = MAX_REPAIRS_PER_STEP,
                max_repairs_total: int = MAX_REPAIRS_TOTAL,
                ) -> Dict[str, Any]:
    """Walk `goal` one verified step at a time.

    step_fn(goal, log, n) does ONE real step and returns its
    summary. check_fn(goal, log) returns {state, note,
    confidence} with state in done | continue | stuck. Ends on
    done, stuck, the step budget, a stop, or a checker verdict
    of "owner" (JARVIS itself judges the owner must decide —
    the one interruption that is allowed).

    With repair_fn(goal, log, failure) supplied, a crashed
    step or a stuck verdict first goes to the doctor:
    {error, origin, root_cause, strategy, instruction} with
    strategy retry | skip | abort. Retry re-runs the SAME
    step with the diagnosis in the log; skip abandons the
    step and walks on; abort ends the mission with the root
    cause named. Without repair_fn the loop behaves exactly
    as it always did — the first failure ends the mission."""
    max_steps = max(1, min(MAX_STEPS, int(max_steps)))
    log: List[Dict] = []
    repairs: List[Dict] = []
    state, note = "stuck", "koi kadam hi nahi chala"
    finished = False
    n = 0
    while n < max_steps and not finished:
        n += 1
        if should_stop is not None and should_stop():
            state, note = "stopped", "owner ne rok diya"
            finished = True
            break
        attempts = 0
        while True:
            attempts += 1
            failure = None
            try:
                summary = str(step_fn(goal, log, n))[:600]
            except Exception as exc:  # a crashed step
                failure = (f"kadam crash: {type(exc).__name__}: "
                           f"{exc}")
                summary = failure
            if failure is None:
                log.append({"step": n, "summary": summary})
                verdict = check_fn(goal, log) or {}
                if on_step is not None:
                    on_step(n, verdict, log[-1])
                state = str(verdict.get("state", "continue"))
                note = str(verdict.get("note", ""))[:300]
                if state in ("done", "owner"):
                    finished = True
                    break
                if state != "stuck":
                    break  # continue → the step is complete
                failure = note or "judge ne stuck kaha"
            else:
                log.append({"step": n, "summary": summary})
            # ---- the self-healing cycle -------------------- #
            can_repair = (
                repair_fn is not None
                and attempts <= max_repairs_per_step
                and len(repairs) < max_repairs_total)
            if not can_repair:
                state, note = "stuck", failure[:300]
                finished = True
                break
            try:
                fix = repair_fn(goal, log, failure) or {}
            except Exception:
                fix = {}
            strategy = str(fix.get("strategy", "retry"))
            if strategy not in ("retry", "skip", "abort"):
                strategy = "retry"
            root_cause = (str(fix.get("root_cause", ""))[:240]
                          or failure[:240])
            record = {"step": n, "attempt": attempts,
                      "error": str(fix.get("error", ""))[:200],
                      "origin": str(fix.get("origin", ""))[:200],
                      "root_cause": root_cause,
                      "strategy": strategy}
            repairs.append(record)
            log.append({
                "step": n,
                "summary": (f"repair: error — "
                            f"{record['error'] or failure[:120]}; "
                            f"root cause — {root_cause}; "
                            f"ilaaj — "
                            f"{str(fix.get('instruction', ''))[:240]}")})
            if strategy == "abort":
                state, note = "stuck", f"root cause: {root_cause}"
                finished = True
                break
            if strategy == "skip":
                break  # this step is abandoned; walk on
            # retry: loop re-runs step n; the diagnosis is in
            # the log the worker reads.
    if not finished:
        state = "budget"
        note = (f"kadam ki had ({max_steps}) puri — goal abhi "
                "baki hai; chaaho to mission dobara chalao, "
                "woh wahin se aage badhega jahan log chhoda tha")
    return {"goal": goal, "state": state, "note": note,
            "steps": log, "steps_taken": len(log),
            "repairs": repairs, "repairs_made": len(repairs)}
