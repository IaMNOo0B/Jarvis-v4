"""Automations — saved routes for repeated work.

The agentic pattern Owais named: JARVIS already has a brain;
what it lacked was a way to take a job it knows how to do
(form filling, scraping, a posting routine, any repeated
sequence), SAVE the route once, and run it on command without
re-thinking it every time. An automation is a named list of
steps — {tool, args} — where args may carry {{param}}
placeholders filled at run time.

Execution honours the whole house: every step goes through
the app dispatch, so the gate, the idempotency ledger, the
rate limiter and — for destructive steps — the confirmation
cards all apply exactly as if the brain had called the tool
itself. A run stops honestly: a step that files a card pauses
the run as waiting_approval (approve the card, then re-run
with start_step to continue); a failed step reports itself
and the steps that remain, so the brain can finish the job
with judgment — the mechanical route first, the thinking
route as its fallback.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MAX_AUTOMATIONS = 100
MAX_STEPS = 25
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def placeholders_in(value: Any) -> set:
    """Every {{name}} used anywhere inside an args value."""
    found = set()
    if isinstance(value, str):
        found.update(_PLACEHOLDER.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            found |= placeholders_in(v)
    elif isinstance(value, list):
        for v in value:
            found |= placeholders_in(v)
    return found


def render(value: Any, params: Dict[str, Any]) -> Any:
    """Substitute {{name}} from params. A placeholder whose
    param is absent stays visible in the output so the caller
    can name exactly what's missing."""
    if isinstance(value, str):
        def _sub(m):
            key = m.group(1)
            return str(params[key]) if key in params else m.group(0)
        return _PLACEHOLDER.sub(_sub, value)
    if isinstance(value, dict):
        return {k: render(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, params) for v in value]
    return value


def derive_triggers(name: str, title: str) -> List[str]:
    """Default trigger phrases for a route: its title said
    plainly, and its name with spaces. Matching is phrase-
    based — one stray word never fires a route."""
    out = []
    for phrase in (str(title or "").strip().lower(),
                   str(name or "").replace("-", " ").strip()):
        if phrase and phrase not in out:
            out.append(phrase)
    return out


def match_automations(rows: List[Dict], text: str):
    """[(row, trigger)] for every route whose trigger phrase
    appears in the text — most specific (longest) first. A
    trigger must be a real phrase (>= 8 chars or >= 2 words):
    routes fire on intent, never on one stray word."""
    low = " " + str(text or "").lower() + " "
    hits = []
    for row in rows or []:
        triggers = row.get("triggers") or derive_triggers(
            row.get("name", ""), row.get("title", ""))
        for trig in triggers:
            t = str(trig).strip().lower()
            if not t:
                continue
            if len(t) < 8 and len(t.split()) < 2:
                continue
            if t in low:
                hits.append((row, t))
                break
    hits.sort(key=lambda h: -len(h[1]))
    return hits


def signature_of(steps) -> str:
    """A route's fingerprint: the tool sequence + static args
    (placeholders normalized away). Two jobs with the same
    signature are the same job."""
    import hashlib
    norm = []
    for step in steps or []:
        args = _PLACEHOLDER.sub("{{}}", json.dumps(
            step.get("args", {}), ensure_ascii=False,
            sort_keys=True, default=str))
        norm.append((step.get("tool", ""), args))
    return hashlib.sha256(
        json.dumps(norm, ensure_ascii=False).encode(
            "utf-8")).hexdigest()[:16]


def parameterize_steps(steps, user_text: str):
    """Lift the owner's own words out of a finished job's args
    into {{params}}: any string value that appears verbatim
    in what the owner said becomes a slot. Returns
    (new_steps, param_names)."""
    text = str(user_text or "")
    params: List[str] = []
    slots: Dict[str, str] = {}

    def lift(value):
        if isinstance(value, str):
            if len(value) >= 4 and value in text:
                if value not in slots:
                    name = f"p{len(params) + 1}"
                    slots[value] = name
                    params.append(name)
                return "{{" + slots[value] + "}}"
            return value
        if isinstance(value, dict):
            return {k: lift(v) for k, v in value.items()}
        if isinstance(value, list):
            return [lift(v) for v in value]
        return value

    return ([{"tool": s.get("tool", ""),
              "args": lift(s.get("args", {}))} for s in steps],
            params)


def validate_steps(steps, known_tools) -> Optional[str]:
    """Creation-time honesty: a route may only name tools that
    exist, and every placeholder it uses must be declared.
    Returns an error string, or None when the route is sound."""
    if not isinstance(steps, list) or not steps:
        return "steps khali hain — kam az kam ek kadam chahiye"
    if len(steps) > MAX_STEPS:
        return f"steps ki hadd {MAX_STEPS} hai"
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or not step.get("tool"):
            return f"kadam {i + 1} me tool ka naam nahi hai"
        if step["tool"] not in known_tools:
            return (f"kadam {i + 1}: tool {step['tool']!r} "
                    "is JARVIS ke paas hai hi nahi")
        if not isinstance(step.get("args", {}), dict):
            return f"kadam {i + 1}: args ek object hone chahiye"
    return None


