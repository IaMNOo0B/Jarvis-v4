"""Golden pairs -> SFT/DPO training data — JARVIS v4, Step 09 data machine.

The owner's corrections ARE Bradley-Terry preference data (study Part D,
Phase 09 L09): every correction yields one (prompt, chosen, rejected)
sample. This module is the deterministic data layer that turns those pairs
into training files. No torch, no training — files + hashes + manifest.

Pipeline (study Part E L03/L06/L08, Part D Phase 09):
    records
      -> partition: preference pairs vs general-mix records
         (tagged source == "general"; they bypass dedup and are written
         verbatim — the 2-5% general-data slot that keeps persona pairs
         from crowding out general chat, Part E L06)
      -> MinHash dedup over pairs: 5-word shingles, 128 hashes,
         estimated Jaccard >= 0.8 = duplicate (Part E L03 — the golden-pair
         store hygiene rule: near-duplicate corrections merge, not pile up)
      -> deterministic train/eval split, applied AFTER dedup so a
         near-duplicate can never straddle the split (leakage rule)
      -> sft.jsonl : response-only masking spec (ignore_index -100 over the
         prompt span; the train span is exactly the response span)
      -> dpo.jsonl : the standard {prompt, chosen, rejected} shape
      -> dataset_manifest.json : sha256 of every output file + counts +
         the beta recommendation recorded as DATA (range 0.1-0.3, sweet
         spot per Part E L08) — training reads beta from the manifest,
         never from a hardcoded trainer default.

Outputs are byte-stable for identical inputs (fixed hash-family seed,
hash-based split), so manifest hashes are reproducible.
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

SHINGLE_SIZE = 5
NUM_HASHES = 128
DEDUP_THRESHOLD = 0.8
IGNORE_INDEX = -100
GENERAL_TARGET_RANGE = (0.02, 0.05)

_PRIME = (1 << 61) - 1
_HASH_SEED = 0xD90  # fixed on purpose: dataset hashes must reproduce

# Universal hash family for MinHash: h_i(x) = (a_i * x + b_i) mod (2^61 - 1).
# Generated once from a fixed seed so signatures are stable across runs.
_rng = random.Random(_HASH_SEED)
_A = tuple(_rng.randrange(1, _PRIME) for _ in range(NUM_HASHES))
_B = tuple(_rng.randrange(0, _PRIME) for _ in range(NUM_HASHES))

# The beta recommendation lives here ONLY so it can be written into the
# dataset manifest as data; nothing in this module trains with it.
BETA_RECOMMENDATION = {
    "value": 0.2,
    "range": [0.1, 0.3],
    "basis": "ai-eng study Part E L08: DPO beta sweet spot 0.1-0.3; "
             "implicit reward R = beta * log(pi/pi_ref)",
}


# ----------------------------------------------------------------------
# MinHash (Part E L03: 5-word shingles, 128 hashes, Jaccard >= 0.8)
# ----------------------------------------------------------------------

def _shingle_ints(text: str) -> set[int]:
    """Hash the word shingles of one text field into a set of ints."""
    words = (text or "").lower().split()
    if not words:
        return set()
    k = min(SHINGLE_SIZE, len(words))
    out: set[int] = set()
    for i in range(len(words) - k + 1):
        shingle = " ".join(words[i : i + k])
        digest = hashlib.sha256(shingle.encode("utf-8")).digest()
        out.add(int.from_bytes(digest[:8], "little"))
    return out


def pair_shingles(pair: dict) -> set[int]:
    """Shingle set of a preference pair, per field, unioned.

    Fields are shingled separately so a boundary between prompt/chosen/
    rejected never invents a shingle neither field contains.
    """
    out: set[int] = set()
    for field in ("prompt", "chosen", "rejected"):
        out |= _shingle_ints(str(pair.get(field, "")))
    return out


def minhash_signature(shingles: set[int]) -> tuple[int, ...]:
    """128-int MinHash signature of a shingle set."""
    if not shingles:
        return tuple(_PRIME for _ in range(NUM_HASHES))
    return tuple(
        min((a * h + b) % _PRIME for h in shingles) for a, b in zip(_A, _B)
    )


def estimated_jaccard(sig_a: tuple[int, ...], sig_b: tuple[int, ...]) -> float:
    """Fraction of equal signature positions — the MinHash Jaccard estimate."""
    if len(sig_a) != len(sig_b):
        raise ValueError("signatures must have equal length")
    if not sig_a:
        return 1.0
    equal = sum(1 for x, y in zip(sig_a, sig_b) if x == y)
    return equal / len(sig_a)


def dedup_pairs(pairs: list[dict]) -> tuple[list[dict], dict]:
    """Collapse near-duplicate pairs (estimated Jaccard >= 0.8).

    Keeps the FIRST occurrence of each cluster, preserves input order.
    Returns (kept_pairs, info) where info lists the duplicate clusters.
    O(n^2) signature comparisons — right-sized for a golden-pair store.
    """
    sigs = [minhash_signature(pair_shingles(p)) for p in pairs]
    parent = list(range(len(pairs)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            if estimated_jaccard(sigs[i], sigs[j]) >= DEDUP_THRESHOLD:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)

    clusters: dict[int, list[int]] = {}
    for idx in range(len(pairs)):
        clusters.setdefault(find(idx), []).append(idx)

    kept_indices = sorted(min(members) for members in clusters.values())
    kept = [pairs[i] for i in kept_indices]
    multi = sorted(
        (sorted(members) for members in clusters.values() if len(members) > 1),
        key=lambda members: members[0],
    )
    info = {
        "method": "minhash",
        "shingle_size": SHINGLE_SIZE,
        "num_hashes": NUM_HASHES,
        "threshold": DEDUP_THRESHOLD,
        "input": len(pairs),
        "kept": len(kept),
        "removed": len(pairs) - len(kept),
        "duplicate_clusters": multi,
    }
    return kept, info


# ----------------------------------------------------------------------
# Record shaping
# ----------------------------------------------------------------------

def _split_for(prompt: str, eval_fraction: float) -> str:
    """Deterministic split from the prompt hash — stable across runs."""
    if eval_fraction <= 0:
        return "train"
    digest = hashlib.sha256((prompt or "").strip().encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "little") % 10_000
    return "eval" if bucket < round(eval_fraction * 10_000) else "train"


def sft_record(pair: dict, split: str) -> dict:
    """One SFT record with a response-only masking spec.

    text = prompt + response, so the character spans partition the text
    exactly: prompt_span is ignored (ignore_index -100), train_span IS the
    response span — nothing more, nothing less is trained on.
    """
    prompt = str(pair["prompt"])
    response = str(pair["chosen"])
    p_end = len(prompt)
    t_end = p_end + len(response)
    return {
        "prompt": prompt,
        "response": response,
        "text": prompt + response,
        "mask": {
            "strategy": "response_only",
            "ignore_index": IGNORE_INDEX,
            "prompt_span": [0, p_end],
            "response_span": [p_end, t_end],
            "train_span": [p_end, t_end],
        },
        "origin": pair.get("origin"),
        "split": split,
        "source": "golden_pair",
    }


def dpo_record(pair: dict, split: str) -> dict:
    """One DPO record in the standard {prompt, chosen, rejected} shape."""
    return {
        "prompt": str(pair["prompt"]),
        "chosen": str(pair["chosen"]),
        "rejected": str(pair["rejected"]),
        "origin": pair.get("origin"),
        "split": split,
        "source": "golden_pair",
    }


# ----------------------------------------------------------------------
# Build
# ----------------------------------------------------------------------

def _is_general(record: dict) -> bool:
    return record.get("source") == "general"


def _validate_pair(record: dict, index: int) -> None:
    for field in ("prompt", "chosen", "rejected"):
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"record {index}: preference pair needs a non-empty {field!r}"
            )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def build_dataset(
    records: Iterable[dict],
    out_dir: str | Path,
    *,
    eval_fraction: float = 0.1,
    beta: Optional[float] = None,
) -> dict:
    """Run the full pipeline and write sft.jsonl, dpo.jsonl, manifest.

    Returns the manifest dict (also written to dataset_manifest.json).
    `beta`, when given, is recorded in the manifest as the chosen value —
    the recommendation itself always ships as manifest data.
    """
    records = list(records)
    general = [r for r in records if _is_general(r)]
    pairs: list[dict] = []
    for idx, record in enumerate(records):
        if _is_general(record):
            continue
        _validate_pair(record, idx)
        pairs.append(record)

    kept, dedup_info = dedup_pairs(pairs)

    sft_lines: list[str] = []
    dpo_lines: list[str] = []
    split_counts = {"train": 0, "eval": 0}
    for pair in kept:
        split = _split_for(str(pair["prompt"]), eval_fraction)
        split_counts[split] += 1
        sft_lines.append(json.dumps(sft_record(pair, split), ensure_ascii=False))
        dpo_lines.append(json.dumps(dpo_record(pair, split), ensure_ascii=False))
    general_in_dpo = 0
    for record in general:
        verbatim = json.dumps(record, ensure_ascii=False)
        sft_lines.append(verbatim)
        if all(isinstance(record.get(k), str) for k in ("prompt", "chosen", "rejected")):
            dpo_lines.append(verbatim)
            general_in_dpo += 1

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sft_path = out / "sft.jsonl"
    dpo_path = out / "dpo.jsonl"
    sft_path.write_text("".join(line + "\n" for line in sft_lines), encoding="utf-8")
    dpo_path.write_text("".join(line + "\n" for line in dpo_lines), encoding="utf-8")

    total_sft = len(sft_lines)
    manifest: dict[str, Any] = {
        "format": "jarvis-dpo-dataset/v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "records": len(records),
            "preference_pairs": len(pairs),
            "general_records": len(general),
        },
        "dedup": dedup_info,
        "split": {
            "method": "sha256(prompt) bucket, assigned after dedup",
            "eval_fraction": eval_fraction,
            "counts": split_counts,
        },
        "general_mix": {
            "records_in_sft": len(general),
            "records_in_dpo": general_in_dpo,
            "fraction_of_sft": (len(general) / total_sft) if total_sft else 0.0,
            "target_range": list(GENERAL_TARGET_RANGE),
            "note": "general records bypass dedup and are written verbatim",
        },
        "outputs": {
            "sft.jsonl": {"sha256": _sha256_file(sft_path), "records": total_sft},
            "dpo.jsonl": {"sha256": _sha256_file(dpo_path), "records": len(dpo_lines)},
        },
        "beta": {"recommendation": dict(BETA_RECOMMENDATION), "chosen": beta},
    }
    (out / "dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest
