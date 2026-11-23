"""Tests for `SmtpFake` and `ImapFake` (`MailFake` protocol, persistence, flags, invariants)."""

from __future__ import annotations

import json

from shape.boundary.invariants import World
from shape.fakes import ImapFake, MailFake, MailRequest, SmtpFake


def _roundtrip(state: dict) -> dict:
    return json.loads(json.dumps(state))


def test_smtp_records_headers_refusals_and_reply_invariant():
    fake = SmtpFake(reject_recipients={"bounce@example.com"})
    assert isinstance(fake, MailFake)
    assert "smtp.example.com" in fake.hosts
    assert fake.api_version == "RFC 5321 / RFC 5322"

    state: dict = {}
    raw_reply = (
        b"From: agent@acme.test\r\n"
        b"To: alice@example.com\r\n"
        b"Subject: Re: Invoice #7\r\n"
        b"Message-ID: <reply-1@acme.test>\r\n"
        b"In-Reply-To: <orig-7@example.com>\r\n"
        b"References: <root-7@example.com> <orig-7@example.com>\r\n"
        b"\r\n"
        b"Your invoice has been refunded.\r\n"
    )
    refused = fake.send("agent@acme.test", ("alice@example.com",), raw_reply, state)
    assert refused == {}
    state = _roundtrip(state)
    assert len(state["messages"]) == 1
    rec = state["messages"][0]
    assert rec["message_id"] == "<reply-1@acme.test>"
    assert rec["to"] == ["alice@example.com"]
    assert rec["subject"] == "Re: Invoice #7"
    assert rec["in_reply_to"] == "<orig-7@example.com>"
    assert rec["thread"] == "<root-7@example.com>"

    req = MailRequest("SEND", "smtp.example.com", ("agent@acme.test", ("alice@example.com",), raw_reply))
    desc = fake.describe(req)
    eff = fake.effect(req, refused)
    assert desc is not None and desc.op == "smtp.reply" and desc.landed is False
    assert eff is not None and eff.op == "smtp.reply" and eff.landed is True
    assert eff.irreversible is True and eff.key is None
    assert eff.scope == "thread:<root-7@example.com>|in_reply_to:<orig-7@example.com>"

    # Rejected recipient -> refused dict, landed=False, not recorded
    bad_refused = fake.send("agent@acme.test", ("bounce@example.com",), raw_reply, state)
    assert "bounce@example.com" in bad_refused
    assert bad_refused["bounce@example.com"][0] == 550
    assert len(state["messages"]) == 1
    bad_req = MailRequest("SEND", "smtp.example.com", ("agent@acme.test", ("bounce@example.com",), raw_reply))
    assert fake.effect(bad_req, bad_refused).landed is False

    # Invalid sender -> refused, landed=False
    bad_sender = fake.send("not-an-email", ("alice@example.com",), raw_reply, state)
    assert bad_sender["alice@example.com"][0] == 553
    bad_sender_req = MailRequest("SEND", "smtp.example.com", ("not-an-email", ("alice@example.com",), raw_reply))
    assert fake.effect(bad_sender_req, bad_sender).landed is False

    # Default invariants: at most one reply per (thread, In-Reply-To) and at most one fresh send
    invs = fake.invariants()
    assert [inv.name for inv in invs] == ["at_most_once(smtp.reply)", "at_most_once(smtp.send)"]
    w_dup = World(
        entries=[
            {"kind": "effect", "op": "smtp.reply", "scope": eff.scope, "key": None},
            {"kind": "effect", "op": "smtp.reply", "scope": eff.scope, "key": None},
        ],
        completed=True,
        incarnations=1,
        max_restarts=1,
    )
    assert invs[0].check(w_dup) is not None


