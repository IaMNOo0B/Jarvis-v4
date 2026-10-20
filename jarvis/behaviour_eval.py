"""jarvis.behaviour_eval — behaviour-level evaluation for JARVIS v4.

WHY THIS EXISTS
---------------
Unit tests test CODE. Nothing tested BEHAVIOUR: on the owner's PC
(2026-10-20, the v2 build) a live JARVIS misrouted a greeting to a
finance skill, answered real requests with a bland deflection,
produced no tool action for "open chrome", created no reminder when
asked, went silent on long conversations, and hung on a provider whose
model name 404'd. Every one of those turns passed the unit suite.

This harness runs scripted turns through the SAME pipeline the owner
uses — jarvis.chat.ChatApp.turn (fast-path router -> Brain.respond ->
gate-mediated dispatch -> skills/memory) — and scores what the turn
OBSERVABLY did, read from the app's journal (tool.call / tool.result
events with gate verdicts), the landed effects (pending reminders,
stored facts), and the reply the user actually saw. Scoring is
deterministic. A model-graded judgement layer may be added later as a
SEPARATE optional module; it is never the default path and never part
of a pass/fail here.

CASES
-----
evals/cases.jsonl — one JSON object per line:
  id           unique case id
  input        the user turn to score
  origin       where this case came from (real failures cite the live
               session / document; nothing here is invented filler)
  category     MAST bucket: specification | coordination | verification,
               plus over_refusal for deflection failures
  expectation  {"kind": ..., "detail": {...}} — kinds:
                 tool_called        a tool.call event names detail.tool;
                                    detail.args_contains optionally checks
                                    call args; detail.effect optionally
                                    verifies a landed effect
                                    ({"check": "reminder_pending",
                                      "contains": "..."})
                 substantive_answer a real reply: non-empty, >=
                                    detail.min_chars, not a known
                                    deflection (detail.deflection_check),
                                    no detail.forbid_substrings, none of
                                    detail.forbid_tool_prefixes called
                 honest_fallback    the turn COMPLETED (no hang) with a
                                    reply that either answers
                                    substantively or admits failure
                                    honestly — never silence, never a
                                    fake success
                 memory_written     the Memory store holds a fact whose
                                    text contains detail.contains
  setup_turns     optional earlier user turns in the same session
  setup_commands  optional slash commands issued first through
                  ChatApp.handle_command (the CLI's own surface)
  brain_fault     optional {"status": 404, "message": "..."} — stub
                  mode only: the app's chain is swapped for one that
                  raises a provider HTTP error like a dead provider.
                  Ignored in provider mode, where the real fallback
                  chain is what is on trial.

MODES
-----
stub      ChatApp is built with its chain replaced by a single
          deterministic StubProvider (jarvis.providers' when it lands,
          jarvis._defaults' until then). Zero keys, zero network in the
          model seat; the fast-path router, gate, skills, memory and
          journal are the real v4 pipeline. Proves the harness and the
          pipeline structure end-to-end — NOT evidence about a real
          brain.
provider  ChatApp exactly as the owner runs it (the real provider
          chain). This is the mode that measures the REAL brain; run
          it on the owner's PC.

RUNNING
-------
  python -m jarvis.behaviour_eval --mode stub
  python -m jarvis.behaviour_eval --mode provider --timeout 90

Writes evals/BASELINE-V4.md (stub) / evals/PROVIDER-V4.md (provider)
and prints the same report. Exit code 0 when every case passes, 1
when any case fails, 2 on harness errors (bad case file, app won't
start).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CASES = BASE_DIR / "evals" / "cases.jsonl"

CATEGORIES = ("specification", "coordination", "verification",
              "over_refusal")
EXPECTATION_KINDS = ("tool_called", "substantive_answer",
                     "honest_fallback", "memory_written")

DEFAULT_TURN_TIMEOUT_S = 60.0

# Known bland deflections, normalized (lowercase, single-spaced). The
# 2026-10-20 live failure was a real request answered with exactly the
# first of these and nothing else. A reply counts as a deflection only
# when it is SHORT and is essentially the offer-to-help phrase — a long
# answer that happens to end politely is not a deflection.
_DEFLECTION_PHRASES = (
    "how can i assist you today",
    "how may i assist you today",
    "how can i help you today",
    "how may i help you today",
)
_DEFLECTION_MAX_CHARS = 120

# Phrases that mark an HONEST failure admission (the reply owns the
# failure instead of faking success or vanishing).
_HONEST_FAILURE_MARKERS = (
    "failed honestly",
    "brain call failed",
    "stopped honestly",
    "couldn't reach",
    "could not reach",
    "can't reach",
    "not available",
    "unable to",
    "no answer is better",
    "i stopped",
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).lower()).strip()


def is_deflection(text: str) -> bool:
    """True when the reply is a known bland deflection and nothing more."""
    norm = _normalize(text)
    if len(norm) > _DEFLECTION_MAX_CHARS:
        return False
    return any(p in norm for p in _DEFLECTION_PHRASES)


def _admits_failure(text: str) -> bool:
    norm = _normalize(text)
    return any(m in norm for m in _HONEST_FAILURE_MARKERS)


# --------------------------------------------------------------------------- #
# Case loading / validation
# --------------------------------------------------------------------------- #

def load_cases(path: Path) -> List[Dict[str, Any]]:
    """Load + validate the JSONL case bank. Raises ValueError naming the
    offending line — a malformed bank must fail loudly, never shrink
    silently (a silently-dropped case is a failure that looks like a pass)."""
    cases: List[Dict[str, Any]] = []
    seen: set = set()
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for lineno, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}")
        for key in ("id", "input", "origin", "category", "expectation"):
            if key not in case:
                raise ValueError(f"{path}:{lineno}: case missing {key!r}")
        if case["id"] in seen:
            raise ValueError(f"{path}:{lineno}: duplicate case id "
                             f"{case['id']!r}")
        seen.add(case["id"])
        if case["category"] not in CATEGORIES:
            raise ValueError(f"{path}:{lineno}: category "
                             f"{case['category']!r} not in {CATEGORIES}")
        exp = case["expectation"]
        if not isinstance(exp, dict) or exp.get("kind") not in EXPECTATION_KINDS:
            raise ValueError(f"{path}:{lineno}: expectation.kind must be "
                             f"one of {EXPECTATION_KINDS}")
        cases.append(case)
    if not cases:
        raise ValueError(f"{path}: no cases found")
    return cases


# --------------------------------------------------------------------------- #
# Stub / fault chains — the v4 seams are the CHAIN object (stub mode
# replaces it) and app.brain.chain (fault injection swaps it).
# --------------------------------------------------------------------------- #

def _provider_http_error_type():
    """The provider-error class the fault chain raises:
    jarvis.providers.ProviderHTTPError (the real one), with the spine
    default's as fallback."""
    try:
        from jarvis.providers import ProviderHTTPError
        return ProviderHTTPError
    except ImportError:
        from jarvis._defaults import ProviderHTTPError
        return ProviderHTTPError


