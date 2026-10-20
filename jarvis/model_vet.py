"""Model vetting calculator — Step 10 (LLMs from Scratch).

Config-first vetting: given a HuggingFace-style ``config.json`` dict, compute
the model's parameter count, its KV-cache footprint at any context length,
and a fit verdict for a target GPU — *before* downloading a single gigabyte.

Method (ai-engineering-from-scratch, Phase 10 · Lesson 14 — the calculator
there reproduces Llama-3-8B and Gemma-2-9B published counts exactly; this
module reproduces that arithmetic and is pinned to it by tests):

* embeddings: ``vocab_size x hidden_size``
* per layer, attention: Q is ``hidden x (n_heads * head_dim)``, K and V are
  ``hidden x (n_kv_heads * head_dim)`` (GQA shrinks K/V), O is
  ``(n_heads * head_dim) x hidden``. ``head_dim`` is taken from the config
  when present (Gemma-2 sets 256 with hidden 3584 / 16 heads), else
  ``hidden // n_heads``.
* per layer, MLP: gated (SwiGLU / GeGLU) = 3 matrices of
  ``hidden x intermediate_size``; a plain MLP = 2.
* norms: RMSNorm weights, ``norms_per_layer`` per block (2 for Llama-style
  pre-norm; Gemma-2 uses pre+post around both sub-blocks = 4) plus one
  final norm.
* LM head: another ``vocab x hidden`` unless ``tie_word_embeddings``.

KV cache (Phase 07 · Lesson 12): ``2 (K and V) x layers x n_kv_heads x
head_dim x tokens x dtype_bytes``. Sliding-window layers only ever cache
``min(context, window)`` tokens, so hybrid models (Gemma-2's alternating
``layer_types``) are summed per layer.

Fit verdict: weights at the chosen quant + KV at the chosen context must
fit the GPU with 10% of VRAM held back as headroom:

* ``total <= 0.90 * VRAM``  -> ``"fits"``
* ``total <= VRAM``         -> ``"tight"`` (inside the headroom reserve)
* ``total >  VRAM``         -> ``"no-fit"``

Stdlib only. No model files are touched — this is pure config arithmetic.
"""

from __future__ import annotations

GIB = 1024 ** 3

#: Effective bytes per parameter for common GGUF-style quants
#: (bit-width / 8 plus scale/zero-point overhead, rounded to the values
#: llama.cpp files actually land at).
QUANT_BYTES_PER_PARAM = {
    "fp32": 4.0,
    "fp16": 2.0,
    "bf16": 2.0,
    "fp8": 1.0,
    "int8": 1.0,
    "q8_0": 1.0625,
    "q4_k_m": 0.5625,
    "q4_0": 0.5625,
    "q2_k": 0.328125,
}

#: RTX 2060 — the JARVIS owner's card. 6 GiB.
RTX_2060_VRAM = 6 * GIB


# --------------------------------------------------------------------------
# Reference configs (excerpts of the real HF config.json files, used as
# fixtures by the tests and as worked examples by the CLI).
# --------------------------------------------------------------------------

LLAMA_3_8B_CONFIG = {
    "vocab_size": 128256,
    "hidden_size": 4096,
    "intermediate_size": 14336,
    "num_hidden_layers": 32,
    "num_attention_heads": 32,
    "num_key_value_heads": 8,
    # head_dim is implicit: 4096 / 32 = 128. Embeddings are NOT tied
    # (missing this is the classic +525M undercount).
    "tie_word_embeddings": False,
}

GEMMA_2_9B_CONFIG = {
    "vocab_size": 256000,
    "hidden_size": 3584,
    "intermediate_size": 14336,
    "num_hidden_layers": 42,
    "num_attention_heads": 16,
    "num_key_value_heads": 8,
    # Explicit head_dim: 16 heads x 256 != hidden 3584 (Q projects wider).
    "head_dim": 256,
    "tie_word_embeddings": True,
    # Gemma-2 runs RMSNorm before AND after each sub-block (attn + MLP).
    "norms_per_layer": 4,
    "sliding_window": 4096,
    # 1:1 alternating sliding / full attention layers.
    "layer_types": ["sliding_attention", "full_attention"] * 21,
}

