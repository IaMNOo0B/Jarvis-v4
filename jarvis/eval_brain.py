"""EvalBrainProvider — a deterministic intent-routing TEST DOUBLE.

WHY THIS EXISTS
---------------
The production chain's last resort (providers.StubProvider) is an
honest *offline* stub: it never requests tools, by design. That makes
it useless for measuring the behaviour pipeline — routing -> gate ->
dispatch -> landed effects — because no tool case can ever fire.
(v2 solved the same problem with its EvalStubBrain; this is v4's.)

WHAT IT IS / IS NOT
-------------------
EvalBrainProvider sits in the chain seat ONLY inside the behaviour
eval harness (jarvis.behaviour_eval, stub mode). It is a minimal
*competent* router written by hand:

- "open chrome"            -> open_app{name: chrome}
- "web search about X"     -> web_search{query: X}
- "... reminder ..."       -> reminder_set{in_minutes, text}
- a tool result coming back -> a short completion reply
- anything else            -> a plain substantive reply

It measures whether the PIPELINE (brain loop, gate, skills, memory,
effects) does its job when the brain asks for the right thing. It does
NOT measure a real model's intelligence — provider mode on the owner's
PC does that. It is never part of the production chain:
providers.build_chain never includes it (pinned by a test).

Stdlib only.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .providers import ProviderReply, ToolCall

_OPEN_RE = re.compile(
    r"\b(?:open|launch|start|kholo|khol do)\s+([a-z0-9][a-z0-9 ._-]*)",
    re.IGNORECASE)
_MINUTES_RE = re.compile(r"(\d+)\s*(?:minute|min)\b", re.IGNORECASE)
_REMINDER_SCAFFOLD = (
    "reminder laga do", "reminder laga de", "reminder set kar do",
    "reminder set karo", "reminder", "yaad dila do", "yaad dilana",
    "mujhe", "please",
)


def _offered_names(tools) -> set:
    names = set()
    for t in tools or ():
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        if not name and isinstance(t.get("function"), dict):
            name = t["function"].get("name")
        if name:
            names.add(name)
    return names


def _last_user_text(messages) -> str:
    for m in reversed(messages or ()):
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                return content
    return ""


def _has_tool_result(messages) -> bool:
    for m in reversed(messages or ()):
        if isinstance(m, dict) and m.get("role") == "tool":
            return True
        if isinstance(m, dict) and m.get("role") == "user":
            return False
    return False


def _reminder_text(raw: str) -> str:
    """Compose the reminder's text from the owner's own words: drop the
    duration phrase and the command scaffolding, keep the topic."""
    text = _MINUTES_RE.sub("", raw)
    low = text.lower()
    for phrase in _REMINDER_SCAFFOLD:
        low = low.replace(phrase, " ")
    topic = re.sub(r"\s+", " ", low).strip(" .,")
    return topic or raw.strip()


class EvalBrainProvider:
    """Provider-shaped deterministic router for the eval harness."""

    name = "eval-brain"
    timeout = 5.0

    def complete(self, messages, tools=None) -> ProviderReply:
        offered = _offered_names(tools)

        if _has_tool_result(messages):
            return ProviderReply(
                text="Ho gaya, sir — jo kaha tha, wo ho chuka hai aur "
                     "is session me darj hai.",
                tool_calls=[], model="eval-brain", provider=self.name)

        raw = _last_user_text(messages)
        text = raw.strip()
        low = text.lower()

        # --- open an application ---------------------------------- #
        m = _OPEN_RE.search(text)
        if m and "open_app" in offered:
            app = re.sub(r"\s+", " ", m.group(1)).strip(" .,")
            # Trim trailing request filler ("chrome please", "chrome now")
            app = re.sub(r"\b(please|now|karo|kar do)\b.*$", "",
                         app).strip()
            if app:
                return ProviderReply(
                    text="",
                    tool_calls=[ToolCall(id="eval-open-1",
                                         name="open_app",
                                         arguments={"name": app})],
                    model="eval-brain", provider=self.name)

        # --- web search ------------------------------------------- #
        if "web_search" in offered and (
                "web search" in low or low.startswith("search ")):
            query = re.sub(r"(?i)^.*?(?:web\s+search|search)\s+"
                           r"(?:about\s+|for\s+)?", "", text).strip()
            return ProviderReply(
                text="",
                tool_calls=[ToolCall(id="eval-search-1",
                                     name="web_search",
                                     arguments={"query": query or text})],
                model="eval-brain", provider=self.name)

        # --- reminder --------------------------------------------- #
        if "reminder_set" in offered and (
                "reminder" in low or "yaad dila" in low):
            mins = _MINUTES_RE.search(text)
            return ProviderReply(
                text="",
                tool_calls=[ToolCall(
                    id="eval-reminder-1", name="reminder_set",
                    arguments={
                        "in_minutes": int(mins.group(1)) if mins else 10,
                        "text": _reminder_text(text),
                    })],
                model="eval-brain", provider=self.name)

        # --- a note request has no tool in the v4 core; a competent
        # minimal brain acknowledges it WITH its content (not a
        # deflection) ---------------------------------------------- #
        if "note" in low and ":" in text:
            note = text.split(":", 1)[1].strip()
            if note:
                return ProviderReply(
                    text=f"Note kar liya hai, sir: {note}",
                    tool_calls=[], model="eval-brain",
                    provider=self.name)

        # --- small deterministic answers --------------------------- #
        if re.search(r"\b2\s*(?:plus|\+)\s*2\b", low):
            return ProviderReply(
                text="2 plus 2 hota hai 4, sir.",
                tool_calls=[], model="eval-brain", provider=self.name)
        if ("how are u" in low or "how are you" in low
                or "kaise ho" in low):
            return ProviderReply(
                text="Main bilkul theek hu, sir. Aap sunaiye — kya kaam hai?",
                tool_calls=[], model="eval-brain", provider=self.name)

        # --- generic substantive fallback -------------------------- #
        return ProviderReply(
            text="Samajh gaya, sir. Aapki baat sun li hai — main is par "
                 "dhyaan de raha hu, aur jo hissa tool se hona hai wo "
                 "main karke dunga.",
            tool_calls=[], model="eval-brain", provider=self.name)


__all__ = ["EvalBrainProvider"]
