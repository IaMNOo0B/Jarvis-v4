"""The many-accounts discipline: rotation spreads the load,
caps rest an account until tomorrow, a hard rest benches it —
and email sends for real (faked transport) from the picked
account, counted by the book."""

from jarvis import mailer
from jarvis.accounts import AccountBook
from jarvis.chat import ChatApp


def test_rotation_caps_and_rest(tmp_path):
    book = AccountBook(tmp_path)
    a = book.add("email", "pehla", "a@gmail.com", cap=2)
    b = book.add("email", "doosra", "b@gmail.com", cap=5)
    book.record_send(a["id"])
    book.record_send(a["id"])                # cap hit
    picked = book.pick("email")
    assert picked["id"] == b["id"]           # a rests till tomorrow
    book.rest(b["id"], 3600, "rate limit")
    assert book.pick("email") is None        # whole stable benched
    overview = {r["id"]: r for r in book.overview()}
    assert overview[a["id"]]["eligible"] is False
    assert overview[b["id"]]["rest_reason"] == "rate limit"


class FakeSMTP:
    sent = []
    def __init__(self, host, port, timeout=0):
        self.host = host
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass
    def login(self, user, password):
        self.user = user
    def send_message(self, msg):
        FakeSMTP.sent.append((self.user, msg["To"]))


def test_email_send_rotates_and_counts(tmp_path, monkeypatch):
    FakeSMTP.sent = []
    real_send = mailer.send_email
    monkeypatch.setattr(mailer, "send_email",
                        lambda *a, **k: real_send(
                            *a, smtp_factory=FakeSMTP))
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert {"email_send", "email_draft",
            "accounts_status"} <= names
    out = app.execute_tool("email_send", {
        "to": "x@y.com", "subject": "s", "body": "b"})
    assert out["ok"] is False and "juda hi nahi" in out["error"]
    app.add_account("email", "mera", "me@gmail.com",
                    secret="app-password", cap=3)
    out = app.execute_tool("email_send", {
        "to": "lead@pod.com", "subject": "Namaste",
        "body": "portfolio link"})
    assert out["ok"] is True and out["account"] == "mera"
    assert FakeSMTP.sent == [("me@gmail.com", "lead@pod.com")]
    status = app.execute_tool("accounts_status", {})
    assert status["accounts"][0]["sent_today"] == 1
    out = app.execute_tool("email_send", {
        "to": "x@y.com", "subject": "s", "body": "b",
        "account": "nahi-hai"})
    assert out["ok"] is False and "mila nahi" in out["error"]


def test_mailer_builds_a_real_message():
    captured = {}
    class Fake(FakeSMTP):
        def send_message(self, msg):
            captured["subject"] = msg["Subject"]
            captured["body"] = msg.get_content()
    out = mailer.send_email("me@gmail.com", "pw", "t@x.com",
                            "Hello", "body here",
                            smtp_factory=Fake)
    assert out["ok"] is True and captured["subject"] == "Hello"
    assert "body here" in captured["body"]
