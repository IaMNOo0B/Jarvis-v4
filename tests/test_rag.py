"""Step 11 RAG core: RRF fusion, BM25, parent-child retrieval, reorder."""

from jarvis.rag import (
    BM25Index,
    Parent,
    chunk_parent_child,
    hyde_prompt,
    index_children,
    interleave_reorder,
    retrieve_parents,
    rrf_fuse,
    rrf_scores,
)


def test_rrf_consistent_placement_beats_one_list_brilliance():
    # The Phase 11 lesson's worked example: doc "b" ranked (3, 2) must
    # beat doc "a" ranked (1, 5): 1/63 + 1/62 > 1/61 + 1/65.
    list1 = ["a", "x", "b", "y", "c"]
    list2 = ["z", "b", "w", "v", "a"]
    scores = rrf_scores([list1, list2], k=60)
    assert scores["b"] > scores["a"]
    fused = rrf_fuse([list1, list2], k=60)
    assert fused.index("b") < fused.index("a")
    assert set(fused) == {"a", "b", "c", "v", "w", "x", "y", "z"}


def test_rrf_single_list_preserves_order():
    assert rrf_fuse([["d1", "d2", "d3"]]) == ["d1", "d2", "d3"]


def test_bm25_ranks_relevant_doc_first():
    docs = [
        "the quick brown fox jumps over the lazy dog",
        "quantum entanglement lets two particles share one state",
        "the cat sat on the mat and then the cat slept all day",
    ]
    index = BM25Index(docs)
    hits = index.search("quantum entanglement")
    assert hits[0][0] == 1
    # term frequency: doc 2 says "cat" twice, doc 0/1 not at all relevant
    hits = index.search("cat")
    assert hits[0][0] == 2
    assert all(score > 0 for _, score in hits)


def test_bm25_rare_term_beats_common_term_doc():
    docs = [
        "the the the the common words everywhere",
        "a rare zymurgy reference appears here",
    ]
    index = BM25Index(docs)
    assert index.search("zymurgy")[0][0] == 1
    assert index.search("nonexistentterm") == []


def _sample_parents():
    filler = " ".join(f"word{i}" for i in range(300))
    text = (
        "alpha bravo charlie delta echo foxtrot golf hotel india juliet "
        + filler
        + " zebra yaks xenon wolf viper umbrella tango sierra romeo quebec"
    )
    return chunk_parent_child(text, child_tokens=25, parent_tokens=100)


def test_chunking_tiles_parents_exactly():
    parents = _sample_parents()
    assert len(parents) > 1
    for parent in parents:
        parent_words = parent.text.split()
        rebuilt = []
        for child in parent.children:
            assert child.parent_id == parent.id
            rebuilt.extend(child.text.split())
            assert child.end_word - child.start_word == len(child.text.split())
        assert rebuilt == parent_words


def test_retrieval_returns_parents_not_children():
    index = index_children(_sample_parents())
    child_ids = index.search_children("zebra")
    assert child_ids, "zebra must hit at least one child"
    parents = retrieve_parents(index, child_ids)
    assert parents and all(isinstance(p, Parent) for p in parents)
    assert any("zebra" in p.text for p in parents)
    # deduplicated
    assert len({p.id for p in parents}) == len(parents)
    # the hit parent is the LAST parent (zebra lives at the end of the text)
    assert parents[0].id == index.parents[-1].id


def test_search_parents_end_to_end():
    index = index_children(_sample_parents())
    parents = index.search_parents("alpha bravo", top_parents=1)
    assert len(parents) == 1
    assert parents[0].id == index.parents[0].id


def test_interleave_puts_best_at_both_ends():
    ranked = ["r1", "r2", "r3", "r4", "r5"]
    out = interleave_reorder(ranked)
    assert out == ["r1", "r3", "r5", "r4", "r2"]
    assert out[0] == "r1"      # rank 1 first
    assert out[-1] == "r2"     # rank 2 last
    assert out[len(out) // 2] == "r5"  # weakest in the middle


def test_interleave_edge_cases():
    assert interleave_reorder([]) == []
    assert interleave_reorder(["only"]) == ["only"]
    assert interleave_reorder(["a", "b"]) == ["a", "b"]


def test_hyde_prompt_asks_for_english_passage():
    prompt = hyde_prompt("transformer me attention kaise kaam karta hai")
    assert "transformer me attention kaise kaam karta hai" in prompt
    assert "English" in prompt
    assert "Passage:" in prompt
