"""The brain — JARVIS v4 spine S3 (Steps 11/14).

Shared spine contract:

    brain = Brain(chain, tool_schemas, dispatch, journal, memory)
    chain.complete(messages, tools) -> ProviderReply
    dispatch(tool_name, args) -> result        (gate-mediated, sibling)
    Brain.respond(history, user_text) -> BrainResult

Messages are OpenAI-shaped dicts throughout.

Design (from the ai-eng study + v2's proven behaviour):

- **Prompt**: assembled by `jarvis.prompt.PromptBuilder` — byte-stable
  persona/protocol prefix, memory + history below the boundary, budget
  ladder with soft cap ~3300 / hard budget from config. An over-budget
  minimal prompt raises PromptBudgetError; it is never sent.
- **ReAct loop**: max 3 rounds / 12 tool calls. A LoopGuard refuses an
  identical (tool, canonical-args) call a third consecutive time.
- **IFC framing** (Step 18): tool results are labelled via
  `jarvis.ifc.label_content` with origin "tool:<name>" — untrusted —
  and wrapped in <tool_output> DATA blocks. Tool text can inform the
  answer; it can never re-task the brain.
- **CompletionAudit**: if the owner asked for an action and no tool
  ran, or every tool failed, the final reply MUST admit it
  (stopped_reason "honest_stop:<reason>"). Success is never fabricated.
- **Golden pairs**: when the owner corrects the previous reply (the
  history flag, see below), the (prompt, chosen=correction,
  rejected=previous reply) triple is appended to the pairs file that
  `jarvis.dpo_data` consumes.

The history flag: callers mark a correction either by passing
``correction=True`` to respond(), or by ending ``history`` with the
current user message carrying ``"correction": true`` (that entry is
consumed as the flag carrier and is not duplicated in the prompt).

Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import ifc as ifc_mod
from .prompt import (PERSONA, PROTOCOL, PromptBudgetError, PromptBuilder,
                     tool_views)

__all__ = ["ProviderReply", "ToolCallRecord", "BrainResult", "Brain",
           "PromptBudgetError"]


# ---------------------------------------------------------------------------
# Spine data shapes
# ---------------------------------------------------------------------------

@dataclass
class ProviderReply:
    """What a provider chain returns from complete()."""
    content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    error: Optional[str] = None


@dataclass
class ToolCallRecord:
    tool: str
    args: Dict[str, Any]
    ok: bool
    error: Optional[str] = None


@dataclass
class BrainResult:
    reply: str
    tool_calls_made: List[ToolCallRecord] = field(default_factory=list)
    stopped_reason: str = "completed"   # completed | honest_stop:<reason>
    rounds: int = 0
    prompt_estimate: int = 0


# ---------------------------------------------------------------------------
# Reply normalization — the chain may answer with a ProviderReply, the
# eval shape ({"content": ..., "tool_calls": [...]}), an OpenAI chat
# completion dict, or a bare string. All become ProviderReply.
# ---------------------------------------------------------------------------

def _normalize_tool_call(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    fn = raw.get("function") if isinstance(raw.get("function"), dict) else raw
    name = fn.get("name") or raw.get("name") or ""
    if not name:
        return None
    args = fn.get("args", fn.get("arguments", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except (json.JSONDecodeError, ValueError):
            args = {"_raw": args}
    if not isinstance(args, dict):
        args = {"value": args}
    return {"name": str(name), "args": args}


def normalize_reply(raw: Any) -> ProviderReply:
    if isinstance(raw, ProviderReply):
        calls = [c for c in (_normalize_tool_call(t) for t in raw.tool_calls)
                 if c]
        return ProviderReply(content=raw.content or "", tool_calls=calls,
                             provider=raw.provider, error=raw.error)
    if isinstance(raw, str):
        return ProviderReply(content=raw)
    if isinstance(raw, dict):
        # OpenAI chat-completion shape.
        choices = raw.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            calls = [c for c in (_normalize_tool_call(t)
                                 for t in (msg.get("tool_calls") or [])) if c]
            return ProviderReply(content=str(msg.get("content") or ""),
                                 tool_calls=calls)
        calls = [c for c in (_normalize_tool_call(t)
                             for t in (raw.get("tool_calls") or [])) if c]
        err = raw.get("error")
        return ProviderReply(
            content=str(raw.get("content") or raw.get("text") or ""),
            tool_calls=calls,
            provider=str(raw.get("provider") or ""),
            error=(str(err) if err else None))
    if raw is None:
        return ProviderReply()
    return ProviderReply(content=str(raw))


# ---------------------------------------------------------------------------
# CompletionAudit helpers
# ---------------------------------------------------------------------------

_ACTION_PATTERNS = (
    re.compile(r"\b(open|launch|start|run|set|create|make|write|note|"
               r"remind|search|find|book|send|call|play|delete|remove|"
               r"download|upload|close|shut|turn\s+(on|off)|switch)\b", re.I),
    re.compile(r"(laga do|lagaa do|bana do|khol do|chalu kar|band kar|"
               r"yaad dila|set kar|book kar|bhej do|likh do|kholna|"
               r"reminder)", re.I),
)

_ADMISSION_MARKERS = ("couldn't", "could not", "cannot", "can't", "unable",
                      "failed", "did not", "didn't", "not done", "sorry",
                      "nahi ho", "nahin ho", "maafi", "koshish ki lekin")
_SUCCESS_MARKERS = ("done", "ho gaya", "opened", "created", "booked",
                    "launched", "set kar diya", "bana diya", "completed",
                    "successfully")


def requests_action(text: str) -> bool:
    """Heuristic: does this user text ask JARVIS to DO something?

    Deterministic and deliberately small — the audit only needs to know
    whether silence/no-tool is a lie for this turn, not to parse the
    language perfectly."""
    t = " ".join(str(text or "").split())
    return any(p.search(t) for p in _ACTION_PATTERNS)


def _admits_failure(text: str) -> bool:
    low = str(text or "").lower()
    return any(m in low for m in _ADMISSION_MARKERS)


def _claims_success(text: str) -> bool:
    low = str(text or "").lower()
    return any(m in low for m in _SUCCESS_MARKERS)


def _result_error(result: Any) -> Optional[str]:
    """The error text of a dispatch result, or None when it succeeded."""
    if isinstance(result, dict):
        if result.get("ok") is False:
            return str(result.get("error") or "tool reported failure")
        if "error" in result and result.get("ok") is not True:
            return str(result.get("error"))
        return None
    if isinstance(result, str) and result.strip().lower().startswith("error:"):
        return result.strip()[6:].strip() or "tool reported failure"
    return None


# ---------------------------------------------------------------------------
# The brain
# ---------------------------------------------------------------------------

class Brain:
    """One conversation brain over a provider chain + gated dispatch."""

    def __init__(self,
                 chain: Any,
                 tool_schemas: Sequence[Any] = (),
                 dispatch: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
                 journal: Any = None,
                 memory: Any = None,
                 *,
                 pairs_path: Optional[str] = None,
                 soft_cap_tokens: int = 3300,
                 hard_budget_tokens: int = 4000,
                 max_rounds: int = 3,
                 max_tool_calls: int = 12,
                 persona: str = PERSONA,
                 protocol: str = PROTOCOL):
        self.chain = chain
        self.views = tool_views(tool_schemas)
        # Simultaneity's safety map: which tools may run side by
        # side. A round of independent, non-destructive calls
        # executes CONCURRENTLY (the owner's order: bahut saari
        # cheezein ek saath); destructive — or merely UNKNOWN —
        # tools always run one by one, in order.
        self._destructive_tools = set()
        self._known_tools = set()
        for _schema in (tool_schemas or []):
            if not isinstance(_schema, dict):
                continue
            _fn = _schema.get("function") \
                if isinstance(_schema.get("function"), dict) \
                else _schema
            _name = _fn.get("name") or _schema.get("name") or ""
            if not _name:
                continue
            self._known_tools.add(str(_name))
            if _schema.get("destructive") or _fn.get("destructive"):
                self._destructive_tools.add(str(_name))
        self.dispatch = dispatch
        self.journal = journal
        self.memory = memory
        self.max_rounds = max(1, int(max_rounds))
        self.max_tool_calls = max(1, int(max_tool_calls))
        self._builder = PromptBuilder(self.views, soft_cap_tokens,
                                      hard_budget_tokens, persona, protocol)
        if pairs_path is not None:
            self.pairs_path = pairs_path
        elif journal is not None and getattr(journal, "path", None):
            self.pairs_path = os.path.join(
                os.path.dirname(os.path.abspath(journal.path)),
                "golden_pairs.jsonl")
        else:
            self.pairs_path = None

    # -- journal / memory plumbing ---------------------------------------

    def _log(self, kind: str, payload: Dict[str, Any]) -> None:
        if self.journal is not None:
            try:
                self.journal.append(kind, payload)
            except Exception:
                pass  # the journal must never break a turn

    def _memory_lines(self, user_text: str) -> List[str]:
        """Memory excerpts for the dynamic prompt region. Defensive:
        memory is a sibling-shaped object — excerpts/recall/search,
        a FactStore (current_facts), or a plain list of strings."""
        mem = self.memory
        if mem is None:
            return []
        if isinstance(mem, (list, tuple)):
            return [str(x) for x in mem][:8]
        for attr in ("excerpts", "recall", "search", "relevant"):
            fn = getattr(mem, attr, None)
            if callable(fn):
                try:
                    out = fn(user_text)
                except TypeError:
                    try:
                        out = fn()
                    except Exception:
                        continue
                except Exception:
                    continue
                if out:
                    return [str(x) for x in list(out)][:8]
        fn = getattr(mem, "current_facts", None)
        if callable(fn):
            try:
                facts = fn() or []
            except Exception:
                facts = []
            lines = []
            for fact in facts[:8]:
                if isinstance(fact, dict):
                    key = fact.get("key") or fact.get("name") or "fact"
                    val = fact.get("value") or fact.get("text") or ""
                    lines.append(f"{key}: {val}".strip())
                else:
                    lines.append(str(fact))
            return lines
        return []

    def _remember(self, text: str) -> None:
        """Offer a note (a compaction summary) to memory, when it takes
        notes at all. Best-effort by design."""
        mem = self.memory
        if mem is None:
            return
        for attr in ("add", "remember", "store", "note"):
            fn = getattr(mem, attr, None)
            if callable(fn):
                try:
                    fn(text)
                except Exception:
                    pass
                return

    # -- golden pairs ------------------------------------------------------

    def _capture_golden_pair(self, history: List[Dict[str, Any]],
                             correction_text: str) -> bool:
        """Append (prompt, chosen=correction, rejected=prev reply).

        The pair is shaped exactly for jarvis.dpo_data.build_dataset:
        {prompt, chosen, rejected, origin, source}."""
        prev_reply = ""
        prompt = ""
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            if msg.get("role") == "assistant" and not prev_reply:
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    prev_reply = content
                    for j in range(i - 1, -1, -1):
                        if history[j].get("role") == "user":
                            p = history[j].get("content")
                            if isinstance(p, str) and p.strip():
                                prompt = p
                            break
                    break
        if not (prompt and prev_reply and correction_text.strip()):
            self._log("brain.golden_pair",
                      {"captured": False,
                       "reason": "previous exchange incomplete"})
            return False
        record = {"prompt": prompt, "chosen": correction_text,
                  "rejected": prev_reply, "origin": "owner_correction",
                  "source": "golden_pair"}
        if self.pairs_path is None:
            self._log("brain.golden_pair",
                      {"captured": False, "reason": "no pairs_path configured"})
            return False
        parent = os.path.dirname(os.path.abspath(self.pairs_path))
        os.makedirs(parent, exist_ok=True)
        with open(self.pairs_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._log("brain.golden_pair", {"captured": True,
                                        "pairs_path": self.pairs_path})
        return True

    # -- tool execution ------------------------------------------------------

    def _frame_tool_result(self, name: str, result: Any) -> str:
        """Wrap a dispatch result as IFC-labelled DATA for the model.

        The label comes from the ORIGIN (tool:<name> — untrusted), and
        the <tool_output> wrapper is the framing: whatever imperative
        text the tool returned sits inside a data block the protocol
        section defines as never-an-instruction."""
        if isinstance(result, str):
            text = result
        else:
            text = json.dumps(result, ensure_ascii=False, default=str)
        labeled = ifc_mod.label_content(text, origin=f"tool:{name}")
        signals = [s["signal"] for s in
                   ifc_mod.screen(labeled)["injection_signals"]]
        err = _result_error(result)
        self._log("tool.result", {"tool": name, "ok": err is None,
                                  "error": err, "label": labeled.label,
                                  "origin": labeled.origin,
                                  "injection_signals": signals})
        key = getattr(self, "spotlight_key", b"")
        if key:
            # Spotlighting: unforgeable HMAC markers inside the
            # framing, so the data cannot fake its own end.
            text = ifc_mod.mark_outside(text, key)
        return (f'<tool_output tool="{name}" origin="tool:{name}" '
                f'trust="untrusted">\n{text}\n</tool_output>')

    def _is_destructive(self, name: str) -> bool:
        """Fail-safe: a tool the brain cannot vouch for (not in
        its catalog, or marked destructive) never runs in
        parallel."""
        return name not in self._known_tools \
            or name in self._destructive_tools

    def _execute(self, call: Dict[str, Any]) -> Any:
        name, args = call["name"], call["args"]
        self._log("tool.call", {"tool": name, "args": args})
        if self.dispatch is None:
            return {"ok": False,
                    "error": "no dispatcher configured for tool calls"}
        try:
            return self.dispatch(name, args)
        except Exception as exc:  # a crashing tool is a failed tool
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # -- the audit -----------------------------------------------------------

    def _audit(self, user_text: str, records: List[ToolCallRecord],
               reply_text: str) -> tuple[str, str]:
        """CompletionAudit. Returns (stopped_reason, final_reply)."""
        action = requests_action(user_text)
        if action and not records:
            reason = "honest_stop:no_tool_executed"
            if _admits_failure(reply_text) and not _claims_success(reply_text):
                return reason, reply_text
            return reason, (
                "I have to be straight with you, sir — I didn't actually "
                "run any tool for that, so nothing has been done yet. "
                "The request is noted; give me the means (or a working "
                "tool) and I will carry it out for real.")
        if records and all(not r.ok for r in records):
            reason = "honest_stop:tool_error"
            first_err = next((r.error for r in records if r.error),
                             "tool failed")
            if _admits_failure(reply_text) and not _claims_success(reply_text):
                return reason, reply_text
            return reason, (
                f"That didn't work, sir — the tool failed: {first_err}. "
                "Nothing was completed, and I won't pretend otherwise.")
        if not str(reply_text or "").strip():
            return "honest_stop:empty_reply", (
                "I don't have an answer for that yet, sir — and I'd "
                "rather say so than invent one.")
        return "completed", reply_text

    # -- main entry ------------------------------------------------------------

    def respond(self, history: Sequence[Dict[str, Any]], user_text: str,
                *, correction: bool = False) -> BrainResult:
        hist = [dict(m) for m in (history or [])]
        is_correction = bool(correction)
        # History-flag form: the current turn rides at the end of
        # history with "correction": true. Consume it as the carrier.
        if hist and hist[-1].get("role") == "user" \
                and hist[-1].get("correction") \
                and hist[-1].get("content") == user_text:
            hist.pop()
            is_correction = True
        if is_correction:
            self._capture_golden_pair(hist, user_text)

        assembled = self._builder.build(hist, user_text,
                                        self._memory_lines(user_text))
        if assembled.ladder_actions:
            self._log("brain.compaction",
                      {"actions": assembled.ladder_actions,
                       "dropped": assembled.dropped,
                       "estimate": assembled.estimate})
            if assembled.summary:
                self._remember(assembled.summary)
        self._log("brain.turn", {"user": str(user_text)[:200],
                                 "estimate": assembled.estimate,
                                 "correction": is_correction})

        messages = list(assembled.messages)
        tools = assembled.tools
        records: List[ToolCallRecord] = []
        last_call_key: Optional[str] = None
        last_call_run = 0
        rounds = 0
        final_text = ""
        provider_error: Optional[str] = None

        while True:
            rounds += 1
            try:
                raw = self.chain.complete(messages, tools)
            except Exception as exc:  # the whole chain is down
                provider_error = f"{type(exc).__name__}: {exc}"
                break
            reply = normalize_reply(raw)
            if reply.error:
                provider_error = reply.error
                break
            if not reply.tool_calls:
                final_text = reply.content
                break

            # Assistant turn carrying the requested calls.
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": [
                    {"id": f"call_{rounds}_{i}", "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": json.dumps(
                                      c["args"], ensure_ascii=False)}}
                    for i, c in enumerate(reply.tool_calls)],
            }
            messages.append(assistant_msg)

            # Plan the round first (LoopGuard + budget are
            # bookkeeping and stay strictly sequential), then
            # EXECUTE: a round of independent, non-destructive
            # calls runs CONCURRENTLY — many things at once —
            # while any destructive or unknown tool keeps the
            # whole round serial, in order.
            planned = []
            planned_execs = 0
            for i, call in enumerate(reply.tool_calls):
                call_id = f"call_{rounds}_{i}"
                key = (call["name"] + "|" + json.dumps(
                    call["args"], sort_keys=True, ensure_ascii=False,
                    default=str))
                if key == last_call_key:
                    last_call_run += 1
                else:
                    last_call_key, last_call_run = key, 1

                if last_call_run > 2:
                    self._log("brain.loopguard",
                              {"tool": call["name"], "args": call["args"],
                               "run": last_call_run})
                    planned.append({
                        "call_id": call_id, "call": call,
                        "refusal": (
                            "LoopGuard: this exact call was already "
                            "made twice in a row and has been refused. "
                            "Work with the results you already have, "
                            "or take a different approach.")})
                    continue
                if len(records) + planned_execs >= self.max_tool_calls:
                    planned.append({
                        "call_id": call_id, "call": call,
                        "refusal": (
                            f"Tool budget for this turn is exhausted "
                            f"({self.max_tool_calls} calls). Answer "
                            "with what you have.")})
                    continue
                planned.append({"call_id": call_id, "call": call,
                                "refusal": None})
                planned_execs += 1

            to_run = [(idx, p) for idx, p in enumerate(planned)
                      if p["refusal"] is None]
            results = {}
            parallel_ok = len(to_run) > 1 and not any(
                self._is_destructive(p["call"]["name"])
                for _, p in to_run)
            if parallel_ok:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(
                        max_workers=min(16, len(to_run))) as pool:
                    futures = {
                        pool.submit(self._execute, p["call"]): idx
                        for idx, p in to_run}
                    for future, idx in futures.items():
                        results[idx] = future.result()
            else:
                for idx, p in to_run:
                    results[idx] = self._execute(p["call"])

            for idx, p in enumerate(planned):
                if p["refusal"] is not None:
                    messages.append({"role": "tool",
                                     "tool_call_id": p["call_id"],
                                     "content": p["refusal"]})
                    continue
                result = results[idx]
                call = p["call"]
                err = _result_error(result)
                records.append(ToolCallRecord(tool=call["name"],
                                              args=call["args"],
                                              ok=err is None, error=err))
                messages.append({"role": "tool",
                                 "tool_call_id": p["call_id"],
                                 "content": self._frame_tool_result(
                                     call["name"], result)})

            if rounds >= self.max_rounds:
                # One forced text-only call: answer from what you have.
                messages.append({
                    "role": "system",
                    "content": "Tool rounds for this turn are exhausted. "
                               "Answer now, in text, from the results "
                               "above. Do not request more tools."})
                rounds += 1
                try:
                    raw = self.chain.complete(messages, [])
                    reply = normalize_reply(raw)
                    final_text = reply.content if not reply.error else ""
                    if reply.error:
                        provider_error = reply.error
                except Exception as exc:
                    provider_error = f"{type(exc).__name__}: {exc}"
                break

        if provider_error is not None:
            stopped = "honest_stop:provider_error"
            final_text = ("My language providers are unreachable right "
                          f"now, sir ({provider_error}). Nothing was "
                          "done — please try again in a little while.")
            self._log("brain.decision", {"decision": "stop",
                                         "stopped_reason": stopped,
                                         "reason": provider_error,
                                         "text": final_text})
            return BrainResult(reply=final_text, tool_calls_made=records,
                               stopped_reason=stopped, rounds=rounds,
                               prompt_estimate=assembled.estimate)

        stopped, final_text = self._audit(user_text, records, final_text)
        if stopped == "completed":
            self._log("brain.reply", {"text": final_text})
        else:
            self._log("brain.decision", {"decision": "stop",
                                         "stopped_reason": stopped,
                                         "reason": stopped,
                                         "text": final_text})
        return BrainResult(reply=final_text, tool_calls_made=records,
                           stopped_reason=stopped, rounds=rounds,
                           prompt_estimate=assembled.estimate)

    # -- self-consistency voting (Step 11) --------------------------------------

    def _default_sampler(self, question: str, _index: int) -> str:
        raw = self.chain.complete(
            [{"role": "user", "content": question}], [])
        return normalize_reply(raw).content

    @staticmethod
    def _vote_key(text: str) -> str:
        key = " ".join(str(text or "").lower().split())
        return key.strip(" .!?\"'`")

    def voted_answer(self, question: str, n: int = 5,
                     sampler: Optional[Callable[[str, int], Any]] = None) -> str:
        """Self-consistency: sample n answers, majority wins.

        The sampler is injectable for offline tests; the default asks
        the chain (temperature lives in the chain's own config — the
        mechanism here is sampling + normalized majority, nothing
        more). Ties break to the earliest-sampled answer, so the vote
        is deterministic for a deterministic sampler."""
        if n < 1:
            raise ValueError("n must be >= 1")
        sample = sampler or self._default_sampler
        tally: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for i in range(n):
            raw = sample(question, i)
            text = raw if isinstance(raw, str) else normalize_reply(raw).content
            key = self._vote_key(text)
            if key not in tally:
                tally[key] = {"text": text, "votes": 0, "first": i}
                order.append(key)
            tally[key]["votes"] += 1
        if not order:
            return ""
        winner = max(order, key=lambda k: (tally[k]["votes"],
                                           -tally[k]["first"]))
        self._log("brain.vote", {"question": str(question)[:120], "n": n,
                                 "winner_votes": tally[winner]["votes"],
                                 "distinct": len(order)})
        return tally[winner]["text"]
