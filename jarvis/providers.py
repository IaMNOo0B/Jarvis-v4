"""LLM providers + fallback chain — JARVIS v4 spine S2 (Step 11/17).

SHARED SPINE CONTRACT
---------------------
``build_chain(config, secrets, goodput_ledger) -> list[Provider]``

A Provider has:

- ``.name``    — stable provider id ("openrouter", "ollama", "stub", ...)
- ``.timeout`` — THIS provider's per-call timeout in seconds, exposed as
  an attribute. **Regression guard:** v2 lost a whole evening because
  CompatProvider never exposed ``.timeout``, so the fallback chain's 5 s
  default clobbered the intended 60 s and keyed calls died before the
  first token. Here every transport call passes ``self.timeout`` — the
  provider's own value, never a chain-level default.
- ``.complete(messages, tools=None) -> ProviderReply`` — raises a
  ``ProviderError`` subclass on failure:
  ``ProviderTimeout``, ``ProviderHTTPError`` (carries ``.status``),
  ``ProviderDown`` (unreachable / connection-level failure).

``ProviderReply`` = {text, tool_calls, model, usage, provider} where
``tool_calls`` is a list of ``ToolCall{id, name, arguments(dict)}`` — one
common shape no matter which backend answered.

``FallbackChain.complete(messages, tools=None)`` tries providers in
order, falls through on any ProviderError AND on an empty reply with no
tool calls (the "JARVIS goes silent" lesson: an empty success is a soft
failure, never an answer), and records EVERY attempt in the goodput
ledger (Step 17): errors as ok=False, empty replies as ok=True +
quality_ok=False, real answers as ok=True + quality_ok=True.

Chain order (Step 11): keyed OpenAI-compatible providers, smartest-first
by preset order → Ollama (local) → Pollinations (keyless, weakest) →
Stub (deterministic offline, always answers honestly as a stub).

ALL HTTP goes through an injectable
``transport(url, body_bytes, headers, timeout) -> (status, bytes)``
so tests never touch the network. The default transport is stdlib
urllib; it returns (status, body) for ANY HTTP status and raises
ProviderTimeout / ProviderDown for network-level failures.

Presets: imported from ``jarvis.keys`` (PRESETS) when that sibling module
has landed; until then the local table below — same shape — is used.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from jarvis.goodput import GoodputLedger

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Base class for every provider failure the chain may fall through."""


class ProviderTimeout(ProviderError):
    """The provider did not answer within its own timeout."""


class ProviderHTTPError(ProviderError):
    """The provider answered with an HTTP error status."""

    def __init__(self, status: int, message: str = ""):
        self.status = int(status)
        super().__init__(message or f"HTTP {self.status}")


class ProviderDown(ProviderError):
    """The provider is unreachable (connection refused, DNS, reset...)."""


class ChainExhaustedError(ProviderError):
    """Every provider in the chain failed or answered empty.

    Carries ``attempts``: the provider names tried, in order.
    """

    def __init__(self, attempts: Sequence[str], last_error: str = ""):
        self.attempts = list(attempts)
        msg = f"all providers failed: {', '.join(self.attempts) or '(none)'}"
        if last_error:
            msg += f" — last error: {last_error}"
        super().__init__(msg)


# ---------------------------------------------------------------------------
# Common reply shapes
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderReply:
    text: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    model: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    provider: str = ""

    def is_empty(self) -> bool:
        """A reply with no text and no tool calls is not an answer."""
        return not (self.text and self.text.strip()) and not self.tool_calls


# ---------------------------------------------------------------------------
# Transport (injectable; default = stdlib urllib)
# ---------------------------------------------------------------------------

Transport = Callable[[str, bytes, Dict[str, str], float], Any]
"""transport(url, body, headers, timeout) -> (status: int, payload: bytes)"""


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host == "localhost" or host == "::1" or host.startswith("127.")


