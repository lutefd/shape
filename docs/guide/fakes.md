# Provider fakes (`shape.fakes`)

When an agent charges a card, sends an email, posts to Slack, or opens a GitHub issue, testing crash safety requires a test double that faithfully mirrors the provider's state machine, idempotency rules, and error shapes. A naive mock that always returns `200 OK` hides duplicate charges, ignores `Idempotency-Key` conflicts, and misses Slack's `200 OK` + `{"ok": false}` error responses.

`shape.fakes` ships stdlib-only provider test doubles built against the `HttpFake` and `MailFake` protocols used by `shape.net` (pass them to `@shape.scenario(net=[...])` or invoke them directly in tests). Every fake keeps its entire state in a JSON-serializable `dict` so state persists cleanly across crashed incarnations and concurrent actors.

## Capture an existing client

Register a fake on the scenario and call your client as usual. Shape captures the write, injects faults at the transport, and uses the fake's default invariants. The client code needs no `@shape.effect`:

```python
import requests

import shape
from shape.fakes import StripeFake


@shape.scenario(net=[StripeFake()])
def checkout(run):
    response = requests.post(
        "https://api.stripe.com/v1/charges",
        data={
            "amount": 500, "currency": "usd", "source": "tok_visa",
            "metadata[order_id]": "42",
        },
        headers={"Idempotency-Key": "order-42-charge"},
    )
    response.raise_for_status()
```

