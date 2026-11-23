import asyncio
import imaplib
import json
import smtplib
import socket
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path

import pytest

import shape
from shape.boundary import runner
from shape.boundary.faults import FAULTS
from shape.fakes.base import EffectInfo, Response

HOST = "provider.invalid"
URL = f"https://{HOST}/items"
SCOPE = "item:one"


class Fake:
    hosts = (HOST, "alias.invalid")
    api_version = "test"

    def handle(self, req, state):
        writes = state.setdefault("writes", [])
        if req.method in {"GET", "HEAD"}:
            data = writes
        elif req.json.get("refuse"):
            return Response(409, {}, b"")
        else:
            key = req.headers.get("idempotency-key")
            if key is None or not any(row["key"] == key for row in writes):
                writes.append({"key": key, **req.json})
            data = {"count": len(writes)}
        return Response(200, {"Content-Type": "application/json"}, json.dumps(data).encode())

    def describe(self, req):
        return EffectInfo("test.write", SCOPE, None, False, False)

    def effect(self, req, resp):
        if hasattr(req, "url"):
            return EffectInfo("test.write", SCOPE, None, False, resp.status == 200)
        if req.method == "SEND":
            return EffectInfo("test.mail", "mailbox", None, True, not resp)
        return EffectInfo("test.imap", "mailbox", None, True, resp[0] == "OK")

    def invariants(self):
        return [shape.at_most_once("test.write")]

    def send(self, from_addr, to_addrs, message, state):
        state.setdefault("messages", []).append(message.decode())
        return {}

    def command(self, name, args, state):
        messages = state.setdefault("messages", [])
        if name in {"SELECT", "EXAMINE"}:
            return "OK", [str(len(messages)).encode()]
        if name == "UID" and args[0] == "SEARCH":
            return "OK", [b" ".join(str(i + 1).encode() for i in range(len(messages)))]
        if name == "FETCH":
            return "OK", [(b"1 (BODY[]", messages[int(args[0]) - 1].encode()), b")"]
        if name == "APPEND":
            messages.append(args[-1].decode())
        if name == "STORE" or (name == "UID" and args[0] == "STORE"):
            state["flag"] = args[-1]
        if name == "EXPUNGE":
            messages.clear()
            return "OK", [b"1"]
        return "OK", [b"stored"]


def execute(scenario, path, fault=None, op="test.write"):
    explicit = {} if fault is None else {f"fault:{op}#0.0": FAULTS.index(fault) + 1}
    return runner.execute(scenario, {"explicit": explicit}, path)


def urllib_call(method, url, payload=None, key=None):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["idempotency-key"] = key
    req = urllib.request.Request(url, None if payload is None else json.dumps(payload).encode(),
                                 headers, method=method)
    with urllib.request.urlopen(req) as response:
        return json.load(response)


def call(client, method, url=URL, payload=None, key=None):
    headers = {"Idempotency-Key": key} if key is not None else None
    if client == "requests":
        import requests

        return requests.request(method, url, headers=headers, json=payload).json()
    if client == "httpx":
        import httpx

        with httpx.Client() as http:
            return http.request(method, url, headers=headers, json=payload).json()
    if client == "httpx-async":
        import httpx

        async def run():
            async with httpx.AsyncClient() as http:
                return (await http.request(method, url, headers=headers, json=payload)).json()

        return asyncio.run(run())
    return urllib_call(method, url, payload, key)


@pytest.mark.parametrize("client", ["requests", "httpx", "httpx-async", "urllib"])
def test_http_clients_use_same_provider_state_and_key(tmp_path, client):
    if client != "urllib":
        pytest.importorskip(client.split("-")[0])

    @shape.scenario(net=[Fake()], max_restarts=0)
    def scenario(run):
        assert call(client, "POST", payload={"value": 500}, key="work") == {"count": 1}
        assert call(client, "POST", payload={"value": 500}, key="work") == {"count": 1}
        assert call(client, "GET", url=URL.replace(HOST, "alias.invalid")) == [
            {"key": "work", "value": 500},
        ]

    out = execute(scenario, tmp_path / "one")
    assert out.ok
    assert out.stats["effects"] == {"test.write": 1}
    assert out.stats["attempts"] == {"test.write": 2}
    assert any("POST provider.invalid/items (value=500, Idempotency-Key='work')" in line
               for line in runner.story(out.entries)[0])


