import base64
import json
from uuid import UUID

import pytest

from shape.fakes import (
    DiscordFake,
    GmailFake,
    GoogleCalendarFake,
    JiraFake,
    LangGraphFake,
    LinearFake,
    Request,
    TelegramFake,
)

MAIL = (
    b"From: shape@example.com\r\nTo: user@example.com\r\nSubject: Work\r\n"
    b"Message-ID: <first@example.com>\r\n\r\nBody"
)


def call(fake, state, method, path, body=None, **headers):
    req = Request(method, f"https://{fake.hosts[0]}{path}", headers, b"", body)
    response = fake.handle(req, state)
    return response.status, json.loads(response.body) if response.body else None, fake.effect(
        req, response
    )


def test_gmail_thread_labels_and_history():
    def scope(req, params):
        return params.get("headers", {}).get("Message-ID", "labels")

    fake = GmailFake(initial_messages=[MAIL], scope=scope)
    state, root = {}, "/gmail/v1/users/me"
    _, results, _ = call(fake, state, "GET", f"{root}/messages?q=is:unread")
    ident = results["messages"][0]["id"]
    _, first, _ = call(fake, state, "GET", f"{root}/messages/{ident}?format=raw")
    assert base64.urlsafe_b64decode(first["raw"]) == MAIL
    _, label, event = call(fake, state, "POST", f"{root}/labels", {"name": "Processed"})
    assert event.landed and event.scope_source == "explicit"
    reply = (
        b"To: shape@example.com\r\nSubject: Re: Work\r\nMessage-ID: <reply@example.com>\r\n"
        b"In-Reply-To: <first@example.com>\r\nReferences: <first@example.com>\r\n\r\nReply"
    )
    payload = {"raw": base64.urlsafe_b64encode(reply).decode(), "threadId": ident}
    _, copied, effect = call(fake, state, "POST", f"{root}/messages/send", payload)
    assert effect.landed and effect.scope == "<reply@example.com>"
    assert copied["id"] != ident and copied["threadId"] == ident
    status, modified, effect = call(fake, state, "POST", f"{root}/threads/{ident}/modify",
                                  {"addLabelIds": [label["id"]], "removeLabelIds": ["UNREAD"]})
    assert status == 200 and effect.landed
    assert len(modified["messages"]) == 2
    assert all(label["id"] in m["labelIds"] for m in modified["messages"])
    _, history, _ = call(fake, state, "GET", f"{root}/history?startHistoryId={first['historyId']}")
    assert int(history["historyId"]) > int(first["historyId"])
    assert [event["messages"][0]["id"] for event in history["history"]] == [
        copied["id"], ident, copied["id"],
    ]
    status, _, effect = call(fake, state, "POST", f"{root}/messages/send", {"raw": "%%%"})
    assert status == 400 and not effect.landed


def test_calendar_patch_etag_and_delete_tombstone():
    fake = GoogleCalendarFake(primary="work@example.com", scope=lambda req, body: "job-1")
    state = {}
    path = "/calendar/v3/calendars/primary/events"
    body = {"id": "job01", "start": {"date": "2030-01-01"}, "end": {"date": "2030-01-02"},
            "summary": "Work", "attendees": [{"email": "a@example.com"}]}
    code, item, effect = call(fake, state, "POST", path, body)
    assert code == 200 and effect.scope == "job-1" and effect.landed
    assert call(fake, state, "POST", path, body)[0] == 409
    path = "/calendar/v3/calendars/work%40example.com/events/job01"
    code, updated, effect = call(fake, state, "PATCH", path, {"attendees": [], "summary": None},
                                 **{"If-Match": item["etag"]})
    assert code == 200 and updated["attendees"] == [] and "summary" not in updated and effect.landed
    code, _, effect = call(fake, state, "PATCH", path, {"summary": "Old"},
                           **{"If-Match": item["etag"]})
    assert code == 412 and not effect.landed
    assert call(fake, state, "DELETE", path)[0] == 204
    assert call(fake, state, "GET", path)[1]["status"] == "cancelled"
    assert call(fake, state, "DELETE", path)[0] == 410


def test_telegram_update_offsets_and_empty_edits():
    fake = TelegramFake(initial_updates=[{"update_id": n} for n in (20, 22, 23)],
                        scope=lambda req, body: str(body["chat_id"]))
    state, root = {}, "/bot123:offline"

    def get_updates(url):
        return call(fake, state, "GET", f"{root}/getUpdates{url}")[1]["result"]

    assert get_updates("?limit=1") == [{"update_id": 20}]
    assert get_updates("?limit=1") == [{"update_id": 20}]
    assert get_updates("?offset=21") == [{"update_id": 22}, {"update_id": 23}]
    assert get_updates("?offset=-1") == [{"update_id": 23}]
    assert get_updates("?offset=24") == []
    body = {"chat_id": 42, "text": "Hi"}
    _, msg, effect = call(fake, state, "POST", f"{root}/sendMessage", body)
    assert effect.landed and effect.scope == "42"
    code, result, effect = call(fake, state, "POST", f"{root}/editMessageText",
                                {**body, "message_id": msg["result"]["message_id"]})
    assert code == 400 and result["error_code"] == 400 and not effect.landed