def _stub_chain():
    """A chain of exactly one EvalBrainProvider (jarvis.eval_brain).

    The production StubProvider is an honest OFFLINE stub — it never
    requests tools, so with it no tool case can ever fire and routing
    is unmeasurable. Stub mode therefore seats a deterministic
    intent-routing TEST DOUBLE in the chain: it asks for the right
    tool on the banked requests, and the harness then measures the
    real pipeline around it (brain loop, gate, dispatch, effects,
    CompletionAudit). It does NOT measure a real model's intelligence
    — provider mode on the owner's PC does that; the baseline report
    says so, in so many words."""
    from jarvis.eval_brain import EvalBrainProvider
    try:
        from jarvis.providers import FallbackChain
        return FallbackChain([EvalBrainProvider()])
    except ImportError:
        from jarvis._defaults import FallbackChain
        return FallbackChain([EvalBrainProvider()])


class _FaultyChain:
    """Every completion raises the configured provider error — a dead
    provider, simulated (stub mode only)."""

    name = "faulty"
    last_provider = "faulty"

    def __init__(self, status: int, message: str) -> None:
        self._error_type = _provider_http_error_type()
        self._status = status
        self._message = message

    def complete(self, messages, tools=None):
        raise self._error_type(self._status, self._message)


# --------------------------------------------------------------------------- #
# Turn observation — everything scored comes from the JOURNAL the turn
# wrote and the reply ChatApp.turn returned: what the pipeline DID,
# not what it claims.
# --------------------------------------------------------------------------- #

@dataclass
class TurnObservation:
    reply: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    timed_out: bool = False
    duration_s: float = 0.0
    error: str = ""


