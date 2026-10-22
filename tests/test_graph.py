"""Knowledge graph: deterministic edges out of plain claims,
one-hop lookups, superseded facts fall silent, and recall gains
the graph's neighbours."""

from jarvis.chat import ChatApp
from jarvis.graph import KnowledgeGraph, extract_edges
from jarvis.memory import Memory


def test_extract_edges_is_conservative():
    assert extract_edges("The workshop fee is 500 rupees") == [
        ("The workshop fee", "is", "500 rupees")]
    assert extract_edges("Owais lives in Pune") == [
        ("Owais", "lives in", "Pune")]
    assert extract_edges("The sky is blue over the harbour today "
                         "and it looks really quite lovely") == []
    assert extract_edges("Hello there, how are you") == []


def test_graph_lookup_walks_one_hop(tmp_path):
    graph = KnowledgeGraph(tmp_path)
    graph.add_fact({"id": "f1", "claim": "The workshop fee is 500 "
                                        "rupees", "ts": "t1"})
    graph.add_fact({"id": "f2", "claim": "Aisha runs the workshop",
                    "ts": "t2"})
    found = graph.about("workshop")
    assert {e["predicate"] for e in found["edges"]} == {"is"}
    assert "500 rupees" in found["neighbours"]


def test_memory_learn_indexes_and_supersession_silences(tmp_path):
    mem = Memory(tmp_path / "data")
    old = mem.learn("The workshop fee is 400 rupees", source="owner")
    assert len(mem.graph) == 1
    mem.learn_lesson("The workshop fee is 400 rupees",
                     "The workshop fee is 500 rupees",
                     "The workshop fee is 500 rupees, not 400",
                     evidence="fee 400 nahi, 500 rupees hai")
    current = {f["id"] for f in mem.facts.current_facts()}
    assert old["id"] not in current
    found = mem.graph.about("workshop", current)
    claims = [e["object"] for e in found["edges"]]
    assert "400 rupees" not in claims      # superseded: silent


def test_recall_expands_through_the_graph(tmp_path):
    mem = Memory(tmp_path / "data")
    mem.learn("The workshop fee is 500 rupees", source="owner")
    mem.learn("The workshop contact is Aisha", source="owner")
    hits = mem.recall("workshop", k=1)
    assert len(hits) >= 1
    texts = [h["claim"] for h in mem.recall("workshop", k=5)]
    assert any("Aisha" in t for t in texts)


def test_app_advertises_graph_lookup(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert "graph_lookup" in names
    app.memory.learn("Owais lives in Pune", source="owner")
    out = app.execute_tool("graph_lookup", {"entity": "Pune"})
    assert out["ok"] is True
    assert any("Pune" in f for f in out["facts"])
