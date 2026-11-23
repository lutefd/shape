import base64
from uuid import UUID

import pytest

import shape
from shape.boundary import runner
from shape.boundary.faults import FAULTS
from shape.fakes import (
    DiscordFake,
    GmailFake,
    GoogleCalendarFake,
    JiraFake,
    LangGraphFake,
    LinearFake,
    TelegramFake,
)


def run_scenario(scenario, directory, *, lost=None):
    choices = {f"fault:{lost}#0.0": FAULTS.index("timeout_after_commit") + 1} if lost else {}
    result = runner.execute(scenario, {"explicit": choices}, directory)
    assert result.completed, (result.dir / "inc-0.out").read_text()
    return result


def test_google_clients_use_httplib2_and_gmail_retries_after_lost_reply(tmp_path):
    discovery = pytest.importorskip("googleapiclient.discovery")
    httplib2 = pytest.importorskip("httplib2")
    fake_mail = GmailFake(scope=lambda req, params: "job")
    fake_calendar = GoogleCalendarFake(scope=lambda req, params: "appointment")
    raw = base64.urlsafe_b64encode(
        b"From: user@example.com\r\nTo: dst@example.com\r\nSubject: Report\r\n\r\nReady"
    ).decode()

    @shape.scenario(net=[fake_mail, fake_calendar], max_restarts=0, strict_scopes=True)
    def send(run):
        mail = discovery.build("gmail", "v1", http=httplib2.Http()).users().messages()

        def send_message():
            request = mail.send(userId="me", body={"raw": raw})
            request.headers["Idempotency-Key"] = "ignored-by-gmail"
            return request.execute()

        with pytest.raises(TimeoutError):
            send_message()
        assert len(mail.list(userId="me", labelIds=["SENT"]).execute()["messages"]) == 1
        retry = send_message()
        assert len(mail.list(userId="me", labelIds=["SENT"]).execute()["messages"]) == 2
        assert mail.get(userId="me", id=retry["id"], format="raw").execute()["raw"] == raw
        events = discovery.build("calendar", "v3", http=httplib2.Http()).events()
        event = events.insert(calendarId="primary", body={
            "summary": "Report", "start": {"date": "2030-01-01"}, "end": {"date": "2030-01-02"},
        }).execute()
        events.patch(calendarId="primary", eventId=event["id"], body={"summary": "Updated"}).execute()
        assert events.get(calendarId="primary", eventId=event["id"]).execute()["summary"] == "Updated"
        events.delete(calendarId="primary", eventId=event["id"]).execute()

    original = httplib2.Http.request
    result = run_scenario(send, tmp_path / "google", lost="gmail.messages.send")
    assert httplib2.Http.request is original
    assert [v.invariant for v in result.violations] == ["at_most_once(gmail.messages.send)"]
    assert result.stats["effects"]["gmail.messages.send"] == 2
    assert result.stats["effects"]["calendar.events.delete"] == 1


def test_telegram_bot_sdk_and_redacted_trace(tmp_path):
    telegram = pytest.importorskip("telegram")
    fake = TelegramFake(initial_updates=[{"update_id": 30}], scope=lambda req, params: "job")

    @shape.scenario(net=[fake], max_restarts=0, strict_scopes=True)
    def run_bot(run):
        async def chat():
            async with telegram.Bot("123:secret-test-token") as bot:
                assert await bot.send_chat_action(1234, "typing")
                message = await bot.send_message(1234, "First")
                edited = await bot.edit_message_text(chat_id=1234, message_id=message.message_id,
                                                     text="Updated")
                assert edited.text == "Updated"
                assert (await bot.get_updates())[0].update_id == 30
                assert await bot.get_updates(offset=31) == ()

        shape.run_async(chat())

    result = run_scenario(run_bot, tmp_path / "telegram")
    assert result.ok
    assert result.stats["effects"]["telegram.sendMessage"] == 1
    for path in result.dir.iterdir():
        if path.suffix in (".jsonl", ".out", ".json"):
            assert "secret-test-token" not in path.read_text()


