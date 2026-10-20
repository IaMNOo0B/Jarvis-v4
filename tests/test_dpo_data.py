"""Tests for jarvis.dpo_data — golden pairs -> SFT/DPO data machine (Step 09).
"""

import hashlib
import json

import pytest

from jarvis.dpo_data import (
    build_dataset,
    dedup_pairs,
    estimated_jaccard,
    minhash_signature,
    pair_shingles,
)


def _pair(tag, chosen_first="Certainly"):
    """A long, tag-unique preference pair (dedup-safe fixture)."""
    prompt = (
        f"user request {tag} alpha{tag} bravo{tag} charlie{tag} "
        f"delta{tag} echo{tag} foxtrot{tag} golf{tag}"
    )
    chosen = (
        f"{chosen_first} sir "
        + " ".join(f"chosen{tag}word{j}" for j in range(40))
        + f" tail{tag}"
    )
    rejected = " ".join(f"rejected{tag}word{j}" for j in range(20))
    return {
        "prompt": prompt,
        "chosen": chosen,
        "rejected": rejected,
        "origin": f"correction-{tag}",
    }


def _read_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_minhash_identical_and_disjoint_pairs():
    p1, p2 = _pair("one"), _pair("two")
    s1 = minhash_signature(pair_shingles(p1))
    s1_again = minhash_signature(pair_shingles(dict(p1)))
    s2 = minhash_signature(pair_shingles(p2))
    assert estimated_jaccard(s1, s1_again) == 1.0
    assert estimated_jaccard(s1, s2) < 0.8


def test_dedup_collapses_near_duplicate_pair():
    original = _pair("one")
    near_dup = _pair("one", chosen_first="Surely")  # one word differs
    distinct = _pair("two")
    kept, info = dedup_pairs([original, near_dup, distinct])
    assert len(kept) == 2
    assert info["removed"] == 1
    assert info["duplicate_clusters"] == [[0, 1]]
    assert kept[0] is original
    assert kept[1] is distinct


def test_build_writes_files_and_manifest_hashes_verify(tmp_path):
    records = [_pair("one"), _pair("two"), _pair("three")]
    manifest = build_dataset(records, tmp_path / "ds")

    for name in ("sft.jsonl", "dpo.jsonl"):
        path = tmp_path / "ds" / name
        assert path.exists()
        entry = manifest["outputs"][name]
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert entry["records"] == len(lines) == 3

    on_disk = json.loads(
        (tmp_path / "ds" / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    assert on_disk["outputs"] == manifest["outputs"]
    assert manifest["inputs"] == {
        "records": 3,
        "preference_pairs": 3,
        "general_records": 0,
    }
    assert manifest["dedup"]["removed"] == 0


def test_sft_mask_covers_exactly_the_response_span(tmp_path):
    pair = _pair("one")
    build_dataset([pair], tmp_path / "ds", eval_fraction=0.0)
    (rec,) = _read_jsonl(tmp_path / "ds" / "sft.jsonl")

    text, mask = rec["text"], rec["mask"]
    ps, rs = mask["prompt_span"], mask["response_span"]
    assert text == pair["prompt"] + pair["chosen"]
    assert text[ps[0] : ps[1]] == pair["prompt"]
    assert text[rs[0] : rs[1]] == pair["chosen"] == rec["response"]
    # the spans partition the text: no gap, no overlap, nothing extra trained
    assert ps == [0, len(pair["prompt"])]
    assert rs == [len(pair["prompt"]), len(text)]
    assert mask["train_span"] == rs
    assert mask["strategy"] == "response_only"
    assert mask["ignore_index"] == -100
    assert rec["split"] == "train"


def test_dpo_records_have_the_standard_shape(tmp_path):
    build_dataset([_pair("one"), _pair("two")], tmp_path / "ds", eval_fraction=0.0)
    for rec in _read_jsonl(tmp_path / "ds" / "dpo.jsonl"):
        assert rec["prompt"] and rec["chosen"] and rec["rejected"]
        assert rec["source"] == "golden_pair"


def test_general_records_pass_through_untouched(tmp_path):
    pair = _pair("one")
    general_sft = {
        "prompt": "namaste",
        "response": "Namaste sir, sab badhiya chal raha hai.",
        "source": "general",
    }
    # even a general record that near-duplicates a pair is NOT deduped
    general_pair = dict(_pair("one", chosen_first="Surely"), source="general")
    manifest = build_dataset(
        [pair, general_pair, general_sft], tmp_path / "ds", eval_fraction=0.0
    )

    sft = _read_jsonl(tmp_path / "ds" / "sft.jsonl")
    dpo = _read_jsonl(tmp_path / "ds" / "dpo.jsonl")
    assert general_sft in sft  # verbatim, byte-for-byte after JSON round-trip
    assert general_pair in sft
    assert general_pair in dpo  # full pair shape -> lands in DPO verbatim too
    assert manifest["inputs"]["general_records"] == 2
    assert manifest["general_mix"]["records_in_sft"] == 2
    assert manifest["general_mix"]["records_in_dpo"] == 1
    # the golden pair itself was untouched by the general near-duplicate
    assert manifest["dedup"]["input"] == 1
    assert manifest["dedup"]["removed"] == 0


def test_beta_recommendation_is_manifest_data(tmp_path):
    manifest = build_dataset([_pair("one")], tmp_path / "a")
    rec = manifest["beta"]["recommendation"]
    assert rec["range"] == [0.1, 0.3]
    assert 0.1 <= rec["value"] <= 0.3
    assert manifest["beta"]["chosen"] is None

    manifest2 = build_dataset([_pair("one")], tmp_path / "b", beta=0.15)
    assert manifest2["beta"]["chosen"] == 0.15


def test_split_is_deterministic_and_dedup_runs_first(tmp_path):
    records = [_pair("one"), _pair("one", chosen_first="Surely"), _pair("two"),
               _pair("three"), _pair("four")]
    build_dataset(records, tmp_path / "a", eval_fraction=0.5)
    build_dataset(records, tmp_path / "b", eval_fraction=0.5)
    dpo_a = (tmp_path / "a" / "dpo.jsonl").read_bytes()
    dpo_b = (tmp_path / "b" / "dpo.jsonl").read_bytes()
    assert dpo_a == dpo_b  # byte-identical: split + order are deterministic
    # the near-duplicate collapsed BEFORE the split: 4 records survive, not 5
    assert len(_read_jsonl(tmp_path / "a" / "dpo.jsonl")) == 4
    splits = {r["split"] for r in _read_jsonl(tmp_path / "a" / "dpo.jsonl")}
    assert splits <= {"train", "eval"}


def test_invalid_pair_is_rejected(tmp_path):
    bad = {"prompt": "kuch karo", "chosen": "ho gaya"}  # no rejected
    with pytest.raises(ValueError):
        build_dataset([bad], tmp_path / "ds")
    assert not (tmp_path / "ds").exists()  # validation happens before any write
