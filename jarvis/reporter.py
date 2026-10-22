"""Run Reporter — JARVIS v4 (Sentient install, Spec 5).

Every substantial run — mission, delegated task, batch,
build — ends with ONE structured, truthful account the
owner can scan in ten seconds:

    {summary, done, failed, files, links, tools_used, open}

The report is written from EVIDENCE, never from the
worker's self-assessment: the inputs are the goal, the
run's real step log (tool calls with ok/error), and the
code-side outcome facts (status, `_result_ok` verdicts,
`served_by` marks, a build's binding report and computed
confidence). One tool-free brain (audit=False) narrates;
code then enforces the charter over its draft:

- anything the outcome facts mark failed is reported as
  failed in plain words, even if the narrative claimed
  success (code appends any failed step the draft missed);
- files and links are copied only from the log — a file or
  link the draft names that appears nowhere in the
  evidence is dropped, never invented;
- when the reporter brain is unavailable, slow, or
  unparseable, a DETERMINISTIC report (counts + failed
  step names) is produced instead — a run never ends
  reportless.

Also here: the findings fallback (section 7.6) — when a
run ends with tool results but no model answer, a plain
findings line is built deterministically from the records
instead of surfacing an empty admission.
"""

from __future__ import annotations

import json
import re
import threading

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
_FILE_HINT_RE = re.compile(
    r"[\w./\\-]+\.(?:md|txt|py|json|jsonl|html|csv|pdf|"
    r"png|jpg|jpeg|zip|docx|xlsx)\b")


def steps_from_records(records) -> list:
    """Brain ToolCallRecords -> reporter step dicts."""
    steps = []
    for rec in records or []:
        steps.append({
            "label": str(getattr(rec, "tool", "?")),
            "tool": str(getattr(rec, "tool", "?")),
            "ok": bool(getattr(rec, "ok", False)),
            "error": (str(getattr(rec, "error", ""))
                      if getattr(rec, "error", None) else None),
        })
    return steps


def findings_line(records) -> str:
    """The deterministic findings fallback: what the tools
    actually did, in plain words, when the model left no
    final answer."""
    records = list(records or [])
    if not records:
        return ""
    ok_names = [str(getattr(r, "tool", "?"))
                for r in records if getattr(r, "ok", False)]
    bad = [(str(getattr(r, "tool", "?")),
            str(getattr(r, "error", "") or "fail"))
           for r in records if not getattr(r, "ok", False)]
    bits = []
    if ok_names:
        bits.append("ye tools chale aur safal hue: "
                    + ", ".join(ok_names[:6]))
    if bad:
        bits.append("ye fail hue: " + "; ".join(
            f"{name} ({err[:60]})" for name, err in bad[:4]))
    return ("Model ne antim jawab nahi diya, lekin asal "
            "nateeja ye hai — " + "; ".join(bits) + ".")


def _evidence_text(goal, steps, outcome) -> str:
    parts = [str(goal or "")]
    for step in steps or []:
        parts.append(str(step.get("label", "")))
        if step.get("error"):
            parts.append(str(step["error"]))
    parts.append(str((outcome or {}).get("detail", "")))
    for key in ("files", "links"):
        parts.extend(str(x) for x in
                     (outcome or {}).get(key, []) or [])
    return "\n".join(parts)


def _harvest(goal, steps, outcome):
    """Files and links the EVIDENCE itself names — the
    only ones a report may ever carry."""
    text = _evidence_text(goal, steps, outcome)
    links = []
    for url in _URL_RE.findall(text):
        url = url.rstrip(".,;")
        if url not in links:
            links.append(url)
    files = [str(f) for f in (outcome or {}).get("files", [])
             or []]
    for name in _FILE_HINT_RE.findall(text):
        if name not in files:
            files.append(name)
    return files[:12], links[:12]


def deterministic_report(goal, steps, outcome) -> dict:
    """The no-brain report: counts and failed step names,
    straight from the log. Always available."""
    steps = list(steps or [])
    outcome = outcome or {}
    done = [str(s.get("label", "?")) for s in steps
            if s.get("ok") is True]
    failed = []
    for s in steps:
        if s.get("ok") is False:
            line = str(s.get("label", "?"))
            if s.get("error"):
                line += f" — {str(s['error'])[:90]}"
            failed.append(line)
    status = str(outcome.get("status", "") or "")
    detail = str(outcome.get("detail", "") or "")[:300]
    summary_bits = []
    if detail:
        summary_bits.append(detail)
    judged = len(done) + len(failed)
    if judged:
        summary_bits.append(
            f"{len(done)}/{judged} kadam safal"
            + (f", {len(failed)} fail" if failed else "")
            + ".")
    if status and status not in detail:
        summary_bits.insert(0, f"Haal: {status}.")
    files, links = _harvest(goal, steps, outcome)
    tools_used = []
    for s in steps:
        name = str(s.get("tool") or s.get("label", ""))
        if name and name not in tools_used:
            tools_used.append(name)
    open_items = [str(x) for x in outcome.get("open", [])
                  or []]
    if status and status not in ("done", "completed") \
            and detail:
        open_items.append(detail[:160])
    return {"summary": " ".join(summary_bits)[:600]
            or "Run khatm hua.",
            "done": done[:12], "failed": failed[:12],
            "files": files, "links": links,
            "tools_used": tools_used[:12],
            "open": open_items[:6],
            "source": "deterministic"}


