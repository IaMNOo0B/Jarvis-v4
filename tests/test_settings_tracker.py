"""The Settings plate: provider keys set from the app (preset and
custom), the custom provider reaching the chain after a restart,
Wikipedia/GitHub tools, and the tracker a long campaign keeps."""

import jarvis.builtin_skills as bs
from jarvis.chat import ChatApp


def test_custom_provider_reaches_the_chain_after_restart(tmp_path):
    app = ChatApp(tmp_path)
    out = app.set_provider("myprov", "sk-test-1234567890",
                           base_url="https://api.example.com/v1",
                           model="cool-model")
    assert out["ok"] is True and out["kind"] == "custom"
    settings = app.provider_settings()
    assert settings["custom"][0]["has_key"] is True
    assert "cool-model" in settings["custom"][0]["model"]
    app2 = ChatApp(tmp_path)               # the restart
    names = [getattr(p, "name", "")
             for p in app2.parts.chain.inner.providers]
    assert names[0] == "myprov"


def test_preset_key_files_like_slash_key(tmp_path):
    app = ChatApp(tmp_path)
    out = app.set_provider("gemini",
                           "AIzaSyDUMMYKEYFORTESTING1234567890")
    assert out["ok"] is True and out["kind"] == "preset"
    presets = {p["name"]: p["keys"]
               for p in app.provider_settings()["presets"]}
    assert presets["gemini"] == 1
    bad = app.set_provider("gemini", "")
    assert bad["ok"] is False
    bad2 = app.set_provider("newprov", "sk-x")
    assert bad2["ok"] is False             # needs base URL + model


def test_wikipedia_and_github_tools(monkeypatch):
    def fake_json(url, ua, timeout=10):
        if "list=search" in url:
            return {"query": {"search": [{"title": "Quokka"},
                                         {"title": "Daisy"}]}}
        if "prop=extracts" in url:
            return {"query": {"pages": {"1": {
                "extract": "A quokka is a small marsupial."}}}}
        if "search/repositories" in url:
            return {"items": [{"full_name": "a/b",
                               "stargazers_count": 42,
                               "description": "d",
                               "html_url": "https://github.com/a/b"}]}
        return {"full_name": "a/b", "stargazers_count": 42,
                "language": "Python", "description": "d",
                "html_url": "https://github.com/a/b"}
    monkeypatch.setattr(bs, "_json_get", fake_json)
    wiki = bs.op_wikipedia("quokka")
    assert wiki["ok"] and wiki["title"] == "Quokka"
    assert "marsupial" in wiki["extract"]
    gh = bs.op_github_search("jarvis")
    assert gh["repos"][0]["stars"] == 42


def test_tracker_keeps_a_campaign(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert {"track_add", "track_list", "track_update",
            "wikipedia", "github_search", "github_repo"} <= names
    out = app.execute_tool("track_add", {
        "table": "leads",
        "record": {"name": "Aisha", "show": "Tech Talks"}})
    assert out["ok"] is True
    row_id = out["row"]["id"]
    listed = app.execute_tool("track_list", {"table": "leads"})
    assert listed["rows"][0]["name"] == "Aisha"
    upd = app.execute_tool("track_update", {
        "table": "leads", "row_id": row_id, "status": "replied"})
    assert upd["row"]["status"] == "replied"
    open_rows = app.execute_tool(
        "track_list", {"table": "leads", "status": "open"})
    assert open_rows["rows"] == []
