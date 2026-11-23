import imaplib
import smtplib
from email.message import EmailMessage

import pytest

import shape
from shape.boundary import runner
from shape.boundary.faults import FAULTS
from shape.fakes import ImapFake, SmtpFake, StripeFake


def test_stripe_reuses_charge_after_requests_loses_reply(tmp_path):
    requests = pytest.importorskip("requests")
    httpx = pytest.importorskip("httpx")
    url = "https://api.stripe.com/v1/charges"
    data = {"amount": 500, "currency": "usd", "source": "tok_visa", "metadata[order_id]": "42"}
    headers = {"Idempotency-Key": "order-42-charge"}

    @shape.scenario(net=[StripeFake()], max_restarts=0)
    def checkout(run):
        with pytest.raises(requests.ReadTimeout):
            requests.post(url, data=data, headers=headers)
        charges = httpx.get(url).raise_for_status().json()["data"]
        assert len(charges) == 1
        retry = httpx.post(url, data=data, headers=headers).raise_for_status()
        assert retry.headers["Idempotent-Replayed"] == "true"
        assert retry.json() == charges[0]

    choices = {"fault:stripe.charge#0.0": FAULTS.index("timeout_after_commit") + 1}
    out = runner.execute(checkout, {"explicit": choices}, tmp_path / "stripe")
    assert out.ok, (out.dir / "inc-0.out").read_text()
    assert out.stats["effects"] == {"stripe.charge": 1}
    assert out.stats["attempts"] == {"stripe.charge": 2}
    assert out.stats["faults"] == {"stripe.charge": {"timeout_after_commit": 1}}


def test_mail_fakes_capture_duplicate_reply_and_persist_seen_after_disconnect(tmp_path):
    original = b"From: customer@example.com\r\nMessage-ID: <ticket@example.com>\r\n\r\nHelp"
    reply = EmailMessage()
    reply["From"] = "support@example.com"
    reply["To"] = "customer@example.com"
    reply["Subject"] = "Re: Ticket"
    reply["In-Reply-To"] = "<ticket@example.com>"
    reply.set_content("Thanks for writing.")

    @shape.scenario(net=[SmtpFake(), ImapFake(initial_messages=[original])], max_restarts=0)
    def reply_to_unread(run):
        with imaplib.IMAP4_SSL("imap.example.com") as mailbox:
            mailbox.login("user", "password")
            assert mailbox.select("INBOX") == ("OK", [b"1"])
            assert mailbox.uid("search", None, "UNSEEN") == ("OK", [b"1"])
            with (smtplib.SMTP_SSL("smtp.example.com") as smtp,
                  pytest.raises(smtplib.SMTPServerDisconnected)):
                smtp.send_message(reply)
            with smtplib.SMTP_SSL("smtp.example.com") as smtp:
                assert smtp.send_message(reply) == {}
            with pytest.raises(imaplib.IMAP4.abort):
                mailbox.uid("store", "1", "+FLAGS.SILENT", r"(\Seen)")
        with imaplib.IMAP4_SSL("imap.example.com") as mailbox:
            mailbox.login("user", "password")
            assert mailbox.select("INBOX") == ("OK", [b"1"])
            assert mailbox.uid("search", None, "UNSEEN") == ("OK", [b""])

    reset = FAULTS.index("connection_reset_after_commit") + 1
    choices = {"fault:smtp.reply#0.0": reset, "fault:imap.store#0.0": reset}
    out = runner.execute(reply_to_unread, {"explicit": choices}, tmp_path / "mail")
    assert out.completed, (out.dir / "inc-0.out").read_text()
    assert [v.invariant for v in out.violations] == ["at_most_once(smtp.reply)"]
    assert out.stats["effects"] == {"smtp.reply": 2, "imap.store": 1}
    assert out.stats["faults"] == {
        "smtp.reply": {"connection_reset_after_commit": 1},
        "imap.store": {"connection_reset_after_commit": 1},
    }


def test_shared_alias_fails_if_a_client_uses_it(tmp_path):
    @shape.scenario(net=[SmtpFake(), ImapFake()], max_restarts=0)
    def scenario(run):
        smtplib.SMTP("localhost")

    out = runner.execute(scenario, {"explicit": {}}, tmp_path / "mail")
    assert out.violated("harness_error")
    assert "multiple send fakes registered for host 'localhost'" in out.violations[0].message