def _ask_brain(brain_factory, prompt: str, timeout: float):
    """One guarded brain call: the factory's brain answers
    in a daemon thread; a slow or crashing brain yields
    None (the caller falls back deterministically) instead
    of stalling the run's ending."""
    box = {}

    def _run():
        try:
            brain = brain_factory()
            out = brain.respond([], prompt)
            box["reply"] = str(out.reply)
        except Exception as exc:
            box["error"] = exc

    worker = threading.Thread(target=_run, daemon=True,
                              name="jarvis-reporter")
    worker.start()
    worker.join(max(0.5, float(timeout)))
    if worker.is_alive() or "reply" not in box:
        return None
    return box["reply"]


def _json_from_reply(text: str):
    cleaned = str(text or "").strip()
    if "```" in cleaned:
        cleaned = cleaned.split("```")[1].strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except Exception:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(cleaned[start:end + 1])
            except Exception:
                return None
    return None


def build_report(brain_factory, *, goal, steps, outcome,
                 timeout: float = 10.0) -> dict:
    """The run's report: brain narration when it can be
    trusted, deterministic accounting always underneath.
    `brain_factory` may be None (deterministic only)."""
    steps = list(steps or [])
    outcome = outcome or {}
    base = deterministic_report(goal, steps, outcome)
    if brain_factory is None:
        return base
    step_lines = "\n".join(
        f"- [{'ok' if s.get('ok') else 'FAIL'}] "
        f"{s.get('label', '?')}"
        + (f" — {str(s['error'])[:100]}"
           if s.get("error") else "")
        for s in steps[:20]) or "(koi tool kadam nahi)"
    prompt = (
        f"RUN GOAL: {str(goal)[:300]}\n"
        f"KADAM (asal log):\n{step_lines}\n"
        f"CODE-SIDE NATIJAA: status="
        f"{outcome.get('status', '?')}; "
        f"{str(outcome.get('detail', ''))[:300]}\n"
        "Tum Run Reporter ho. Upar ke SABOOT se ek report "
        "likho — worker ke daavon se nahi. Jo kadam FAIL "
        "hai woh failed me saaf likhna hai, chahe summary "
        "kuch bhi kahe. Files/links sirf wahi jo log me "
        "dikh rahe hain — naye kabhi mat banao.\n"
        'Reply ONLY with JSON: {"summary": "2-3 line me '
        'asal me kya hua", "done": ["jo pura hua"], '
        '"failed": ["jo fail hua, wajah ke saath"], '
        '"files": [], "links": [], "open": ["jo owner ke '
        'liye bacha hai"]}')
    reply = _ask_brain(brain_factory, prompt, timeout)
    parsed = _json_from_reply(reply) if reply else None
    if not isinstance(parsed, dict) \
            or not str(parsed.get("summary", "")).strip():
        return base
    report = dict(base)
    report["summary"] = str(parsed["summary"])[:600]
    report["source"] = "brain"
    for key in ("done", "open"):
        rows = parsed.get(key)
        if isinstance(rows, list) and rows:
            report[key] = [str(x)[:160] for x in rows][:12]
    # The charter, enforced in code: every failed step is
    # reported failed, whatever the narration said.
    said_failed = " ".join(
        str(x) for x in (parsed.get("failed") or [])
        if isinstance(x, str)).lower()
    merged_failed = [str(x)[:160] for x in
                     (parsed.get("failed") or [])
                     if isinstance(x, str)][:12]
    for line in base["failed"]:
        label = line.split(" — ")[0].strip().lower()
        if label and label not in said_failed:
            merged_failed.append(line)
    report["failed"] = merged_failed[:12]
    # Files/links: only what the evidence itself names.
    evidence = _evidence_text(goal, steps, outcome)
    for key in ("files", "links"):
        rows = parsed.get(key)
        if isinstance(rows, list):
            kept = [str(x) for x in rows
                    if str(x) in evidence]
            for item in base[key]:
                if item not in kept:
                    kept.append(item)
            report[key] = kept[:12]
    return report


def render_text(report: dict) -> str:
    """The ten-second scan: summary, then the lists that
    matter, one line each."""
    lines = [str(report.get("summary", ""))[:600]]
    if report.get("failed"):
        lines.append("Fail: " + "; ".join(
            str(x)[:110] for x in report["failed"][:4]))
    if report.get("files"):
        lines.append("Files: " + ", ".join(
            str(x) for x in report["files"][:5]))
    if report.get("links"):
        lines.append("Links: " + ", ".join(
            str(x) for x in report["links"][:5]))
    if report.get("open"):
        lines.append("Baaki: " + "; ".join(
            str(x)[:110] for x in report["open"][:3]))
    return "\n".join(line for line in lines if line)
