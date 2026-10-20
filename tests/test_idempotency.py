"""Tests for jarvis.idempotency — Step 13 saboot: kill-mid-send, dohraya asar ZERO."""

import pytest

from jarvis.idempotency import (
    EffectFailedError,
    IdempotencyConflictError,
    IdempotencyLedger,
    LedgerStateError,
    UnknownOutcomeError,
    business_key,
)
from jarvis.journal import Journal

ARGS = {"tool": "send_message", "to": "owais", "text": "meeting 5 baje hai"}


def _counting_effect(calls):
    def effect(payload):
        calls["n"] += 1
        return {"delivered": True, "echo": payload["text"]}

    return effect


def test_business_key_is_canonical_and_stable():
    k1 = business_key("thread-1", {"b": 2, "a": 1})
    k2 = business_key("thread-1", {"a": 1, "b": 2})  # key order must not matter
    assert k1 == k2 and len(k1) == 64
    assert business_key("thread-2", {"a": 1, "b": 2}) != k1
    assert business_key("thread-1", {"a": 1, "b": 3}) != k1


def test_run_executes_once_then_replays_recorded_result(tmp_path):
    ledger = IdempotencyLedger(str(tmp_path / "j.jsonl"))
    key = business_key("thread-1", ARGS)
    calls = {"n": 0}

    r1 = ledger.run(key, ARGS, _counting_effect(calls))
    r2 = ledger.run(key, ARGS, _counting_effect(calls))

    assert calls["n"] == 1  # the effect ran exactly once, ever
    assert r1 == r2 == {"delivered": True, "echo": ARGS["text"]}
    rec = ledger.lookup(key)
    assert rec["status"] == "committed"
    assert rec["unknown_outcome"] is False


def test_replay_survives_restart(tmp_path):
    path = str(tmp_path / "j.jsonl")
    key = business_key("thread-1", ARGS)
    calls = {"n": 0}
    IdempotencyLedger(path).run(key, ARGS, _counting_effect(calls))

    ledger2 = IdempotencyLedger(path)  # reopen: state rebuilt from journal
    result = ledger2.run(key, ARGS, _counting_effect(calls))
    assert calls["n"] == 1  # still not re-executed
    assert result["delivered"] is True
    assert ledger2.unknown_outcomes() == []


def test_same_key_different_args_is_a_hard_error(tmp_path):
    ledger = IdempotencyLedger(str(tmp_path / "j.jsonl"))
    key = business_key("thread-1", ARGS)
    ledger.run(key, ARGS, _counting_effect({"n": 0}))

    other = dict(ARGS, text="kuch aur bhej do")
    with pytest.raises(IdempotencyConflictError):
        ledger.begin(key, other)
    with pytest.raises(IdempotencyConflictError):
        ledger.lookup(key, other)
    with pytest.raises(IdempotencyConflictError):
        ledger.run(key, other, _counting_effect({"n": 0}))


def test_kill_mid_send_leaves_unknown_outcome_and_never_retries(tmp_path):
    path = str(tmp_path / "j.jsonl")
    key = business_key("thread-9", ARGS)

    # Process 1: PENDING hits the journal, then the process "dies" mid-send.
    ledger1 = IdempotencyLedger(path)
    begun = ledger1.begin(key, ARGS)
    assert begun["status"] == "pending"
    del ledger1

    # Process 2 (after restart): the key is reported UNKNOWN-OUTCOME.
    ledger2 = IdempotencyLedger(path)
    unknowns = ledger2.unknown_outcomes()
    assert [u["key"] for u in unknowns] == [key]
    assert unknowns[0]["unknown_outcome"] is True

    # run() refuses to auto-retry: the effect must not fire again.
    calls = {"n": 0}
    with pytest.raises(UnknownOutcomeError):
        ledger2.run(key, ARGS, _counting_effect(calls))
    assert calls["n"] == 0


def test_second_begin_returns_existing_record_without_duplicating(tmp_path):
    path = str(tmp_path / "j.jsonl")
    ledger = IdempotencyLedger(path)
    key = business_key("thread-1", ARGS)
    first = ledger.begin(key, ARGS)
    entries = len(ledger.journal)
    second = ledger.begin(key, ARGS)
    assert second["begun_seq"] == first["begun_seq"]
    assert len(ledger.journal) == entries  # no duplicate BEGIN appended


def test_failed_effect_is_terminal_for_the_key(tmp_path):
    ledger = IdempotencyLedger(str(tmp_path / "j.jsonl"))
    key = business_key("thread-1", ARGS)
    calls = {"n": 0}

    def boom(payload):
        calls["n"] += 1
        raise ValueError("network down")

    with pytest.raises(ValueError):
        ledger.run(key, ARGS, boom)
    assert ledger.lookup(key)["status"] == "failed"

    with pytest.raises(EffectFailedError):
        ledger.run(key, ARGS, _counting_effect(calls))
    assert calls["n"] == 1  # only the original failing attempt ran


def test_commit_and_fail_require_pending(tmp_path):
    ledger = IdempotencyLedger(str(tmp_path / "j.jsonl"))
    key = business_key("thread-1", ARGS)
    with pytest.raises(LedgerStateError):
        ledger.commit(key, {"x": 1})
    ledger.begin(key, ARGS)
    ledger.commit(key, {"x": 1})
    with pytest.raises(LedgerStateError):
        ledger.commit(key, {"x": 2})
    with pytest.raises(LedgerStateError):
        ledger.fail(key, "too late")


def test_recorded_result_is_a_defensive_copy(tmp_path):
    ledger = IdempotencyLedger(str(tmp_path / "j.jsonl"))
    key = business_key("thread-1", ARGS)
    result = ledger.run(key, ARGS, lambda p: {"items": [1, 2]})
    result["items"].append(99)  # mutate the caller's copy
    assert ledger.lookup(key)["result"] == {"items": [1, 2]}


def test_journal_chain_still_verifies_after_ledger_ops(tmp_path):
    path = str(tmp_path / "j.jsonl")
    ledger = IdempotencyLedger(path)
    calls = {"n": 0}
    for i in range(3):
        args = dict(ARGS, text=f"msg {i}")
        ledger.run(business_key(f"t-{i}", args), args, _counting_effect(calls))
    ok, bad = Journal(path).verify()
    assert ok is True and bad is None
