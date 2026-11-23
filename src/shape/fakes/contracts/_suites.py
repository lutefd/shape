"""Provider-specific scripts and the configuration for their test services."""

import base64
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from functools import partial
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID

from .. import (
    DiscordFake,
    GitHubFake,
    GmailFake,
    GoogleCalendarFake,
    JiraFake,
    LangGraphFake,
    LinearFake,
    SendGridFake,
    SlackFake,
    StripeFake,
    TelegramFake,
    TwilioFake,
)
from ..base import HttpFake, Request
from . import ContractFailure, Probe, _live_send


@dataclass(frozen=True)
class Suite:
    fake: Callable[[], HttpFake]
    settings: dict[str, str]
    connect: Callable[[Mapping[str, str], bool], tuple[str, dict[str, str]]]
    script: Callable[[Probe, Mapping[str, str], bool], None]


def _bearer(url, var, headers=None, *, scheme="Bearer"):
    def connect(env, live):
        token = env[var]
        return url, {**(headers or {}), "Authorization": f"{scheme} {token}" if scheme else token}

    return connect


def _basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _stripe_connect(env, live):
    if live and not env["STRIPE_TEST_KEY"].startswith(("sk_test_", "rk_test_")):
        raise ContractFailure("STRIPE_TEST_KEY must be a Stripe test-mode key")
    return _bearer("https://api.stripe.com", "STRIPE_TEST_KEY",
                   {"Stripe-Version": "2024-06-20"})(env, live)


def _stripe(p, env, live):
    path = "/v1/charges"
    form = {"amount": 500, "currency": "usd", "source": "tok_visa",
            "metadata[shape_contract]": p.marker}
    fields = ("id", "object", "amount", "currency", "paid", "metadata.shape_contract")
    first = p.call("commit (reply dropped for retry)", "POST", path, 200,
                   form=form, key=p.marker, fields=fields)
    retry = p.call("retry same key", "POST", path, 200, form=form, key=p.marker, fields=fields)
    ident = p.require(retry, "id")
    p.check("same resource after retry", p.require(first, "id") == ident)
    p.call("changed parameters", "POST", path, 400, form={**form, "amount": 501},
           key=p.marker, code_path="error.type", code="idempotency_error",
           fields=("error.message",))
    read = p.call("read committed charge", "GET", f"{path}/{ident}", 200, fields=fields)
    p.check("original amount persisted", p.require(read, "amount") == 500)
    p.call("card decline", "POST", path, 402, form={**form, "source": "tok_chargeDeclined"},
           code_path="error.code", code="card_declined", fields=("error.type", "error.message"))


