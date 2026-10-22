"""Build tasks — Antigravity's Planning Mode, JARVIS-bound.

Reverse-engineered from Google's Antigravity (study:
jarvis-knowledge/antigravity-study) and rebuilt on JARVIS's
own rails. The flow Google made famous:

  PLAN   — an implementation plan artifact with a fixed
           schema (Goal → review notes → Proposed Changes
           tagged [NEW]/[MODIFY]/[DELETE] → Verification
           Plan). Nothing executes before the plan exists.
  GATE   — planning mode waits for the owner's Proceed;
           fast mode walks straight through (his choice).
  BUILD  — the plan's steps execute one by one; task.md is
           a LIVE checklist on disk, ticked as steps land.
  PROVE  — the plan's own Verification Plan is RUN, and the
           plan is BOUND to reality: tree snapshots before
           and after execution are diffed, and every planned
           file change is checked against what actually
           changed. Antigravity's known hole — the agent
           ignoring its own approved plan — is closed here
           mechanically: missing work and unplanned extras
           are printed in the walkthrough, never hidden.
  REPORT — walkthrough.md: what was built, the binding
           report, verification results, and a confidence
           that is COMPUTED (verification pass rate +
           binding completeness) with its justification —
           Antigravity's ConfidenceScore, made truthful.

Artifacts live under data/builds/<id>/ — outside any repo,
per build, exactly like the original design.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

PLAN_ACTIONS = ("NEW", "MODIFY", "DELETE")
MAX_SNAPSHOT_FILES = 5000
MAX_HASH_BYTES = 2_000_000


# ------------------------------------------------------------------ #
# The plan schema
# ------------------------------------------------------------------ #

def _safe_relpath(path: str) -> bool:
    p = str(path).replace("\\", "/")
    if not p or p.startswith("/") or p.startswith("~"):
        return False
    return all(part not in ("", "..") for part in p.split("/"))


def validate_plan(plan) -> list:
    """Problems with a candidate plan; [] means it may be
    filed. A plan without changes, steps, or a verification
    plan is refused — the Verification Plan is the soul of
    the schema: the builder commits, BEFORE building, to how
    success will be proven."""
    problems = []
    if not isinstance(plan, dict):
        return ["plan ek object hi nahi hai"]
    if not str(plan.get("goal", "")).strip():
        problems.append("goal khali hai")
    changes = plan.get("changes") or []
    if not changes:
        problems.append("changes khali hain — koi file badlegi "
                        "hi nahi to build kaisa")
    for i, ch in enumerate(changes[:40]):
        if not isinstance(ch, dict):
            problems.append(f"change {i + 1} object nahi hai")
            continue
        if ch.get("action") not in PLAN_ACTIONS:
            problems.append(
                f"change {i + 1}: action "
                f"{ch.get('action')!r} — NEW/MODIFY/DELETE me "
                "se ek chahiye")
        if not _safe_relpath(str(ch.get("path", ""))):
            problems.append(
                f"change {i + 1}: path "
                f"{ch.get('path')!r} surakshit relative path "
                "nahi hai")
        if not str(ch.get("what", "")).strip():
            problems.append(f"change {i + 1}: 'what' khali hai")
    steps = plan.get("steps") or []
    if not steps or not any(str(s).strip() for s in steps):
        problems.append("steps khali hain")
    verify = plan.get("verification") or []
    if not verify:
        problems.append("verification plan khali hai — "
                        "safalta kaise saabit hogi, pehle se "
                        "likhna padta hai")
    for i, item in enumerate(verify[:12]):
        if not isinstance(item, dict) or \
                not str(item.get("detail", "")).strip():
            problems.append(f"verification {i + 1}: detail "
                            "khali hai")
        elif item.get("kind") not in ("command", "check"):
            problems.append(
                f"verification {i + 1}: kind "
                f"{item.get('kind')!r} — command ya check "
                "chahiye")
    return problems


def render_plan_md(plan: dict) -> str:
    lines = [f"# Implementation Plan — {plan.get('goal', '')}",
             ""]
    notes = plan.get("review_notes") or []
    if notes:
        lines += ["## Dhyaan dene wali baatein (User Review)", ""]
        lines += [f"> ⚠️ {n}" for n in notes]
        lines.append("")
    lines += ["## Proposed Changes", ""]
    for ch in plan.get("changes") or []:
        lines.append(f"- [{ch.get('action')}] `{ch.get('path')}`"
                     f" — {ch.get('what')}")
    lines += ["", "## Kaam ke kadam", ""]
    lines += [f"{i + 1}. {s}" for i, s in
              enumerate(plan.get("steps") or [])]
    lines += ["", "## Verification Plan", ""]
    for item in plan.get("verification") or []:
        lines.append(f"- ({item.get('kind')}) "
                     f"{item.get('detail')}")
    lines.append("")
    return "\n".join(lines)


def render_task_md(plan: dict, step_states: list) -> str:
    """The live checklist: [x] done, [/] running, [ ] pending,
    [!] failed — Antigravity's task.md, ticked from disk."""
    marks = {"done": "x", "running": "/", "failed": "!",
             "pending": " "}
    lines = [f"# Task — {plan.get('goal', '')}", ""]
    for i, step in enumerate(plan.get("steps") or []):
        state = step_states[i] if i < len(step_states) \
            else "pending"
        lines.append(f"- [{marks.get(state, ' ')}] {step}")
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Tree snapshots — the plan↔execution binding
# ------------------------------------------------------------------ #

