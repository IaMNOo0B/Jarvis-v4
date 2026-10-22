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


def _message_body(msg) -> str:
    """The plain-text body of a parsed message, capped —
    the multipart walk prefers the text/plain part."""
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            return str(part.get_content())[:4000]
    except Exception:
        pass
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        return str(
                            part.get_content())[:4000]
                    except Exception:
                        continue
            return ""
        return str(msg.get_content())[:4000]
    except Exception:
        return ""


def fetch_recent(address: str, password: str, limit: int = 40,
                 boxes=("INBOX",), imap_factory=None) -> list:
    """Recent messages from the account's own boxes over
    IMAP, parsed to plain dicts: {id, box, from, to,
    reply_to, subject, date_ts, body}. Headers ride along
    verbatim — the follow-up classifier fixes recipients
    from them BEFORE any model sees any text, and never
    from a message body. A box that will not open is
    skipped, never fatal."""
    import imaplib
    from email.parser import BytesParser
    from email.policy import default as _policy
    from email.utils import parsedate_to_datetime
    _, (host, port) = hosts_for(address)
    factory = imap_factory or imaplib.IMAP4_SSL
    try:
        server_cm = factory(host, port, timeout=30)
    except TypeError:
        server_cm = factory(host, port)
    out = []
    with server_cm as server:
        server.login(address, password)
        for box in boxes:
            try:
                status, _ = server.select(box, readonly=True)
            except Exception:
                continue
            if str(status).upper() != "OK":
                continue
            try:
                status, data = server.search(None, "ALL")
            except Exception:
                continue
            if str(status).upper() != "OK" or not data \
                    or not data[0]:
                continue
            ids = data[0].split()[-max(1, int(limit)):]
            for mid in reversed(ids):
                try:
                    status, fetched = server.fetch(
                        mid, "(RFC822)")
                except Exception:
                    continue
                if str(status).upper() != "OK":
                    continue
                raw = None
                for part in fetched or []:
                    if isinstance(part, tuple) \
                            and len(part) > 1 \
                            and isinstance(part[1],
                                           (bytes, bytearray)):
                        raw = bytes(part[1])
                        break
                if raw is None:
                    continue
                try:
                    msg = BytesParser(
                        policy=_policy).parsebytes(raw)
                except Exception:
                    continue
                date_ts = 0.0
                try:
                    dt = parsedate_to_datetime(
                        str(msg.get("Date", "")))
                    if dt is not None:
                        date_ts = dt.timestamp()
                except Exception:
                    date_ts = 0.0
                out.append({
                    "id": str(msg.get("Message-ID")
                              or f"{box}:{mid.decode('ascii', 'ignore')}"),
                    "box": box,
                    "from": str(msg.get("From", "")),
                    "to": str(msg.get("To", "")),
                    "reply_to": str(msg.get("Reply-To", "")),
                    "subject": str(msg.get("Subject", "")),
                    "date_ts": date_ts,
                    "body": _message_body(msg),
                })
    return out


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
