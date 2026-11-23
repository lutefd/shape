"""Mailtrap SDK example: model the email tool and the CLI's last-result reporting."""

import json
import re

import mailtrap as mt
from requests import RequestException

import shape
from shape.fakes.base import EffectInfo, Response

# Tool and CLI behavior from the upstream LangChain example:
# https://github.com/mailtrap/mailtrap-langchain-example/tree/5b2bc0e6
OWN_EMAIL = "me@example.com"
ALLOWED_DOMAIN = "example.com"
CATEGORY = "langchain-example"


def send_email(to, subject, body):
    to = to.strip()
    normalized = to.lower()
    domain = normalized.rpartition("@")[-1]
    if not re.fullmatch(r"[^@\s]+@[^@\s]+", normalized) or (
        normalized != OWN_EMAIL and domain != ALLOWED_DOMAIN
    ):
        return f"REJECTED: {to} is outside the allowed domain"
    client = mt.MailtrapClient(token="fake-test-token")
    mail = mt.Mail(
        sender=mt.Address(email="sender@example.com", name="Mailtrap Example"),
        to=[mt.Address(email=to)],
        subject=subject,
        text=body,
        category=CATEGORY,
        headers={"X-MT-Category": CATEGORY},
    )
    try:
        response = client.send(mail)
    except (mt.MailtrapError, RequestException) as error:
        return f"FAILED to send to {to}: {error}"
    return f"SENT to {to} (message_ids={response.get('message_ids', [])})"


def cli_last_result(call_count):
    results = [send_email(OWN_EMAIL, "Welcome", "Welcome!") for _ in range(call_count)]
    print(results[-1])
    return results[-1]


class MailtrapFake:
    hosts = ("send.api.mailtrap.io",)
    api_version = "Mailtrap Email Sending API (SDK 2.6.1)"

    def handle(self, req, state):
        if req.method != "POST" or not req.url.endswith("/api/send"):
            return Response(404, {}, b'{"errors":["Unknown endpoint"]}')
        accepted = state.setdefault("accepted", [])
        accepted.append(req.json["to"][0]["email"])
        body = json.dumps({"success": True, "message_ids": [f"test-{len(accepted)}"]}).encode()
        return Response(200, {"Content-Type": "application/json"}, body)

    def describe(self, req):
        return EffectInfo("email.send", req.json["to"][0]["email"], None, True, False)

    def effect(self, req, resp):
        return EffectInfo("email.send", req.json["to"][0]["email"], None, True, resp.status == 200)

    def invariants(self):
        return [shape.claims_true()]


@shape.scenario(net=[MailtrapFake()], max_restarts=0)
def mailtrap_reports_last_result(run):
    calls = shape.choose("send_email calls", [1, 2])
    status = cli_last_result(calls)
    shape.claim(OWN_EMAIL, "email.send", ok=status.startswith("SENT"))