def snapshot_tree(root) -> dict:
    """{relative path: sha256} for the tree under root."""
    root = Path(root)
    out = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if len(out) >= MAX_SNAPSHOT_FILES:
            break
        if not path.is_file() or ".git" in path.parts \
                or "__pycache__" in path.parts \
                or path.suffix == ".pyc":
            continue
        rel = path.relative_to(root).as_posix()
        try:
            data = path.read_bytes()[:MAX_HASH_BYTES]
        except OSError:
            continue
        out[rel] = hashlib.sha256(data).hexdigest()
    return out


def diff_trees(before: dict, after: dict) -> dict:
    created = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(p for p in set(before) & set(after)
                      if before[p] != after[p])
    return {"created": created, "modified": modified,
            "deleted": deleted}


def binding_report(plan: dict, diff: dict) -> dict:
    """Every planned change, checked against the real diff.
    done = the file changed the way the plan said; missing =
    planned but untouched; extras = changed but never
    planned. Nothing is hidden: all three lists ship in the
    walkthrough."""
    changed = {"NEW": set(diff["created"]),
               "MODIFY": set(diff["modified"]),
               "DELETE": set(diff["deleted"])}
    # A NEW file later edited still counts as created; a
    # MODIFY that was actually a create counts too — what
    # matters is that the planned path changed at all.
    any_changed = (set(diff["created"]) | set(diff["modified"])
                   | set(diff["deleted"]))
    done, missing = [], []
    planned_paths = set()
    for ch in plan.get("changes") or []:
        path = str(ch.get("path", "")).replace("\\", "/")
        planned_paths.add(path)
        hit = path in changed.get(ch.get("action"), set()) \
            or (ch.get("action") in ("NEW", "MODIFY")
                and path in any_changed)
        (done if hit else missing).append(
            f"[{ch.get('action')}] {path}")
    extras = sorted(p for p in any_changed
                    if p not in planned_paths)
    return {"done": done, "missing": missing, "extras": extras}


def computed_confidence(verify_results: list,
                        binding: dict) -> tuple:
    """(score, justification) — never a vibe. Half the score
    is the verification pass rate, half is how much of the
    plan actually landed; the justification names both."""
    if verify_results:
        passed = sum(1 for r in verify_results if r.get("ok"))
        v_rate = passed / len(verify_results)
        v_txt = f"verification {passed}/{len(verify_results)}"
    else:
        v_rate, v_txt = 0.0, "verification kuch chala hi nahi"
    total = len(binding["done"]) + len(binding["missing"])
    if total:
        b_rate = len(binding["done"]) / total
        b_txt = (f"plan ke {len(binding['done'])}/{total} "
                 "badlav asal me hue")
    else:
        b_rate, b_txt = 0.0, "plan me badlav hi nahi the"
    return round(0.5 * v_rate + 0.5 * b_rate, 2), \
        f"{v_txt}; {b_txt}"


# ------------------------------------------------------------------ #
# The store — data/builds/<id>/
# ------------------------------------------------------------------ #

class BuildStore:
    STATUSES = ("planning", "awaiting_proceed", "executing",
                "verifying", "done", "failed", "denied")

    def __init__(self, data_dir):
        self.root = Path(data_dir) / "builds"
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, build_id: str) -> Path:
        return self.root / build_id

    def create(self, build_id: str, goal: str, plan: dict,
               mode: str, workspace: str) -> dict:
        d = self._dir(build_id)
        (d / "workspace").mkdir(parents=True, exist_ok=True)
        state = {"id": build_id, "goal": goal, "plan": plan,
                 "mode": mode, "workspace": workspace,
                 "status": "awaiting_proceed"
                 if mode == "planning" else "executing",
                 "step_states": ["pending"] * len(
                     plan.get("steps") or []),
                 "created": time.time(), "finished": None,
                 "confidence": None, "note": ""}
        (d / "implementation_plan.md").write_text(
            render_plan_md(plan), encoding="utf-8")
        (d / "task.md").write_text(
            render_task_md(plan, state["step_states"]),
            encoding="utf-8")
        self.save(state)
        return state

    def save(self, state: dict) -> None:
        d = self._dir(state["id"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=1),
            encoding="utf-8")
        (d / "task.md").write_text(
            render_task_md(state["plan"],
                           state["step_states"]),
            encoding="utf-8")

    def load(self, build_id: str):
        path = self._dir(build_id) / "state.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def list_builds(self, limit: int = 10) -> list:
        out = []
        for d in sorted(self.root.iterdir(), reverse=True):
            state = self.load(d.name)
            if state:
                out.append({k: state.get(k) for k in
                            ("id", "goal", "status", "mode",
                             "created", "confidence")})
            if len(out) >= limit:
                break
        return out

    def write_walkthrough(self, build_id: str, text: str) -> None:
        (self._dir(build_id) / "walkthrough.md").write_text(
            text, encoding="utf-8")