def _observe(app, after_seq: int, returned_reply: str,
             obs: TurnObservation) -> TurnObservation:
    """Collect this turn's evidence from the journal. Both the brain
    and the dispatch wrapper journal tool.call — the brain's entry is
    the bare proposal (no verdict), the wrapper's carries the gate
    verdict. Entries are merged per (tool, args): the verdicted entry
    wins, so one real call is never double-counted."""
    reply = ""
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for rec in app.journal.replay():
        if int(rec.get("seq", 0)) <= after_seq:
            continue
        payload = rec.get("payload") or {}
        if rec.get("kind") == "tool.call":
            key = json.dumps(
                {"tool": payload.get("tool"),
                 "args": payload.get("args") or {}},
                sort_keys=True, default=str)
            if key not in merged:
                merged[key] = payload
                order.append(key)
            elif "gate_verdict" in payload:
                merged[key] = payload
        elif rec.get("kind") == "tool.result":
            obs.tool_results.append(payload)
        elif rec.get("kind") == "assistant.reply":
            if payload.get("text"):
                reply = str(payload["text"])
    obs.tool_calls = [merged[key] for key in order]
    obs.reply = returned_reply or reply
    return obs


def _run_one_turn(app, text: str, timeout_s: float) -> TurnObservation:
    """Drive ONE ChatApp.turn, bounded by a timeout thread. The stores
    are lock-guarded, so a worker thread is safe; a turn that outlives
    its timeout is reported timed_out (a hang is a RESULT, scored as
    failure — the runner moves on via the daemon thread)."""
    obs = TurnObservation()
    before = app.journal.seq
    returned: List[str] = []
    errors: List[Exception] = []

    def target() -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                returned.append(app.turn(text))
            except Exception as exc:  # a crashed turn is evidence too
                errors.append(exc)

    started = time.monotonic()
    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout_s)
    obs.duration_s = round(time.monotonic() - started, 2)
    obs.timed_out = worker.is_alive()
    if errors:
        obs.error = f"{type(errors[0]).__name__}: {errors[0]}"
    return _observe(app, before, returned[0] if returned else "", obs)


# --------------------------------------------------------------------------- #
# Deterministic scoring
# --------------------------------------------------------------------------- #

def _check_effect(app, effect: Dict[str, Any]) -> Tuple[bool, str]:
    """Verify a landed effect against real state (never the reply text)."""
    check = effect.get("check")
    contains = str(effect.get("contains", "")).lower()
    if check == "reminder_pending":
        try:
            listed = app.execute_tool("reminder_list", {})
        except Exception as exc:
            return False, f"effect check failed to run: {exc}"
        texts = [str(r.get("text", "")).lower()
                 for r in (listed or {}).get("reminders", [])]
        if any(contains in t for t in texts):
            return True, f"pending reminder contains {contains!r}"
        return False, (f"no pending reminder contains {contains!r} "
                       f"(pending: {texts[:3]})")
    return False, f"unknown effect check {check!r}"


