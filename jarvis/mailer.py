"""Mailer — JARVIS v4: real email from the owner's own accounts.

Sending uses the standard roads, no tricks: SMTP to send, IMAP
APPEND to file a draft in the account's own Drafts folder. Each
account authenticates with its address + an app password held in
the vault (the owner files it in Settings; it never appears
here, in logs, or in this file). Gmail is the first-class road
(smtp.gmail.com:465 SSL / imap.gmail.com:993 SSL); a custom host
can ride in the account's settings when the owner adds one.

Factories are injectable so the tests drive fakes — the real
network objects are built only on the send path.
"""

from __future__ import annotations

import email.message
import smtplib
import time

GMAIL_SMTP = ("smtp.gmail.com", 465)
GMAIL_IMAP = ("imap.gmail.com", 993)


def hosts_for(address: str) -> tuple:
    """((smtp_host, smtp_port), (imap_host, imap_port)) by the
    address's domain; Gmail family gets the known roads, others
    get the conventional imap./smtp. hosts for their domain."""
    domain = str(address).rsplit("@", 1)[-1].lower()
    if domain in ("gmail.com", "googlemail.com"):
        return GMAIL_SMTP, GMAIL_IMAP
    return (f"smtp.{domain}", 465), (f"imap.{domain}", 993)


def _message(sender: str, to: str, subject: str,
             body: str) -> email.message.EmailMessage:
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def _recipients(to: str) -> list:
    parts = str(to).replace(";", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


def send_email(address: str, password: str, to: str,
               subject: str, body: str,
               smtp_factory=None) -> dict:
    """One real send over SMTP-SSL. Raises on failure — callers
    report honestly, never swallow. send_message's refusal
    dict is SURFACED (audit 2026-10-22: it was discarded, so
    a refused recipient still reported a clean send): partial
    refusals ride in the result; a total refusal raises. The
    result names every recipient so the caller can count EACH
    one against the account's daily cap."""
    (host, port), _ = hosts_for(address)
    factory = smtp_factory or smtplib.SMTP_SSL
    msg = _message(address, to, subject, body)
    recipients = _recipients(to)
    with factory(host, port, timeout=30) as server:
        server.login(address, password)
        refused = server.send_message(msg) or {}
    refused = {str(k) for k in refused}
    if recipients and refused >= set(recipients):
        raise RuntimeError(
            "saare recipients refuse ho gaye: "
            + ", ".join(sorted(refused)))
    return {"ok": True, "from": address, "to": to,
            "recipients": recipients,
            "refused": sorted(refused),
            "via": f"{host}:{port}"}


def draft_email(address: str, password: str, to: str,
                subject: str, body: str,
                imap_factory=None) -> dict:
    """File the message in the account's own Drafts folder via
    IMAP APPEND — a draft the owner can open and send himself."""
    import imaplib
    _, (host, port) = hosts_for(address)
    factory = imap_factory or imaplib.IMAP4_SSL
    msg = _message(address, to, subject, body)
    raw = msg.as_bytes()
    try:
        server_cm = factory(host, port, timeout=30)
    except TypeError:
        server_cm = factory(host, port)
    with server_cm as server:
        server.login(address, password)
        folder = "[Gmail]/Drafts" if "gmail" in host else "Drafts"
        # The APPEND result is the only proof the draft
        # landed — a "NO" used to sail through as ok
        # (audit 2026-10-22).
        status, detail = server.append(
            folder, "\\Draft",
            imaplib.Time2Internaldate(time.time()), raw)
        if str(status).upper() != "OK":
            raise RuntimeError(
                f"draft APPEND mana hua ({status}): {detail!r}")
    return {"ok": True, "from": address, "to": to,
            "draft_in": folder}
