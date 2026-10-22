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
import os
import re
import threading
import time
from pathlib import Path

# One lock per builds root, shared across BuildStore
# instances (audit 2026-10-22: the Proceed gate did
# load-check-save with no lock at all — a double Proceed,
# UI + chat together, started the executor twice).
_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(root) -> threading.RLock:
    key = str(Path(root).resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


_ID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _check_id(build_id: str) -> str:
    """A build id is ONE path segment — anything else could
    walk out of the builds root."""
    bid = str(build_id)
    if not _ID_OK.match(bid):
        raise ValueError(f"build id theek nahi hai: {bid!r}")
    return bid

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
    # Windows drive paths — absolute (C:/x, C:\\x) AND
    # drive-relative (D:x) — are not relative paths at all
    # (audit 2026-10-22: "C:" survived as an ordinary first
    # segment and passed).
    if len(p) >= 2 and p[1] == ":" and p[0].isalpha():
        return False
    # "." and empty segments normalize away; what remains must
    # be real names, none of them "..".
    parts = [part for part in p.split("/")
             if part not in ("", ".")]
    if not parts:
        return False
    return all(part != ".." for part in parts)


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
    # EVERY change is validated (audit 2026-10-22: the [:40]
    # and [:12] slices were silent caps — item 41 could be
    # ../evil.py and never be looked at).
    for i, ch in enumerate(changes):
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
    for i, item in enumerate(verify):
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

def _hash_file(path) -> str:
    """sha256 of the first MAX_HASH_BYTES, STREAMED in chunks
    (audit 2026-10-22: read_bytes() loaded the whole file —
    a 2 GB video in the workspace was read fully just to hash
    its first 2 MB)."""
    digest = hashlib.sha256()
    remaining = MAX_HASH_BYTES
    with open(path, "rb") as fh:
        while remaining > 0:
            chunk = fh.read(min(65536, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def snapshot_tree(root) -> dict:
    """{relative path: sha256} for the tree under root."""
    snap, _truncated = snapshot_tree_full(root)
    return snap


def snapshot_tree_full(root):
    """(snapshot, truncated) — truncated says the file cap
    cut the walk short, so a diff against this snapshot can
    miss changes; the walkthrough must say so."""
    root = Path(root)
    out = {}
    truncated = False
    if not root.exists():
        return out, truncated
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts \
                or "__pycache__" in path.parts \
                or path.suffix == ".pyc":
            continue
        if len(out) >= MAX_SNAPSHOT_FILES:
            truncated = True
            continue
        rel = path.relative_to(root).as_posix()
        try:
            out[rel] = _hash_file(path)
        except OSError:
            continue
    return out, truncated


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
    # A NEW/MODIFY counts done ONLY when the path was created
    # or modified — never when it was DELETED (audit
    # 2026-10-22: the old any_changed union let a deleted-only
    # diff score planned NEWs as done). A DELETE counts done
    # only when the path is actually gone.
    landed = set(diff["created"]) | set(diff["modified"])
    deleted = set(diff["deleted"])
    any_changed = landed | deleted
    done, missing = [], []
    planned_paths = set()
    for ch in plan.get("changes") or []:
        path = str(ch.get("path", "")).replace("\\", "/")
        planned_paths.add(path)
        if ch.get("action") == "DELETE":
            hit = path in deleted
        else:
            hit = path in landed
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
    score = 0.5 * v_rate + 0.5 * b_rate
    extras = len(binding.get("extras") or [])
    if extras:
        # Unplanned changes dilute the score (audit
        # 2026-10-22: 50 extras once scored a perfect 1.0).
        if total:
            score *= total / (total + extras)
        else:
            score = 0.0
        b_txt += f"; {extras} bina-plan badlav bhi hue"
    return round(score, 2), f"{v_txt}; {b_txt}"


# ------------------------------------------------------------------ #
# The store — data/builds/<id>/
# ------------------------------------------------------------------ #

class BuildStore:
    STATUSES = ("planning", "awaiting_proceed", "executing",
                "verifying", "done", "failed", "denied")

    def __init__(self, data_dir):
        self.root = Path(data_dir) / "builds"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = _lock_for(self.root)

    def _dir(self, build_id: str) -> Path:
        return self.root / _check_id(build_id)

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
        with self._lock:
            d = self._dir(state["id"])
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / "state.json.tmp"
            tmp.write_text(
                json.dumps(state, ensure_ascii=False, indent=1),
                encoding="utf-8")
            os.replace(tmp, d / "state.json")
            (d / "task.md").write_text(
                render_task_md(state["plan"],
                               state["step_states"]),
                encoding="utf-8")

    def load(self, build_id: str):
        try:
            path = self._dir(build_id) / "state.json"
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def claim(self, build_id: str, approve: bool):
        """Atomically move awaiting_proceed → executing (or
        denied) and return the state — or None when this call
        did NOT win the transition (already claimed, unknown,
        or in any other state). The Proceed gate's whole
        load-check-save happens under the store lock, so two
        Proceeds can never both start the executor."""
        with self._lock:
            state = self.load(build_id)
            if not state \
                    or state.get("status") != "awaiting_proceed":
                return None
            state["status"] = "executing" if approve else "denied"
            self.save(state)
            return state

    def list_builds(self, limit: int = 10) -> list:
        # LATEST first by state mtime (audit 2026-10-22: the
        # old sort was by directory NAME — uuid hex — so the
        # freshest build could sit invisible past the limit
        # while ancient ones filled the HUD's list).
        dirs = [d for d in self.root.iterdir() if d.is_dir()]

        def _mtime(d):
            try:
                return (d / "state.json").stat().st_mtime
            except OSError:
                return 0.0

        out = []
        for d in sorted(dirs, key=_mtime, reverse=True):
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
