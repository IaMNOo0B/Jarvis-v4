"""Reader: one tool-less extraction call, every value validated
in code against the document. Invented emails/urls/numbers die;
wrong-kind values die; extra fields are dropped."""

import json

from jarvis.chat import ChatApp
from jarvis.reader import read_fields

DOC = ("Workshop on Saturday. Fee is 500 rupees. "
       "Contact: aisha@example.com. Register at "
       "https://talks.example/register before 2026-11-05. "
       "Seats are limited, booking is open.")


def _complete_returning(payload):
    def complete(messages, tools):
        assert tools == []          # the Reader call carries NO tools
        return {"content": json.dumps(payload)}
    return complete


FIELDS = [{"name": "fee", "kind": "number"},
          {"name": "contact", "kind": "email"},
          {"name": "register", "kind": "url"},
          {"name": "deadline", "kind": "date"},
          {"name": "open", "kind": "yes_no"},
          {"name": "summary", "kind": "text"}]


def test_reader_extracts_and_validates():
    out = read_fields(_complete_returning({
        "fee": 500, "contact": "aisha@example.com",
        "register": "https://talks.example/register",
        "deadline": "2026-11-05", "open": True,
        "summary": "A Saturday workshop.", "extra": "dropped"}),
        DOC, FIELDS)
    assert out["ok"] is True
    assert out["fields"] == {
        "fee": 500.0, "contact": "aisha@example.com",
        "register": "https://talks.example/register",
        "deadline": "2026-11-05", "open": True,
        "summary": "A Saturday workshop."}
    assert out["read_untrusted_content"] is True   # text/email/url


def test_reader_kills_invented_values():
    out = read_fields(_complete_returning({
        "fee": 999,                                  # not in the doc
        "contact": "mallory@evil.example",           # not in the doc
        "register": "https://evil.example/phish",    # not in the doc
        "deadline": "2026-02-30",                    # not a real date
        "open": "maybe",                             # not a boolean
        "summary": None}), DOC, FIELDS)
    assert out["fields"] == {"fee": None, "contact": None,
                             "register": None, "deadline": None,
                             "open": None, "summary": None}
    assert out["read_untrusted_content"] is False


def test_numbers_only_result_carries_no_instruction_risk():
    out = read_fields(_complete_returning({"fee": 500, "open": False}),
                      DOC, [{"name": "fee", "kind": "number"},
                            {"name": "open", "kind": "yes_no"}])
    assert out["fields"] == {"fee": 500.0, "open": False}
    assert out["read_untrusted_content"] is False


def test_app_advertises_read_fields(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert "read_fields" in names