def score_case(app, case: Dict[str, Any],
               obs: TurnObservation) -> Tuple[bool, List[str]]:
    """Score one observed turn against the case expectation.
    Returns (passed, reasons) — reasons always say WHY, pass or fail."""
    exp = case["expectation"]
    kind = exp["kind"]
    detail = exp.get("detail") or {}
    reasons: List[str] = []
    if obs.timed_out:
        return False, [f"turn TIMED OUT (>{obs.duration_s}s) — a hang "
                       "is a failure, not a pass"]
    if obs.error:
        reasons.append(f"turn raised: {obs.error}")

    called = [str(c.get("tool", "")) for c in obs.tool_calls]

    if kind == "tool_called":
        tool = str(detail.get("tool", ""))
        hits = [c for c in obs.tool_calls if c.get("tool") == tool]
        if not hits:
            return False, reasons + [
                f"{tool} was never called (tools called: "
                f"{called or 'none'})"]
        verdicts = [str(c.get("gate_verdict", "")) for c in hits]
        if any(v in ("allow", "allow_with_undo") for v in verdicts):
            reasons.append(f"{tool} called and gate-allowed "
                           f"(verdicts {verdicts})")
        elif "require_approval" in verdicts:
            reasons.append(f"{tool} called and PROPOSED — execution "
                           "awaits owner approval, which the harness "
                           "never grants (verdicts "
                           f"{verdicts})")
        else:
            return False, reasons + [
                f"{tool} was attempted but the gate DENIED it "
                f"(verdicts {verdicts}) — the pipeline refused to act, "
                "which is the failure this case guards"]
        failed_results = [r for r in obs.tool_results
                          if r.get("ok") is False]
        if failed_results:
            reasons.append(
                f"execution note: {len(failed_results)} tool result(s) "
                "reported errors this turn (see reply) — the call "
                "happened; its execution is a separate signal")
        for key, want in (detail.get("args_contains") or {}).items():
            got = str((hits[0].get("args") or {}).get(key, "")).lower()
            if str(want).lower() not in got:
                return False, reasons + [
                    f"{tool} arg {key}={got!r} does not contain "
                    f"{want!r}"]
            reasons.append(f"arg {key} contains {want!r}")
        effect = detail.get("effect")
        if effect:
            ok, why = _check_effect(app, effect)
            reasons.append(("effect: " if ok else "effect MISSING: ")
                           + why)
            if not ok:
                return False, reasons
        return True, reasons

    if kind == "substantive_answer":
        reply = obs.reply.strip()
        if not reply:
            return False, reasons + ["no reply was produced (silence)"]
        if detail.get("deflection_check", True) and is_deflection(reply):
            return False, reasons + [
                f"reply is a known bland deflection: {reply[:90]!r}"]
        min_chars = int(detail.get("min_chars", 10))
        if len(reply) < min_chars:
            return False, reasons + [
                f"reply too short ({len(reply)} < {min_chars} chars): "
                f"{reply[:90]!r}"]
        for sub in detail.get("forbid_substrings") or []:
            if str(sub).lower() in reply.lower():
                return False, reasons + [
                    f"reply contains forbidden text {sub!r} "
                    "(the failure signature this case guards)"]
        for prefix in detail.get("forbid_tool_prefixes") or []:
            bad = [t for t in called if t.startswith(str(prefix))]
            if bad:
                return False, reasons + [
                    f"forbidden tool(s) called: {bad}"]
        contains = detail.get("contains")
        if contains and str(contains).lower() not in reply.lower():
            return False, reasons + [
                f"reply does not contain {contains!r}: {reply[:120]!r}"]
        reasons.append(f"substantive reply ({len(reply)} chars)")
        return True, reasons

    if kind == "honest_fallback":
        reply = obs.reply.strip()
        if not reply:
            return False, reasons + [
                "no reply at all — silence is the failure this case "
                "guards, not an honest fallback"]
        if _admits_failure(reply):
            reasons.append("reply honestly admits the failure")
            return True, reasons
        if not is_deflection(reply) and len(reply) >= 20:
            reasons.append("turn completed with a substantive answer "
                           "(a fallback provider answered)")
            return True, reasons
        return False, reasons + [
            f"reply neither answers nor admits failure: {reply[:120]!r}"]

    if kind == "memory_written":
        contains = str(detail.get("contains", ""))
        try:
            hits = app.memory.recall(contains, k=10) if contains else []
        except Exception as exc:
            return False, reasons + [f"memory recall failed: {exc}"]
        blob = json.dumps(hits, default=str).lower()
        if contains and contains.lower() in blob:
            reasons.append(f"memory store holds a fact containing "
                           f"{contains!r}")
            return True, reasons
        return False, reasons + [
            f"memory store holds no fact containing {contains!r}"]

    return False, reasons + [f"unscored expectation kind {kind!r}"]


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

@dataclass
class CaseResult:
    case_id: str
    category: str
    passed: bool
    reasons: List[str]
    reply_preview: str
    tools_called: List[str]
    provider: str
    duration_s: float


def build_app(mode: str, data_dir: Path):
    """Construct ChatApp the v4 way. Stub mode swaps ONLY the chain
    for a lone StubProvider — fast-path router, gate, skills, memory
    and journal are the real pipeline either way."""
    from jarvis.chat import ChatApp
    if mode == "stub":
        return ChatApp(data_dir, chain=_stub_chain())
    return ChatApp(data_dir)