def urllib_transport(url: str, body: bytes, headers: Dict[str, str],
                     timeout: float):
    """Default transport. Returns (status, bytes) for any HTTP status;
    raises ProviderTimeout / ProviderDown for network-level failures.

    Loopback URLs (the local Ollama) are opened DIRECTLY, never via a
    proxy: urllib's urlopen honours the system/env proxy, browsers
    bypass proxies for localhost but urllib does not — a PC with any
    proxy configured would silently route 127.0.0.1:11434 into the
    proxy and the local brain would look dead while Ollama is fine."""
    req = urllib.request.Request(url, data=body, headers=dict(headers),
                                 method="POST")
    if _is_loopback(url):
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))
        open_fn = opener.open
    else:
        open_fn = urllib.request.urlopen
    try:
        with open_fn(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as e:  # an answer, not a network failure
        try:
            payload = e.read()
        except Exception:
            payload = b""
        return int(e.code), payload
    except (socket.timeout, TimeoutError) as e:
        raise ProviderTimeout(f"timeout after {timeout}s: {e}") from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise ProviderTimeout(f"timeout after {timeout}s: {reason}") from e
        raise ProviderDown(f"unreachable: {reason}") from e
    except OSError as e:
        raise ProviderDown(f"connection error: {e}") from e


# ---------------------------------------------------------------------------
# Presets (reconciled with jarvis.keys when it lands)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    name: str
    base_url: str
    model: str
    timeout: float = 60.0


_LOCAL_PRESETS: List[Preset] = [
    # Smartest-first: this order IS the chain order for keyed providers.
    Preset("openrouter", "https://openrouter.ai/api/v1",
           "meta-llama/llama-3.3-70b-instruct"),
    Preset("gemini", "https://generativelanguage.googleapis.com/v1beta/openai",
           "gemini-2.5-flash"),  # free tier has NO Pro — v2 hotfix, 2026-10-20
    Preset("groq", "https://api.groq.com/openai/v1",
           "llama-3.3-70b-versatile"),
    Preset("deepseek", "https://api.deepseek.com/v1", "deepseek-chat"),
    Preset("xai", "https://api.x.ai/v1", "grok-4"),
    Preset("mistral", "https://api.mistral.ai/v1", "mistral-large-latest"),
    Preset("nvidia", "https://integrate.api.nvidia.com/v1",
           "meta/llama-3.3-70b-instruct"),
    Preset("together", "https://api.together.xyz/v1",
           "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    Preset("fireworks", "https://api.fireworks.ai/inference/v1",
           "accounts/fireworks/models/llama-v3p3-70b-instruct"),
    Preset("cerebras", "https://api.cerebras.ai/v1", "llama-3.3-70b"),
    Preset("cohere", "https://api.cohere.ai/compatibility/v1",
           "command-r-plus"),
    Preset("perplexity", "https://api.perplexity.ai", "sonar"),
    Preset("chutes", "https://llm.chutes.ai/v1",
           "deepseek-ai/DeepSeek-V3"),
]


def _load_presets() -> List[Preset]:
    """Prefer jarvis.keys.PRESETS (the sibling keys mission owns the
    canonical table); fall back to the local table until it lands. Either
    way the shape is a list of {name, base_url, model[, timeout]} — dicts,
    objects with attributes, or Preset instances are all accepted."""
    try:
        from jarvis.keys import PRESETS as keys_presets  # type: ignore
    except Exception:
        return list(_LOCAL_PRESETS)
    out: List[Preset] = []
    if isinstance(keys_presets, Mapping):
        # jarvis.keys shape: {name: {"base_url": ..., "model": ...}} —
        # the dict key is the name, and dict order is the preset order.
        pairs = list(keys_presets.items())
    else:
        pairs = [(None, item) for item in keys_presets]
    for key_name, item in pairs:
        if isinstance(item, Preset):
            out.append(item)
        elif isinstance(item, Mapping):
            out.append(Preset(name=str(item.get("name") or key_name),
                              base_url=str(item["base_url"]),
                              model=str(item["model"]),
                              timeout=float(item.get("timeout", 60.0))))
        else:
            out.append(Preset(name=str(getattr(item, "name", key_name)),
                              base_url=str(item.base_url),
                              model=str(item.model),
                              timeout=float(getattr(item, "timeout", 60.0))))
    return out or list(_LOCAL_PRESETS)


PRESETS: List[Preset] = _load_presets()

# ---------------------------------------------------------------------------
# config / secrets access (accept mappings or attribute objects)
# ---------------------------------------------------------------------------


def _cfg(config: Any, *path: str, default: Any = None) -> Any:
    value = _cfg_walk(config, path)
    return default if value is None else value


def _cfg_walk(config: Any, path: Sequence[str]) -> Any:
    cur = config
    for key in path:
        if cur is None:
            return None
        if isinstance(cur, Mapping):
            cur = cur.get(key)
        else:
            cur = getattr(cur, key, None)
    return cur


def _secret_for(secrets: Any, name: str) -> Optional[str]:
    """One provider's key from whatever secrets store was handed in:
    a mapping name->str|list[str], or an object with .get_key/.get."""
    if secrets is None:
        return None
    value = None
    if isinstance(secrets, Mapping):
        value = secrets.get(name)
    elif hasattr(secrets, "get_key"):
        value = secrets.get_key(name)
    elif hasattr(secrets, "get"):
        value = secrets.get(name)
    if isinstance(value, (list, tuple)):
        for item in value:
            if item:
                return str(item)
        return None
    return str(value) if value else None


def _parse_arguments(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, Mapping) else \
                {"value": parsed}
        except (ValueError, TypeError):
            return {"raw": raw}
    return {}


# ---------------------------------------------------------------------------
# HTTP provider base
# ---------------------------------------------------------------------------


class _HTTPProvider:
    """Shared plumbing: POST JSON via the injected transport, using THIS
    provider's own timeout — never a chain-level default."""

    name = "http"
    timeout = 60.0
    model = ""

    def __init__(self, transport: Optional[Transport] = None):
        self._transport: Transport = transport or urllib_transport

    def _post_json(self, url: str, payload: Dict[str, Any],
                   headers: Optional[Dict[str, str]] = None
                   ) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        status, raw = self._transport(url, body, hdrs, float(self.timeout))
        if int(status) >= 400:
            snippet = raw[:200].decode("utf-8", "replace") \
                if isinstance(raw, (bytes, bytearray)) else str(raw)[:200]
            raise ProviderHTTPError(int(status),
                                    f"{self.name}: HTTP {status}: {snippet}")
        try:
            data = json.loads(raw.decode("utf-8")
                              if isinstance(raw, (bytes, bytearray)) else raw)
        except (ValueError, UnicodeDecodeError) as e:
            raise ProviderDown(f"{self.name}: unparseable reply: {e}") from e
        if not isinstance(data, dict):
            raise ProviderDown(f"{self.name}: unexpected reply shape")
        return data


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class OllamaProvider(_HTTPProvider):
    """Local Ollama — POST {base}/api/chat. No key; default timeout 120 s
    (a cold 7B load on a 6 GB card is slow, and correctly so)."""

    name = "ollama"

    def __init__(self, base_url: str = "http://localhost:11434",
                 model: str = "qwen2.5:7b-instruct-q4_K_M",
                 timeout: float = 120.0,
                 transport: Optional[Transport] = None):
        super().__init__(transport)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = float(timeout)

    def complete(self, messages, tools=None) -> ProviderReply:
        payload: Dict[str, Any] = {"model": self.model,
                                   "messages": list(messages),
                                   "stream": False}
        if tools:
            payload["tools"] = list(tools)
        data = self._post_json(f"{self.base_url}/api/chat", payload)
        msg = data.get("message") or {}
        calls: List[ToolCall] = []
        for i, raw_call in enumerate(msg.get("tool_calls") or []):
            fn = raw_call.get("function") or {}
            calls.append(ToolCall(
                id=str(raw_call.get("id") or f"ollama-{i}"),
                name=str(fn.get("name", "")),
                arguments=_parse_arguments(fn.get("arguments"))))
        usage = {}
        if "prompt_eval_count" in data:
            usage["prompt_tokens"] = data["prompt_eval_count"]
        if "eval_count" in data:
            usage["completion_tokens"] = data["eval_count"]
        return ProviderReply(text=str(msg.get("content") or ""),
                             tool_calls=calls,
                             model=str(data.get("model") or self.model),
                             usage=usage, provider=self.name)


class CompatProvider(_HTTPProvider):
    """OpenAI-compatible chat completions with a Bearer key.
    Per-provider timeout default 60 s — exposed, never clobbered."""

    def __init__(self, name: str, base_url: str, model: str,
                 api_key: Optional[str] = None, timeout: float = 60.0,
                 transport: Optional[Transport] = None):
        super().__init__(transport)
        self.name = str(name)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = float(timeout)

    def complete(self, messages, tools=None) -> ProviderReply:
        payload: Dict[str, Any] = {"model": self.model,
                                   "messages": list(messages)}
        if tools:
            payload["tools"] = list(tools)
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = self._post_json(f"{self.base_url}/chat/completions",
                               payload, headers)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderDown(f"{self.name}: reply had no choices")
        msg = choices[0].get("message") or {}
        calls: List[ToolCall] = []
        for i, raw_call in enumerate(msg.get("tool_calls") or []):
            fn = raw_call.get("function") or {}
            calls.append(ToolCall(
                id=str(raw_call.get("id") or f"{self.name}-{i}"),
                name=str(fn.get("name", "")),
                arguments=_parse_arguments(fn.get("arguments"))))
        usage = data.get("usage") if isinstance(data.get("usage"), dict) \
            else {}
        return ProviderReply(text=str(msg.get("content") or ""),
                             tool_calls=calls,
                             model=str(data.get("model") or self.model),
                             usage=dict(usage), provider=self.name)


# ---------------------------------------------------------------------------
# Native Gemini (the Mark-LVI lesson, 2026-10-21)
#
# The repo the owner sent (FatihMakes/Mark-LVI) talks to Google AI
# Studio through Gemini's NATIVE REST dialect — POST
# /v1beta/models/<model>:generateContent with an x-goog-api-key
# header, the system prompt carried as systemInstruction, tools as
# functionDeclarations — never through the OpenAI-shaped detour v4
# used before. And when a model fails it does not give up: it walks
# a MODEL LADDER, resting each failed rung by error class (429 ->
# 5 min, 403/404 -> 6 h, 503/504 -> 30 min). Both ideas are
# implemented here in stdlib only (no google-genai SDK dependency).
# ---------------------------------------------------------------------------

GEMINI_MODEL_LADDER = ("gemini-2.5-flash", "gemini-2.5-flash-lite",
                       "gemini-2.0-flash", "gemini-flash-latest")

_GEMINI_REST_S = {429: 300.0, 403: 6 * 3600.0, 404: 6 * 3600.0,
                  503: 30 * 60.0, 504: 30 * 60.0}


def _gemini_schema(node):
    """OpenAI-flavoured JSON schema -> Gemini's dialect (uppercase
    types, only the keys Gemini accepts)."""
    if not isinstance(node, Mapping):
        return {"type": "OBJECT", "properties": {}}
    out: Dict[str, Any] = {}
    typ = node.get("type")
    if typ:
        out["type"] = str(typ).upper()
    if node.get("description"):
        out["description"] = str(node["description"])
    props = node.get("properties")
    if isinstance(props, Mapping):
        out["properties"] = {str(k): _gemini_schema(v)
                             for k, v in props.items()}
    if isinstance(node.get("items"), Mapping):
        out["items"] = _gemini_schema(node["items"])
    if node.get("required"):
        out["required"] = [str(r) for r in node["required"]]
    if node.get("enum"):
        out["enum"] = [str(e) for e in node["enum"]]
    if "type" not in out:
        out["type"] = "OBJECT"
    return out


def _message_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for item in content:
            if isinstance(item, Mapping):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    return str(content)


def _gemini_payload(messages, tools):
    """OpenAI-shaped conversation -> Gemini generateContent body."""
    system_texts: List[str] = []
    contents: List[Dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, Mapping):
            continue
        role = str(msg.get("role") or "user")
        if role in ("system", "developer"):
            text = _message_text(msg.get("content"))
            if text:
                system_texts.append(text)
            continue
        if role in ("tool", "function"):
            contents.append({
                "role": "user",
                "parts": [{"functionResponse": {
                    "name": str(msg.get("name") or "tool"),
                    "response": {"result":
                                 _message_text(msg.get("content"))}}}]})
            continue
        parts: List[Dict[str, Any]] = []
        text = _message_text(msg.get("content"))
        if text:
            parts.append({"text": text})
        if role == "assistant":
            for call in msg.get("tool_calls") or []:
                if isinstance(call, Mapping):
                    fn = call.get("function") or call
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        args = _parse_arguments(args)
                    parts.append({"functionCall": {
                        "name": str(fn.get("name") or ""),
                        "args": args or {}}})
        contents.append({"role": "model" if role == "assistant"
                         else "user",
                         "parts": parts or [{"text": ""}]})
    payload: Dict[str, Any] = {"contents": contents}
    if system_texts:
        payload["systemInstruction"] = {
            "parts": [{"text": "\n\n".join(system_texts)}]}
    if tools:
        decls = []
        for tool in tools:
            if not isinstance(tool, Mapping):
                continue
            fn = tool.get("function") or tool
            name = str(fn.get("name") or tool.get("name") or "")
            if not name:
                continue
            decls.append({
                "name": name,
                "description": str(fn.get("description")
                                   or tool.get("description") or ""),
                "parameters": _gemini_schema(
                    fn.get("parameters") or tool.get("schema")
                    or {"type": "object"})})
        if decls:
            payload["tools"] = [{"functionDeclarations": decls}]
    return payload


class GeminiProvider(_HTTPProvider):
    """Google AI Studio over the native generateContent API, with
    Mark-LVI's model ladder: a failed rung rests (429 -> 5 min,
    403/404 -> 6 h, 503/504 -> 30 min) and the next model answers."""

    name = "gemini"

    def __init__(self, api_key: str,
                 models: Sequence[str] = GEMINI_MODEL_LADDER,
                 base_url: str = "https://generativelanguage.googleapis.com",
                 timeout: float = 60.0,
                 transport: Optional[Transport] = None,
                 clock: Callable[[], float] = time.monotonic):
        super().__init__(transport)
        self.api_key = api_key
        self.models = [str(m) for m in models if m] \
            or list(GEMINI_MODEL_LADDER)
        self.model = self.models[0]
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self._clock = clock
        self._rested_until: Dict[str, float] = {}

    def _rungs(self) -> List[str]:
        now = self._clock()
        live = [m for m in self.models
                if self._rested_until.get(m, 0.0) <= now]
        return live or list(self.models)  # all resting -> try anyway

    def complete(self, messages, tools=None) -> ProviderReply:
        payload = _gemini_payload(messages, tools)
        last_error: Optional[ProviderError] = None
        for model in self._rungs():
            url = (f"{self.base_url}/v1beta/models/{model}"
                   ":generateContent")
            try:
                data = self._post_json(
                    url, payload,
                    headers={"x-goog-api-key": self.api_key})
            except ProviderHTTPError as e:
                rest = _GEMINI_REST_S.get(e.status)
                if rest:
                    self._rested_until[model] = self._clock() + rest
                last_error = e
                continue
            return self._reply(data, model)
        raise last_error or ProviderDown("gemini: no model answered")

    @staticmethod
    def _reply(data: Dict[str, Any], model: str) -> ProviderReply:
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            raise ProviderDown(
                "gemini: reply had no candidates "
                f"({feedback.get('blockReason', 'unknown reason')})")
        parts = ((candidates[0].get("content") or {}).get("parts")
                 or [])
        texts: List[str] = []
        calls: List[ToolCall] = []
        for i, part in enumerate(parts):
            if not isinstance(part, Mapping):
                continue
            if part.get("text"):
                texts.append(str(part["text"]))
            fn = part.get("functionCall")
            if isinstance(fn, Mapping):
                calls.append(ToolCall(
                    id=f"gemini-{i}",
                    name=str(fn.get("name", "")),
                    arguments=fn.get("args") or {}))
        usage = {}
        meta = data.get("usageMetadata") or {}
        if "promptTokenCount" in meta:
            usage["prompt_tokens"] = meta["promptTokenCount"]
        if "candidatesTokenCount" in meta:
            usage["completion_tokens"] = meta["candidatesTokenCount"]
        return ProviderReply(text="\n".join(texts), tool_calls=calls,
                             model=str(data.get("modelVersion")
                                       or model),
                             usage=usage, provider="gemini")


class PollinationsProvider(CompatProvider):
    """Keyless last resort — clearly the weakest brain in the chain
    (it failed 9/10 real-prompt turns in v2's reality test). Present so
    JARVIS degrades to SOMETHING rather than silence; never trusted."""

    def __init__(self, base_url: str = "https://text.pollinations.ai/openai",
                 model: str = "openai", timeout: float = 60.0,
                 transport: Optional[Transport] = None):
        super().__init__("pollinations", base_url, model, api_key=None,
                         timeout=timeout, transport=transport)


class StubProvider:
    """Deterministic offline provider. No network, no randomness: the
    same conversation always earns the same reply, and the reply is
    honest about being a stub. The harness/eval seat and the final
    safety net when every real provider is down."""

    name = "stub"
    timeout = 1.0
    model = "stub"

    def complete(self, messages, tools=None) -> ProviderReply:
        last_user = ""
        for msg in reversed(list(messages)):
            if isinstance(msg, Mapping) and msg.get("role") == "user":
                last_user = str(msg.get("content") or "")
                break
        text = ("I am running on the offline stub provider — no AI brain "
                "is reachable right now, so I cannot truly answer. "
                f"(You said: {last_user[:120]})")
        return ProviderReply(text=text, tool_calls=[], model=self.model,
                             usage={}, provider=self.name)


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------


class FallbackChain:
    """Tries providers in order; every attempt lands in the goodput
    ledger. A ProviderError falls through; an empty reply (no text, no
    tool calls) falls through as a soft failure. ``total_deadline_s``
    bounds the whole chain: once the budget is spent no NEW attempt
    starts (an in-flight attempt always keeps its provider's own
    timeout — the chain never rewrites it)."""

    name = "fallback-chain"

    def __init__(self, providers: Sequence[Any],
                 ledger: Optional[GoodputLedger] = None,
                 total_deadline_s: Optional[float] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.providers = list(providers)
        self.ledger = ledger
        self.total_deadline_s = total_deadline_s
        self._clock = clock

    def complete(self, messages, tools=None) -> ProviderReply:
        started = self._clock()
        attempts: List[str] = []
        last_error = ""
        for provider in self.providers:
            if self.total_deadline_s is not None and attempts and \
                    (self._clock() - started) > float(self.total_deadline_s):
                last_error = "chain deadline exhausted"
                break
            attempts.append(provider.name)
            t0 = self._clock()
            try:
                reply = provider.complete(messages, tools=tools)
            except ProviderError as e:
                self._record(provider.name, t0, ok=False, quality_ok=False,
                             error=str(e))
                last_error = f"{provider.name}: {e}"
                continue
            if reply.is_empty():
                # The "goes silent" lesson: an empty success is a failure.
                self._record(provider.name, t0, ok=True, quality_ok=False,
                             error="empty reply")
                last_error = f"{provider.name}: empty reply"
                continue
            self._record(provider.name, t0, ok=True, quality_ok=True)
            reply.provider = provider.name
            return reply
        raise ChainExhaustedError(attempts, last_error)

    def _record(self, provider: str, t0: float, ok: bool,
                quality_ok: bool, error: str = "") -> None:
        if self.ledger is not None:
            latency_ms = max(0.0, (self._clock() - t0) * 1000.0)
            self.ledger.record(provider, latency_ms, ok, quality_ok,
                               error=error)


# ---------------------------------------------------------------------------
# Chain construction (the shared spine contract)
# ---------------------------------------------------------------------------


def build_chain(config: Any = None, secrets: Any = None,
                goodput_ledger: Optional[GoodputLedger] = None,
                transport: Optional[Transport] = None) -> List[Any]:
    """build_chain(config, secrets, goodput_ledger) -> list[Provider]

    Order: keyed compat providers (preset order = smartest first;
    providers whose key is absent are SKIPPED) + any custom providers
    declared in config["providers"] -> Ollama -> Pollinations -> Stub.

    config (mapping or object; all optional):
      providers: [{name, base_url, model, timeout?, secret?}]
      ollama:    {base_url, model, timeout}
      pollinations: {base_url, model, timeout}
      compat_timeout / per-preset timeouts via config["timeouts"][name]

    ``goodput_ledger`` is accepted per the spine contract: the list
    itself is inert — wrap it with FallbackChain(providers, ledger) or
    use build_fallback_chain() to get a chain that records attempts.
    """
    chain: List[Any] = []
    timeouts = _cfg(config, "timeouts", default={}) or {}

    # 1. Keyed compat providers, smartest-first by preset order.
    for preset in PRESETS:
        key = _secret_for(secrets, preset.name)
        if not key:
            continue  # no key -> not in the chain at all
        override = _cfg(config, "providers_config", preset.name, default={})
        if preset.name == "gemini":
            # Native Gemini API + model ladder (the Mark-LVI lesson) —
            # never the OpenAI-shaped detour for an AI Studio key.
            primary = str(_cfg(override, "model",
                               default=preset.model))
            ladder = [primary] + [m for m in GEMINI_MODEL_LADDER
                                   if m != primary]
            chain.append(GeminiProvider(
                api_key=key, models=ladder,
                timeout=float(timeouts.get(
                    preset.name,
                    _cfg(override, "timeout", default=preset.timeout))),
                transport=transport))
            continue
        chain.append(CompatProvider(
            name=preset.name,
            base_url=str(_cfg(override, "base_url", default=preset.base_url)),
            model=str(_cfg(override, "model", default=preset.model)),
            api_key=key,
            timeout=float(timeouts.get(preset.name,
                                       _cfg(override, "timeout",
                                            default=preset.timeout))),
            transport=transport))

    # 2. Owner-declared custom providers (config["providers"] list).
    for entry in _cfg(config, "providers", default=[]) or []:
        if not isinstance(entry, Mapping) or "base_url" not in entry:
            continue
        name = str(entry.get("name") or entry["base_url"])
        key = _secret_for(secrets, str(entry.get("secret") or name))
        if not key:
            continue
        chain.append(CompatProvider(
            name=name, base_url=str(entry["base_url"]),
            model=str(entry.get("model") or ""),
            api_key=key,
            timeout=float(entry.get("timeout", 60.0)),
            transport=transport))

    # 3. Local Ollama.
    ocfg = _cfg(config, "ollama", default={}) or {}
    chain.append(OllamaProvider(
        base_url=str(_cfg(ocfg, "base_url",
                          default="http://localhost:11434")),
        model=str(_cfg(ocfg, "model",
                       default="qwen2.5:7b-instruct-q4_K_M")),
        timeout=float(_cfg(ocfg, "timeout", default=120.0)),
        transport=transport))

    # 4. Keyless last resort (weakest real brain).
    pcfg = _cfg(config, "pollinations", default={}) or {}
    chain.append(PollinationsProvider(
        base_url=str(_cfg(pcfg, "base_url",
                          default="https://text.pollinations.ai/openai")),
        model=str(_cfg(pcfg, "model", default="openai")),
        timeout=float(_cfg(pcfg, "timeout", default=60.0)),
        transport=transport))

    # 5. Deterministic stub — always answers, honestly labelled.
    chain.append(StubProvider())
    return chain


def build_fallback_chain(config: Any = None, secrets: Any = None,
                         goodput_ledger: Optional[GoodputLedger] = None,
                         transport: Optional[Transport] = None,
                         total_deadline_s: Optional[float] = None
                         ) -> FallbackChain:
    """Convenience: build_chain() already wrapped in a FallbackChain
    that records every attempt into ``goodput_ledger``."""
    deadline = total_deadline_s
    if deadline is None:
        deadline = _cfg(config, "chain", "total_deadline_s", default=None)
    return FallbackChain(build_chain(config, secrets, goodput_ledger,
                                     transport=transport),
                         ledger=goodput_ledger,
                         total_deadline_s=deadline)


__all__ = [
    "ProviderError", "ProviderTimeout", "ProviderHTTPError", "ProviderDown",
    "ChainExhaustedError", "ToolCall", "ProviderReply", "Preset", "PRESETS",
    "Transport", "urllib_transport", "OllamaProvider", "CompatProvider",
    "GeminiProvider", "GEMINI_MODEL_LADDER",
    "PollinationsProvider", "StubProvider", "FallbackChain",
    "build_chain", "build_fallback_chain",
]