def test_imap_uids_flags_persistence_and_commands():
    msg1 = b"From: bob@example.com\r\nTo: support@acme.test\r\nSubject: Help\r\n\r\nBody 1"
    msg2 = b"From: carol@example.com\r\nTo: support@acme.test\r\nSubject: Refund\r\n\r\nBody 2"
    fake = ImapFake(initial_messages=[msg1])
    assert isinstance(fake, MailFake)
    assert fake.api_version == "RFC 3501 (IMAP4rev1)"

    state: dict = {}

    # Commands before SELECT fail with NO and landed=False
    no_sel = fake.command("STORE", ("1", "+FLAGS", "(\\Seen)"), state)
    assert no_sel[0] == "NO"
    assert fake.effect(MailRequest("STORE", "imap.example.com", ("1", "+FLAGS", "(\\Seen)")), no_sel).landed is False

    # SELECT INBOX -> 1 message
    typ, data = fake.command("SELECT", ("INBOX",), state)
    assert typ == "OK" and data == [b"1"]
    assert fake.effect(MailRequest("SELECT", "imap.example.com", ("INBOX",)), (typ, data)) is None

    # APPEND adds UID 2
    append_resp = fake.command("APPEND", ("INBOX", "", None, msg2), state)
    assert append_resp[0] == "OK"
    assert b"APPENDUID 1 2" in append_resp[1][0]
    assert fake.effect(MailRequest("APPEND", "imap.example.com", ("INBOX", "", None, msg2)), append_resp).landed is True

    # JSON round-trip across process restart
    state = _roundtrip(state)

    # SEARCH UNSEEN (both imaplib None charset and wire CHARSET prefix) -> both messages unseen
    typ, data = fake.command("SEARCH", (None, "UNSEEN"), state)
    assert typ == "OK" and data == [b"1 2"]
    assert fake.command("SEARCH", ("CHARSET", "UTF-8", "UNSEEN"), state) == ("OK", [b"1 2"])
    assert fake.effect(MailRequest("SEARCH", "imap.example.com", (None, "UNSEEN")), (typ, data)) is None

    # BODY.PEEK does NOT set \Seen
    typ, data = fake.command("UID", ("FETCH", "1", "(BODY.PEEK[])"), state)
    assert typ == "OK" and data[0][1] == msg1
    assert fake.command("SEARCH", (None, "UNSEEN"), state)[1] == [b"1 2"]

    # Non-PEEK RFC822 fetch sets \Seen implicitly
    typ, data = fake.command("UID", ("FETCH", "1", "(RFC822)"), state)
    assert typ == "OK" and data[0][1] == msg1
    state = _roundtrip(state)
    assert fake.command("UID", ("SEARCH", None, "UNSEEN"), state)[1] == [b"2"]
    assert fake.command("UID", ("SEARCH", None, "SEEN"), state)[1] == [b"1"]

    # STORE +FLAGS (\Seen \Deleted) on UID 2 persists through JSON round-trip
    store_args = ("STORE", "2", "+FLAGS", "(\\Seen \\Deleted)")
    store_resp = fake.command("UID", store_args, state)
    assert store_resp[0] == "OK"
    assert fake.effect(MailRequest("UID", "imap.example.com", store_args), store_resp).landed is True
    state = _roundtrip(state)
    assert fake.command("UID", ("SEARCH", None, "UNSEEN"), state)[1] == [b""]

    # EXPUNGE permanently removes UID 2 (marked \Deleted), leaving UID 1 intact
    exp_resp = fake.command("EXPUNGE", (), state)
    assert exp_resp[0] == "OK" and exp_resp[1] == [b"2"]
    exp_eff = fake.effect(MailRequest("EXPUNGE", "imap.example.com", ()), exp_resp)
    assert exp_eff is not None and exp_eff.irreversible is True and exp_eff.landed is True
    state = _roundtrip(state)
    assert fake.command("UID", ("SEARCH", None, "ALL"), state)[1] == [b"1"]

    # APPEND to nonexistent mailbox -> NO, landed=False
    bad_app = fake.command("APPEND", ("Missing", "", None, msg2), state)
    assert bad_app[0] == "NO"
    assert fake.effect(MailRequest("APPEND", "imap.example.com", ("Missing", "", None, msg2)), bad_app).landed is False
