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


def send_email(address: str, password: str, to: str,
               subject: str, body: str,
               smtp_factory=None) -> dict:
    """One real send over SMTP-SSL. Raises on failure — callers
    report honestly, never swallow."""
    (host, port), _ = hosts_for(address)
    factory = smtp_factory or smtplib.SMTP_SSL
    msg = _message(address, to, subject, body)
    with factory(host, port, timeout=30) as server:
        server.login(address, password)
        server.send_message(msg)
    return {"ok": True, "from": address, "to": to,
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
    with factory(host, port) as server:
        server.login(address, password)
        folder = "[Gmail]/Drafts" if "gmail" in host else "Drafts"
        server.append(folder, "\\Draft",
                      imaplib.Time2Internaldate(time.time()), raw)
    return {"ok": True, "from": address, "to": to,
            "draft_in": folder}
