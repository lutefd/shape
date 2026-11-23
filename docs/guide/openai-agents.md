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

Each entry in `ops` accepts an op string, a dict with any `@shape.effect` option (`op`, `scope`, `key`, `irreversible`, `errors`, `idempotent`, `result`, `hide`), or a `shape.agents.Effect(...)` dataclass.

### What `shape.agents.effects` passes to `scope` and `key`

`shape.agents.effects` wraps `FunctionTool.on_invoke_tool`, parses the JSON argument string sent by the model, and passes only those parsed JSON keys to `scope` and `key`.

That has two practical consequences today:

- `scope` and `key` do **not** receive `ctx` (`RunContextWrapper`) or omitted Python default parameter values. If your key or scope depends on a ticket ID or tenant ID from `ctx.context`, capture it from the scenario via closure (as `examples/openai_agents/checks.py` does with `refund_key(order_id)`), or decorate your underlying client method with `@shape.effect` directly instead of wrapping the tool.
- If a tool checks agent state first and returns early without calling the provider, or runs its own retry loop internally, put `@shape.effect` on the client method instead of calling `shape.agents.effects`.

Passing `RunContextWrapper` context into `scope` and `key` from `shape.agents.effects` is planned.

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

**Tool error detection:** `ScriptedModel` treats a tool output as an error when it starts with `"An error occurred while running the tool."` (the Agents SDK's default `default_tool_error_function`) or `"Error:"`, and as a rejection when it contains `"not approved"`. If a tool catches exceptions internally and returns a custom dict such as `{"ok": False}`, `ScriptedModel` treats it as success and `shape` records it as a landed effect (a `landed=` predicate on `@shape.effect` is planned).

## 3. Drive the agent with `shape.agents.drive`

```python
shape.agents.drive(
    agent,
    "Please refund order 17.",
    run,
    context=ctx,
    session_factory=lambda d: SQLiteSession(TICKET, str(d / "session.db")),
    human=_approve if approval else None,
)
```

What `shape.agents.drive` handles for you:

- **Session persistence:** Opens `SQLiteSession("shape", str(run.dir / "session.db"))` by default, or calls `session_factory(run.dir)`.
- **No mid-turn resume:** The OpenAI Agents SDK does not checkpoint an in-flight tool call inside `Runner.run`. On restart (`run.restarts > 0`), `drive` redelivers `input` to `Runner.run` with the same session, modeling an at-least-once queue or client retry.
- **Human-in-the-loop (`needs_approval=True`):** When `Runner.run` pauses with `result.interruptions`, `drive` saves `result.to_state()` to `run.dir / "paused-run.json"` (defaulting `context_serializer` to `lambda _: {}` when `context` is supplied, and reloading with `context_override=context`). On restart, if `paused-run.json` exists, `drive` reloads that `RunState`, calls `human(item)` for each interruption (`True` calls `state.approve(item)`, `False` calls `state.reject(item)`), and deletes `paused-run.json` once the run completes with no remaining interruptions.
- **No network or API key:** `drive` passes `RunConfig(tracing_disabled=True)` so local runs do not export traces.

### Worker scenarios vs. interactive chat agents

In [`examples/openai_agents/checks.py`](../../examples/openai_agents/checks.py), the scenario models a support job worker that checks `if not payments.has_refund("17"): raise TicketUnresolved(...)` so a failed attempt triggers a restart and `exactly_once("payments.refund")` holds. If your agent is an interactive chat bot that may legitimately reply "I could not complete your refund" after repeated provider errors, use `shape.at_most_once("payments.refund")` and `shape.claims_true()` instead of `exactly_once` (see [Invariants](invariants.md#when-to-use-claims_true-instead-of-exactly_once)).

## 4. Real findings in `examples/openai_agents/` and their fixes

Run from `examples/openai_agents/`:

```sh
cd examples/openai_agents
.venv/bin/shape test checks.py::refund_unkeyed --seeds 300 --jobs 4
.venv/bin/shape test checks.py::refund_keyed --seeds 300 --jobs 4
```

### Finding 1: Unkeyed refund double-refunds under model repeat, after-commit errors, and mid-tool crashes (`refund_unkeyed`)

`refund_unkeyed` fails 52 of 300 seeds across 4 distinct causes, all violating `at_most_once(payments.refund)`:

1. **Model repeat (33 of 52 failing seeds, 0 faults):** The model calls `issue_refund` twice in a row.
2. **After-commit `ConnectionResetError` (7 of 52) and `TimeoutError` (7 of 52):** The refund lands, the tool raises, the Agents SDK hands `"An error occurred while running the tool."` to the model, and the model retries.
3. **Mid-tool `crash_after` (5 of 52):** The process dies after the refund lands. Because the SDK did not save the in-flight turn to `SQLiteSession`, the restarted incarnation redelivers the customer message and refunds again.

Real output for causes 1 and 4:

```text
scenario=refund_unkeyed  seeds=300 (from 0)  failing=52  wall=12.29s  24 seeds/s
coverage over 300 seeds:
  op                landed  attempts
  payments.refund      362       427
                   faults injected: client_error 10, connection_reset_after_commit 13, crash_after 18, crash_before 11, rate_limit 16, server_error 11, timeout 17, timeout_after_commit 13
  model choices taken: after restart: call 1: lookup_order -> 'reissue' x5, call 1: lookup_order -> 'drop' x26, call 1: lookup_order -> 'repeat' x22, call 1: lookup_order -> 'swap' x20, call 2: issue_refund -> 'drop' x24, call 2: issue_refund -> 'repeat' x35, error at call 2: issue_refund -> 'retry_0' x5, error at call 2: issue_refund -> 'retry_2' x3
4 distinct causes across 52 failing seeds

cause 1 of 4: 33 of 52 failing seeds (4, 13, 20, 42, 43, ...)
FAIL checks.py::refund_unkeyed  seed=4  invariant=at_most_once(payments.refund)
  payments.refund happened 2 times for scope 'order:17' (at most 1 allowed)
  minimized to 3 steps (1 of 1 deviations kept, 3 replays):
    incarnation 0
      1. model chose 'repeat' at 'call 2: issue_refund' (nominal: issue_refund(order_id='17', amount_cents=4200))
      2. issue_refund(order_id='17', amount_cents=4200)
           effect landed
      3. issue_refund(order_id='17', amount_cents=4200)
           effect landed
      finished
```

```text
cause 4 of 4: 5 of 52 failing seeds (41, 65, 186, 271, 287)
FAIL checks.py::refund_unkeyed  seed=41  invariant=at_most_once(payments.refund)
  payments.refund happened 2 times for scope 'order:17' (at most 1 allowed)
  minimized to 2 steps (1 of 2 deviations kept, 5 replays):
    incarnation 0
      1. issue_refund(order_id='17', amount_cents=4200)
           effect landed, then process killed after the effect landed, before the caller saw it
      -> process died
    incarnation 1 (restarted)
      2. issue_refund(order_id='17', amount_cents=4200)
           effect landed
      finished
```

**Fix (`refund_keyed`):** Pass a stable idempotency key derived from durable business facts (`f"{TICKET}:refund:{order_id}"`, not from a per-call `tool_call_id`). All 300 seeds pass (`failing=0`).

### Finding 2: Approval alone does not prevent duplicate refunds (`refund_approval_unkeyed`)

With `needs_approval=True`, `approved_before(payments.refund)` holds across every seed, but `refund_approval_unkeyed` still fails `at_most_once(payments.refund)` on 54 of 300 seeds (including `crash_after`, where reloading `paused-run.json` executes the approved refund a second time, matching the SDK's own human-in-the-loop documentation).

**Fix (`refund_approval_keyed`):** Combine `needs_approval=True` with the stable idempotency key. All 300 seeds pass (`failing=0`).

### Finding 3: Model substitutes an unauthorized order ID (`refund_wrong_order_unguarded`)

With `alternatives={"order_id": ["18"]}`, `refund_wrong_order_unguarded` fails 20 of 300 seeds with zero injected faults:

```text
cause 1 of 1: 20 of 20 failing seeds (43, 50, 58, 113, 130, ...)
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
    2. issue_refund(order_id='17', amount_cents=4200)
         effect landed
    finished
```
