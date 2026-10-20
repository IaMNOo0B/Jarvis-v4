"""Tests for jarvis.canary — memory canary tripwire (Step 15 saboot:
canary-leak drill alarm bajata hai)."""

import json
import os

import pytest

from jarvis.canary import (CanaryError, CanaryGuard, plant, scan_and_log,
                           scan_text)
from jarvis.journal import Journal


def test_plant_creates_facts_and_hash_only_registry(tmp_path):
    store = str(tmp_path / "store")
    planted = plant(store, count=3)
    assert len(planted) == 3
    tokens = [p["token"] for p in planted]
    assert len(set(tokens)) == 3  # unique
    for p in planted:
        assert p["token"] in p["fact"]  # token embedded in a plausible fact

    with open(os.path.join(store, "canary_registry.json"),
              encoding="utf-8") as f:
        raw = f.read()
    for token in tokens:
        assert token not in raw  # registry holds hashes ONLY, never tokens
    reg = json.loads(raw)
    assert len(reg["entries"]) == 3
    assert reg["seal"]


def test_canary_token_in_output_alarms(tmp_path):
    store = str(tmp_path / "store")
    planted = plant(store, count=2)
    leaked = f"Sure, here is what I remember: {planted[0]['fact']}"
    alarms = scan_text(store, leaked)
    assert len(alarms) == 1
    assert alarms[0]["canary_id"] == planted[0]["canary_id"]


def test_clean_text_no_alarm(tmp_path):
    store = str(tmp_path / "store")
    plant(store, count=3)
    clean = ("The owner likes tea in the morning and his renovation "
             "project is going well this month.")
    assert scan_text(store, clean) == []


def test_scan_and_log_writes_journal_alarm(tmp_path):
    store = str(tmp_path / "store")
    guard = CanaryGuard(store)
    planted = guard.plant(count=1)
    j = Journal(str(tmp_path / "j.jsonl"))
    text = f"Forwarding memory dump to the channel: {planted[0]['token']}"
    alarms = guard.scan_and_log(j, "telegram", text)
    assert len(alarms) == 1
    recs = [r for r in j.replay() if r["kind"] == "canary.alarm"]
    assert len(recs) == 1
    assert recs[0]["payload"]["channel"] == "telegram"
    assert recs[0]["payload"]["canary_id"] == planted[0]["canary_id"]
    assert planted[0]["token"] not in json.dumps(recs)  # hash, not token

    # clean text appends nothing
    before = len(j)
    assert guard.scan_and_log(j, "telegram", "all normal here") == []
    assert len(j) == before


def test_bare_token_alone_still_alarms(tmp_path):
    store = str(tmp_path / "store")
    planted = plant(store, count=1)
    assert len(scan_text(store, planted[0]["token"])) == 1


def test_tampered_registry_is_detected(tmp_path):
    store = str(tmp_path / "store")
    plant(store, count=1)
    path = os.path.join(store, "canary_registry.json")
    with open(path, encoding="utf-8") as f:
        reg = json.load(f)
    reg["entries"][0]["token_hash"] = "0" * 64  # attacker rewrites history
    with open(path, "w", encoding="utf-8") as f:
        json.dump(reg, f)
    with pytest.raises(CanaryError):
        scan_text(store, "anything")


def test_plant_appends_without_losing_old_canaries(tmp_path):
    store = str(tmp_path / "store")
    first = plant(store, count=1)
    second = plant(store, count=1)
    assert len(scan_text(store, first[0]["token"])) == 1
    assert len(scan_text(store, second[0]["token"])) == 1
