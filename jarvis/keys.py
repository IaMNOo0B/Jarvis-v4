"""The /key brain for JARVIS v4 — paste a key, it files itself.

Spine contract (shared with brain/chat):

    from jarvis.keys import add_key, list_keys, fingerprint_key, PRESETS
    provider = add_key(store, raw)        # detects provider, stores, returns name
    counts = list_keys(store)             # {"openrouter": 2, "groq": 1} — names+counts only

PRIVACY RULE (owner's standing order, from the v2 F-12 lesson): adding a
key performs NO network probing. Ever. A provider is identified by the
key's SHAPE (fingerprint) or by the owner naming it explicitly. Nothing
in this module imports a network library — that is by design, and the
tests enforce it by making sockets explode.

Storage layout (inside jarvis.secrets.SecretStore):
    llm:<provider>:<n>     n = 1, 2, 3... per provider (rotation pool)

Fingerprint table (shape only — ambiguous shapes return None rather
than guessing; the owner can always name the provider explicitly):
    sk-or-...        openrouter      gsk_...          groq
    nvapi-...        nvidia          xai-...          xai
    pplx-...         perplexity      csk-...          cerebras
    AIza... (>=30)   gemini          AQ.... (>=40)    gemini  (new AI Studio shape)
    AQ.... (<40)     unknown         sk-... (bare)    unknown (openai/deepseek/together share it)
    ghp_ / github_pat_               github — identified, but NOT an LLM key

Stdlib only. No network imports, deliberately.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Presets — the 13 OpenAI-compatible providers (data, not code).
# "model" is each provider's sensible default; owners can override in config.
# ---------------------------------------------------------------------------
PRESETS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "meta/llama-3.3-70b-instruct",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-large-latest",
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "model": "llama-3.3-70b",
    },
    "cohere": {
        "base_url": "https://api.cohere.ai/compatibility/v1",
        "model": "command-r-plus-08-2024",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-2.5-flash",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "xai": {
        "base_url": "https://api.x.ai/v1",
        "model": "grok-4",
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    },
    "fireworks": {
        "base_url": "https://api.fireworks.ai/inference/v1",
        "model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
    },
    "perplexity": {
        "base_url": "https://api.perplexity.ai",
        "model": "sonar",
    },
    "chutes": {
        "base_url": "https://llm.chutes.ai/v1",
        "model": "deepseek-ai/DeepSeek-V3-0324",
    },
}

_NAME_PREFIX = "llm:"
_NAME_RE = re.compile(r"^llm:([a-z0-9_-]+):(\d+)$")

_GEMINI_AQ_MIN_LEN = 40   # new AI Studio "AQ." keys run ~54 chars
_GEMINI_AIZA_MIN_LEN = 30  # classic Google keys run 39 chars


class KeysError(Exception):
    """Base for /key failures. Messages never contain the key itself."""


class UnknownKeyError(KeysError):
    """The key's shape matches no known provider fingerprint."""


class NotAnLLMKeyError(KeysError):
    """The key was identified, but it is not an LLM provider key."""


# ---------------------------------------------------------------------------
# Fingerprinting — shape only, no network, no guessing
# ---------------------------------------------------------------------------
def fingerprint_detail(raw) -> dict:
    """Identify a key by shape.

    Returns {"provider": <name|None>, "kind": "llm"|"not-llm"|"unknown"}.
    """
    if not isinstance(raw, str):
        return {"provider": None, "kind": "unknown"}
    key = raw.strip()
    if not key:
        return {"provider": None, "kind": "unknown"}

    # Not-LLM first: a GitHub token must never land in the LLM pool.
    if key.startswith(("ghp_", "github_pat_")):
        return {"provider": "github", "kind": "not-llm"}

    if key.startswith("sk-or-"):
        return {"provider": "openrouter", "kind": "llm"}
    if key.startswith("gsk_"):
        return {"provider": "groq", "kind": "llm"}
    if key.startswith("nvapi-"):
        return {"provider": "nvidia", "kind": "llm"}
    if key.startswith("xai-"):
        return {"provider": "xai", "kind": "llm"}
    if key.startswith("pplx-"):
        return {"provider": "perplexity", "kind": "llm"}
    if key.startswith("csk-"):
        return {"provider": "cerebras", "kind": "llm"}
    if key.startswith("AQ."):
        if len(key) >= _GEMINI_AQ_MIN_LEN:
            return {"provider": "gemini", "kind": "llm"}
        return {"provider": None, "kind": "unknown"}
    if key.startswith("AIza") and len(key) >= _GEMINI_AIZA_MIN_LEN:
        return {"provider": "gemini", "kind": "llm"}

    # Bare "sk-..." and everything else: shared by several providers —
    # ambiguous, so unknown. The owner names the provider explicitly.
    return {"provider": None, "kind": "unknown"}