class AutomationBook:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "automations.json"
        self._lock = threading.Lock()

    def _rows(self) -> List[Dict]:
        try:
            rows = json.loads(
                self.path.read_text(encoding="utf-8"))
            return rows if isinstance(rows, list) else []
        except Exception:
            return []

    def _save(self, rows: List[Dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(rows, ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass

    def all(self) -> List[Dict]:
        return self._rows()

    def seen_count(self, signature: str) -> int:
        """How often a job-signature has been observed (the
        repetition ledger behind auto-creation: a job earns
        its route the SECOND time it shows up)."""
        path = self.path.with_name("automations_seen.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return int((data or {}).get(signature, 0))
        except Exception:
            return 0

    def note_seen(self, signature: str) -> int:
        path = self.path.with_name("automations_seen.json")
        with self._lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
            data[signature] = int(data.get(signature, 0)) + 1
            try:
                path.write_text(json.dumps(data),
                                encoding="utf-8")
            except Exception:
                pass
            return data[signature]

    def create(self, name: str, title: str, steps: List[Dict],
               params: List[str], triggers: List[str] = None,
               origin: str = "manual") -> Tuple[Optional[Dict],
                                                 Optional[str]]:
        slug = re.sub(r"[^a-z0-9_-]+", "-",
                      str(name or "").strip().lower()).strip("-")
        if not slug:
            return None, "naam khali hai"
        with self._lock:
            rows = self._rows()
            if any(r.get("name") == slug for r in rows):
                return None, (f"{slug!r} naam ka automation "
                              "pehle se hai — dusra naam chunein "
                              "ya pehle use hatayein")
            if len(rows) >= MAX_AUTOMATIONS:
                return None, f"automations ki hadd {MAX_AUTOMATIONS} puri hai"
            used = set()
            for step in steps:
                used |= placeholders_in(step.get("args", {}))
            undeclared = used - set(params or [])
            if undeclared:
                return None, ("in placeholders ke params elaan "
                              "nahi hue: "
                              + ", ".join(sorted(undeclared)))
            row = {"id": uuid.uuid4().hex[:8], "name": slug,
                   "title": str(title or slug),
                   "steps": steps,
                   "params": list(params or []),
                   "triggers": list(triggers) if triggers
                   else derive_triggers(slug, title or slug),
                   "origin": origin,
                   "signature": signature_of(steps),
                   "created": time.time(), "runs": 0,
                   "last_status": ""}
            rows.append(row)
            self._save(rows)
            return row, None

    def get(self, name: str) -> Optional[Dict]:
        slug = re.sub(r"[^a-z0-9_-]+", "-",
                      str(name or "").strip().lower()).strip("-")
        for row in self._rows():
            if row.get("name") == slug:
                return row
        return None

    def list(self) -> List[Dict]:
        out = []
        for row in self._rows():
            out.append({"name": row.get("name"),
                        "title": row.get("title"),
                        "steps": len(row.get("steps") or []),
                        "params": row.get("params") or [],
                        "runs": row.get("runs", 0),
                        "last_status": row.get("last_status", "")})
        return out

    def remove(self, name: str) -> bool:
        slug = re.sub(r"[^a-z0-9_-]+", "-",
                      str(name or "").strip().lower()).strip("-")
        with self._lock:
            rows = self._rows()
            kept = [r for r in rows if r.get("name") != slug]
            if len(kept) == len(rows):
                return False
            self._save(kept)
            return True

    def note_run(self, name: str, status: str) -> None:
        with self._lock:
            rows = self._rows()
            for row in rows:
                if row.get("name") == name:
                    row["runs"] = int(row.get("runs", 0)) + 1
                    row["last_status"] = status
            self._save(rows)


def run_steps(dispatch, auto: Dict, params: Dict[str, Any],
              start_step: int = 0) -> Dict:
    """Execute a saved route through the app's dispatch.
    Returns a run report: done / waiting_approval / failed /
    missing_params, with per-step outcomes and what remains."""
    declared = list(auto.get("params") or [])
    missing = [p for p in declared if p not in (params or {})]
    if missing:
        return {"ok": False, "status": "missing_params",
                "automation": auto.get("name"),
                "missing": missing,
                "error": "ye params chahiye: " + ", ".join(missing)}
    steps = auto.get("steps") or []
    done: List[Dict] = []
    for i in range(max(0, int(start_step)), len(steps)):
        step = steps[i]
        args = render(step.get("args", {}), params or {})
        result = dispatch(step["tool"], args)
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        if result.get("needs_approval"):
            return {"ok": False, "status": "waiting_approval",
                    "automation": auto.get("name"),
                    "stopped_at_step": i + 1,
                    "card": result.get("card"),
                    "completed": done,
                    "error": (f"kadam {i + 1} ({step['tool']}) par "
                              "confirmation card bana hai. Card "
                              "manzoor karke run_automation "
                              f"start_step={i + 1} se dobara "
                              "chalayein — route wahi se aage "
                              "badhega.")}
        # The brain's own success rule (chat._result_ok): an
        # explicit ok=False, or an error payload without
        # ok=True, is a failure.
        failed = result.get("ok") is False or (
            "error" in result and result.get("ok") is not True)
        if failed:
            return {"ok": False, "status": "failed",
                    "automation": auto.get("name"),
                    "stopped_at_step": i + 1,
                    "completed": done,
                    "remaining": steps[i + 1:],
                    "error": (f"kadam {i + 1} ({step['tool']}) "
                              f"fail: {result.get('error')}")}
        brief = result.get("result", result)
        done.append({"step": i + 1, "tool": step["tool"],
                     "ok": True,
                     "brief": str(brief)[:160]})
    return {"ok": True, "status": "done",
            "automation": auto.get("name"),
            "completed": done,
            "message": f"{auto.get('name')} automation poora "
                       f"chala — {len(done)} kadam."}
