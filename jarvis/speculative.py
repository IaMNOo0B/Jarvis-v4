"""Speculative decoding — the tuning math for the 7B + 0.5B pair.

Frame Step 07 (Transformers Deep Dive), from the ai-engineering-from
-scratch study notes (part-D-transformers-genai-rl.md, Phase 07
Lesson 16):

    Draft-and-verify decoding changes NO output distribution: results
    are provably identical to sampling from the verifier alone. The
    draft proposes N tokens; the verifier scores them in one parallel
    pass; token i is accepted with probability min(1, q_i / p_i); on
    the first rejection the replacement comes from the residual
    (q - p)_+ renormalised; if all N are accepted, one bonus token
    comes free from q_{N+1}. One verifier pass yields up to N+1 tokens.

    Expected tokens per verifier pass at per-token acceptance alpha:

        E = (1 - alpha**(N+1)) / (1 - alpha)

    Speedup model: a verifier pass costs 1 (one target-token latency;
    decoding is memory-bound, so the parallel pass costs about one
    token), and each drafted token costs c (draft/target latency
    ratio — ~0.05 for a 0.5B draft against a 7B target):

        speedup = E / (1 + N*c)

    Alpha is highest on predictable text — JARVIS's tool-call JSON is
    its highest-acceptance workload class — and falls with
    temperature. Counter-rule from the lesson: skip speculation for
    1-5 token outputs; short confirmations gain nothing.

``optimal_draft_len`` returns the SMALLEST N whose modelled speedup
is within ``plateau`` (default 1%) of the best over the search range.
Past the knee, extra draft length buys <1% speed for more KV churn
and rollback waste, so the knee is the engineering choice. (At
alpha=0.8, c=0.05 the raw argmax is N=8 at 3.092x vs N=7 at 3.082x —
a 0.3% difference; the knee rule returns 7, inside the frame's
N~5-7 guidance.)

Python stdlib only.
"""

from __future__ import annotations

from typing import Iterable, Tuple

__all__ = [
    "expected_tokens",
    "speedup",
    "optimal_draft_len",
    "estimate_alpha",
    "estimate_alpha_from_rounds",
    "draft_modelfile",
]


def _check_alpha(alpha: float) -> None:
    if not 0.0 <= alpha <= 1.0:
        raise ValueError(f"alpha must be in [0, 1], got {alpha!r}")


def _check_n(n: int) -> None:
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ValueError(f"n must be a non-negative int, got {n!r}")


def _check_c(c: float) -> None:
    if c < 0:
        raise ValueError(f"cost ratio c must be >= 0, got {c!r}")


def expected_tokens(alpha: float, n: int) -> float:
    """Expected tokens yielded per verifier pass.

    E = (1 - alpha**(n+1)) / (1 - alpha). At alpha=0 every pass still
    yields exactly 1 token (the residual resample); at alpha=1 it
    yields n+1 (all drafts plus the bonus token).
    """
    _check_alpha(alpha)
    _check_n(n)
    if alpha == 1.0:
        return float(n + 1)
    return (1.0 - alpha ** (n + 1)) / (1.0 - alpha)


def speedup(alpha: float, n: int, c: float) -> float:
    """Modelled wall-clock speedup vs plain autoregressive decoding."""
    _check_c(c)
    return expected_tokens(alpha, n) / (1.0 + n * c)


def optimal_draft_len(
    alpha: float,
    c: float,
    max_n: int = 32,
    plateau: float = 0.01,
) -> int:
    """Smallest draft length within ``plateau`` of the best speedup.

    Searches n in 1..max_n. See the module docstring for why the knee
    (not the raw argmax) is returned.
    """
    _check_alpha(alpha)
    _check_c(c)
    if not isinstance(max_n, int) or isinstance(max_n, bool) or max_n < 1:
        raise ValueError(f"max_n must be a positive int, got {max_n!r}")
    if not 0.0 <= plateau < 1.0:
        raise ValueError(f"plateau must be in [0, 1), got {plateau!r}")
    speeds = {n: speedup(alpha, n, c) for n in range(1, max_n + 1)}
    best = max(speeds.values())
    floor = best * (1.0 - plateau)
    for n in range(1, max_n + 1):
        if speeds[n] >= floor:
            return n
    return max_n  # unreachable: the argmax itself always clears the floor


def estimate_alpha(outcomes: Iterable[object]) -> float:
    """Acceptance rate alpha from per-token outcomes.

    ``outcomes`` is an iterable of booleans (or 0/1 ints): True/1 =
    the drafted token was accepted. This is the number to measure on
    real traffic (the frame's Step 07 saboot) before trusting any
    modelled speedup.
    """
    total = 0
    accepted = 0
    for outcome in outcomes:
        if outcome is True or outcome == 1:
            accepted += 1
        elif outcome is False or outcome == 0:
            pass
        else:
            raise ValueError(
                f"outcomes must be booleans or 0/1, got {outcome!r}"
            )
        total += 1
    if total == 0:
        raise ValueError("estimate_alpha needs at least one outcome")
    return accepted / total


def estimate_alpha_from_rounds(rounds: Iterable[Tuple[int, int]]) -> float:
    """Acceptance rate from per-pass (accepted, drafted) tallies.

    Matches how a serving loop actually logs speculation: each
    verifier pass reports how many of its drafted tokens survived.
    """
    total_drafted = 0
    total_accepted = 0
    for accepted, drafted in rounds:
        if drafted <= 0:
            raise ValueError(f"drafted must be > 0, got {drafted!r}")
        if not 0 <= accepted <= drafted:
            raise ValueError(
                f"accepted must be in [0, drafted], got {accepted!r} "
                f"of {drafted!r}"
            )
        total_accepted += accepted
        total_drafted += drafted
    if total_drafted == 0:
        raise ValueError("estimate_alpha_from_rounds needs at least one round")
    return total_accepted / total_drafted


def draft_modelfile(model: str, draft_model: str, num_predict: int) -> str:
    """Ollama Modelfile text for the speculative pair.

    ``num_predict`` is the draft length: how many tokens the draft
    proposes per verifier pass (tune it with ``optimal_draft_len``).
    The DRAFT line follows the house format proven in v2's
    jarvis-spec.Modelfile: the draft must share the target's
    tokenizer/vocabulary family, and on the owner's PC it must be a
    local GGUF path (a registry name is not accepted there) — this
    generator emits whatever identifier it is given and says so in
    the header comments.
    """
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    if not isinstance(draft_model, str) or not draft_model.strip():
        raise ValueError("draft_model must be a non-empty string")
    _check_n(num_predict)
    return (
        "# JARVIS v4 — speculative decoding pair (Frame Step 07).\n"
        "# Math and tuning: jarvis/speculative.py — the draft proposes,\n"
        "# the target verifies in ONE parallel pass; the output\n"
        "# distribution is identical to the target alone, only faster.\n"
        "# The draft MUST share the target's tokenizer/vocabulary\n"
        "# family. On the owner's PC the DRAFT line must be a local\n"
        "# GGUF file path (a registry name is not accepted there).\n"
        f"FROM {model}\n"
        f"DRAFT {draft_model}\n"
        "# Tokens the draft proposes per verifier pass.\n"
        f"PARAMETER draft_num_predict {num_predict}\n"
    )
