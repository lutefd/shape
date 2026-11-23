# Testing OpenAI Agents SDK agents

`shape.agents` tests real OpenAI Agents SDK agents (`@function_tool`, `Agent`, `Runner.run`, `SQLiteSession`, and `needs_approval=True` interruptions) locally with no API key and no network calls, without editing your agent or tool bodies.

For the concise adapter reference, see [docs/adapters.md](../adapters.md).

## Install

```sh
pip install "agent-shape[agents] @ git+https://github.com/lutefd/shape"
```

At release on PyPI this becomes `pip install 'agent-shape[agents]'`.

To run the examples in this repository:

```sh
python3 -m venv examples/openai_agents/.venv
examples/openai_agents/.venv/bin/pip install -e . -r examples/openai_agents/requirements.txt
```

## 1. Wrap `FunctionTool`s with `shape.agents.effects`

Pass your agent's tools and an `ops` dict naming the tools that perform side effects. Read-only tools pass through untouched:

```python
tools = shape.agents.effects(
    build_tools(approval=approval),
    ops={
        "issue_refund": {
            "op": "payments.refund",
            "scope": lambda order_id, **_: f"order:{order_id}",
            "key": lambda order_id, **_: refund_key(order_id) if keyed else None,
        }
    },
)
```

Each entry in `ops` accepts an op string, a dict with any `@shape.effect` option (`op`, `scope`, `key`, `key_from_result`, `landed`, `irreversible`, `errors`, `idempotent`, `result`, `hide`), or a `shape.agents.Effect(...)` dataclass.

### What `shape.agents.effects` passes to hooks

`shape.agents.effects` wraps `FunctionTool.on_invoke_tool` and passes both the model's parsed JSON arguments and the Agents SDK `ToolContext` (a `RunContextWrapper`, so `ctx.context` holds your run context state) into `scope`, `key`, `result`, `landed`, and `key_from_result`:

- Access the `ToolContext` via `ctx`, `context`, or your `@function_tool`'s own leading context parameter name (for example `wrapper`). It is hidden automatically from rendered calls, world log `args`, and `approved_before` hashes.
- If any hook raises, `shape` raises `shape.HarnessError` (`BaseException`) so the SDK's failure-handling tool invoker never swallows it into `"An error occurred while running the tool."` — the run fails immediately pointing at your lambda.
- If a tool checks agent state first and returns early or dedupes via state, prefer placing `@shape.effect` on the underlying API client method, or pass `landed=` / `key_from_result=` in `ops`.

## 2. Perturb the model with `ScriptedModel`

`shape.agents.ScriptedModel` implements the Agents SDK `Model` interface and turns a plain plan into deterministic, shrinkable `shape.choose` perturbations:

```python
PLAN = [
    ("lookup_order", {"order_id": "17"}),
    ("issue_refund", {"order_id": "17", "amount_cents": 4200}),
]
model = shape.agents.ScriptedModel(
    PLAN,
    final="Order 17 is refunded.",
    alternatives={"order_id": ["18"]},
)
agent = build_agent(model, tools=tools)
```

On nominal runs (all choices `0`), `ScriptedModel` emits each planned tool call in order and returns `final`. On seeded runs it tests repeat, drop, swap, argument substitution (`alternatives`), retry count after tool errors (`retry_1`, `retry_0`, `retry_2`), and post-restart re-issue.