QWEN25_7B_CONFIG = {
    "vocab_size": 151936,
    "hidden_size": 3584,
    "intermediate_size": 18944,
    "num_hidden_layers": 28,
    "num_attention_heads": 28,
    "num_key_value_heads": 4,
    "tie_word_embeddings": True,
}

LLAMA_3_70B_CONFIG = {
    "vocab_size": 128256,
    "hidden_size": 8192,
    "intermediate_size": 28672,
    "num_hidden_layers": 80,
    "num_attention_heads": 64,
    "num_key_value_heads": 8,
    "tie_word_embeddings": False,
}


# --------------------------------------------------------------------------
# Parameter counting
# --------------------------------------------------------------------------

def head_dim_of(config: dict) -> int:
    """Attention head dimension — explicit in the config when present."""
    if config.get("head_dim"):
        return int(config["head_dim"])
    return int(config["hidden_size"]) // int(config["num_attention_heads"])


def count_parameters(config: dict) -> dict:
    """Exact parameter count for a decoder-only transformer config.

    Returns a breakdown dict; ``total`` is the headline number. Bias
    vectors are counted only when the config opts in
    (``attention_bias`` / ``mlp_bias``, both default False — the Llama /
    Gemma reference counts are bias-free weight counts).
    """
    hidden = int(config["hidden_size"])
    vocab = int(config["vocab_size"])
    inter = int(config["intermediate_size"])
    layers = int(config["num_hidden_layers"])
    n_heads = int(config["num_attention_heads"])
    n_kv = int(config.get("num_key_value_heads", n_heads))
    d_head = head_dim_of(config)

    q_width = n_heads * d_head
    kv_width = n_kv * d_head

    embedding = vocab * hidden

    attn = hidden * q_width + 2 * hidden * kv_width + q_width * hidden
    if config.get("attention_bias", False):
        attn += q_width + 2 * kv_width + hidden

    gated = config.get("gated_mlp", True)
    mlp = 3 * hidden * inter if gated else 2 * hidden * inter
    if config.get("mlp_bias", False):
        # one bias per projection output: gate+up (inter each) + down (hidden)
        mlp += 2 * inter + hidden if gated else inter + hidden

    norms_per_layer = int(config.get("norms_per_layer", 2))
    norms = norms_per_layer * hidden

    per_layer = attn + mlp + norms
    final_norm = hidden if config.get("final_norm", True) else 0
    lm_head = 0 if config.get("tie_word_embeddings", False) else vocab * hidden

    total = embedding + layers * per_layer + final_norm + lm_head
    return {
        "embedding": embedding,
        "attention_per_layer": attn,
        "mlp_per_layer": mlp,
        "norms_per_layer": norms,
        "per_layer": per_layer,
        "layers_total": layers * per_layer,
        "final_norm": final_norm,
        "lm_head": lm_head,
        "total": total,
    }


# --------------------------------------------------------------------------
# KV cache
# --------------------------------------------------------------------------

def _layer_cached_tokens(config: dict, context: int) -> list[int]:
    """Tokens actually cached per layer at the given context length."""
    layers = int(config["num_hidden_layers"])
    window = config.get("sliding_window")
    layer_types = config.get("layer_types")
    cached = []
    for i in range(layers):
        is_sliding = False
        if layer_types is not None:
            is_sliding = "sliding" in str(layer_types[i])
        elif window is not None and config.get("all_layers_sliding", False):
            is_sliding = True
        if is_sliding and window is not None:
            cached.append(min(context, int(window)))
        else:
            cached.append(context)
    return cached


