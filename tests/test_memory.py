"""Memory (S5): facts over FactStore + FTS5/BM25 conversation history.
"""

from __future__ import annotations

import pytest

from jarvis.journal import Journal
from jarvis.memory import Memory


@pytest.fixture()
def mem(tmp_path):
    m = Memory(tmp_path / "data")
    yield m
    m.close()


def test_learn_and_recall_ranked_by_overlap(mem):
    mem.learn("mera naam Owais hai")
    mem.learn("mera sheher Mumbai hai, Maharashtra me")
    hits = mem.recall("mera naam kya hai", k=5)
    assert hits, "recall returned nothing"
    assert "Owais" in hits[0]["text"]
    assert hits[0]["source"] == "owner"
    assert hits[0]["source_trust"] == "owner"
    assert all(h["score"] > 0 for h in hits)


def test_learn_records_trust_from_source(mem):
    mem.learn("web se mila ek daawa", source="web")
    mem.learn("khud ka inference", source="inference")
    by_text = {f["claim"]: f for f in mem.facts.current_facts()}
    assert by_text["web se mila ek daawa"]["source_trust"] == "web"
    assert by_text["khud ka inference"]["source_trust"] == "inference"
    # An unknown source can never smuggle in owner trust.
    mem.learn("anjaan source", source="mars")
    by_text = {f["claim"]: f for f in mem.facts.current_facts()}
    assert by_text["anjaan source"]["source_trust"] == "inference"


def test_every_learn_is_journaled(tmp_path):
    journal = Journal(str(tmp_path / "data" / "journal.jsonl"))
    mem = Memory(tmp_path / "data", journal=journal)
    mem.learn("journal me likho ye baat")
    kinds = [(r["kind"], r["payload"]) for r in journal.replay()]
    learns = [p for k, p in kinds if k == "memory.learn"]
    assert learns and learns[0]["claim"] == "journal me likho ye baat"
    mem.close()


def test_remember_is_inference_trust(mem):
    out = mem.remember("model ke dwara note ki gayi baat")
    assert out["stored"] == "model ke dwara note ki gayi baat"
    facts = mem.facts.current_facts()
    assert facts[0]["source_trust"] == "inference"
    assert mem.recall("note ki gayi baat")


def test_excerpts_returns_plain_strings(mem):
    mem.learn("mera pasandeeda khaana biryani hai")
    lines = mem.excerpts("pasandeeda khaana")
    assert lines == ["mera pasandeeda khaana biryani hai"]


def test_history_search_fts(mem):
    mem.note_turn("user", "kal subah doctor ko call karna hai")
    mem.note_turn("assistant", "Ji sir, reminder set kar diya.")
    hits = mem.search_history("doctor call")
    assert hits and hits[0]["role"] == "user"
    assert "doctor" in hits[0]["text"]


def test_history_search_bm25_fallback(tmp_path):
    mem = Memory(tmp_path / "data", use_fts=False)
    assert mem.fts_active is False
    mem.note_turn("user", "mumbai ki train ka time batao")
    hits = mem.search_history("train time")
    assert hits and "train" in hits[0]["text"]
    mem.close()


def test_history_search_deduplicates(mem):
    mem.note_turn("user", "same line do baar")
    mem.note_turn("user", "same line do baar")
    hits = mem.search_history("same line")
    user_hits = [h for h in hits if h["role"] == "user"]
    assert len(user_hits) == 1


def test_recall_of_empty_store_and_history(mem):
    assert mem.recall("kuch bhi") == []
    assert mem.search_history("kuch bhi") == []