def run_cases(cases: List[Dict[str, Any]], mode: str,
              timeout_s: float = DEFAULT_TURN_TIMEOUT_S,
              data_dir: Optional[Path] = None) -> List[CaseResult]:
    """Run every case against ONE fresh app + data dir (a session is
    a conversation: setup turns and cases share it in file order,
    like the owner's real session did)."""
    own_tmp = None
    if data_dir is None:
        own_tmp = tempfile.TemporaryDirectory(prefix="jarvis-beval-")
        data_dir = Path(own_tmp.name) / "data"
    try:
        app = build_app(mode, data_dir)
        results: List[CaseResult] = []
        for case in cases:
            for cmd in case.get("setup_commands") or []:
                with contextlib.redirect_stdout(io.StringIO()):
                    app.handle_command(cmd)
            for setup_text in case.get("setup_turns") or []:
                _run_one_turn(app, setup_text, timeout_s)
            fault = case.get("brain_fault") if mode == "stub" else None
            orig_chain = app.chain
            orig_brain_chain = getattr(app.brain, "chain", None)
            if fault:
                faulty = _FaultyChain(int(fault.get("status", 500)),
                                      str(fault.get("message",
                                                     "simulated fault")))
                app.chain = faulty
                if hasattr(app.brain, "chain"):
                    app.brain.chain = faulty
            try:
                obs = _run_one_turn(app, str(case["input"]), timeout_s)
            finally:
                app.chain = orig_chain
                if hasattr(app.brain, "chain"):
                    app.brain.chain = orig_brain_chain
            passed, reasons = score_case(app, case, obs)
            results.append(CaseResult(
                case_id=str(case["id"]),
                category=str(case["category"]),
                passed=passed,
                reasons=reasons,
                reply_preview=obs.reply[:160],
                tools_called=[str(c.get("tool", ""))
                              for c in obs.tool_calls],
                provider=("faulty (injected provider error)"
                          if fault else app.last_provider),
                duration_s=obs.duration_s))
        return results
    finally:
        if own_tmp is not None:
            own_tmp.cleanup()


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def render_report(results: List[CaseResult], mode: str,
                  case_count: int) -> str:
    passed = sum(1 for r in results if r.passed)
    title = "BASELINE-V4" if mode == "stub" else "PROVIDER-V4"
    lines = [
        f"# Behaviour Eval — {title} ({mode} mode)",
        "",
        "Generated by `python -m jarvis.behaviour_eval --mode "
        f"{mode}`. Cases: evals/cases.jsonl ({case_count} cases, every "
        "one seeded from a documented real failure or an everyday owner "
        "request). Scoring is deterministic: journal tool.call events "
        "with gate verdicts, landed effects, and the reply the user "
        "actually saw.",
        "",
    ]
    if mode == "stub":
        lines += [
            "> Stub mode swaps ONLY the provider chain for the v4 "
            "StubProvider — an honest offline stub that never requests "
            "tools. The fast-path router, gate, skills, memory and "
            "journal are the real v4 pipeline, and the app runs with "
            "its default allowances (pc.control OFF). This baseline "
            "proves the harness, the fast paths, memory, and the "
            "brain's honest-stop audit — it is NOT evidence about a "
            "real brain or about brain-driven tool routing. Those "
            "numbers come from provider mode on the owner's PC.",
            "",
        ]
    lines += [f"## Totals: {passed}/{len(results)} passed", ""]
    lines.append("## By category")
    lines.append("")
    for cat in CATEGORIES:
        rows = [r for r in results if r.category == cat]
        if not rows:
            continue
        ok = sum(1 for r in rows if r.passed)
        lines.append(f"- {cat}: {ok}/{len(rows)}")
    lines += ["", "## Cases", ""]
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        lines.append(f"### [{mark}] {r.case_id} ({r.category})")
        lines.append(f"- tools called: "
                     f"{', '.join(r.tools_called) or 'none'}")
        lines.append(f"- provider: {r.provider} · {r.duration_s}s")
        if r.reply_preview:
            lines.append(f"- reply: {r.reply_preview!r}")
        for reason in r.reasons:
            lines.append(f"- {reason}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jarvis.behaviour_eval",
        description="Run the behaviour-eval case bank through the real "
                    "v4 ChatApp pipeline and score it deterministically.")
    parser.add_argument("--mode", choices=("stub", "provider"),
                        default="stub")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--timeout", type=float,
                        default=DEFAULT_TURN_TIMEOUT_S,
                        help="per-turn timeout in seconds (a hang fails)")
    parser.add_argument("--report", type=Path, default=None,
                        help="report path (default: evals/BASELINE-V4.md "
                             "for stub, evals/PROVIDER-V4.md for "
                             "provider)")
    args = parser.parse_args(argv)

    try:
        cases = load_cases(args.cases)
    except (ValueError, OSError) as exc:
        print(f"[behaviour_eval] case bank error: {exc}", file=sys.stderr)
        return 2
    try:
        results = run_cases(cases, args.mode, timeout_s=args.timeout)
    except Exception as exc:
        print(f"[behaviour_eval] harness error: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    report = render_report(results, args.mode, len(cases))
    report_path = args.report or (
        BASE_DIR / "evals" /
        ("BASELINE-V4.md" if args.mode == "stub" else "PROVIDER-V4.md"))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"[behaviour_eval] report written: {report_path}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