def test_telegram_get_method_records_write(tmp_path):
    requests = pytest.importorskip("requests")
    fake = TelegramFake(scope=lambda req, params: "job")

    @shape.scenario(net=[fake], max_restarts=0)
    def send(run):
        response = requests.get("https://api.telegram.org/bot123:offline/sendMessage",
                                params={"chat_id": 42, "text": "Ready"})
        assert response.json()["result"]["text"] == "Ready"

    result = run_scenario(send, tmp_path / "telegram-get")
    assert result.ok
    assert result.stats["effects"] == {"telegram.sendMessage": 1}


def test_discord_requests_deduplicates_after_lost_reply(tmp_path):
    requests = pytest.importorskip("requests")
    fake = DiscordFake(scope=lambda req, params: "job")
    url = "https://discord.com/api/v10/channels/123/messages"
    data = {"content": "Hello", "nonce": "job", "enforce_nonce": True}

    @shape.scenario(net=[fake], max_restarts=0, strict_scopes=True)
    def send(run):
        with pytest.raises(requests.ReadTimeout):
            requests.post(url, json=data)
        value = requests.post(url, json={**data, "content": "Changed"}).json()
        assert value["content"] == "Hello"
        assert requests.get(f"{url}/{value['id']}").json()["id"] == value["id"]

    result = run_scenario(send, tmp_path / "discord", lost="discord.message.create")
    assert result.ok
    assert result.stats["attempts"]["discord.message.create"] == 2
    assert result.stats["effects"]["discord.message.create"] == 1


def test_jira_and_linear_through_requests(tmp_path):
    requests = pytest.importorskip("requests")
    linear = LinearFake(scope=lambda req, params: "job")
    jira = JiraFake(scope=lambda req, params: "job")

    @shape.scenario(net=[linear, jira], max_restarts=0, strict_scopes=True)
    def create_tasks(run):
        created = requests.post("https://jira.example.com/rest/api/3/issue", json={
            "fields": {"project": {"key": "SHAPE"}, "issuetype": {"name": "Task"}, "summary": "Work"},
        })
        created.raise_for_status()
        issue = created.json()
        response = requests.post("https://api.linear.app/graphql", json={
            "query": "mutation($input: IssueCreateInput!) { issueCreate(input: $input) "
                     "{ success issue { title } } }",
            "variables": {"input": {"teamId": "team", "title": f"Mirror {issue['key']}"}},
        })
        response.raise_for_status()
        assert response.json()["data"]["issueCreate"]["issue"]["title"] == "Mirror SHAPE-1"

    result = run_scenario(create_tasks, tmp_path / "tasks")
    assert result.ok
    assert result.stats["effects"] == {"jira.issue.create": 1, "linear.issueCreate": 1}


def test_langgraph_sdk_streams_sse_and_reads_checkpoint(tmp_path):
    sdk = pytest.importorskip("langgraph_sdk")
    fake = LangGraphFake(scope=lambda req, params: "job")

    @shape.scenario(net=[fake], max_restarts=0, strict_scopes=True)
    def use_sdk(run):
        client = sdk.get_sync_client(url="http://localhost:2024")
        thread_id = str(UUID(int=42))
        thread = client.threads.create(thread_id=thread_id, if_exists="do_nothing")
        assert thread["thread_id"] == thread_id
        events = list(client.runs.stream(thread_id, "agent", input={"value": "test"},
                                        stream_mode="values"))
        assert any(event.event == "values" and event.data == {"value": "test"} for event in events)
        assert client.threads.get_state(thread_id)["values"] == {"value": "test"}

    result = run_scenario(use_sdk, tmp_path / "langgraph")
    assert result.ok
    assert result.stats["effects"]["langgraph.runs.create"] == 1