def fingerprint_key(raw):
    """Provider name for a key's shape, or None when not identifiable.

    "github" IS returned for GitHub tokens (identified) — callers that
    store keys must check the kind via fingerprint_detail; add_key does.
    """
    return fingerprint_detail(raw)["provider"]


# ---------------------------------------------------------------------------
# Storage — on top of SecretStore's get/set/names only
# ---------------------------------------------------------------------------
def _next_index(store, provider: str) -> int:
    highest = 0
    for name in store.names():
        m = _NAME_RE.match(name)
        if m and m.group(1) == provider:
            highest = max(highest, int(m.group(2)))
    return highest + 1


def secret_name(provider: str, index: int) -> str:
    return f"{_NAME_PREFIX}{provider}:{index}"


def add_key(store, raw, provider: str | None = None) -> str:
    """Store an LLM key under its provider; returns the provider name.

    provider=None -> detect by fingerprint. An explicit provider wins
    over the fingerprint (the owner's word beats the shape), but a key
    identified as a non-LLM key (GitHub) is refused either way, and an
    explicit provider must be one of the PRESETS.

    NO network access happens here — by design (see module docstring).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise UnknownKeyError("empty key")
    key = raw.strip()
    detail = fingerprint_detail(key)
    if detail["kind"] == "not-llm":
        raise NotAnLLMKeyError(
            f"this looks like a {detail['provider']} key — not an LLM key; "
            "it was not stored"
        )
    if provider is None:
        provider = detail["provider"]
        if provider is None:
            raise UnknownKeyError(
                f"unrecognized key shape (length {len(key)}); "
                "name the provider explicitly, e.g. /key <provider> <key>"
            )
    else:
        provider = provider.strip().lower()
        if provider == "github":
            raise NotAnLLMKeyError(
                "github keys are not LLM keys; it was not stored"
            )
        if provider not in PRESETS:
            raise KeysError(
                f"unknown provider {provider!r}; known: "
                + ", ".join(sorted(PRESETS))
            )
    # Filing the SAME key twice must not inflate the pool
    # (audit 2026-10-22: a re-filed key silently doubled the
    # provider's key count and skewed rotation).
    if key in get_keys(store, provider):
        return provider
    index = _next_index(store, provider)
    store.set(secret_name(provider, index), key)
    return provider


def list_keys(store) -> dict:
    """{provider: count} for stored LLM keys — names and counts only."""
    counts: dict = {}
    for name in store.names():
        m = _NAME_RE.match(name)
        if m:
            provider = m.group(1)
            counts[provider] = counts.get(provider, 0) + 1
    return counts


def get_keys(store, provider: str) -> list:
    """All stored key VALUES for one provider, in index order.

    This is the one deliberate value exit-point for the provider layer
    (brain/providers) — chat and /keys listings must use list_keys.
    """
    indexed = []
    for name in store.names():
        m = _NAME_RE.match(name)
        if m and m.group(1) == provider:
            indexed.append((int(m.group(2)), name))
    indexed.sort()
    return [store.get(name) for _, name in indexed]
