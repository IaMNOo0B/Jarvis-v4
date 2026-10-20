"""Tests for jarvis.skills — quarantine, approval, drift, dispatch."""

import pytest

from jarvis.gate import Gate
from jarvis.journal import Journal
from jarvis.skills import (APPROVED, QUARANTINED, DispatchRefused,
                           SkillError, SkillRegistry)

KEY = b"test-secret-key"
OWNER = "owner"


def _echo_manifest():
    return {
        "name": "test.echo",
        "version": "1.0.0",
        "tools": [{
            "name": "echo",
            "op": "echo",
            "permission": "test.echo",
            "schema": {"type": "object",
                       "properties": {"text": {"type": "string"}},
                       "required": ["text"],
                       "additionalProperties": False},
        }],
    }


def _wipe_manifest():
    return {
        "name": "test.wipe",
        "version": "1.0.0",
        "tools": [{
            "name": "wipe_file",
            "op": "wipe",
            "permission": "fs.delete",
            "schema": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"],
                       "additionalProperties": False},
        }],
    }


@pytest.fixture()
def rig(tmp_path):
    journal = Journal(str(tmp_path / "j.jsonl"))
    gate = Gate(journal, KEY)
    registry = SkillRegistry(gate=gate, journal=journal)
    return gate, registry


def test_quarantined_until_approved(rig):
    _, registry = rig
    registry.register(_echo_manifest(), ops={"echo": lambda text: text.upper()})
    assert registry.state("test.echo") == QUARANTINED
    assert registry.advertised_tools({"test.echo"}) == []
    with pytest.raises(DispatchRefused):
        registry.dispatch("echo", {"text": "hi"})


def test_approve_advertises_and_dispatches(rig):
    _, registry = rig
    registry.register(_echo_manifest(), ops={"echo": lambda text: text.upper()})
    registry.approve("test.echo")
    assert registry.state("test.echo") == APPROVED
    names = [t["name"] for t in registry.advertised_tools({"test.echo"})]
    assert names == ["echo"]
    assert registry.dispatch("echo", {"text": "hi"}) == "HI"


def test_manifest_drift_falls_back_to_quarantine(rig):
    _, registry = rig
    registry.register(_echo_manifest(), ops={"echo": lambda text: text})
    registry.approve("test.echo")
    assert registry.state("test.echo") == APPROVED
    changed = _echo_manifest()
    changed["version"] = "1.0.1"            # any edit = a new manifest
    registry.update_manifest("test.echo", changed)
    assert registry.state("test.echo") == QUARANTINED
    assert registry.advertised_tools({"test.echo"}) == []
    with pytest.raises(DispatchRefused):
        registry.dispatch("echo", {"text": "hi"})


def test_duplicate_tool_rejected(rig):
    _, registry = rig
    registry.register(_echo_manifest(), ops={"echo": lambda text: text})
    other = _echo_manifest()
    other["name"] = "test.other"
    with pytest.raises(SkillError):
        registry.register(other, ops={"echo": lambda text: text})


def test_dispatch_unknown_tool_refused(rig):
    _, registry = rig
    with pytest.raises(DispatchRefused):
        registry.dispatch("ghost", {})


def test_destructive_dispatch_requires_verified_decision(rig):
    gate, registry = rig
    wiped = []
    registry.register(_wipe_manifest(),
                      ops={"wipe": lambda path: wiped.append(path) or "wiped"})
    registry.approve("test.wipe")

    # no decision at all
    with pytest.raises(DispatchRefused):
        registry.dispatch("wipe_file", {"path": "a.txt"})
    assert wiped == []

    # a proper gate round-trip: evaluate -> require_approval -> dispatch
    decision = gate.evaluate("wipe_file", {"path": "a.txt"}, OWNER,
                             {"fs.delete"})
    assert decision.verdict == "require_approval"
    assert registry.dispatch("wipe_file", {"path": "a.txt"},
                             decision=decision) == "wiped"
    assert wiped == ["a.txt"]

    # the same decision cannot be replayed (nonce burned by dispatch)
    with pytest.raises(DispatchRefused):
        registry.dispatch("wipe_file", {"path": "a.txt"}, decision=decision)


def test_destructive_dispatch_with_changed_args_refused(rig):
    gate, registry = rig
    wiped = []
    registry.register(_wipe_manifest(),
                      ops={"wipe": lambda path: wiped.append(path) or "wiped"})
    registry.approve("test.wipe")
    decision = gate.evaluate("wipe_file", {"path": "a.txt"}, OWNER,
                             {"fs.delete"})
    with pytest.raises(DispatchRefused):
        registry.dispatch("wipe_file", {"path": "b.txt"}, decision=decision)
    assert wiped == []


def test_gate_catalogue_synced_from_registry(rig):
    gate, registry = rig
    registry.register(_echo_manifest(), ops={"echo": lambda text: text})
    assert gate.knows("echo")
    d = gate.evaluate("echo", {"text": "x"}, OWNER, {"test.echo"})
    assert d.verdict == "allow"
