import json

import pytest

from shape.cli import main
from shape.fakes import _PROVIDERS, SendGridFake, StripeFake, TwilioFake
from shape.fakes.base import Request, Response
from shape.fakes.contracts import assert_contract, run_contract
from shape.fakes.contracts._suites import CONTRACTS


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_documented_contract(name):
    assert_contract(name)


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_live_missing_credentials_skips(name):
    result = run_contract(name, live=True, env={})
    assert all(key in result.skipped for key in CONTRACTS[name].settings)
    assert result.verified_on is None


def test_cli_json_report_and_metadata(tmp_path, capsys):
    path = tmp_path / "result.json"
    assert main(["fakes", "contract", "stripe", "--report", str(path)]) == 0
    assert "PASS stripe (fake," in capsys.readouterr().out
    result = json.loads(path.read_text())
    assert result["verified_on"] is None
    assert result["observations"]["changed parameters"]["code"] == "idempotency_error"
    assert all(fake.verification_status in ("unverified", f"verified against live on {fake.verified_on}")
               for fake in _PROVIDERS.values())


def test_live_checks_replay_identity_and_diffs_shape_without_printing_values(monkeypatch):
    fake, state = StripeFake(), {}
    token = "sk_test_secret"

    def send(req):
        assert req.headers["Authorization"] == f"Bearer {token}"
        return fake.handle(req, state)

    monkeypatch.setattr("shape.fakes.contracts._live_send", send)
    result = assert_contract("stripe", live=True, env={"STRIPE_TEST_KEY": token})
    assert result.verified_on

    def changed(req):
        response = send(req)
        if req.method == "GET":
            return Response(response.status, response.headers, json.dumps({"id": token}).encode())
        return response

    monkeypatch.setattr("shape.fakes.contracts._live_send", changed)
    result = run_contract("stripe", live=True, env={"STRIPE_TEST_KEY": token})
    assert result.verified_on is None
    assert any("fake=" in line and "live=" in line for line in result.differences)
    assert token not in repr(result)


def test_sendgrid_sandbox_validates_without_effect():
    fake = SendGridFake()
    body = {
        "from": {"email": "sender@example.com"}, "subject": "Report",
        "personalizations": [{"to": [{"email": "test@example.com"}]}],
        "content": [{"type": "text/plain", "value": "Ready"}],
        "mail_settings": {"sandbox_mode": {"enable": True}},
    }
    req = Request("POST", "https://api.sendgrid.com/v3/mail/send", {}, b"", body)
    state = {}
    result = fake.handle(req, state)
    assert result.status == 200
    assert not fake.effect(req, result).landed
    assert state["messages"] == []


def test_twilio_test_credentials_return_ephemeral_sid():
    fake = TwilioFake(test_mode=True)
    base = "https://api.twilio.com/2010-04-01/Accounts/AC" + "0" * 32
    req = Request("POST", base + "/Messages.json", {}, b"",
                  {"To": "+14155552671", "From": "+15005550006", "Body": "Ready"})
    state = {}
    result = fake.handle(req, state)
    assert result.status == 201
    assert not fake.effect(req, result).landed
    ident = json.loads(result.body)["sid"]
    result = fake.handle(Request("GET", f"{base}/Messages/{ident}.json", {}, b"", None), state)
    assert result.status == 403
    assert json.loads(result.body)["code"] == 20008