@pytest.mark.parametrize("client", ["requests", "httpx", "httpx-async", "urllib"])
@pytest.mark.parametrize("fault", ["timeout_after_commit", "connection_reset_after_commit"])
def test_http_commit_precedes_library_exception(tmp_path, client, fault):
    if client == "requests":
        library = pytest.importorskip("requests")
        timeout, reset = library.ReadTimeout, library.ConnectionError
    elif client.startswith("httpx"):
        library = pytest.importorskip("httpx")
        timeout, reset = library.ReadTimeout, library.ReadError
    else:
        timeout, reset = TimeoutError, ConnectionResetError
    error = timeout if fault.startswith("timeout") else reset

    @shape.scenario(net=[Fake()], max_restarts=0)
    def scenario(run):
        with pytest.raises(error):
            call(client, "POST", payload={})
        assert urllib_call("GET", URL) == [{"key": None}]

    out = execute(scenario, tmp_path / "one", fault)
    assert out.ok
    assert out.stats["faults"] == {"test.write": {fault: 1}}


def test_refused_call_does_not_inject_after_commit_fault(tmp_path):
    @shape.scenario(net=[Fake()], max_restarts=0)
    def scenario(run):
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib_call("POST", URL, {"refuse": True})
        assert exc.value.code == 409
        assert urllib_call("GET", URL) == []

    out = execute(scenario, tmp_path / "one", "timeout_after_commit")
    assert out.ok and out.realized
    assert out.stats["faults"] == {}
    assert out.stats["no_effects"] == {"test.write": 1}


def test_provider_state_survives_crash_and_restarts(tmp_path):
    @shape.scenario(net=[Fake()], max_restarts=1)
    def scenario(run):
        if run.restarts:
            assert urllib_call("GET", URL) == [{"key": "work"}]
        assert urllib_call("POST", URL, {}, "work") == {"count": 1}

    out = execute(scenario, tmp_path / "one", "crash_after")
    assert out.ok
    assert out.stats["effects"] == {"test.write": 1}
    assert out.stats["attempts"] == {"test.write": 2}


@pytest.mark.parametrize("operation", [
    lambda: urllib_call("GET", "https://unregistered.invalid"),
    lambda: smtplib.SMTP("unregistered.invalid"),
    lambda: imaplib.IMAP4_SSL("unregistered.invalid"),
    lambda: socket.create_connection(("10.255.255.1", 9)),
])
def test_unregistered_or_uncaptured_network_is_a_harness_failure(tmp_path, operation):
    @shape.scenario(net=[Fake()], max_restarts=0)
    def scenario(run):
        try:
            operation()
        except Exception as exc:  # noqa: BLE001 - agents can swallow ordinary tool errors
            print(f"agent swallowed {exc}")

    out = execute(scenario, tmp_path / "one")
    assert out.violated("harness_error")
    assert "shape.net:" in out.violations[0].message


@pytest.mark.parametrize("smtp,imap", [
    (smtplib.SMTP, imaplib.IMAP4), (smtplib.SMTP_SSL, imaplib.IMAP4_SSL),
])
def test_smtp_envelope_and_imap_commands_share_state(tmp_path, smtp, imap):
    @shape.scenario(net=[Fake()], max_restarts=0)
    def scenario(run):
        with smtp(HOST) as sender:
            assert sender.login("user", "password")[0] == 235
            assert sender.sendmail("from@example.org", "to@example.org", "body") == {}
            msg = EmailMessage()
            msg["From"] = "from@example.org"
            msg["To"] = "to@example.org"
            msg["Bcc"] = "bcc@example.org"
            msg.set_content("Hello")
            assert sender.send_message(msg) == {}
        with imap(HOST) as mailbox:
            assert mailbox.login("user", "password")[0] == "OK"
            assert mailbox.select("INBOX") == ("OK", [b"2"])
            assert mailbox.uid("search", None, "ALL") == ("OK", [b"1 2"])
            assert mailbox.fetch("1", "(BODY[])")[1][0][1] == b"body"
            assert b"Bcc:" not in mailbox.fetch("2", "(BODY[])")[1][0][1]
            assert mailbox.uid("store", "1", "+FLAGS", r"\Deleted")[0] == "OK"
            assert mailbox.copy("1", "Archive")[0] == "OK"
            assert mailbox.uid("move", "1", "Archive")[0] == "OK"
            assert mailbox.append("INBOX", None, None, b"third")[0] == "OK"
            assert mailbox.expunge() == ("OK", [b"1"])
            assert mailbox.select("INBOX") == ("OK", [b"0"])

    out = execute(scenario, tmp_path / "one")
    assert out.ok, (out.dir / "inc-0.out").read_text()
    assert out.stats["attempts"] == {"test.write": 7}
    assert out.stats["effects"] == {"test.mail": 2, "test.imap": 5}