def test_discord_nonce_replay_then_expiration():
    now = [0]
    fake = DiscordFake(clock=lambda: now[0], nonce_ttl=60,
                       scope=lambda req, body: "send-once")
    state, path = {}, "/api/v10/channels/123/messages"
    body = {"content": "hello", "nonce": "task", "enforce_nonce": True}
    code, msg, effect = call(fake, state, "POST", path, body)
    assert code == 200 and effect.landed and effect.scope == "send-once"
    _, replay, effect = call(fake, state, "POST", path, {**body, "content": "edited"})
    assert replay == msg and not effect.landed
    now[0] = 60
    _, other, effect = call(fake, state, "POST", path, body)
    assert other["id"] != msg["id"] and effect.landed


def test_linear_graphql_projection_aliases_and_200_error():
    fake = LinearFake(teams={"team": "T"}, scope=lambda req, params: params.get("title", "ticket"))
    state = {}
    code, result, effect = call(fake, state, "POST", "/graphql", {"query": r'''
        mutation Example {
          created: issueCreate(input: { teamId: "team", title: "A \"ticket\"" }) {
            issue { id name: title identifier }
          }
        }
    '''})
    issue = result["data"]["created"]["issue"]
    assert code == 200 and effect.landed and effect.scope == 'A "ticket"'
    assert issue == {"id": issue["id"], "name": 'A "ticket"', "identifier": "T-1"}
    code, result, effect = call(fake, state, "POST", "/graphql", {"query": '''
        mutation { commentCreate(input: { issueId: "unknown", body: "Hi" }) { success } }
    '''})
    assert code == 200 and result["errors"][0]["extensions"]["code"] == "ENTITY_NOT_FOUND"
    assert not effect.landed


def test_jira_cloud_transitions_validate_before_mutating():
    fake = JiraFake(projects={"OPS": "10000"}, scope=lambda req, body: "workflow-run")
    state, root = {}, "/rest/api/3/issue"
    body = {"fields": {"project": {"id": "10000"}, "summary": "Task", "issuetype": {"id": "1"}}}
    code, item, effect = call(fake, state, "POST", root, body)
    assert code == 201 and effect.landed and effect.scope == "workflow-run"
    path = f"{root}/{item['key']}"
    code, _, effect = call(fake, state, "POST", f"{path}/transitions", {"transition": {"id": "21"}})
    assert code == 204 and effect.landed
    code, _, effect = call(fake, state, "POST", f"{path}/transitions", {"transition": {"id": "99"}})
    assert code == 400 and not effect.landed
    assert call(fake, state, "GET", path)[1]["fields"]["status"]["name"] == "In Progress"


def test_langgraph_replay_run_join_and_cron_state():
    fake = LangGraphFake(scope=lambda req, body: "job", run_result=lambda params: {"n": 5})
    state = {}
    ident = str(UUID(int=42))
    body = {"thread_id": ident, "if_exists": "do_nothing", "metadata": {"v": 1}}
    code, first, effect = call(fake, state, "POST", "/threads", body)
    assert code == 200 and effect.landed and effect.scope == "job"
    code, replayed, effect = call(fake, state, "POST", "/threads", {**body, "metadata": {"v": 2}})
    assert code == 200 and replayed == first and not effect.landed
    thread = f"/threads/{ident}"
    _, run, effect = call(fake, state, "POST", f"{thread}/runs", {"assistant_id": "agent"})
    assert effect.landed and run["status"] == "pending"
    conflict = {"assistant_id": "agent", "multitask_strategy": "reject"}
    code, _, effect = call(fake, state, "POST", f"{thread}/runs", conflict)
    assert code == 409 and not effect.landed
    assert call(fake, state, "GET", f"{thread}/runs/{run['run_id']}/join")[1] == {"n": 5}
    assert call(fake, state, "GET", f"{thread}/state")[1]["values"] == {"n": 5}
    cron = {"assistant_id": "agent", "schedule": "0 1 * * *"}
    code, created, effect = call(fake, state, "POST", f"{thread}/runs/crons", cron)
    assert code == 200 and effect.landed and created["thread_id"] == ident
    resource = f"/runs/crons/{created['cron_id']}"
    assert call(fake, state, "PATCH", resource, {"enabled": False})[1]["enabled"] is False
    assert call(fake, state, "POST", "/runs/crons/search", {"enabled": True})[1] == []
    assert call(fake, state, "DELETE", resource)[0] == 200
    assert call(fake, state, "GET", resource)[0] == 404


@pytest.mark.parametrize("fake", [GmailFake, GoogleCalendarFake, TelegramFake, DiscordFake,
                                  LinearFake, JiraFake, LangGraphFake])
def test_invariants_and_host_override(fake):
    provider = fake(hosts=["example.invalid"])
    assert provider.hosts == ("example.invalid",)
    assert provider.api_version
    assert {next(iter(inv.ops)) for inv in provider.invariants()} == set(provider.once)
