"""Tests for `SlackFake`, `GitHubFake`, `SendGridFake`, and `TwilioFake`."""

from __future__ import annotations

import json
from typing import Any

from shape.boundary.invariants import World
from shape.fakes import GitHubFake, HttpFake, Request, SendGridFake, SlackFake, TwilioFake


def _roundtrip(state: dict) -> dict:
    return json.loads(json.dumps(state))


def _json_req(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Request:
    hdrs = dict(headers or {})
    body = b""
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    return Request(method=method, url=url, headers=hdrs, body=body, json=payload)


def test_slack_post_update_history_and_ok_false_200():
    fake = SlackFake(channels={"C1"}, valid_tokens={"xoxb-ok"})
    assert isinstance(fake, HttpFake)
    assert "slack.com" in fake.hosts
    assert fake.api_version == "Slack Web API (2024)"
    state: dict = {}

    # Bad token -> HTTP 200 with ok=False, landed=False
    bad_auth = _json_req("POST", "https://slack.com/api/chat.postMessage", {"channel": "C1", "text": "hi"})
    r_auth = fake.handle(bad_auth, state)
    assert r_auth.status == 200
    assert json.loads(r_auth.body) == {"ok": False, "error": "invalid_auth"}
    assert fake.effect(bad_auth, r_auth).landed is False

    hdrs = {"Authorization": "Bearer xoxb-ok"}
    # Unknown channel -> HTTP 200 with ok=False, landed=False
    bad_ch = _json_req("POST", "https://slack.com/api/chat.postMessage", {"channel": "C99", "text": "hi"}, hdrs)
    r_ch = fake.handle(bad_ch, state)
    assert r_ch.status == 200 and json.loads(r_ch.body)["error"] == "channel_not_found"
    assert fake.effect(bad_ch, r_ch).landed is False

    # Missing text -> HTTP 200 with ok=False, landed=False
    no_txt = _json_req("POST", "https://slack.com/api/chat.postMessage", {"channel": "C1"}, hdrs)
    r_txt = fake.handle(no_txt, state)
    assert r_txt.status == 200 and json.loads(r_txt.body)["error"] == "no_text"
    assert fake.effect(no_txt, r_txt).landed is False

    # Valid post -> HTTP 200 ok=True, landed=True; posting twice creates 2 distinct messages
    post = _json_req("POST", "https://slack.com/api/chat.postMessage", {"channel": "C1", "text": "alert 1"}, hdrs)
    r1 = fake.handle(post, state)
    assert r1.status == 200 and json.loads(r1.body)["ok"] is True
    ts1 = json.loads(r1.body)["ts"]
    eff1 = fake.effect(post, r1)
    assert eff1 is not None and eff1.op == "slack.chat.postMessage"
    assert eff1.scope == "channel:C1" and eff1.landed is True and eff1.irreversible is True

    state = _roundtrip(state)
    r2 = fake.handle(post, state)
    ts2 = json.loads(r2.body)["ts"]
    assert ts2 != ts1

    # chat.update modifies existing message, fails with message_not_found for unknown ts
    upd = _json_req("POST", "https://slack.com/api/chat.update", {"channel": "C1", "ts": ts1, "text": "updated"}, hdrs)
    r_upd = fake.handle(upd, state)
    assert r_upd.status == 200 and json.loads(r_upd.body)["ok"] is True
    assert fake.effect(upd, r_upd).landed is True

    bad_upd = _json_req("POST", "https://slack.com/api/chat.update", {"channel": "C1", "ts": "0.0", "text": "x"}, hdrs)
    r_bupd = fake.handle(bad_upd, state)
    assert r_bupd.status == 200 and json.loads(r_bupd.body)["error"] == "message_not_found"
    assert fake.effect(bad_upd, r_bupd).landed is False

    # conversations.history returns newest-first and is read-only (effect is None)
    state = _roundtrip(state)
    hist = _json_req("GET", "https://slack.com/api/conversations.history?channel=C1&limit=1", headers=hdrs)
    r_hist = fake.handle(hist, state)
    body = json.loads(r_hist.body)
    assert body["ok"] is True and body["has_more"] is True and body["messages"][0]["ts"] == ts2
    assert fake.effect(hist, r_hist) is None

    # Default invariant catches duplicate posts in same scope
    invs = fake.invariants()
    assert invs[0].name == "at_most_once(slack.chat.postMessage)"
    w = World(
        entries=[
            {"kind": "effect", "op": "slack.chat.postMessage", "scope": "channel:C1", "key": None},
            {"kind": "effect", "op": "slack.chat.postMessage", "scope": "channel:C1", "key": None},
        ],
        completed=True, incarnations=1, max_restarts=1,
    )
    assert invs[0].check(w) is not None


def test_github_issues_create_comment_close_and_non_idempotency():
    fake = GitHubFake(repos={"acme/app"})
    assert isinstance(fake, HttpFake)
    assert fake.hosts == ("api.github.com",)
    assert fake.api_version == "2022-11-28"
    state: dict = {}

    # Missing title -> 422 Validation Failed, landed=False
    bad = _json_req("POST", "https://api.github.com/repos/acme/app/issues", {"body": "no title"})
    r_bad = fake.handle(bad, state)
    assert r_bad.status == 422
    assert json.loads(r_bad.body)["errors"][0]["field"] == "title"
    assert fake.effect(bad, r_bad).landed is False

    # Unknown repo -> 404 Not Found, landed=False
    unk = _json_req("POST", "https://api.github.com/repos/other/repo/issues", {"title": "Bug"})
    r_unk = fake.handle(unk, state)
    assert r_unk.status == 404
    assert fake.effect(unk, r_unk).landed is False

    # Issue creation is NOT idempotent: two identical POSTs create #1 and #2
    create = _json_req("POST", "https://api.github.com/repos/acme/app/issues", {"title": "Outage", "body": "500s"})
    r1 = fake.handle(create, state)
    assert r1.status == 201 and r1.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert json.loads(r1.body)["number"] == 1
    eff1 = fake.effect(create, r1)
    assert eff1 is not None and eff1.op == "github.issue.create"
    assert eff1.scope == "acme/app:title:Outage" and eff1.landed is True

    state = _roundtrip(state)
    r2 = fake.handle(create, state)
    assert r2.status == 201 and json.loads(r2.body)["number"] == 2

    # Comment + list comments
    c_req = _json_req("POST", "https://api.github.com/repos/acme/app/issues/1/comments", {"body": "Investigating"})
    rc = fake.handle(c_req, state)
    assert rc.status == 201
    assert fake.effect(c_req, rc).op == "github.issue.comment"
    assert fake.effect(c_req, rc).landed is True

    # Empty comment body -> 422, landed=False
    bad_c = _json_req("POST", "https://api.github.com/repos/acme/app/issues/1/comments", {"body": ""})
    assert fake.handle(bad_c, state).status == 422
    assert fake.effect(bad_c, fake.handle(bad_c, state)).landed is False

    # Close issue #1 via PATCH
    close_req = _json_req("PATCH", "https://api.github.com/repos/acme/app/issues/1", {"state": "closed"})
    r_close = fake.handle(close_req, state)
    assert r_close.status == 200 and json.loads(r_close.body)["state"] == "closed"
    close_eff = fake.effect(close_req, r_close)
    assert close_eff is not None and close_eff.op == "github.issue.close" and close_eff.landed is True

    # GET open issues returns only #2; GET is read-only
    state = _roundtrip(state)
    list_req = _json_req("GET", "https://api.github.com/repos/acme/app/issues?state=open")
    r_list = fake.handle(list_req, state)
    assert r_list.status == 200 and [i["number"] for i in json.loads(r_list.body)] == [2]
    assert fake.effect(list_req, r_list) is None

    # Default invariant catches duplicate issue creation for same title
    inv = fake.invariants()[0]
    assert inv.name == "at_most_once(github.issue.create)"
    w = World(
        entries=[
            {"kind": "effect", "op": "github.issue.create", "scope": eff1.scope, "key": None},
            {"kind": "effect", "op": "github.issue.create", "scope": eff1.scope, "key": None},
        ],
        completed=True, incarnations=1, max_restarts=1,
    )
    assert inv.check(w) is not None


def test_sendgrid_202_validation_and_invariant():
    fake = SendGridFake(valid_api_keys={"SG.secret"})
    assert isinstance(fake, HttpFake)
    assert fake.hosts == ("api.sendgrid.com",)
    assert fake.api_version == "v3"
    state: dict = {}

    payload = {
        "personalizations": [{"to": [{"email": "user@example.com"}]}],
        "from": {"email": "noreply@acme.test"},
        "subject": "Order shipped",
        "content": [{"type": "text/plain", "value": "On the way"}],
        "custom_args": {"order_id": "99"},
    }
    # Missing auth -> 401, landed=False
    unauth = _json_req("POST", "https://api.sendgrid.com/v3/mail/send", payload)
    r_401 = fake.handle(unauth, state)
    assert r_401.status == 401 and fake.effect(unauth, r_401).landed is False

    hdrs = {"Authorization": "Bearer SG.secret"}
    # Missing content (and no template_id) -> 400, landed=False
    bad = _json_req("POST", "https://api.sendgrid.com/v3/mail/send", {**payload, "content": []}, hdrs)
    r_400 = fake.handle(bad, state)
    assert r_400.status == 400 and json.loads(r_400.body)["errors"][0]["field"] == "content"
    assert fake.effect(bad, r_400).landed is False

    # Accepted send -> 202 Accepted, empty body, X-Message-Id
    req = _json_req("POST", "https://api.sendgrid.com/v3/mail/send", payload, hdrs)
    r1 = fake.handle(req, state)
    assert r1.status == 202 and r1.body == b"" and r1.headers["X-Message-Id"] == "sg_msg_1"
    eff = fake.effect(req, r1)
    assert eff is not None and eff.op == "sendgrid.mail.send"
    assert eff.scope == "order:99" and eff.irreversible is True and eff.landed is True

    state = _roundtrip(state)
    r2 = fake.handle(req, state)
    assert r2.status == 202 and r2.headers["X-Message-Id"] == "sg_msg_2"
    assert len(state["messages"]) == 2
    assert fake.invariants()[0].name == "at_most_once(sendgrid.mail.send)"


def test_twilio_sms_sids_errors_and_invariant():
    fake = TwilioFake(invalid_numbers={"+15550000000"})
    assert isinstance(fake, HttpFake)
    assert fake.hosts == ("api.twilio.com",)
    assert fake.api_version == "2010-04-01"
    state: dict = {}
    url = "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"

    # Form-encoded POST -> 201 Created with 34-char SM... SID
    req = Request(
        method="POST",
        url=url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=b"To=%2B15551234567&From=%2B15559876543&Body=Code+1234",
        json=None,
    )
    r1 = fake.handle(req, state)
    assert r1.status == 201
    msg1 = json.loads(r1.body)
    assert msg1["sid"].startswith("SM") and len(msg1["sid"]) == 34
    assert msg1["status"] == "queued" and msg1["to"] == "+15551234567"
    eff = fake.effect(req, r1)
    assert eff is not None and eff.op == "twilio.message.create"
    assert eff.scope == "to:+15551234567" and eff.irreversible is True and eff.landed is True

    state = _roundtrip(state)
    # Non-idempotent retry creates second SID
    r2 = fake.handle(req, state)
    assert r2.status == 201 and json.loads(r2.body)["sid"] != msg1["sid"]

    # GET message and list messages (reads -> effect is None)
    get_req = _json_req("GET", f"https://api.twilio.com/2010-04-01/Accounts/AC123/Messages/{msg1['sid']}.json")
    r_get = fake.handle(get_req, state)
    assert r_get.status == 200 and json.loads(r_get.body)["body"] == "Code 1234"
    assert fake.effect(get_req, r_get) is None

    # Validation errors -> 400 with Twilio code, landed=False
    bad_num = _json_req("POST", url, {"To": "+15550000000", "From": "+15559876543", "Body": "hi"})
    r_bad = fake.handle(bad_num, state)
    assert r_bad.status == 400 and json.loads(r_bad.body)["code"] == 21211
    assert fake.effect(bad_num, r_bad).landed is False

    no_body = _json_req("POST", url, {"To": "+15551234567", "From": "+15559876543"})
    r_nb = fake.handle(no_body, state)
    assert r_nb.status == 400 and json.loads(r_nb.body)["code"] == 21602
    assert fake.effect(no_body, r_nb).landed is False

    assert fake.invariants()[0].name == "at_most_once(twilio.message.create)"