**Tool error detection:** `ScriptedModel` treats a tool output as an error when it starts with `"An error occurred while running the tool."` (the Agents SDK's default `default_tool_error_function`) or `"Error:"`, and as a rejection when it contains `"not approved"`. `shape.agents.effects` also auto-detects `"An error occurred while running the tool."` strings as not landed without needing `landed=`. If a tool catches exceptions internally and returns a custom refusal string or dict such as `{"ok": False}`, pass `landed=lambda res: ...` in `ops` so `shape` logs it as `returned without effect`.

## 3. Drive the agent with `shape.agents.drive`

```python
shape.agents.drive(
    agent,
    "Please refund order 17.",
    run,
    context=ctx,
    session_factory=lambda d: SQLiteSession(TICKET, str(d / "session.db")),
    human=_approve if approval else None,
    on_restart="redeliver",
)
```

What `shape.agents.drive` handles for you:

- **Distinct session per job by default:** Each `drive()` call in a scenario opens its own `SQLiteSession("shape-job-<n>", str(run.dir / "session.db"))` (`shape-job-0`, `shape-job-1`, ...), so running two independent jobs in one scenario never shares conversation history or causes the second job to skip its tool call. To share a session deliberately across turns (for example a multi-turn conversation), pass the same `session_id="conv-1"` to each `drive()` call (or supply `session_factory=`).
- **Explicit restart policy (`on_restart`):** The OpenAI Agents SDK does not checkpoint an in-flight tool call inside `Runner.run`, so `drive` does not invent supervisor recovery you did not configure:
  - `on_restart="none"` (**default**): on a restarted incarnation (`run.restarts > 0`), `drive` does nothing and returns `None`, matching an app with no supervisor.
  - `on_restart="redeliver"`: models an at-least-once queue or supervisor. On restart, `drive` logs `harness redelivered the request (on_restart='redeliver')` in the trace and reloads any saved approval `RunState` or resends `input` into the same session.
  - `on_restart=callable`: calls `fn(run)` (sync or async) on restart so you can model your app's exact recovery logic.
- **Human-in-the-loop (`needs_approval=True`):** When `Runner.run` pauses with `result.interruptions`, `drive` saves `result.to_state()` under `run.dir` (defaulting `context_serializer` to `lambda _: {}` when `context` is supplied, and reloading with `context_override=context`), calls `human(item)` for each interruption (`True` calls `state.approve(item)`, `False` calls `state.reject(item)`), and deletes the saved state file once the run completes with no remaining interruptions. With `on_restart="redeliver"`, a restarted incarnation reloads that saved `RunState`.
- **No network or API key:** `drive` passes `RunConfig(tracing_disabled=True)` so local runs do not export traces.

### Worker scenarios vs. interactive chat agents

In [`examples/openai_agents/checks.py`](../../examples/openai_agents/checks.py), the scenario models a support job worker behind an at-least-once queue (`on_restart="redeliver"` plus `if not payments.has_refund("17"): raise TicketUnresolved(...)`), so a crashed or unresolved attempt triggers a restart and `exactly_once("payments.refund")` holds. If your agent is an interactive chat bot with no supervisor that may legitimately reply "I could not complete your refund" after repeated provider errors, leave `on_restart="none"` and use `shape.at_most_once("payments.refund")` and `shape.claims_true()` instead of `exactly_once` (see [Invariants](invariants.md#when-to-use-claims_true-instead-of-exactly_once)).

## 4. Real findings in `examples/openai_agents/` and their fixes

Run from `examples/openai_agents/`:

```sh
cd examples/openai_agents
.venv/bin/shape test checks.py::refund_unkeyed --seeds 300 --jobs 4
.venv/bin/shape test checks.py::refund_keyed --seeds 300 --jobs 4
```

### Finding 1: Unkeyed refund double-refunds under model repeat, reply loss, and mid-tool crashes (`refund_unkeyed`)

`refund_unkeyed` fails 52 of 300 seeds across 3 distinct causes, all violating `at_most_once(payments.refund)`:

1. **Model choice (33 of 52 failing seeds, 0 faults):** The model repeats `issue_refund` twice in a row.
2. **Reply lost (14 of 52 failing seeds — `connection_reset_after_commit` and `timeout_after_commit`):** The refund lands, the reply is lost so the SDK hands `"An error occurred while running the tool."` to the model, and the same process retries without a key.
3. **Crash after commit (5 of 52 failing seeds — `crash_after`):** The process dies mid-tool before `SQLiteSession` saves the turn; on restart, the trace explicitly shows `harness redelivered the request (on_restart='redeliver')` before the second refund lands.

Real output:

```text
scenario=refund_unkeyed  seeds=300 (from 0)  failing=52  wall=8.06s  37 seeds/s
coverage over 300 seeds:
  op                landed  attempts
  payments.refund      362       427
                   faults injected: client_error 10, connection_reset_after_commit 13, crash_after 18, crash_before 11, rate_limit 16, server_error 11, timeout 17, timeout_after_commit 13
  model choices taken: after restart: call 1: lookup_order -> 'reissue' x5, call 1: lookup_order -> 'drop' x26, call 1: lookup_order -> 'repeat' x22, call 1: lookup_order -> 'swap' x20, call 2: issue_refund -> 'drop' x24, call 2: issue_refund -> 'repeat' x35, error at call 2: issue_refund -> 'retry_0' x5, error at call 2: issue_refund -> 'retry_2' x3
  interleaving choices taken: none
scope assignment:
  payments.refund: explicit scope= (e.g. 'order:17')
3 distinct causes across 52 failing seeds

cause 1 of 3: 33 of 52 failing seeds (4, 13, 20, 42, 43, ...)
  needs: model choice
FAIL checks.py::refund_unkeyed  seed=4  invariant=at_most_once(payments.refund)
  payments.refund happened 2 times for scope 'order:17' (at most 1 allowed)
  minimized to 3 steps (1 of 1 deviations kept, 2 replays):
    incarnation 0
      1. model chose 'repeat' at 'call 2: issue_refund' (nominal: issue_refund(order_id='17', amount_cents=4200))
      2. issue_refund(order_id='17', amount_cents=4200)
           effect landed
      3. issue_refund(order_id='17', amount_cents=4200)
           effect landed
      finished
  replay: runs/refund_unkeyed/seed-4.min.json

cause 2 of 3: 14 of 52 failing seeds (7, 15, 23, 63, 67, ...)
  needs: reply lost (commit landed, caller saw an error: connection_reset_after_commit, timeout_after_commit)
FAIL checks.py::refund_unkeyed  seed=7  invariant=at_most_once(payments.refund)
  payments.refund happened 2 times for scope 'order:17' (at most 1 allowed)
  minimized to 2 steps (1 of 1 deviations kept, 2 replays):
    incarnation 0
      1. issue_refund(order_id='17', amount_cents=4200)
           effect landed, then connection reset after commit: the caller saw ConnectionResetError
      2. issue_refund(order_id='17', amount_cents=4200)
           effect landed
      finished
  replay: runs/refund_unkeyed/seed-7.min.json

cause 3 of 3: 5 of 52 failing seeds (41, 65, 186, 271, 287)
  needs: crash after commit (process died after the commit: crash_after)
FAIL checks.py::refund_unkeyed  seed=41  invariant=at_most_once(payments.refund)
  payments.refund happened 2 times for scope 'order:17' (at most 1 allowed)
  minimized to 2 steps (1 of 2 deviations kept, 5 replays):
    incarnation 0
      1. issue_refund(order_id='17', amount_cents=4200)
           effect landed, then process killed after the effect landed, before the caller saw it
      -> process died
    incarnation 1 (restarted)
      harness redelivered the request (on_restart='redeliver')
      2. issue_refund(order_id='17', amount_cents=4200)
           effect landed
      finished
  replay: runs/refund_unkeyed/seed-41.min.json
```

**Fix (`refund_keyed`):** Pass a stable idempotency key derived from durable business facts (`f"{TICKET}:refund:{order_id}"`, not from a per-call `tool_call_id`). All 300 seeds pass (`failing=0`).

### Finding 2: Approval alone does not prevent duplicate refunds (`refund_approval_unkeyed`)

With `needs_approval=True`, `approved_before(payments.refund)` holds across every seed, but `refund_approval_unkeyed` still fails `at_most_once(payments.refund)` on 54 of 300 seeds across distinct causes (model repeat, reply lost after commit, crash after commit reloading `paused-run.json`, and `crash_before` + post-restart model reissue).

**Fix (`refund_approval_keyed`):** Combine `needs_approval=True` with the stable idempotency key. All 300 seeds pass (`failing=0`).

### Finding 3: Model substitutes an unauthorized order ID (`refund_wrong_order_unguarded`)

With `alternatives={"order_id": ["18"]}`, `refund_wrong_order_unguarded` fails 20 of 300 seeds with zero injected faults:

```text
cause 1 of 1: 20 of 20 failing seeds (43, 50, 58, 113, 130, ...)
  needs: model choice
FAIL checks.py::refund_wrong_order_unguarded  seed=50  invariant=approved_before(payments.refund)
  issue_refund(order_id='18', amount_cents=4200) ran with no earlier approval for scope 'order:18'
  minimized to 3 steps (1 of 1 deviations kept, 3 replays):
    incarnation 0
      1. approval recorded for 'order:17'
      2. model chose "substitute order_id='18'" at 'call 2: issue_refund' (nominal: issue_refund(order_id='17', amount_cents=4200))
      3. issue_refund(order_id='18', amount_cents=4200)
           effect landed (idempotency key 'ticket-17:refund:18')
      finished
```

**Fix (`refund_wrong_order_guarded`):** Validate `order_id in ctx.context.allowed_orders` inside `issue_refund` before calling the payments client. All 300 seeds pass (`failing=0`).

### Replaying a checked-in trace

```sh
cd examples/openai_agents
.venv/bin/shape test checks.py::refund_unkeyed --replay traces/refund_unkeyed.crash_after.min.json
```

Real output:

```text
FAIL refund_unkeyed  invariant=at_most_once(payments.refund) (replayed)
  payments.refund happened 2 times for scope 'order:17' (at most 1 allowed)
  incarnation 0
    1. issue_refund(order_id='17', amount_cents=4200)
         effect landed, then process killed after the effect landed, before the caller saw it
    -> process died
  incarnation 1 (restarted)
    harness redelivered the request (on_restart='redeliver')
    2. issue_refund(order_id='17', amount_cents=4200)
         effect landed
    finished
```
