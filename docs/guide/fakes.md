# Provider fakes (`shape.fakes`)

When an agent charges a card, sends an email, posts to Slack, or opens a GitHub issue, testing crash safety requires a test double that faithfully mirrors the provider's state machine, idempotency rules, and error shapes. A naive mock that always returns `200 OK` hides duplicate charges, ignores `Idempotency-Key` conflicts, and misses Slack's `200 OK` + `{"ok": false}` error responses.

`shape.fakes` ships stdlib-only provider test doubles built against the `HttpFake` and `MailFake` protocols used by `shape.net` (pass them to `@shape.scenario(net=[...])` or invoke them directly in tests). Every fake keeps its entire state in a JSON-serializable `dict` so state persists cleanly across crashed incarnations and concurrent actors.

> **Verification status:** These fakes are written directly from each provider's published API documentation and tested against their documented semantics, error shapes, and idempotency rules. **They are not yet verified against live provider sandboxes.** Automated contract tests that run the same suite against both each fake and the real provider sandbox are planned work (see [Core simulation design](../design.md#fakes)).

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
| `SmtpFake` | `shape.fakes.smtp` | `smtp.example.com`, `localhost` | `RFC 5321 / RFC 5322` | `at_most_once("smtp.reply")` |
| `ImapFake` | `shape.fakes.imap` | `imap.example.com`, `localhost` | `RFC 3501 (IMAP4rev1)` | none (mailbox reads/flags/expunge) |
| `SlackFake` | `shape.fakes.slack` | `slack.com`, `api.slack.com` | `Slack Web API (2024)` | `at_most_once("slack.chat.postMessage")` |
| `GitHubFake` | `shape.fakes.github` | `api.github.com` | `2022-11-28` | `at_most_once("github.issue.create")` |
| `SendGridFake` | `shape.fakes.sendgrid` | `api.sendgrid.com` | `v3` | `at_most_once("sendgrid.mail.send")` |
| `TwilioFake` | `shape.fakes.twilio` | `api.twilio.com` | `2010-04-01` | `at_most_once("twilio.message.create")` |

## What each fake models (and does not)

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
  - `POST /v3/mail/send` returning `202 Accepted` with empty body and `X-Message-Id` header
  - Validation of `personalizations[].to[].email`, `from.email`, `subject`, `content` (or `template_id`) with SendGrid `400 {"errors": [...]}` bodies, plus optional `valid_api_keys` (`401`)
  - Non-idempotent sends (every accepted POST records a new email)
- **Does not model:** scheduled sends, batch IDs, dynamic template rendering, suppression lists, or bounce webhooks.

### `TwilioFake` (`2010-04-01`)

- **Models:**
  - `POST /2010-04-01/Accounts/{AccountSid}/Messages.json` (form-encoded or JSON) returning `201 Created` with deterministic 34-char `SM...` SIDs; non-idempotent
  - `GET /2010-04-01/Accounts/{AccountSid}/Messages.json` (with `To`/`From` filters) and `GET .../Messages/{Sid}.json`
  - Twilio error payloads (`code`, `message`, `more_info`, `status`) for missing `To` (`21604`), invalid `To` (`21211`), missing `From`/`MessagingServiceSid` (`21603`), missing `Body`/`MediaUrl` (`21602`), and not found (`20404`)
- **Does not model:** delivery status callbacks, carrier segment splitting, MMS media downloads, or Voice/Verify resources.

## Configuring scopes

Invariants such as `at_most_once` group effects by `scope`. Every fake has a sensible default scope, and every fake accepts a `scope=` callable so you can bind scopes to your own business entity (such as an order ID, ticket ID, or run step):

```python
from shape.fakes import GitHubFake, SendGridFake, SlackFake, StripeFake, TwilioFake

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

# SendGrid and Twilio: scope per order or recipient+template
sendgrid = SendGridFake(
    scope=lambda req, params: f"order:{(params.get('custom_args') or {}).get('order_id', '')}"
)
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

1. **Return `None` from `effect()` (and `describe()`) for reads.** Reads do not enter the world log.
2. **Return `landed=False` from `effect()` for rejected writes.** Any 4xx/5xx error (or Slack-style `ok: false`) must set `landed=False` so `shape` does not log a landed effect or inject after-commit faults.
3. **Never use `random`, `uuid4()`, or wall-clock `time.time()` inside `handle()`.** Use counters stored in `state` and an injected `clock` so replays and shrinking remain deterministic.
4. **Implement optional `describe(req)`** when you want domain-named fault keys before fault injection; if omitted, `shape.net` falls back to `METHOD host/path` keys and still uses `effect(req, resp)` for the world log.

For how invariants evaluate the world log produced by effects, see [Invariants](invariants.md) and [Boundary mode reference](../boundary.md).