For a mail agent, use `net=[SmtpFake(), ImapFake(initial_messages=[sample_email_bytes])]` and point its existing clients at `smtp.example.com` and `imap.example.com`. If your application uses different hosts, set `hosts=` on each fake. Use a different host for each mail fake since their default `localhost` alias is shared. See [Network capture](../boundary.md#network-capture).

## Included providers

| Fake | Module | Default hosts | Mirrored API version | Default invariants |
| --- | --- | --- | --- | --- |
| `StripeFake` | `shape.fakes.stripe` | `api.stripe.com` | `2024-06-20` | `at_most_once("stripe.charge")`, `at_most_once("stripe.refund")`, `stripe_refunds_le_charges` |
| `SmtpFake` | `shape.fakes.smtp` | `smtp.example.com`, `localhost` | `RFC 5321 / RFC 5322` | `at_most_once("smtp.reply")`, `at_most_once("smtp.send")` |
| `ImapFake` | `shape.fakes.imap` | `imap.example.com`, `localhost` | `RFC 3501 (IMAP4rev1)` | none (mailbox reads/flags/expunge) |
| `SlackFake` | `shape.fakes.slack` | `slack.com`, `api.slack.com` | `Slack Web API (2024)` | `at_most_once("slack.chat.postMessage")` |
| `GitHubFake` | `shape.fakes.github` | `api.github.com` | `2022-11-28` | `at_most_once("github.issue.create")` |
| `SendGridFake` | `shape.fakes.sendgrid` | `api.sendgrid.com` | `v3` | `at_most_once("sendgrid.mail.send")` |
| `TwilioFake` | `shape.fakes.twilio` | `api.twilio.com` | `2010-04-01` | `at_most_once("twilio.message.create")` |
| `GmailFake` | `shape.fakes.gmail` | `gmail.googleapis.com` | Gmail API v1 | `at_most_once("gmail.messages.send")`, `at_most_once("gmail.labels.create")` |
| `GoogleCalendarFake` | `shape.fakes.google_calendar` | `www.googleapis.com`, `calendar.googleapis.com` | Google Calendar API v3 | `at_most_once("calendar.events.insert")` |
| `TelegramFake` | `shape.fakes.telegram` | `api.telegram.org` | Telegram Bot API (HTTP) | `at_most_once("telegram.sendMessage")` |
| `DiscordFake` | `shape.fakes.discord` | `discord.com`, `discordapp.com` | Discord REST v10 | `at_most_once("discord.message.create")` |
| `LinearFake` | `shape.fakes.linear` | `api.linear.app` | Linear GraphQL (unversioned) | `at_most_once("linear.issueCreate")`, `at_most_once("linear.commentCreate")` |
| `JiraFake` | `shape.fakes.jira` | `jira.example.com` | Jira Cloud REST v3 | `at_most_once("jira.issue.create")`, `at_most_once("jira.comment.create")` |
| `LangGraphFake` | `shape.fakes.langgraph` | `langgraph.example.com`, `localhost`, `127.0.0.1` | LangSmith Agent Server OpenAPI 0.1.0 | `at_most_once("langgraph.threads.create")`, `at_most_once("langgraph.runs.create")`, `at_most_once("langgraph.crons.create")` |

## Provider behavior

Each fake intentionally models only the semantics safety invariants depend on: resource state transitions, idempotency behavior, error status codes and payload shapes, and write visibility. None of them is a full API clone.

### `StripeFake` (`2024-06-20`)

- **Models:**
  - `POST/GET /v1/payment_intents`, `POST /v1/payment_intents/{id}/confirm`, `POST /v1/payment_intents/{id}/cancel`
  - `POST/GET /v1/charges`
  - `POST/GET /v1/refunds` (full or partial; rejects refunding more than the unrefunded remainder with `400 amount_too_large` and rejects refunding a fully refunded charge with `400 charge_already_refunded`)
  - `POST/GET /v1/customers`
  - Form-encoded bodies (including bracketed keys like `metadata[order_id]=42`) and JSON bodies
  - Exact `Idempotency-Key` semantics on `POST`: same key + same parameters replays the original response (including errors) with `Idempotent-Replayed: true`; same key + different parameters returns `400 idempotency_error` without overwriting the saved result; keys expire after 24 hours (`idempotency_ttl=86400.0`) when a virtual `clock` callable is passed, and never expire when `clock=None`
  - Stripe error envelopes (`{"error": {"type", "message", "code", "param", "decline_code"}}`) and test decline tokens (`pm_card_chargeDeclined`, `tok_chargeDeclined` -> `402 card_error`)
- **Does not model:** webhooks, 3D Secure / `requires_action` flows, disputes, subscriptions, Connect accounts, pagination cursors, or real card validation.

### `SmtpFake` (`RFC 5321 / RFC 5322`) and `ImapFake` (`RFC 3501`)

- **`SmtpFake` models:**
  - `send(from_addr, to_addrs, message, state)` returning `{refused_addr: (code, reason_bytes)}` (empty dict when all recipients succeed, matching `smtplib.SMTP.sendmail`)
  - Parsing `Message-ID`, `To`, `Subject`, `In-Reply-To`, and `References` headers; non-idempotent sends (retrying sends a duplicate email)
  - Configurable `reject_recipients` returning `550` refusals; `effect()` reports `landed=False` when every recipient is refused
  - Default scope for replies (`In-Reply-To` present) is `thread:<thread>|in_reply_to:<in_reply_to>` with op `smtp.reply`, enforcing at most one reply per `(thread, In-Reply-To)`
  - Default scope for fresh sends (no `In-Reply-To`) is Message-ID-less `to:<recipients>|subject:<norm_subject>` with op `smtp.send`, enforcing `at_most_once("smtp.send")` (with a default-scope warning unless you pass `scope=`)
  - Parsed headers passed to `scope(req, parsed)` include `headers` (lowercased header dict, e.g. `parsed["headers"]["x-job-id"]`), `subject`, `norm_subject`, `to_header`, `message_id`, `in_reply_to`, `references`, and `thread`
- **`ImapFake` models:**
  - `SELECT` / `EXAMINE`, `SEARCH` (`ALL`, `SEEN`, `UNSEEN`, `FLAGGED`, `DELETED`, `ANSWERED`, `FROM`, `TO`, `SUBJECT`, `HEADER`), `FETCH` (`RFC822`, `BODY[]`, `BODY.PEEK[]`, `BODY[HEADER]`, `FLAGS`, `UID`), `STORE` (`+FLAGS`, `-FLAGS`, `FLAGS`, `.SILENT`), `APPEND`, `EXPUNGE`, `UID` variants, plus `LOGIN`, `LIST`, `CREATE`, `CLOSE`, `NOOP`, `LOGOUT`
  - Persistent UIDs and flags (`\Seen` set implicitly on non-`PEEK` body fetches or explicitly via `STORE`, surviving across crashed processes via JSON `state`)
- **Does not model:** STARTTLS/AUTH negotiation, MIME multipart encoding transforms, IDLE push notifications, COPY/MOVE, or real TCP wire framing.

### `SlackFake` (`Slack Web API (2024)`)

- **Models:**
  - `POST /api/chat.postMessage` (non-idempotent, generates monotonic `ts`), `POST /api/chat.update` (updates in place or returns `message_not_found`), and `GET`/`POST /api/conversations.history` (newest-first, `limit`, `oldest`, `latest`)
  - Slack's HTTP `200 OK` + `{"ok": false, "error": "..."}` convention (`channel_not_found`, `no_text`, `message_not_found`, `invalid_auth`, `unknown_method`); `effect()` inspects `ok` so rejected writes always report `landed=False`
  - Optional `channels` allowlist and `valid_tokens` check
- **Does not model:** Socket Mode, Events API webhooks, Block Kit schema validation, reactions, file uploads, or rate-limit headers (`shape` injects `rate_limit` faults itself).

### `GitHubFake` (`2022-11-28`)

- **Models:**
  - `POST/GET /repos/{owner}/{repo}/issues`, `GET/PATCH /repos/{owner}/{repo}/issues/{number}` (close/reopen/edit), `POST/GET /repos/{owner}/{repo}/issues/{number}/comments`
  - Non-idempotent issue and comment creation (retrying after a lost ack allocates `#2`, `#3`, ...)
  - GitHub `404 Not Found` and `422 Validation Failed` error bodies plus `X-GitHub-Api-Version: 2022-11-28`
  - Optional `repos` allowlist
- **Does not model:** pull requests, GraphQL, issue search, webhooks, fine-grained token scopes, or pagination links.

### `SendGridFake` (`v3`)

- **Models:**
  - `POST /v3/mail/send` returning `202 Accepted` with empty body and `X-Message-Id` header. Sandbox validation (`mail_settings.sandbox_mode.enable` set to true) returns `200 OK` with no send effect.
  - Validation of `personalizations[].to[].email`, `from.email`, `subject`, `content` (or `template_id`) with SendGrid `400 {"errors": [...]}` bodies, plus optional `valid_api_keys` (`401`)
  - Non-idempotent sends (every accepted POST records a new email)
- **Does not model:** scheduled sends, batch IDs, dynamic template rendering, suppression lists, or bounce webhooks.

### `TwilioFake` (`2010-04-01`)

- **Models:**
  - `POST /2010-04-01/Accounts/{AccountSid}/Messages.json` (form-encoded or JSON) returning `201 Created` with deterministic 34-char `SM...` SIDs; non-idempotent
  - `GET /2010-04-01/Accounts/{AccountSid}/Messages.json` (with `To`/`From` filters) and `GET .../Messages/{Sid}.json`
  - Twilio error payloads (`code`, `message`, `more_info`, `status`) for missing `To` (`21604`), invalid `To` (`21211`), missing `From`/`MessagingServiceSid` (`21603`), missing `Body`/`MediaUrl` (`21602`), and not found (`20404`)
- With `test_mode=True`, creates return ephemeral test SIDs and reads return `403` / `20008`.
- **Does not model:** delivery status callbacks, carrier segment splitting, MMS media downloads, or Voice/Verify resources.

### `GmailFake` (Gmail API v1)

- Send base64url-encoded RFC 5322 mail. A retry can send a second message even with the same Message-ID or generic idempotency header. Supply `initial_messages=[raw_rfc5322_bytes]` to seed the mailbox. Reads support message and thread lists, API formats, label filters, and basic `q` terms. Thread replies use a thread ID, matching subject, and reply headers.
- Modify message or thread labels, create/read/update/delete user labels, and list changes after a history ID. A thread label edit affects its current messages. History IDs are monotonic and retained for the scenario. MIME uploads, delivery to other mailboxes, and the full Gmail search grammar are outside this fake. [Gmail API reference](https://developers.google.com/workspace/gmail/api/reference/rest), [Gmail threads](https://developers.google.com/workspace/gmail/api/guides/threads).

### `GoogleCalendarFake` (Google Calendar API v3)

- Insert, read, list, patch, and delete events by calendar. A caller-supplied event ID returns a duplicate conflict on reuse. PATCH updates supplied fields, replaces arrays, and checks `If-Match`. Deleted events are kept as cancelled entries. Recurrence expansion, guest notifications, and sync tokens are outside this fake. [Events reference](https://developers.google.com/workspace/calendar/api/v3/reference/events), [Resource versions](https://developers.google.com/workspace/calendar/api/guides/version-resources).

### `TelegramFake` (Telegram Bot API)

- Send and edit text messages in numeric chats and acknowledge chat actions. Retries of `sendMessage` create distinct messages. Repeating the same text edit returns Telegram's 400 envelope. Seed `initial_updates=[...]` to test redelivery, positive offset confirmation, and negative offset truncation. Polling returns immediately, and bot sends generate no incoming updates. [Telegram Bot API](https://core.telegram.org/bots/api).

### `DiscordFake` (Discord REST v10)

- Create, retrieve, and delete channel messages. With `enforce_nonce=true`, a repeated nonce for the same author returns the existing message and commits no new send. A `clock=` callable enables expiration after `nonce_ttl` seconds, which defaults to 300. A run with no clock keeps nonces for the duration of the scenario. This fake covers JSON message requests. [Discord message API](https://discord.com/developers/docs/resources/message#create-message).

### `LinearFake` (GraphQL)

- Execute `issueCreate`, `commentCreate`, and single issue/comment lookups. Specify inputs with GraphQL variables or inline arguments. The fake supports nested field selections and aliases with one root field per operation. Mutations create a new resource on retry. It represents rejected resolvers with a GraphQL error envelope, including errors returned with HTTP 200. [Linear GraphQL](https://linear.app/developers/graphql), [Linear errors](https://linear.app/developers/sdk-errors).

### `JiraFake` (Jira Cloud REST v3)

- Create or retrieve an issue, create or retrieve Atlassian Document Format comments, and list or apply transitions. Pass `hosts=` for your own Jira hostname, `projects=` to map project keys to IDs, and `transitions=` to map workflow transition IDs to status names. The supplied transitions are available on each issue. Issue and comment creates can duplicate on retry. [Jira issues](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/), [Jira comments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/).

### `LangGraphFake` (LangSmith Agent Server OpenAPI 0.1.0)

- Create and read threads and launch background, streaming, or wait-for-output runs. Creating a thread with a stable ID and `if_exists="do_nothing"` returns the saved thread on retry. Run creation can duplicate. Stream replies use server-sent events. Runs resolve on stream, join, or wait and return their input by default. Pass `run_result=` for a scripted output.
- Create thread-bound or standalone cron definitions, search, patch, and delete them. The fake stores definitions for inspection. Graph execution, crontab scheduling, and webhooks are outside this fake. [Agent Server API reference](https://docs.langchain.com/langsmith/server-api-ref), [OpenAPI schema](https://github.com/langchain-ai/docs/blob/main/src/langsmith/agent-server-openapi.json).

## Seeding a fake

Every fake takes `initial_state=`: a JSON dict merged into the fake's provider state before the first call of each seed, so a scenario can start with customers, channels or threads that already exist. Its keys are the fake's own state keys, the JSON it keeps between calls. The agent's changes persist across incarnations; the seed is applied once. For setup that should go through the fake's API instead, use [`@shape.scenario(setup=...)`](world-fixtures.md#fault-free-setup), which makes the calls with no faults and logs them as setup.

```python
slack = shape.fakes.SlackFake(initial_state={"channels": {"C1": []}})
threads = shape.fakes.LangGraphFake(threads=[{"metadata": {"user": "u1"}, "values": {"messages": []}}])
```

`LangGraphFake(threads=[...])` takes dicts with an optional `thread_id` (a UUID; one is derived from the position otherwise), `metadata`, `values` and `status`.

## Contract tests

```sh
shape fakes contract stripe
shape fakes contract stripe --live --report contract-stripe.json
```

The suite runs documented requests against the fake by default. With `--live`, it sends the same script to the configured test service. It compares HTTP status, provider error codes, selected response field types, and whether an immediate retry of the write returns the same resource, conflicts, or creates a duplicate. The script always retries the initial write to model a caller that lost its reply. Separate scenario tests exercise real transport clients with injected loss after commit.

SendGrid's sandbox validates without sending. Twilio test credentials return an ephemeral SID and reject reads. Telegram's Bot API can check a committed message through an edit. The Gmail suite sends only to the configured test mailbox. GitHub, Slack, Telegram, Discord, Gmail, Calendar, Linear, Jira, and LangGraph use dedicated scratch resources. The suite attempts to clean up its writes after each run.

The pytest helper `from shape.fakes.contracts import assert_contract` takes the same provider name and `live=True` when desired. Missing environment variables produce a skip. Mismatches return a failing CLI exit code and pytest assertion. JSON reports contain selected response field types and comparisons.

<!-- provider-verification:start -->
| Provider | Verification | Required environment variables |
| --- | --- | --- |
| `discord` | unverified | `DISCORD_BOT_TOKEN`, `DISCORD_TEST_CHANNEL` |
| `github` | unverified | `GITHUB_TOKEN`, `GITHUB_SCRATCH_REPO` |
| `gmail` | unverified | `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`, `GMAIL_TEST_EMAIL` |
| `google-calendar` | unverified | `CALENDAR_CLIENT_ID`, `CALENDAR_CLIENT_SECRET`, `CALENDAR_REFRESH_TOKEN`, `CALENDAR_TEST_ID` |
| `imap` | unverified | — |
| `jira` | unverified | `JIRA_TEST_SITE`, `JIRA_TEST_EMAIL`, `JIRA_TEST_API_TOKEN`, `JIRA_TEST_PROJECT`, `JIRA_TEST_ISSUE_TYPE`, `JIRA_TEST_TRANSITION` |
| `langgraph` | unverified | `LANGGRAPH_TEST_URL`, `LANGGRAPH_TEST_ASSISTANT_ID` |
| `linear` | unverified | `LINEAR_API_KEY`, `LINEAR_TEST_TEAM_ID` |
| `sendgrid` | unverified | `SENDGRID_API_KEY`, `SENDGRID_TEST_FROM` |
| `slack` | unverified | `SLACK_BOT_TOKEN`, `SLACK_TEST_CHANNEL` |
| `smtp` | unverified | — |
| `stripe` | unverified | `STRIPE_TEST_KEY` |
| `telegram` | unverified | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_TEST_CHAT_ID` |
| `twilio` | unverified | `TWILIO_TEST_ACCOUNT_SID`, `TWILIO_TEST_AUTH_TOKEN` |
<!-- provider-verification:end -->

Use the same names for scheduled GitHub Actions secrets, except GitHub's suite takes secrets `CONTRACT_GITHUB_TOKEN` and `CONTRACT_GITHUB_SCRATCH_REPO`. The workflow maps them to `GITHUB_TOKEN` and `GITHUB_SCRATCH_REPO`. `LANGGRAPH_TEST_API_KEY` is an optional variable and secret for authenticated Agent Servers. `LANGGRAPH_TEST_URL` accepts an HTTPS deployment or an HTTP localhost URL when running locally.

- **GitHub:** fine-grained token with Issues read/write access to the scratch repository. Set `GITHUB_SCRATCH_REPO` to `owner/repo`.
- **Slack:** bot in the test channel with `chat:write` and the channel's history scope. **Telegram:** a numeric test chat that has started the bot or admitted it. **Discord:** a bot with permission to send, read and delete its own messages in the specified test channel.
- **SendGrid:** Mail Send permission. **Twilio:** legacy test Account SID and Auth Token.
- **Gmail:** OAuth refresh token for the test mailbox with `https://www.googleapis.com/auth/gmail.modify`. **Calendar:** test calendar ID with an OAuth refresh token authorized to write its events.
- **Linear:** scratch team UUID and a personal API key with issue create, read, comment, and archive access. **Jira:** Atlassian site hostname such as `shape-test.atlassian.net`, account email and API token, scratch project key, issue type ID, and an available transition ID. **LangGraph:** use a test assistant that accepts a JSON object containing a string `shape` field and returns an object.

The packaged [contract results](../../src/shape/fakes/contract_results.json) are the source for each fake's `verified_on` and `verification_status` attributes and the table above. After a successful live comparison, copy its JSON report into the corresponding provider entry and run `python scripts/sync_fake_verification.py`. The scheduled workflow uploads reports for review.

## Configuring scopes

Invariants such as `at_most_once` group effects by `scope`. Every fake has a default scope heuristic so quick prototypes work out of the box, **but default heuristics can merge two legitimate jobs** (for example, two separate jobs sending emails with the same recipient and subject under SendGrid or SMTP's `to|subject` rule).

After every sweep, `shape` prints a `scope assignment:` section showing how each op's scope was derived and warns when a default rule is used by a duplicate-sensitive invariant:

```text
scope assignment:
  sendgrid.mail.send: fake default rule `to|subject` (e.g. 'to:alice@example.com|subject:Daily report')
  warning: sendgrid.mail.send uses a default scope rule with at_most_once(sendgrid.mail.send); pass scope= to the fake or effect (or run with --strict-scopes)
```

In CI, pass `--strict-scopes` (or `@shape.scenario(..., strict_scopes=True)`) so duplicate-sensitive invariants refuse default rules and require an explicit business `scope=` on every fake:

```python
from shape.fakes import GitHubFake, SendGridFake, SlackFake, SmtpFake, StripeFake, TwilioFake

# SendGrid: bind scope to your business job/order ID in custom_args
sendgrid = SendGridFake(
    scope=lambda req, params: f"job:{params['custom_args']['job_id']}"
)

# SMTP: bind fresh sends (or replies) to a custom header such as X-Job-Id
smtp = SmtpFake(
    scope=lambda req, parsed: f"job:{parsed['headers']['x-job-id']}"
)

# Stripe: read scope from a specific metadata key, or pass a custom function
stripe = StripeFake(scope_metadata_key="order_id")
stripe_custom = StripeFake(
    scope=lambda req, params: f"order:{params.get('metadata', {}).get('order_id', 'unknown')}"
)

# Slack: one alert per incident ID in message metadata or text
slack = SlackFake(
    channels={"C_ALERTS"},
    scope=lambda req, params: f"incident:{params.get('metadata', {}).get('event_payload', {}).get('incident_id', '')}",
)

# GitHub: default scope is "{owner}/{repo}:title:{title}", or derive from your own key
github = GitHubFake(
    repos={"acme/backend"},
    scope=lambda req, params: f"ticket:{params.get('title', '').split(':')[0]}",
)

# Twilio: scope per job or recipient
twilio = TwilioFake(
    scope=lambda req, params: f"sms:{params.get('To', '')}"
)
```

For `StripeFake`, if your scenario tests refunds against a charge that already existed before the run, pass `initial_charges={"order:42": 5000}` so the `stripe_refunds_le_charges` invariant knows the pre-existing charged amount for that scope.

For `StripeFake` idempotency key expiration under virtual time, pass `clock=my_clock` (a `() -> float` returning seconds):

```python
now = [1_700_000_000.0]
stripe = StripeFake(clock=lambda: now[0])
```

## Writing your own fake

Implement the `HttpFake` (or `MailFake`) protocol from `shape.fakes.base`. Keep all mutable state inside the `state: dict` argument using plain JSON types (dicts, lists, strings, ints, floats, bools, `None`), never instance attributes, so state persists across `os._exit(137)` restarts and concurrent processes.

```python
from shape.boundary.invariants import at_most_once
from shape.fakes.base import EffectInfo, Request, Response
import json


class PagerDutyFake:
    hosts = ("events.pagerduty.com",)
    api_version = "Events API v2"

    def describe(self, req: Request) -> EffectInfo | None:
        if req.method.upper() != "POST" or not req.url.endswith("/v2/enqueue"):
            return None
        body = req.json or {}
        return EffectInfo(
            op="pagerduty.enqueue",
            scope=f"routing:{body.get('routing_key', '')}",
            key=body.get("dedup_key"),
            irreversible=True,
            landed=False,
        )

    def handle(self, req: Request, state: dict) -> Response:
        incidents = state.setdefault("incidents", {})
        body = req.json if isinstance(req.json, dict) else json.loads(req.body or b"{}")
        if not body.get("routing_key"):
            return Response(400, {"Content-Type": "application/json"}, b'{"message":"Invalid event"}')
        dedup = body.get("dedup_key") or f"pd_{len(incidents) + 1}"
        incidents[dedup] = body
        payload = json.dumps({"status": "success", "dedup_key": dedup}).encode()
        return Response(202, {"Content-Type": "application/json"}, payload)

    def effect(self, req: Request, resp: Response) -> EffectInfo | None:
        info = self.describe(req)
        if info is None:
            return None
        return EffectInfo(
            op=info.op,
            scope=info.scope,
            key=info.key,
            irreversible=info.irreversible,
            landed=resp.status == 202,
        )

    def invariants(self) -> list:
        return [at_most_once("pagerduty.enqueue")]
```

Key rules for custom fakes:

1. **Return `None` from `effect()` (and `describe()`) for reads.** Shape captures standard read methods as scheduling points and leaves them out of effect and attempt counts.
2. **Return `landed=False` from `effect()` for rejected writes.** Any 4xx/5xx error (or Slack-style `ok: false`) must set `landed=False` so `shape` does not log a landed effect or inject after-commit faults.
3. **Never use `random`, `uuid4()`, or wall-clock `time.time()` inside `handle()`.** Use counters stored in `state` and an injected `clock` so replays and shrinking remain deterministic.
4. **Implement optional `describe(req)`** when you want domain-named fault keys before fault injection; if omitted, `shape.net` falls back to `METHOD host/path` keys and still uses `effect(req, resp)` for the world log.

For how invariants evaluate the world log produced by effects, see [Invariants](invariants.md) and [Boundary mode reference](../boundary.md).
