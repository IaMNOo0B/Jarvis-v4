"""File access (the owner's full-PC doctrine) + self-diagnosis from
JARVIS's own records. Hermetic: tmp folders, planted ledgers."""
import json
from pathlib import Path

from jarvis import builtin_skills as bs
from jarvis.chat import ChatApp, _diagnose_op
from jarvis.providers import FallbackChain, StubProvider


class CountingChain:
    name = "counting"

    def __init__(self):
        self.inner = FallbackChain([StubProvider()])
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        return self.inner.complete(messages, tools)


def test_list_and_read_round_trip(tmp_path):
    (tmp_path / "notes").mkdir()
    target = tmp_path / "notes" / "plan.txt"
    target.write_text("Project Falcon launch notes.", encoding="utf-8")
    listing = bs.op_list_files(str(tmp_path / "notes"))
    assert listing["entries"][0]["name"] == "plan.txt"
    out = bs.op_read_file(str(target))
    assert out["text"] == "Project Falcon launch notes."
    assert out["truncated"] is False


def test_read_file_missing_and_binary_are_honest(tmp_path):
    assert "error" in bs.op_read_file(str(tmp_path / "nope.txt"))
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"\x00\x01\x02binary")
    out = bs.op_read_file(str(blob))
    assert out["binary"] is True and out["size"] == 9


def test_search_files_by_name(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "falcon-report.txt").write_text("x")
    (tmp_path / "a" / "other.txt").write_text("x")
    out = bs.op_search_files(root=str(tmp_path), query="falcon")
    assert len(out["matches"]) == 1
    assert out["matches"][0].endswith("falcon-report.txt")


def test_write_file_creates_and_reads_back(tmp_path):
    target = tmp_path / "deep" / "out.txt"
    out = bs.op_write_file(str(target), "likha gaya")
    assert out["chars"] == 10
    assert bs.op_read_file(str(target))["text"] == "likha gaya"


def test_diagnose_names_a_dead_provider_and_missing_key(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    rows = [{"provider": "ollama", "latency_ms": 5.0, "ok": False,
             "quality_ok": False, "ts": 1.0}] * 4
    rows.append({"provider": "stub", "latency_ms": 1.0, "ok": True,
                 "quality_ok": False, "ts": 2.0})
    (data / "goodput.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    app = ChatApp(data, chain=CountingChain())
    report = _diagnose_op(data, app.parts.secrets)()
    joined = " ".join(report["findings"])
    assert "ollama" in joined and "jawab nahi aaya" in joined
    assert "koi cloud key" in joined.lower() or \
        "Koi cloud key" in joined
    assert report["providers"]["ollama"]["attempts"] == 4


def test_diagnose_is_advertised_to_the_brain(tmp_path):
    app = ChatApp(tmp_path / "data", chain=CountingChain())
    names = {t["name"]
             for t in app.registry.advertised_tools(app.allowances)}
    assert "diagnose" in names
    assert {"list_files", "read_file", "search_files",
            "write_file"} <= names