def test_smtp_disconnect_after_accept(tmp_path):
    @shape.scenario(net=[Fake()], max_restarts=0)
    def scenario(run):
        with smtplib.SMTP_SSL(HOST) as sender, pytest.raises(smtplib.SMTPServerDisconnected):
            sender.sendmail("from", "to", "hello")
        with imaplib.IMAP4(HOST) as mailbox:
            mailbox.login("user", "password")
            assert mailbox.select("INBOX") == ("OK", [b"1"])

    out = execute(scenario, tmp_path / "one", "timeout_after_commit")
    assert out.ok, (out.dir / "inc-0.out").read_text()
    assert out.stats["faults"] == {"test.mail": {"timeout_after_commit": 1}}


def test_reads_make_visible_actor_points_and_share_state(tmp_path):
    def writer(run):
        urllib_call("POST", URL, {}, "work")

    @shape.scenario(net=[Fake()], concurrent=[writer], max_restarts=0)
    def scenario(run):
        urllib_call("GET", URL)
        with imaplib.IMAP4(HOST) as mailbox:
            mailbox.login("user", "password")
            mailbox.select("INBOX")
            mailbox.search(None, "ALL")
        assert urllib_call("GET", URL) == [{"key": "work"}]

    choices = {"sched:actors#0.0": 1}
    out = runner.execute(scenario, {"explicit": choices}, tmp_path / "one")
    assert out.ok, (out.dir / "inc-0.out").read_text()
    points = [e["label"] for e in out.entries if e["kind"] == "point"]
    assert points == ["GET provider.invalid/items", "IMAP SELECT provider.invalid (args=('INBOX',))",
                      "IMAP SEARCH provider.invalid (args=('ALL',))", "GET provider.invalid/items"]
    assert out.stats["attempts"] == {"test.write": 1}


def test_async_http_points_label_task_interleavings(tmp_path):
    httpx = pytest.importorskip("httpx")

    @shape.scenario(net=[Fake()], max_restarts=0)
    def scenario(run):
        async def read_and_write():
            async with httpx.AsyncClient() as http:
                read, _ = await asyncio.gather(http.get(URL), http.post(URL, json={}))
                assert read.json() == [{"key": None}]

        shape.run_async(read_and_write())

    choices = {"sched:tasks#0.2": 1}
    out = runner.execute(scenario, {"explicit": choices}, tmp_path / "one")
    assert out.ok, (out.dir / "inc-0.out").read_text()
    draw = next(e for e in out.entries if e["kind"] == "schedule")
    assert "at 'POST provider.invalid/items'" in draw["chosen"]
    assert "at 'GET provider.invalid/items'" in draw["nominal"]


def test_mailtrap_example_reproduces_upstream_last_result_bug(tmp_path):
    pytest.importorskip("mailtrap")
    from shape.boundary.child import load_module

    example = Path(__file__).resolve().parents[2] / "examples/net/mailtrap.py"
    scenario = load_module(example).mailtrap_reports_last_result
    choice = {"model:send_email calls#0.0": 1,
              "fault:email.send#0.1": FAULTS.index("server_error") + 1}
    out = runner.execute(scenario, {"explicit": choice}, tmp_path / "one")
    assert out.violated("claims_true")
    assert out.stats["effects"] == {"email.send": 1}
    assert "FAILED to send to me@example.com" in (out.dir / "inc-0.out").read_text()
    assert any("caller saw HTTP 503" in line for line in runner.story(out.entries)[0])
