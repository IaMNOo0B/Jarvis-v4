"""SkillBook — JARVIS v4 (Sentient install, Spec 6).

The store for LEARNED SKILL PLAYBOOKS: named, reusable
procedures JARVIS proposed (or learned from the owner's
documents) and the owner accepted. A playbook is data —
{name, description, when_to_use, procedure, pitfalls,
verification, checks} — not code: it rides into the
brain's context as an excerpt when a turn matches its
when_to_use, the way instincts and calibration lines do.

The book also keeps the evidence the lifecycle runs on:
uses / successes / failures per skill (counted from real
run tool logs, never from claims), and the results of a
skill's self-checks. Self-checks are a contract (section
7.8): {file, expect} checks run BEFORE a proposal is even
queued; {run, expect} checks run after the owner accepts,
inside a scrubbed environment, and a skill whose run
checks fail is stored FLAGGED, never silently trusted.

No auto-archive: retirement is the owner's act. The book
only counts, surfaces, and remembers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path) -> threading.Lock:
    key = str(Path(path).resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


_TOKEN_RE = re.compile(r"[a-z0-9_]+")   # underscores stay:
# tool names (web_search) are single tokens by design.


def tokens(text: str) -> set:
    return set(_TOKEN_RE.findall(str(text).lower()))


def overlap(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def skill_text(skill: dict) -> str:
    """The searchable text of one playbook."""
    parts = [str(skill.get("name", "")),
             str(skill.get("description", "")),
             str(skill.get("when_to_use", ""))]
    parts += [str(s) for s in skill.get("procedure") or []]
    return " ".join(parts)


def named_tools(skill: dict, known_tools) -> set:
    """Which REAL tools a playbook's procedure names —
    the deterministic 'was this skill used?' signal: a
    run whose tool log covers at least half of these is
    counted as a use of the skill."""
    words = tokens(skill_text(skill))
    return {t for t in known_tools if t in words}


def _scrubbed_env() -> dict:
    """A minimal environment for self-check commands: no
    secrets ride along, ever (the Forge's rule)."""
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
            "LANG", "LC_ALL", "HOME")
    return {k: v for k, v in os.environ.items() if k in keep}


def run_checks(checks, base_dir=None) -> list:
    """Execute declared self-checks. File checks read and
    match; run checks execute in a fresh temp dir with a
    scrubbed environment and a hard timeout. Every result
    is evidence: {check, ok, detail}."""
    results = []
    for check in checks or []:
        if not isinstance(check, dict):
            continue
        expect = str(check.get("expect", ""))
        if check.get("file"):
            path = Path(str(check["file"]))
            if base_dir and not path.is_absolute():
                path = Path(base_dir) / path
            try:
                body = path.read_text(encoding="utf-8",
                                     errors="replace")
                ok = expect in body
                detail = "mila" if ok else "expect nahi mila"
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}"
            results.append({"check": f"file:{check['file']}",
                            "ok": ok, "detail": detail})
        elif check.get("run"):
            try:
                with tempfile.TemporaryDirectory(
                        prefix="jarvis-skillcheck-") as td:
                    proc = subprocess.run(
                        str(check["run"]), shell=True,
                        cwd=td, env=_scrubbed_env(),
                        capture_output=True, text=True,
                        timeout=30)
                blob = (proc.stdout or "") \
                    + (proc.stderr or "")
                ok = expect in blob
                detail = (f"exit {proc.returncode}; "
                          + ("mila" if ok
                             else "expect nahi mila"))
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: " \
                    f"{exc}"[:120]
            results.append({"check": f"run:{check['run']}",
                            "ok": ok, "detail": detail})
    return results


def file_checks_pass(checks, base_dir=None) -> bool:
    """The pre-queue gate: every {file, expect} check a
    draft declares must pass BEFORE the proposal exists."""
    file_checks = [c for c in checks or []
                   if isinstance(c, dict) and c.get("file")]
    if not file_checks:
        return True
    return all(r["ok"] for r in run_checks(file_checks,
                                           base_dir))


class SkillBook:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "learned_skills.json"
        self._lock = _lock_for(self.path)

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(
                encoding="utf-8"))
            if isinstance(data, dict) \
                    and isinstance(data.get("skills"), dict):
                return data
        except Exception:
            pass
        return {"skills": {}}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.path)

    def list(self) -> list:
        return list(self._load()["skills"].values())

    def get(self, name: str):
        return self._load()["skills"].get(str(name))

    def add(self, skill: dict) -> dict:
        """Create or replace one playbook (accept of a
        create/patch/merge proposal). Usage stats survive
        a patch — the evidence belongs to the name."""
        import time
        name = str(skill.get("name", "")).strip()
        if not name:
            raise ValueError("skill ka naam khaali hai")
        with self._lock:
            data = self._load()
            old = data["skills"].get(name, {})
            row = {
                "name": name,
                "description": str(
                    skill.get("description", ""))[:300],
                "when_to_use": str(
                    skill.get("when_to_use", ""))[:300],
                "procedure": [str(s)[:300] for s in
                              skill.get("procedure") or []],
                "pitfalls": [str(s)[:240] for s in
                             skill.get("pitfalls") or []],
                "verification": [str(s)[:240] for s in
                                 skill.get("verification")
                                 or []],
                "checks": [c for c in
                           skill.get("checks") or []
                           if isinstance(c, dict)],
                "uses": int(old.get("uses", 0)),
                "successes": int(old.get("successes", 0)),
                "failures": int(old.get("failures", 0)),
                "checks_status": old.get("checks_status",
                                         ""),
                "created": old.get("created") or time.time(),
                "updated": time.time(),
            }
            data["skills"][name] = row
            self._save(data)
            return row

    def remove(self, name: str) -> bool:
        with self._lock:
            data = self._load()
            if str(name) in data["skills"]:
                del data["skills"][str(name)]
                self._save(data)
                return True
        return False

    def record_use(self, name: str, ok: bool) -> None:
        with self._lock:
            data = self._load()
            row = data["skills"].get(str(name))
            if row is None:
                return
            row["uses"] = int(row.get("uses", 0)) + 1
            key = "successes" if ok else "failures"
            row[key] = int(row.get(key, 0)) + 1
            self._save(data)

    def set_checks_status(self, name: str,
                          status: str) -> None:
        with self._lock:
            data = self._load()
            row = data["skills"].get(str(name))
            if row is not None:
                row["checks_status"] = str(status)[:200]
                self._save(data)

    def context_lines(self, query: str, k: int = 2) -> list:
        """Playbooks whose when_to_use matches this turn,
        as brain-context lines (the return path that makes
        a learned skill actually get used)."""
        scored = []
        for skill in self.list():
            score = overlap(
                query, str(skill.get("when_to_use", ""))
                + " " + str(skill.get("name", "")))
            if score >= 0.2:
                scored.append((score, skill))
        scored.sort(key=lambda pair: -pair[0])
        lines = []
        for _score, skill in scored[:k]:
            steps = "; ".join(
                str(s) for s in
                (skill.get("procedure") or [])[:3])
            flag = ""
            if skill.get("checks_status", "").startswith(
                    "fail"):
                flag = " [checks fail hue the — dhyaan se]"
            lines.append(
                f"Seekha hua skill '{skill['name']}': "
                f"{skill.get('when_to_use', '')} — kadam: "
                f"{steps}{flag}")
        return lines
