"""Step 10 saboot: the vetting calculator must reproduce published counts.

Targets are the exact totals the ai-engineering-from-scratch Phase 10
calculator reproduces (and that published parameter counts confirm):
Llama-3-8B = 8,030,261,248 and Gemma-2-9B = 9,241,705,984.
"""

import pytest

from jarvis.model_vet import (
    GEMMA_2_9B_CONFIG,
    LLAMA_3_8B_CONFIG,
    LLAMA_3_70B_CONFIG,
    QWEN25_7B_CONFIG,
    RTX_2060_VRAM,
    count_parameters,
    kv_cache_bytes,
    vet_model,
)


def test_llama3_8b_exact_param_count():
    assert count_parameters(LLAMA_3_8B_CONFIG)["total"] == 8_030_261_248


def test_gemma2_9b_exact_param_count():
    assert count_parameters(GEMMA_2_9B_CONFIG)["total"] == 9_241_705_984


def test_tied_embeddings_have_no_lm_head():
    breakdown = count_parameters(GEMMA_2_9B_CONFIG)
    assert breakdown["lm_head"] == 0
    untied = count_parameters(LLAMA_3_8B_CONFIG)
    assert untied["lm_head"] == untied["embedding"] == 128256 * 4096


def test_qwen25_7b_kv_bytes_per_token_fp16():
    # 2 (K,V) x 28 layers x 4 KV heads x 128 head_dim x 2 B = 57,344 B
    # = 56 KiB per token — the figure the Modelfile sizing was built on.
    kv = kv_cache_bytes(QWEN25_7B_CONFIG, context=4096, dtype_bytes=2)
    assert kv["bytes_per_token"] == 57_344
    assert kv["bytes_per_token"] // 1024 == 56
    assert kv["total_bytes"] == 57_344 * 4096


def test_gemma2_sliding_layers_cap_the_cache():
    # At 8192 context, sliding layers cache only the 4096 window.
    kv = kv_cache_bytes(GEMMA_2_9B_CONFIG, context=8192, dtype_bytes=2)
    assert kv["per_layer_tokens"][:4] == [4096, 8192, 4096, 8192]
    full_only = 2 * 8 * 256 * 2 * 42 * 8192
    assert kv["total_bytes"] < full_only
    assert kv["total_bytes"] == 2 * 8 * 256 * 2 * (21 * 4096 + 21 * 8192)


def test_70b_does_not_fit_on_2060():
    result = vet_model(LLAMA_3_70B_CONFIG, gpu_vram_bytes=RTX_2060_VRAM)
    assert result["verdict"] == "no-fit"
    assert result["weights_bytes"] > RTX_2060_VRAM


def test_8b_fits_on_2060_at_q4_with_arithmetic_shown():
    result = vet_model(LLAMA_3_8B_CONFIG, gpu_vram_bytes=RTX_2060_VRAM,
                       quant="q4_k_m", context=4096)
    assert result["verdict"] == "fits"
    assert result["total_bytes"] == result["weights_bytes"] + result["kv_bytes"]
    assert result["total_bytes"] <= result["usable_bytes"]
    assert any("8,030,261,248" in line for line in result["lines"])


def test_unknown_quant_rejected():
    with pytest.raises(ValueError):
        vet_model(LLAMA_3_8B_CONFIG, quant="q9_nope")
