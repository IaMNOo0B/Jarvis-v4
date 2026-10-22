"""Correction detection + lesson extraction — JARVIS v4, Wave 1.3.

The Nova audit's headline finding: Brain.respond already accepts
correction=True and captures golden pairs, and the FactStore already
implements supersession — but NOTHING in the live app ever detected
a correction, so the whole learn-from-being-wrong pipeline sat
dormant, and no caller anywhere passed supersedes=.

This module is the missing front half, in Nova's two-stage shape:

1. A zero-cost regex pre-filter (English + Hinglish) decides whether
   a message even SOUNDS like a correction. Almost no turns reach
   stage 2, so the cost of the feature is ~zero.
2. One structured model call confirms it and extracts
   {wrong, correct, lesson}. The confirmation matters: "actually, I
   was thinking about pasta tonight" trips loose patterns but is a
   topic change, not a correction — the model stage rejects it.

Detection never breaks a turn: any failure returns None and the
conversation proceeds exactly as before. Learning is fail-closed
(nothing is stored on doubt), conversation is fail-open.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

_PATTERNS = [
    # English
    r"\bthat'?s (wrong|incorrect|not right|not correct)\b",
    r"\byou'?re wrong\b",
    r"\byou (got|have) (it|that) wrong\b",
    r"\bnot [a-z]+[,.]? (it'?s|its|the answer is)\b",
    r"\bno[,.]? (it'?s|that'?s|the|not)\b",
    r"\bactually[,.]? (it'?s|its|the|that'?s)\b",
    r"\bincorrect\b",
    r"\bmistake\b",
    r"\bi (said|meant|told you)\b",
    r"\bnot ([a-z]+) but ([a-z]+)\b",
    r"\bcorrection\b",
    # Hinglish (Roman) — the owner's own correction language
    r"\bgalat\b",
    r"\bgalti\b",
    r"\baisa nahi\b",
    r"\baisi nahi\b",
    r"\bnahi hota\b",
    r"\bnahin hota\b",
    r"\bmaine kaha tha\b",
    r"\bmera matlab\b",
    r"\bsahi ye(h)? hai\b",
    r"\bnahi[,.]? [a-z]+ nahi\b",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _PATTERNS]

_EXTRACT_PROMPT = (
    "A user message may be correcting the assistant's previous "
    "reply.\nPrevious assistant reply: \"\"\"{prev}\"\"\"\n"
    "User message: \"\"\"{text}\"\"\"\n"
    "If the user is correcting something the assistant said or "
    "believed, answer with ONLY this JSON (no other words):\n"
    "{\"is_correction\": true, \"wrong\": \"<the wrong claim, "
    "short>\", \"correct\": \"<the correct claim, short>\", "
    "\"lesson\": \"<one sentence: what to remember>\"}\n"
    "If it is NOT a correction (a new topic, a question, a change "
    "of plans, small talk), answer with ONLY:\n"
    "{\"is_correction\": false}"
)


def looks_like_correction(text: str) -> bool:
    """Stage 1: the zero-cost regex pre-filter."""
    s = str(text or "")
    if len(s) < 4:
        return False
    return any(p.search(s) for p in _COMPILED)


def extract_correction(complete: Callable[..., Any], text: str,
                       prev_reply: str) -> Optional[dict]:
    """Stage 2: one structured model call. Returns
    {wrong, correct, lesson} for a confirmed correction, else None.
    `complete` is the app chain's complete(messages, tools)."""
    prompt = _EXTRACT_PROMPT.replace("{prev}", str(prev_reply)[:600]) \
                            .replace("{text}", str(text)[:600])
    try:
        reply = complete([{"role": "user", "content": prompt}], [])
    except Exception:
        return None
    if isinstance(reply, dict):
        content = reply.get("content") or ""
        error = reply.get("error")
    else:
        content = getattr(reply, "content", "") or ""
        error = getattr(reply, "error", None)
    if error or not str(content).strip():
        return None
    start = str(content).find("{")
    end = str(content).rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(str(content)[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("is_correction"):
        return None
    wrong = str(data.get("wrong") or "").strip()
    correct = str(data.get("correct") or "").strip()
    lesson = str(data.get("lesson") or "").strip()
    if not (wrong and correct and lesson):
        return None
    return {"wrong": wrong, "correct": correct, "lesson": lesson}