def _github(p, env, live):
    repo = env["GITHUB_SCRATCH_REPO"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ContractFailure("GITHUB_SCRATCH_REPO must be owner/repo")
    path = f"/repos/{repo}/issues"
    title = f"Shape provider contract {p.marker}"
    fields = ("id", "number", "title", "state", "url", "html_url")
    issues = []
    try:
        for step, suffix in (("commit", ""), ("retry same key", ""), ("changed parameters", " edit")):
            value = p.call(step, "POST", path, 201, body={"title": title + suffix},
                           key=p.marker, fields=fields)
            issues.append(p.require(value, "number"))
        p.check("three distinct issues", len(set(issues)) == 3)
        read = p.call("read committed issue", "GET", f"{path}/{issues[0]}", 200, fields=fields)
        p.check("original title persisted", p.require(read, "title") == title)
        p.call("missing issue", "GET", f"{path}/999999999", 404, fields=("message",))
    finally:
        if live:
            for number in issues:
                p.cleanup("PATCH", f"{path}/{number}", body={"state": "closed"})


def _slack(p, env, live):
    channel = env["SLACK_TEST_CHANNEL"]
    path = "/api/chat.postMessage"
    fields = ("ok", "channel", "ts", "message.text")
    messages = []
    try:
        for step, suffix in (("commit", ""), ("retry same key", ""), ("changed parameters", " edit")):
            if live and messages:
                time.sleep(1.1)
            msg = p.call(step, "POST", path, 200, key=p.marker, fields=fields,
                         body={"channel": channel, "text": p.marker + suffix})
            messages.append(p.require(msg, "ts"))
        p.check("three distinct messages", len(set(messages)) == 3)
        history = p.call("read channel history", "GET", "/api/conversations.history", 200,
                         query={"channel": channel, "limit": 10},
                         fields=("ok", "messages.0.ts", "has_more"))
        p.check("posted messages visible", set(messages) <= {
            item["ts"] for item in p.require(history, "messages")
        })
        p.call("missing text", "POST", path, 200, body={"channel": channel},
               code_path="error", code="no_text", fields=("ok",))
    finally:
        if live:
            for ts in messages:
                p.cleanup("POST", "/api/chat.delete", body={"channel": channel, "ts": ts})


def _telegram(p, env, live):
    token = env["TELEGRAM_BOT_TOKEN"]
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise ContractFailure("TELEGRAM_BOT_TOKEN must have bot-id:token form")
    chat_id = env["TELEGRAM_TEST_CHAT_ID"]
    base = f"/bot{token}"
    fields = ("ok", "result.message_id", "result.chat.id", "result.text", "result.date")
    ids = []
    try:
        for step, suffix in (("commit", ""), ("retry same key", ""), ("changed parameters", " edit")):
            msg = p.call(step, "POST", f"{base}/sendMessage", 200, key=p.marker, fields=fields,
                         body={"chat_id": chat_id, "text": p.marker + suffix})
            ids.append(p.require(msg, "result.message_id"))
        p.check("three distinct messages", len(set(ids)) == 3)
        body = {"chat_id": chat_id, "message_id": ids[0], "text": p.marker}
        p.call("read via unchanged edit", "POST", f"{base}/editMessageText", 400,
               body=body, code_path="error_code", code=400, fields=("ok", "description"))
        changed = p.call("edit committed message", "POST", f"{base}/editMessageText", 200,
                         body={**body, "text": p.marker + " updated"}, fields=fields)
        p.check("edit retained original message ID", p.require(changed, "result.message_id") == ids[0])
        p.call("chat action", "POST", f"{base}/sendChatAction", 200,
               body={"chat_id": chat_id, "action": "typing"}, fields=("ok", "result"))
    finally:
        if live:
            for ident in ids:
                p.cleanup("POST", f"{base}/deleteMessage",
                          body={"chat_id": chat_id, "message_id": ident})


def _discord(p, env, live):
    channel = env["DISCORD_TEST_CHANNEL"]
    if not channel.isdigit():
        raise ContractFailure("DISCORD_TEST_CHANNEL must be a channel ID")
    path = f"/api/v10/channels/{channel}/messages"
    fields = ("id", "channel_id", "content", "author.id")
    body = {"content": p.marker, "nonce": p.marker[:25], "enforce_nonce": True}
    ids = set()
    try:
        for step, suffix in (("commit", ""), ("retry same nonce", ""), ("changed parameters", " edit")):
            value = p.call(step, "POST", path, 200, body={**body, "content": body["content"] + suffix},
                           key=p.marker, fields=fields)
            ids.add(p.require(value, "id"))
        p.check("nonce replay kept one message", len(ids) == 1)
        read = p.call("read committed message", "GET", f"{path}/{p.require(value, 'id')}", 200,
                      fields=fields)
        p.check("original content persisted", p.require(read, "content") == p.marker)
        p.call("empty message", "POST", path, 400, body={}, fields=("message",),
               code_path="code", code=50006)
    finally:
        if live:
            for ident in ids:
                p.cleanup("DELETE", f"{path}/{ident}")


def _sendgrid(p, env, live):
    body = {
        "personalizations": [{"to": [{"email": "shape-contract@example.com"}]}],
        "from": {"email": env["SENDGRID_TEST_FROM"]}, "subject": p.marker,
        "content": [{"type": "text/plain", "value": p.marker}],
        "mail_settings": {"sandbox_mode": {"enable": True}},
    }
    path = "/v3/mail/send"
    for step, suffix in (("validate", ""), ("retry same key", ""), ("changed parameters", " edit")):
        p.call(step, "POST", path, 200, key=p.marker, body={**body, "subject": p.marker + suffix})
    p.call("missing content", "POST", path, 400, body={**body, "content": []},
           fields=("errors.0.message", "errors.0.field"), code_path="errors.0.field", code="content")


def _twilio_connect(env, live):
    return "https://api.twilio.com", {
        "Authorization": _basic(env["TWILIO_TEST_ACCOUNT_SID"], env["TWILIO_TEST_AUTH_TOKEN"]),
    }


def _twilio(p, env, live):
    sid = env["TWILIO_TEST_ACCOUNT_SID"]
    if not re.fullmatch(r"AC[0-9a-fA-F]{32}", sid):
        raise ContractFailure("TWILIO_TEST_ACCOUNT_SID must be an AC account SID")
    path = f"/2010-04-01/Accounts/{sid}/Messages"
    form = {"From": "+15005550006", "To": "+14155552671", "Body": p.marker}
    fields = ("sid", "status", "to", "from", "body", "account_sid")
    for step, suffix in (("validate", ""), ("retry same key", ""), ("changed parameters", " edit")):
        value = p.call(step, "POST", f"{path}.json", 201, fields=fields, key=p.marker,
                       form={**form, "Body": p.marker + suffix})
    p.call("test SID is inaccessible", "GET", f"{path}/{p.require(value, 'sid')}.json", 403,
           code_path="code", code=20008, fields=("message", "status"))
    p.call("invalid destination", "POST", f"{path}.json", 400, form={**form, "To": "+15005550001"},
           fields=("message", "status"), code_path="code", code=21211)


def _google_connect(prefix, base):
    def connect(env, live):
        token = "offline"
        if live:
            data = urlencode({
                "client_id": env[f"{prefix}_CLIENT_ID"],
                "client_secret": env[f"{prefix}_CLIENT_SECRET"],
                "refresh_token": env[f"{prefix}_REFRESH_TOKEN"],
                "grant_type": "refresh_token",
            }).encode()
            response = _live_send(Request("POST", "https://oauth2.googleapis.com/token",
                                          {"Content-Type": "application/x-www-form-urlencoded"},
                                          data, None))
            if response.status != 200:
                raise ContractFailure(f"OAuth token refresh returned HTTP {response.status}")
            try:
                token = json.loads(response.body)["access_token"]
            except (ValueError, KeyError, TypeError):
                raise ContractFailure("OAuth response omitted access_token") from None
        return base, {"Authorization": f"Bearer {token}"}

    return connect


def _gmail(p, env, live):
    email = env["GMAIL_TEST_EMAIL"]
    path = "/gmail/v1/users/me/messages"
    fields = ("id", "threadId", "labelIds")
    ids = []
    mail = EmailMessage()
    mail["From"] = email
    mail["To"] = email
    mail["Subject"] = f"Shape provider contract {p.marker}"
    mail["Message-ID"] = f"<{p.marker}@shape.invalid>"
    try:
        for step, suffix in (("commit", ""), ("retry same key", ""), ("changed parameters", " edit")):
            mail.set_content(p.marker + suffix)
            raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
            value = p.call(step, "POST", f"{path}/send", 200, body={"raw": raw},
                           key=p.marker, fields=fields)
            ids.append(p.require(value, "id"))
        p.check("three distinct sends", len(set(ids)) == 3)
        value = p.call("read committed email", "GET", f"{path}/{ids[0]}", 200,
                       query={"format": "minimal"}, fields=(*fields, "historyId"))
        p.check("first message is in Sent", "SENT" in p.require(value, "labelIds"))
        p.call("missing message", "GET", f"{path}/0000000000000000", 404,
               fields=("error.code", "error.message"), code_path="error.code", code=404)
    finally:
        if live:
            for ident in set(ids):
                p.cleanup("POST", f"{path}/{ident}/trash")


def _calendar(p, env, live):
    path = f"/calendar/v3/calendars/{quote(env['CALENDAR_TEST_ID'], safe='')}/events"
    event_id = p.marker
    resource = f"{path}/{event_id}"
    body = {"id": event_id, "summary": p.marker, "start": {"date": "2030-01-01"},
            "end": {"date": "2030-01-02"}}
    fields = ("id", "summary", "start.date", "end.date", "etag")
    created = False
    try:
        value = p.call("commit", "POST", path, 200, body=body, key=p.marker, fields=fields)
        created = p.require(value, "id") == event_id
        for step, suffix in (("retry same ID", ""), ("changed parameters", " edit")):
            p.call(step, "POST", path, 409, body={**body, "summary": p.marker + suffix},
                   key=p.marker, fields=("error.message",),
                   code_path="error.errors.0.reason", code="duplicate")
        read = p.call("read committed event", "GET", resource, 200, fields=fields)
        p.check("original summary persisted", p.require(read, "summary") == p.marker)
        p.call("patch committed event", "PATCH", resource, 200, fields=fields,
               body={"summary": p.marker + " updated"})
        p.call("delete event", "DELETE", resource, 204)
        created = False
    finally:
        if live and created:
            p.cleanup("DELETE", resource)


_CONTRACT_TEAM = "5ba3c574-5192-467e-8d2c-b31105aa0dbf"


def _linear(p, env, live):
    path = "/graphql"
    fields = ("data.issueCreate.success", "data.issueCreate.issue.id",
              "data.issueCreate.issue.title", "data.issueCreate.issue.identifier")
    create = ("mutation($input: IssueCreateInput!) { issueCreate(input: $input) "
              "{ success issue { id title identifier } } }")
    issue_ids = []
    try:
        for step, suffix in (("commit", ""), ("retry same key", ""), ("changed parameters", " edit")):
            body = {"query": create, "variables": {"input": {
                "teamId": env["LINEAR_TEST_TEAM_ID"], "title": p.marker + suffix}}}
            value = p.call(step, "POST", path, 200, body=body, key=p.marker, fields=fields)
            issue_ids.append(p.require(value, "data.issueCreate.issue.id"))
        p.check("three distinct issues", len(set(issue_ids)) == 3)
        body = {"query": "query($id: String!) { issue(id: $id) { id title } }",
                "variables": {"id": issue_ids[0]}}
        read = p.call("read committed issue", "POST", path, 200,
                      body=body, fields=("data.issue.id", "data.issue.title"))
        p.check("original title persisted", p.require(read, "data.issue.title") == p.marker)
        comment = ("mutation($input: CommentCreateInput!) { commentCreate(input: $input) "
                   "{ success comment { id body } } }")
        p.call("comment on first issue", "POST", path, 200,
               body={"query": comment, "variables": {"input": {
                   "issueId": issue_ids[0], "body": p.marker}}},
               fields=("data.commentCreate.success", "data.commentCreate.comment.id"))
        p.call("invalid GraphQL selection", "POST", path, 400, body={"query": "{ noSuchField }"},
               fields=("errors.0.message",), code_path="errors.0.extensions.code",
               code="GRAPHQL_VALIDATION_FAILED")
    finally:
        if live:
            for ident in issue_ids:
                p.cleanup("POST", path, body={
                    "query": "mutation($id: String!) { issueArchive(id: $id) { success } }",
                    "variables": {"id": ident}})


def _jira_connect(env, live):
    domain = env["JIRA_TEST_SITE"]
    if not re.fullmatch(r"[A-Za-z0-9-]+\.atlassian\.net", domain):
        raise ContractFailure("JIRA_TEST_SITE must be a *.atlassian.net hostname")
    return f"https://{domain}", {
        "Authorization": _basic(env["JIRA_TEST_EMAIL"], env["JIRA_TEST_API_TOKEN"]),
    }


def _jira(p, env, live):
    path = "/rest/api/3/issue"
    fields = ("id", "key", "self")
    ids = []
    try:
        for step, suffix in (("commit", ""), ("retry same key", ""), ("changed parameters", " edit")):
            body = {"fields": {"project": {"key": env["JIRA_TEST_PROJECT"]},
                               "issuetype": {"id": env["JIRA_TEST_ISSUE_TYPE"]},
                               "summary": p.marker + suffix}}
            value = p.call(step, "POST", path, 201, body=body, key=p.marker, fields=fields)
            ids.append(p.require(value, "id"))
        p.check("three distinct issues", len(set(ids)) == 3)
        resource = f"{path}/{ids[0]}"
        read = p.call("read committed issue", "GET", resource, 200, fields=(*fields, "fields.summary"))
        p.check("original summary persisted", p.require(read, "fields.summary") == p.marker)
        adf = {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": p.marker}]}]}
        p.call("create comment", "POST", f"{resource}/comment", 201, body={"body": adf},
               fields=("id", "body.content.0.content.0.text", "self"))
        transition = {"transition": {"id": env["JIRA_TEST_TRANSITION"]}}
        p.call("transition issue", "POST", f"{resource}/transitions", 204, body=transition)
        p.call("missing summary", "POST", path, 400, body={
            "fields": {"project": {"key": env["JIRA_TEST_PROJECT"]},
                       "issuetype": {"id": env["JIRA_TEST_ISSUE_TYPE"]}}},
               fields=("errors.summary", "errorMessages"))
    finally:
        if live:
            for ident in ids:
                p.cleanup("DELETE", f"{path}/{ident}")


def _langgraph_connect(env, live):
    url = env["LANGGRAPH_TEST_URL"].rstrip("/")
    parsed = urlsplit(url)
    localhost = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if (not parsed.hostname or not (parsed.scheme == "https" or parsed.scheme == "http" and localhost)
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path):
        raise ContractFailure("LANGGRAPH_TEST_URL must be an HTTPS origin or localhost HTTP origin")
    token = env.get("LANGGRAPH_TEST_API_KEY") if live else None
    return url, {"X-Api-Key": token} if token else {}


def _langgraph(p, env, live):
    thread_id = str(UUID(hex=p.marker))
    path = f"/threads/{thread_id}"
    body = {"thread_id": thread_id, "if_exists": "do_nothing", "metadata": {"shape": p.marker}}
    fields = ("thread_id", "status", "metadata.shape", "created_at", "updated_at")
    created = False
    try:
        result = p.call("commit", "POST", "/threads", 200, body=body, key=p.marker, fields=fields)
        created = p.require(result, "thread_id") == thread_id
        p.call("retry same thread ID", "POST", "/threads", 200, body=body, key=p.marker, fields=fields)
        p.call("changed parameters", "POST", "/threads", 409, body={**body, "if_exists": "raise"},
               key=p.marker, fields=("detail",))
        read = p.call("read committed thread", "GET", path, 200, fields=fields)
        p.check("original metadata persisted", p.require(read, "metadata.shape") == p.marker)
        request = {"assistant_id": env["LANGGRAPH_TEST_ASSISTANT_ID"], "input": {"shape": p.marker}}
        run = p.call("create background run", "POST", f"{path}/runs", 200, body=request,
                     fields=("run_id", "thread_id", "status"))
        p.call("join background run", "GET", f"{path}/runs/{p.require(run, 'run_id')}/join", 200)
    finally:
        if live and created:
            p.cleanup("DELETE", path)


CONTRACTS = {
    "stripe": Suite(StripeFake, {"STRIPE_TEST_KEY": "sk_test_offline"}, _stripe_connect, _stripe),
    "github": Suite(GitHubFake, {"GITHUB_TOKEN": "offline", "GITHUB_SCRATCH_REPO": "shape/scratch"},
                    _bearer("https://api.github.com", "GITHUB_TOKEN", {
                        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                        "User-Agent": "shape-provider-contract",
                    }), _github),
    "slack": Suite(SlackFake, {"SLACK_BOT_TOKEN": "offline", "SLACK_TEST_CHANNEL": "C0000000000"},
                   _bearer("https://slack.com", "SLACK_BOT_TOKEN"), _slack),
    "telegram": Suite(TelegramFake, {"TELEGRAM_BOT_TOKEN": "123:offline",
                                    "TELEGRAM_TEST_CHAT_ID": "1234"},
                      lambda env, live: ("https://api.telegram.org", {}), _telegram),
    "discord": Suite(DiscordFake, {"DISCORD_BOT_TOKEN": "offline",
                                  "DISCORD_TEST_CHANNEL": "123456789012345678"},
                     _bearer("https://discord.com", "DISCORD_BOT_TOKEN", {
                         "User-Agent": "DiscordBot (https://github.com/lutefd/shape, 0.1)"
                     }, scheme="Bot"), _discord),
    "sendgrid": Suite(SendGridFake, {"SENDGRID_API_KEY": "offline",
                                    "SENDGRID_TEST_FROM": "sender@example.com"},
                      _bearer("https://api.sendgrid.com", "SENDGRID_API_KEY"), _sendgrid),
    "twilio": Suite(partial(TwilioFake, test_mode=True, invalid_numbers={"+15005550001"}),
                    {"TWILIO_TEST_ACCOUNT_SID": f"AC{'0' * 32}", "TWILIO_TEST_AUTH_TOKEN": "offline"},
                    _twilio_connect, _twilio),
    "gmail": Suite(GmailFake, {"GMAIL_CLIENT_ID": "offline", "GMAIL_CLIENT_SECRET": "offline",
                              "GMAIL_REFRESH_TOKEN": "offline", "GMAIL_TEST_EMAIL": "user@example.com"},
                   _google_connect("GMAIL", "https://gmail.googleapis.com"), _gmail),
    "google-calendar": Suite(GoogleCalendarFake, {
        "CALENDAR_CLIENT_ID": "offline", "CALENDAR_CLIENT_SECRET": "offline",
        "CALENDAR_REFRESH_TOKEN": "offline", "CALENDAR_TEST_ID": "primary",
    }, _google_connect("CALENDAR", "https://www.googleapis.com"), _calendar),
    "linear": Suite(LinearFake, {"LINEAR_API_KEY": "offline", "LINEAR_TEST_TEAM_ID": _CONTRACT_TEAM},
                    _bearer("https://api.linear.app", "LINEAR_API_KEY", scheme=""), _linear),
    "jira": Suite(JiraFake, {"JIRA_TEST_SITE": "shape-test.atlassian.net", "JIRA_TEST_EMAIL": "offline",
                            "JIRA_TEST_API_TOKEN": "offline", "JIRA_TEST_PROJECT": "SHAPE",
                            "JIRA_TEST_ISSUE_TYPE": "10001", "JIRA_TEST_TRANSITION": "21"},
                  _jira_connect, _jira),
    "langgraph": Suite(LangGraphFake, {"LANGGRAPH_TEST_URL": "http://localhost:2024",
                                      "LANGGRAPH_TEST_ASSISTANT_ID": "echo"},
                       _langgraph_connect, _langgraph),
}
