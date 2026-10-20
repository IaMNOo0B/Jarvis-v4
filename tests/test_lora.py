"""Tests for jarvis.lora — adapter lifecycle registry (Steps 08/09).

The ratchet rule under test: no recorded eval delta, no activation.
"""

import json

import pytest

from jarvis.journal import Journal
from jarvis.lora import ActivationRefused, AdapterError, AdapterRegistry

BASE = "qwen2.5:7b-instruct-q4_K_M"
OTHER_BASE = "qwen2.5:0.5b-instruct"


def _reg(tmp_path, **kw):
    return AdapterRegistry(tmp_path / "adapters", **kw)


def _register(reg, name="persona-hinglish", base=BASE, **over):
    args = dict(
        name=name,
        base_model=base,
        rank=16,
        alpha=32.0,
        created_from="sha256:dataset-v1",
    )
    args.update(over)
    return reg.register(**args)


def test_register_creates_candidate_and_manifest_on_disk(tmp_path):
    reg = _reg(tmp_path)
    m = _register(reg)
    assert m["status"] == "candidate"
    assert m["eval_delta"] is None
    on_disk = json.loads(
        (tmp_path / "adapters" / "persona-hinglish" / "adapter.json").read_text(
            encoding="utf-8"
        )
    )
    assert on_disk["base_model"] == BASE
    assert on_disk["rank"] == 16
    assert on_disk["created_from"] == "sha256:dataset-v1"
    assert reg.get("persona-hinglish")["status"] == "candidate"


def test_register_rejects_duplicates_and_bad_manifests(tmp_path):
    reg = _reg(tmp_path)
    _register(reg)
    with pytest.raises(AdapterError):
        _register(reg)  # same name twice
    with pytest.raises(AdapterError):
        _register(reg, name="bad-rank", rank=0)
    with pytest.raises(AdapterError):
        _register(reg, name="no-provenance", created_from="")
    with pytest.raises(AdapterError):
        _register(reg, name="../escape")
    with pytest.raises(AdapterError):
        reg.get("ghost")


def test_activation_refused_without_eval_delta(tmp_path):
    reg = _reg(tmp_path)
    _register(reg)
    with pytest.raises(ActivationRefused):
        reg.activate("persona-hinglish")
    # the refusal changed nothing, on disk either
    assert reg.get("persona-hinglish")["status"] == "candidate"
    assert reg.active_for(BASE) is None


def test_activate_after_eval_and_journal_trail(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    reg = _reg(tmp_path, journal=journal)
    _register(reg)
    reg.record_eval("persona-hinglish", 0.07, suite="behaviour")
    m = reg.activate("persona-hinglish")
    assert m["status"] == "active"
    assert reg.active_for(BASE)["name"] == "persona-hinglish"

    kinds = [rec["kind"] for rec in journal.replay()]
    assert kinds == [
        "adapter.registered",
        "adapter.eval_recorded",
        "adapter.activated",
    ]
    assert journal.verify() == (True, None)


def test_one_active_per_base_model(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    reg = _reg(tmp_path, journal=journal)
    _register(reg, name="adapter-a")
    reg.record_eval("adapter-a", 0.05)
    reg.activate("adapter-a")

    _register(reg, name="adapter-b")
    reg.record_eval("adapter-b", 0.09)
    reg.activate("adapter-b")

    assert reg.get("adapter-a")["status"] == "retired"
    assert reg.get("adapter-b")["status"] == "active"
    actives = reg.list(base_model=BASE, status="active")
    assert [m["name"] for m in actives] == ["adapter-b"]
    retired = [r for r in journal.replay() if r["kind"] == "adapter.retired"]
    assert retired and retired[0]["payload"]["reason"] == "superseded_by:adapter-b"

    # a different base model has its own independent active slot
    _register(reg, name="draft-tuner", base=OTHER_BASE)
    reg.record_eval("draft-tuner", 0.02)
    reg.activate("draft-tuner")
    assert reg.active_for(OTHER_BASE)["name"] == "draft-tuner"
    assert reg.active_for(BASE)["name"] == "adapter-b"


def test_retire_and_terminal_state_rules(tmp_path):
    reg = _reg(tmp_path)  # no journal attached: registry must still work
    _register(reg)
    reg.record_eval("persona-hinglish", 0.03)
    reg.activate("persona-hinglish")

    with pytest.raises(ActivationRefused):
        reg.activate("persona-hinglish")  # already active, not a candidate

    m = reg.retire("persona-hinglish", reason="eval regressed")
    assert m["status"] == "retired"
    assert reg.active_for(BASE) is None

    with pytest.raises(AdapterError):
        reg.retire("persona-hinglish")  # already retired
    with pytest.raises(ActivationRefused):
        reg.activate("persona-hinglish")  # retired is terminal
    with pytest.raises(AdapterError):
        reg.record_eval("persona-hinglish", 0.5)  # no evals for the retired


def test_record_eval_keeps_latest_and_history(tmp_path):
    reg = _reg(tmp_path)
    _register(reg)
    reg.record_eval("persona-hinglish", 0.01, suite="behaviour")
    m = reg.record_eval("persona-hinglish", 0.04, suite="behaviour")
    assert m["eval_delta"]["delta"] == 0.04
    assert [e["delta"] for e in m["eval_history"]] == [0.01, 0.04]
