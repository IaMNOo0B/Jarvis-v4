"""Tests for jarvis.gate — fail-closed evaluation + HMAC approvals."""

import pytest

from jarvis.gate import (ALLOW, ALLOW_WITH_UNDO, DENY, REQUIRE_APPROVAL,
                         Gate)
from jarvis.journal import Journal

KEY = b"test-secret-key"
OWNER = "owner"
ALL = {"clock.read", "fs.write", "fs.delete", "message.send"}


@pytest.fixture()
def gate(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    g = Gate(journal, KEY)
    g.register_tool("time_now", permission="clock.read",
                    schema={"type": "object", "properties": {},
                            "additionalProperties": False})
    g.register_tool("write_file", permission="fs.write", undoable=True,
                    schema={"type": "object",
                            "properties": {"path": {"type": "string"},
                                           "text": {"type": "string"}},
                            "required": ["path", "text"],
                            "additionalProperties": False})
    g.register_tool("delete_file", permission="fs.delete",
                    schema={"type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                            "additionalProperties": False})
    g.register_tool("send_message", permission="message.send",
                    schema={"type": "object",
                            "properties": {"to": {"type": "string"},
                                           "body": {"type": "string"}},
                            "required": ["to", "body"],
                            "additionalProperties": False})
    return g


def test_unknown_tool_denied(gate):
    d = gate.evaluate("nope", {}, OWNER, ALL)
    assert d.verdict == DENY
    assert "unknown tool" in d.reason


def test_untrusted_origin_denied(gate):
    d = gate.evaluate("time_now", {}, "untrusted", ALL)
    assert d.verdict == DENY
    d2 = gate.evaluate("time_now", {}, "web:example.com", ALL)
    assert d2.verdict == DENY
    assert "IFC" in d2.reason


def test_missing_permission_denied_and_named(gate):
    d = gate.evaluate("time_now", {}, OWNER, {"fs.write"})
    assert d.verdict == DENY
    assert d.missing_capabilities == ["clock.read"]
    assert "clock.read" in d.reason


def test_allow_and_allow_with_undo(gate):
    assert gate.evaluate("time_now", {}, OWNER, ALL).verdict == ALLOW
    d = gate.evaluate("write_file", {"path": "a.txt", "text": "hi"}, OWNER, ALL)
    assert d.verdict == ALLOW_WITH_UNDO


def test_invalid_args_denied_with_exact_path(gate):
    d = gate.evaluate("write_file", {"path": 42}, OWNER, ALL)
    assert d.verdict == DENY
    assert d.schema_errors  # paths are reported
    joined = " ".join(d.schema_errors)
    assert "/path" in joined          # wrong type points at /path
    assert d.reason.startswith("invalid arguments")


def test_destructive_requires_approval(gate):
    d = gate.evaluate("delete_file", {"path": "x"}, OWNER, ALL)
    assert d.verdict == REQUIRE_APPROVAL
    assert d.approval_token
    d2 = gate.evaluate("send_message", {"to": "a", "body": "b"}, OWNER, ALL)
    assert d2.verdict == REQUIRE_APPROVAL


def test_approval_token_verifies_once(gate):
    d = gate.evaluate("delete_file", {"path": "x"}, OWNER, ALL)
    assert gate.verify_approval(d.approval_token, "delete_file",
                                {"path": "x"}) is True
    # nonce burned — replay fails
    assert gate.verify_approval(d.approval_token, "delete_file",
                                {"path": "x"}) is False


def test_one_changed_argument_kills_token(gate):
    d = gate.evaluate("delete_file", {"path": "x"}, OWNER, ALL)
    assert gate.verify_approval(d.approval_token, "delete_file",
                                {"path": "y"}) is False


def test_token_bound_to_tool(gate):
    d = gate.evaluate("delete_file", {"path": "x"}, OWNER, ALL)
    assert gate.verify_approval(d.approval_token, "send_message",
                                {"path": "x"}) is False


def test_expired_token_rejected(tmp_path):
    clock = [1000.0]
    journal = Journal(str(tmp_path / "j.jsonl"))
    g = Gate(journal, KEY, now_fn=lambda: clock[0])
    g.register_tool("delete_file", permission="fs.delete",
                    schema={"type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"]})
    d = g.evaluate("delete_file", {"path": "x"}, OWNER, {"fs.delete"})
    clock[0] += 121  # past the 120 s TTL
    assert g.verify_approval(d.approval_token, "delete_file",
                             {"path": "x"}) is False


def test_tampered_token_rejected(gate):
    d = gate.evaluate("delete_file", {"path": "x"}, OWNER, ALL)
    body, _, _ = d.approval_token.partition(".")
    forged = body + "." + "0" * 64
    assert gate.verify_approval(forged, "delete_file", {"path": "x"}) is False
    assert gate.verify_approval("garbage", "delete_file", {"path": "x"}) is False
    assert gate.verify_approval(None, "delete_file", {"path": "x"}) is False


def test_every_decision_is_journaled(gate, tmp_path):
    gate.evaluate("time_now", {}, OWNER, ALL)
    gate.evaluate("nope", {}, OWNER, ALL)
    kinds = [rec["kind"] for rec in gate.journal.replay()]
    assert kinds.count("gate.decision") == 2
    rec = [r for r in gate.journal.replay() if r["kind"] == "gate.decision"][0]
    assert rec["payload"]["tool"] == "time_now"
    assert rec["payload"]["verdict"] == ALLOW
    # raw args never enter the journal — only their digest
    assert "args" not in rec["payload"]
    assert len(rec["payload"]["args_digest"]) == 64


def test_spent_token_stays_spent_across_restart(tmp_path):
    path = str(tmp_path / "j.jsonl")
    g1 = Gate(Journal(path), KEY)
    g1.register_tool("delete_file", permission="fs.delete",
                     schema={"type": "object",
                             "properties": {"path": {"type": "string"}},
                             "required": ["path"]})
    d = g1.evaluate("delete_file", {"path": "x"}, OWNER, {"fs.delete"})
    assert g1.verify_approval(d.approval_token, "delete_file",
                               {"path": "x"}) is True
    g2 = Gate(Journal(path), KEY)  # fresh gate, same journal
    assert g2.verify_approval(d.approval_token, "delete_file",
                              {"path": "x"}) is False