def kv_cache_bytes(config: dict, context: int, dtype_bytes: int = 2) -> dict:
    """KV-cache size at ``context`` tokens.

    ``bytes_per_token`` is exact for homogeneous (all-full-attention)
    models; for hybrid sliding/full models it is the effective average
    (``total_bytes // context``) and ``per_layer_tokens`` shows the split.
    """
    n_kv = int(config.get("num_key_value_heads",
                          config["num_attention_heads"]))
    d_head = head_dim_of(config)
    per_token_per_layer = 2 * n_kv * d_head * dtype_bytes
    per_layer_tokens = _layer_cached_tokens(config, context)
    total = per_token_per_layer * sum(per_layer_tokens)
    return {
        "bytes_per_token": total // context if context else 0,
        "total_bytes": total,
        "per_layer_tokens": per_layer_tokens,
        "bytes_per_token_per_layer": per_token_per_layer,
    }


# --------------------------------------------------------------------------
# Fit verdict
# --------------------------------------------------------------------------

def vet_model(config: dict,
              gpu_vram_bytes: int = RTX_2060_VRAM,
              quant: str = "q4_k_m",
              context: int = 4096,
              kv_dtype_bytes: int = 2,
              headroom_fraction: float = 0.10) -> dict:
    """Will this model run on this GPU? Shows its arithmetic.

    Returns a dict with the verdict (``"fits"`` / ``"tight"`` /
    ``"no-fit"``), every byte figure that fed it, and human-readable
    ``lines`` spelling out the sums.
    """
    if quant not in QUANT_BYTES_PER_PARAM:
        raise ValueError(
            f"unknown quant {quant!r}; known: {sorted(QUANT_BYTES_PER_PARAM)}")
    bpp = QUANT_BYTES_PER_PARAM[quant]

    params = count_parameters(config)["total"]
    weights_bytes = int(params * bpp)
    kv = kv_cache_bytes(config, context, kv_dtype_bytes)
    kv_bytes = kv["total_bytes"]
    total_bytes = weights_bytes + kv_bytes

    usable = int(gpu_vram_bytes * (1.0 - headroom_fraction))
    if total_bytes <= usable:
        verdict = "fits"
    elif total_bytes <= gpu_vram_bytes:
        verdict = "tight"
    else:
        verdict = "no-fit"

    def gib(n: int) -> str:
        return f"{n / GIB:.2f} GiB"

    lines = [
        f"parameters        : {params:,} x {bpp} B ({quant}) "
        f"= {weights_bytes:,} B ({gib(weights_bytes)})",
        f"KV cache @ {context:,} tok: {kv_bytes:,} B ({gib(kv_bytes)}) "
        f"[fp{kv_dtype_bytes * 8} KV, {kv['bytes_per_token']:,} B/token]",
        f"total             : {total_bytes:,} B ({gib(total_bytes)})",
        f"GPU budget        : {gpu_vram_bytes:,} B ({gib(gpu_vram_bytes)}), "
        f"{headroom_fraction:.0%} headroom -> usable {usable:,} B "
        f"({gib(usable)})",
        f"verdict           : {verdict.upper()}",
    ]
    return {
        "verdict": verdict,
        "params": params,
        "weights_bytes": weights_bytes,
        "kv_bytes": kv_bytes,
        "kv_bytes_per_token": kv["bytes_per_token"],
        "total_bytes": total_bytes,
        "gpu_vram_bytes": gpu_vram_bytes,
        "usable_bytes": usable,
        "quant": quant,
        "context": context,
        "lines": lines,
    }


def _demo() -> None:
    for name, cfg in (("Llama-3-8B", LLAMA_3_8B_CONFIG),
                      ("Gemma-2-9B", GEMMA_2_9B_CONFIG),
                      ("Qwen2.5-7B", QWEN25_7B_CONFIG),
                      ("Llama-3-70B", LLAMA_3_70B_CONFIG)):
        print(f"== {name} on RTX 2060 6GB, q4_k_m, 4K context ==")
        print("\n".join(vet_model(cfg)["lines"]))
        print()


if __name__ == "__main__":
    _demo()
