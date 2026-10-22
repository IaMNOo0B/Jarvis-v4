"""Team — JARVIS v4, Wave 4 (Dodaios's coordination, combined
with this house's own rules).

A coding task too big for one pair of hands runs as a TEAM:

1. PLAN (the coordinator). One call splits the task into at
   most MAX_AGENTS subtasks, each with EXPLICIT file ownership.
   Ownership sets must not overlap — checked in code; an
   overlapping plan is rejected with the conflict named. No
   overlap, no merge conflicts: by construction, not by luck.
2. WORK (the workers). Each worker gets its own isolated
   workspace folder and works only its own files. Workers run
   in parallel; isolation is what makes that safe.
3. CRITIQUE (the critic). A SEPARATE call — fresh context, the
   brief, the rubric, and the actual files read back in code —
   judges each worker's output. The generator never judges its
   own work (this house's standing rule, and Dodaios's).
4. BOUNDED REPAIR. A failed subtask gets at most MAX_REPAIRS
   extra rounds with the critic's issues in hand. Then it is
   reported failed — honestly, with the issues attached. No
   infinite loops, ever.
5. MERGE + VERIFY. Owned files are copied into the output
   folder, then the deliverable is verified by EXECUTION:
   every Python file compiles; a tests/ folder, when present,
   runs under pytest. The report says what actually happened.

team.py owns the mechanism; the app supplies the brains
(planner / worker / critic callables) — tests drive it with
fakes, production with delegated Brains.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

MAX_AGENTS = 5
MAX_REPAIRS = 2
_FILE_CAP = 6000


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def validate_plan(raw) -> tuple:
    """(subtasks, '') or (None, honest error). Ownership overlap
    is a plan-killer, named file and all."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None, "planner ka jawab JSON nahi tha, sir"
    items = raw.get("subtasks") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        return None, "planner ne koi subtask nahi diya, sir"
    if len(items) > MAX_AGENTS:
        return None, (f"planner ne {len(items)} hisse banaye — "
                      f"had {MAX_AGENTS} hai, sir")
    seen = {}
    out = []
    for item in items:
        if not isinstance(item, dict):
            return None, "planner ka ek hissa kharab hai, sir"
        files = [str(f).strip() for f in item.get("files") or []
                 if str(f).strip()]
        if not files:
            return None, "har hisse ke paas kam se kam ek file " \
                         "honi chahiye, sir"
        for name in files:
            if name in seen:
                return None, (f"file '{name}' do hisson me hai "
                              f"({seen[name]} aur "
                              f"{item.get('title', '?')}) — ek file, "
                              "ek hi malik; plan radd")
            seen[name] = str(item.get("title", "?"))
        out.append({"title": str(item.get("title", "hissa")),
                    "files": files,
                    "brief": str(item.get("brief", ""))[:800]})
    return out, ""


# ---------------------------------------------------------------------------
# Verification (executable, never vibes)
# ---------------------------------------------------------------------------

def verify_output(output_dir: Path) -> dict:
    """Compile every .py; run pytest when a tests folder exists.
    Returns what execution actually said."""
    py_files = sorted(output_dir.rglob("*.py"))
    for path in py_files:
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
        except Exception as exc:
            return {"ok": False, "stage": "compile",
                    "detail": f"{path.name}: "
                              f"{type(exc).__name__}: {exc}"}
    tests_dir = output_dir / "tests"
    if tests_dir.is_dir():
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "tests"],
                cwd=str(output_dir), capture_output=True,
                text=True, timeout=300)
            tail = (proc.stdout + proc.stderr).strip()[-400:]
            return {"ok": proc.returncode == 0, "stage": "pytest",
                    "detail": tail}
        except Exception as exc:
            return {"ok": False, "stage": "pytest",
                    "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "stage": "compile",
            "detail": f"{len(py_files)} python files compile "
                      "ho gaye; tests folder nahi tha"}


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def _read_workspace(workspace: Path, files: list) -> str:
    parts = []
    total = 0
    for name in files:
        path = workspace / name
        if path.exists():
            text = path.read_text(encoding="utf-8",
                                 errors="replace")
            parts.append(f"--- {name} ---\n{text[:_FILE_CAP]}")
            total += len(text)
        else:
            parts.append(f"--- {name} ---\n(file nahi bani)")
        if total > _FILE_CAP * 2:
            break
    return "\n".join(parts)


def _one_subtask(task: str, subtask: dict, workspace: Path,
                 worker: Callable, critic: Callable) -> dict:
    workspace.mkdir(parents=True, exist_ok=True)
    issues = ""
    rounds = 0
    while True:
        rounds += 1
        summary = worker(task, subtask, workspace, issues)
        content = _read_workspace(workspace, subtask["files"])
        verdict = critic(task, subtask, content, summary)
        if verdict.get("ok"):
            return {"title": subtask["title"], "ok": True,
                    "rounds": rounds, "summary": str(summary)[:300]}
        issues = str(verdict.get("issues", ""))[:600]
        if rounds > MAX_REPAIRS:
            return {"title": subtask["title"], "ok": False,
                    "rounds": rounds, "issues": issues,
                    "summary": str(summary)[:300]}


def run_team(data_dir, task: str, planner: Callable,
             worker: Callable, critic: Callable,
             target_dir: Optional[Path] = None) -> dict:
    """Run the whole mechanism. planner(task) -> plan JSON;
    worker(task, subtask, workspace, issues) -> summary;
    critic(task, subtask, files_text, summary) -> {ok, issues}.
    Never raises; failures arrive in the report."""
    run_id = uuid.uuid4().hex[:8]
    base = Path(data_dir) / "team" / run_id
    report = {"run_id": run_id, "task": task[:200], "ts": time.time()}
    try:
        subtasks, error = validate_plan(planner(task))
    except Exception as exc:
        subtasks, error = None, f"planner fail: " \
                                f"{type(exc).__name__}: {exc}"
    if subtasks is None:
        report.update({"ok": False, "stage": "plan",
                       "error": error})
        return report
    results = []
    with ThreadPoolExecutor(max_workers=min(3, len(subtasks))) \
            as pool:
        futures = [
            pool.submit(_one_subtask, task, subtask,
                        base / f"agent-{i + 1}", worker, critic)
            for i, subtask in enumerate(subtasks)]
        for future in futures:
            results.append(future.result())
    output = Path(target_dir) if target_dir else base / "output"
    output.mkdir(parents=True, exist_ok=True)
    for i, (subtask, result) in enumerate(zip(subtasks, results)):
        if not result.get("ok"):
            continue
        workspace = base / f"agent-{i + 1}"
        for name in subtask["files"]:
            source = workspace / name
            if source.exists():
                dest = output / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, dest)
    verification = verify_output(output)
    report.update({
        "ok": all(r.get("ok") for r in results)
              and verification["ok"],
        "subtasks": results,
        "verification": verification,
        "output_dir": str(output)})
    return report
