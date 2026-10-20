"""Tests for jarvis.journal — written and run by Ovi personally."""

import json

import pytest

from jarvis.journal import GENESIS_HASH, Journal, JournalError


def test_append_and_replay_order(tmp_path):
    j = Journal(str(tmp_path / "j.jsonl"))
    j.append("user.message", {"text": "chrome kholo"})
    j.append("tool.called", {"tool": "open_app", "app": "chrome"})
    j.append("assistant.reply", {"text": "Chrome khul gaya, sir."})
    recs = list(j.replay())
    assert [r["seq"] for r in recs] == [1, 2, 3]
    assert [r["kind"] for r in recs] == ["user.message", "tool.called", "assistant.reply"]
    assert recs[0]["prev"] == GENESIS_HASH
    assert recs[1]["prev"] == recs[0]["hash"]
    assert len(j) == 3


def test_hinglish_payload_survives(tmp_path):
    j = Journal(str(tmp_path / "j.jsonl"))
    j.append("user.message", {"text": "yaar kal subah 7 baje utha dena"})
    rec = list(j.replay())[0]
    assert rec["payload"]["text"] == "yaar kal subah 7 baje utha dena"


def test_verify_clean_chain(tmp_path):
    j = Journal(str(tmp_path / "j.jsonl"))
    for i in range(5):
        j.append("tick", {"i": i})
    ok, bad = j.verify()
    assert ok is True and bad is None


def test_verify_detects_tampering(tmp_path):
    p = tmp_path / "j.jsonl"
    j = Journal(str(p))
    for i in range(4):
        j.append("payment.intent", {"amount": 100 + i})
    lines = p.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["payload"]["amount"] = 999999  # history rewritten
    lines[1] = json.dumps(rec, ensure_ascii=False)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    j2 = Journal(str(p))
    ok, bad = j2.verify()
    assert ok is False and bad == 2


def test_snapshot_round_trip_and_reopen(tmp_path):
    p = str(tmp_path / "j.jsonl")
    j = Journal(p)
    j.append("fact.learned", {"fact": "owner ko chai pasand hai"})
    j.snapshot({"facts": 1, "mood": "calm"})
    j.append("fact.learned", {"fact": "owner raat ko kaam karta hai"})
    snap = j.latest_snapshot()
    assert snap is not None
    seq, state = snap
    assert seq == 2 and state == {"facts": 1, "mood": "calm"}
    # reopen: seq resumes, chain still verifies
    j2 = Journal(p)
    assert len(j2) == 3
    assert j2.verify() == (True, None)
    j2.append("tick", {})
    assert len(j2) == 4


def test_replay_upto_seq(tmp_path):
    j = Journal(str(tmp_path / "j.jsonl"))
    for i in range(5):
        j.append("n", {"i": i})
    recs = list(j.replay(upto_seq=3))
    assert [r["seq"] for r in recs] == [1, 2, 3]


def test_empty_journal(tmp_path):
    j = Journal(str(tmp_path / "missing.jsonl"))
    assert len(j) == 0
    assert list(j.replay()) == []
    assert j.latest_snapshot() is None
    assert j.verify() == (True, None)


def test_reserved_kind_rejected(tmp_path):
    j = Journal(str(tmp_path / "j.jsonl"))
    with pytest.raises(ValueError):
        j.append("__snapshot__", {})
    with pytest.raises(ValueError):
        j.append("", {})
