"""The version doctrine, mechanized: the latest route runs
first; on an honest failure the preserved alternate route is
tried automatically and the result SAYS which route served
(served_by). A gate denial or an approval pause is a
decision, not a failure — those never fall back."""

from types import SimpleNamespace

from jarvis.chat import FALLBACK_ROUTES, ChatApp, make_dispatch


class _Journal:
    def __init__(self):
        self.events = []

    def append(self, kind, payload):
        self.events.append((kind, payload))


class _Gate:
    def __init__(self, verdicts=None):
        self.verdicts = verdicts or {}

    def evaluate(self, name, args, origin, allowances):
        return SimpleNamespace(
            verdict=self.verdicts.get(name, "allow"),
            reason="test", approval_token=None)


class _Registry:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def advertised_tools(self, allowances):
        return [{"name": n, "destructive": False}
                for n in self.outcomes]

    def dispatch(self, name, args, decision=None):
        self.calls.append(name)
        return self.outcomes[name]


def _dispatch(outcomes, verdicts=None):
    journal = _Journal()
    return make_dispatch(_Gate(verdicts), _Registry(outcomes),
                         journal, {"net.outbound"}), journal


def test_scrape_falls_back_to_fetch_and_says_so():
    dispatch, journal = _dispatch({
        "web_scrape": {"ok": False, "error": "scrape toota"},
        "web_fetch": {"ok": True, "text": "asal matter"}})
    out = dispatch("web_scrape", {"url": "https://x.test"})
    assert out["served_by"] == "web_fetch"
    assert out["fallback_from"] == "web_scrape"
    assert "scrape toota" in out["primary_error"]
    assert ("tool.fallback", {"from": "web_scrape",
                              "to": "web_fetch",
                              "ok": True}) in journal.events


def test_the_chain_reaches_the_third_route():
    dispatch, _ = _dispatch({
        "web_scrape": {"ok": False, "error": "e1"},
        "web_fetch": {"error": "e2"},
        "web_fetch_tor": {"ok": True, "text": "tor se aaya"}})
    out = dispatch("web_scrape", {"url": "https://x.test"})
    assert out["served_by"] == "web_fetch_tor"


def test_all_routes_fail_reports_both_errors():
    dispatch, _ = _dispatch({
        "web_scrape": {"ok": False, "error": "e1"},
        "web_fetch": {"error": "e2"},
        "web_fetch_tor": {"error": "e3"}})
    out = dispatch("web_scrape", {"url": "https://x.test"})
    assert out["ok"] is False
    assert out["fallback_tried"] == "web_fetch_tor" or \
        out["fallback_error"]


def test_denial_and_approval_never_fall_back():
    dispatch, _ = _dispatch(
        {"web_search": {"ok": False, "error": "x"},
         "search_browser": {"ok": True, "text": "y"}},
        verdicts={"web_search": "deny"})
    out = dispatch("web_search", {"query": "q"})
    assert out.get("served_by") is None
    dispatch2, _ = _dispatch({
        "web_search": {"ok": False, "needs_approval": True,
                       "error": "card bana"},
        "search_browser": {"ok": True, "text": "y"}})
    out2 = dispatch2("web_search", {"query": "q"})
    assert out2.get("needs_approval") is True
    assert out2.get("served_by") is None


def test_legacy_launcher_lives_in_the_catalog(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert "open_app" in names and "open_app_legacy" in names
    assert FALLBACK_ROUTES["open_app"] == "open_app_legacy"
