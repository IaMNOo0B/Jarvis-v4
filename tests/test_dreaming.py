"""Dreaming: similarity alone never decides (the Pune/Mumbai
rule). Pure duplicates merge model-free; near-duplicates merge
only with identical entity sets; numbers-only disagreements
settle newer-wins; everything else is kept apart and recorded."""

from jarvis.dreaming import dream
from jarvis.memory import Memory


def _mem(tmp_path, claims):
    mem = Memory(tmp_path / "data")
    for claim in claims:
        mem.learn(claim, source="owner")
    return mem


def _claims(mem):
    return sorted(f["claim"] for f in mem.facts.current_facts())


def test_pure_duplicates_merge_to_one(tmp_path):
    # Raw duplicates are written through the store directly:
    # Memory.learn() dedupes identical claims at the door
    # (audit fix), so duplicates reach dreaming via imports,
    # restores and older data — dreaming is the second wall.
    mem = Memory(tmp_path / "data")
    for _ in range(3):
        mem.facts.add(claim="The owner lives in Pune",
                      source="owner", source_trust="owner",
                      presented_as="owner")
    report = dream(tmp_path / "data", mem.facts)
    assert report["merged"] == 2
    assert _claims(mem) == ["The owner lives in Pune"]


def test_pune_mumbai_are_kept_apart_forever(tmp_path):
    mem = _mem(tmp_path, ["The owner lives in Pune",
                          "The owner lives in Mumbai"])
    report = dream(tmp_path / "data", mem.facts)
    assert report["merged"] == 0 and report["settled"] == 0
    assert len(mem.facts.current_facts()) == 2
    # a second pass does not re-litigate the pair
    report2 = dream(tmp_path / "data", mem.facts)
    assert report2["examined"] == 0


def test_numbers_only_change_settles_newer_wins(tmp_path):
    mem = _mem(tmp_path, ["The workshop fee is 400 rupees",
                          "The workshop fee is 500 rupees"])
    report = dream(tmp_path / "data", mem.facts)
    assert report["settled"] == 1
    assert _claims(mem) == ["The workshop fee is 500 rupees"]


def test_near_duplicates_with_same_entities_merge(tmp_path):
    mem = _mem(tmp_path, [
        "The owner likes filter coffee in the morning",
        "The owner likes filter coffee in the morning daily"])
    report = dream(
        tmp_path / "data", mem.facts,
        merge_text=lambda a, b: "The owner likes filter coffee "
                                "in the morning daily")
    assert report["merged"] == 1
    assert _claims(mem) == [
        "The owner likes filter coffee in the morning daily"]


def test_unrelated_facts_are_untouched(tmp_path):
    mem = _mem(tmp_path, ["The owner lives in Pune",
                          "The sky is blue over the harbour"])
    report = dream(tmp_path / "data", mem.facts)
    assert report == {"merged": 0, "settled": 0, "kept_apart": 0,
                      "examined": 0}
    assert len(mem.facts.current_facts()) == 2
