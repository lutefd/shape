# shape on an OpenAI Agents SDK agent

A support agent written the way the [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) docs write one (`@function_tool`, `Agent`, `Runner.run`, a `SQLiteSession`), tested with shape's [boundary mode](../../docs/boundary.md) and [`shape.agents`](../../docs/adapters.md) with zero shape imports in `agent.py` or `payments.py`. `checks.py` wraps `issue_refund` from outside with `shape.agents.effects(...)`.

No network and no API key: `checks.py` plugs in `shape.agents.ScriptedModel` (implementing the SDK's `Model` interface with shrinkable `shape.choose` perturbations) and runs the agent with `shape.agents.drive(...)`.

## Run it

```sh
examples/openai_agents/run.sh        # clean venv, pinned deps, ~1 min; exits 1 if any result below changes
```

or by hand, from this folder (`.venv/bin/pip install -r requirements.txt -e ../..` first):

```sh
.venv/bin/shape test checks.py::refund_unkeyed --seeds 300      # fails, shrinks to 1 fault
.venv/bin/shape test checks.py::refund_keyed --seeds 300        # passes
.venv/bin/shape test checks.py::refund_unkeyed --replay traces/refund_unkeyed.crash_after.min.json
```

CI: `.github/workflows/example-openai-agents.yml` runs `run.sh`, the sweeps and the replays and asserts the outcomes.

## Versions

`openai-agents==0.25.0` (latest on PyPI on 2026-11-23, all deps pinned in `requirements.txt`), `openai==3.57.0`, `pydantic==2.14.4`. Python 3.14.7 locally and 3.12 in CI; Linux. SQLite 3 for the SDK's `SQLiteSession`, the paused `RunState` file and the payments fake. SDK docs fetched 2026-11-23. Tracing is disabled (it exports to the OpenAI platform).

## The agent

- Tools: `lookup_order` (read-only, sync, so the SDK runs it in a thread) and `issue_refund` (async), which calls `PaymentsClient.refund`. `checks.py` wraps `issue_refund` via `shape.agents.effects(..., ops={"issue_refund": {"op": "payments.refund", ...}})` without editing `agent.py` or `payments.py`. The provider fake is a sqlite file that, like a real provider, returns the original refund for a repeated key.
- Persistence: `SQLiteSession("ticket-17", run.dir/"session.db")` as in the [Sessions docs](https://openai.github.io/openai-agents-python/sessions/), wired via `shape.agents.drive(...)`. A restarted process (`run.restarts > 0`) runs the same handler again with the same session: the customer's message is redelivered because the worker never acked it. The SDK documents no resume for a run that was killed mid-turn, other than the `RunState` flow below and the Temporal / Dapr / Restate integrations, so this is the app-level pattern.
- Approval variant: `function_tool(..., needs_approval=True)`, the [Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/) flow: `result.interruptions`, `result.to_state()` saved to `paused-run.json`, `state.approve(...)`, `Runner.run(agent, state, session=session)`, handled by `shape.agents.drive(..., human=_approve)`. A restarted process reloads the saved paused state.
- The scripted model (`shape.agents.ScriptedModel`) runs the nominal plan (lookup, then refund) and perturbs it via `shape.choose` (repeat, drop, swap, wrong order substitution, retry count after tool errors, and post-restart re-issue). The worker (`checks.py`) raises `TicketUnresolved`, so the queue redelivers it, when the agent finished without refunding order 17.
- Variants: `refund_unkeyed` calls the client without an idempotency key; `refund_keyed` sends `ticket-17:refund:<order>` (derived from the ticket and order, not from anything the model or the run generates); `refund_approval_unkeyed` and `refund_approval_keyed` add `needs_approval=True`; `refund_wrong_order_unguarded` and `refund_wrong_order_guarded` add `alternatives={"order_id": ["18"]}` to test allowlist enforcement.
- Invariants: `at_most_once` and `exactly_once` on `payments.refund`, plus `approved_before` on the approval and wrong-order variants.

## Results

Single fault on the first refund call (everything else nominal). Cell: first violated invariant, real refunds / calls to the payments client.

| variant | timeout, rate_limit, server_error, crash_before | timeout_after_commit, connection_reset_after_commit, crash_after |
| --- | --- | --- |
| `refund_unkeyed` | ok 1/1 | **at_most_once 2/2** |
| `refund_keyed` | ok 1/1 | ok 1/2 |
| `refund_approval_unkeyed` | ok 1/1 | **at_most_once 2/2** |
| `refund_approval_keyed` | ok 1/1 | ok 1/2 |

`run.sh` prints the full 8-column table. Seed sweeps (random fault kinds + model perturbations, up to 3 restarts), 300 seeds each:

| variant | failing | first invariant |
| --- | --- | --- |
| `refund_unkeyed` | 52 / 300 | `at_most_once(payments.refund)` |
| `refund_keyed` | 0 / 300 | |
| `refund_approval_unkeyed` | 54 / 300 | `at_most_once(payments.refund)` |
| `refund_approval_keyed` | 0 / 300 | |
| `refund_wrong_order_unguarded` | 20 / 300 | `approved_before(payments.refund)` |
| `refund_wrong_order_guarded` | 0 / 300 | |

`exactly_once` (the refund did happen) and `approved_before` hold in every run of every variant. Each shrunk trace replays 10 of 10 times (`traces/`, checked in CI). A zero means 300 seeds without a failure, not a proof.

## Findings

Both findings are in the unkeyed variants only, and both are the hazard the SDK's docs describe. Neither is new.

**Claims** (quoted from the docs, fetched 2026-11-23):

- OA-C01, [Sessions](https://openai.github.io/openai-agents-python/sessions/): "After each run: All new items generated during the run (user input, assistant responses, tool calls, etc.) are automatically stored in the session."
- OA-C02, [Tools](https://openai.github.io/openai-agents-python/tools/): "This is a function that provides an error response to the LLM in case the tool call crashes." and "By default (i.e. if you don't pass anything), it runs a default_tool_error_function which tells the LLM an error occurred."
- OA-C03, [Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/): "Approval permits a tool call to execute; it does not guarantee exactly-once execution. If a tool performs an external action and the process stops before the application stores the updated run state, resuming from the older approved snapshot can execute the tool again. For example, a payment can complete even though the saved snapshot has no record of the payment result."
- OA-C04, same page: "the application should assign a stable idempotency key to each logical action and pass that key to a destination that atomically prevents duplicate effects."
- OA-C05, [Models](https://openai.github.io/openai-agents-python/models/): "Directly providing a Model implementation." (how `shape.agents.ScriptedModel` is plugged into `Agent(model=...)`).

**F1: a tool that raised after its effect landed is retried by the model (OA-C02).** Faults `timeout_after_commit`, `connection_reset_after_commit` (the call raises after the refund landed). The SDK turns the exception into the string `An error occurred while running the tool.`, which says nothing about whether the effect happened. The scripted model retries once. Observed in `inc-0.out`: model #2 issues `issue_refund`, model #3 sees the error and issues it again, 2 refunds.

```
incarnation 0
  1. PaymentsClient.refund(order_id='17', amount_cents=4200, idempotency_key=None)
       effect landed, then connection reset after commit: the caller saw ConnectionResetError
  2. PaymentsClient.refund(order_id='17', amount_cents=4200, idempotency_key=None)
       effect landed
```

**F2: a crash while the tool runs is replayed on resume (OA-C01, OA-C03).** Fault `crash_after` (process killed after the refund landed). The resumed model sees 4 session items: the customer's message, the lookup call and its output (the SDK had saved that finished turn), and the redelivered message. The in-flight `issue_refund` call and its result are not in the session, so the model issues the refund again. In the approval variant the paused `RunState` on disk predates the refund, so the restarted run executes the approved call again, which is the case OA-C03 describes.

```
incarnation 0
  1. PaymentsClient.refund(order_id='17', amount_cents=4200, idempotency_key=None)
       effect landed, then process killed after the effect landed, before the caller saw it
  -> process died
incarnation 1 (restarted)
  2. PaymentsClient.refund(order_id='17', amount_cents=4200, idempotency_key=None)
       effect landed
```

(The approval variant's trace has a `shape.approval` step before each refund; `traces/refund_approval_unkeyed.crash_after.min.json`.)

**Classification** (per [methodology](../../docs/jepsen/methodology.md) section 2): class 2, a footgun in tutorial-shaped code. The unkeyed variants copy what the docs' own examples show, a side-effecting `@function_tool` with no idempotency key, and they break `at_most_once`. They are not class 1 (OA-C01 to OA-C03 promise nothing that is false here; OA-C03 states F2 outright) and not class 3 (the hardened variants, which apply OA-C04, pass). Minimized fault provenance: `timeout_after_commit`, `connection_reset_after_commit` and `crash_after` are tagged `documented` in `docs/boundary.md`; this example does not attach the HTTP-library or provider quote that section 3 asks for. The model's retry is an assumption of the scripted model, not a measured behavior of a real model. No approval bypass: `approved_before` never failed.

**Mitigation (OA-C04), tested:** `refund_keyed` and `refund_approval_keyed` pass 300 of 300 seeds, with the provider deduping by key (`ok 1/2` above: two calls, one refund). The key comes from the ticket and the order. A key taken from the tool call id would not cover F1, because the model's retry is a new call.

## Limits

- The model is scripted. What a real model does after `An error occurred while running the tool.` or after seeing a redelivered message is not tested here.
- The restart pattern (redeliver the message into the same session) is ours. Another resume strategy is another experiment.
- The payments provider is a sqlite fake that mirrors idempotency-key semantics, not a provider sandbox, and there is no standalone repro without shape, so this is not publication-grade under methodology section 5.
- Threads: sync tools run through `asyncio.to_thread`, and `@shape.effect` is used on an `async def`. The latter needed a small change in `src/shape/boundary/api.py` (async effects are now recorded and faulted around the awaited body, with tests); nothing else in boundary mode needed changing.
- Not tested: streaming runs, handoffs, guardrails, `timeout_seconds`, custom `failure_error_function`, `RunConfig` retry policies.
