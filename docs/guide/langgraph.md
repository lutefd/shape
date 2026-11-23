# Testing LangGraph agents

`shape.langgraph` tests real LangGraph graphs (`StateGraph`, `ToolNode`, `SqliteSaver`, `RetryPolicy`, `interrupt()`, `Command(resume=...)`) under faults, real process crashes, and model perturbations without modifying your tool bodies.

For the concise adapter reference, see [docs/adapters.md](../adapters.md).

## Install

```sh
pip install "agent-shape[langgraph] @ git+https://github.com/lutefd/shape"
```

At release on PyPI this becomes `pip install 'agent-shape[langgraph]'`.

To run the examples in this repository:

```sh
python3 -m venv examples/langgraph/.venv
examples/langgraph/.venv/bin/pip install -e . -r examples/langgraph/requirements.txt
```

## 1. Wrap side-effecting tools with `shape.langgraph.effects`

Pass your tool list and an `ops` dict naming only the tools that touch the outside world. Read-only tools (such as `lookup_order`) pass through unchanged:

```python
tools = shape.langgraph.effects(
    [lookup_order, issue_refund],
    ops={
        "issue_refund": {
            "op": "payments.refund",
            "scope": lambda order_id, **_: f"order:{order_id}",
            "key": lambda order_id, **_: f"{THREAD_ID}:refund:{order_id}",
        }
    },
)
```

Each entry in `ops` accepts an op string (`"payments.refund"`), a dict supporting every `@shape.effect` option (`op`, `scope`, `key`, `key_from_result`, `landed`, `irreversible`, `errors`, `idempotent`, `result`, `hide`), or a `shape.langgraph.Effect(...)` instance. Unknown tool names raise `ValueError`.

### What `shape.langgraph.effects` passes to hooks

- `scope`, `key`, `result`, `landed`, and `key_from_result` receive the tool's named arguments **plus** LangGraph framework context: `config` (`RunnableConfig`, even when your `@tool` does not declare a `config` parameter) and any `InjectedState`, `InjectedStore`, or `ToolRuntime` parameters declared by your tool.
- All injected parameters (`config`, `runtime`, `InjectedState`, `InjectedStore`) are hidden from rendered calls, world log `args`, and `approved_before` argument hashes automatically.
- For custom `BaseTool` subclasses: `effects()` wraps `tool._run` and `tool._arun`, forwarding `config` and `run_manager` and passing `config` to all hooks.
- If any hook raises, `shape` raises `shape.HarnessError` (`BaseException`) so `ToolNode(handle_tool_errors=True)` never swallows it into a tool error message — the run fails immediately pointing at your lambda.

If a tool checks local state and returns early without calling the external provider, or runs its own internal retry loop, put `@shape.effect` directly on the underlying API client method instead of wrapping the outer tool (see [`examples/langgraph/durability.py`](../../examples/langgraph/durability.py)), or pass `landed=` / `key_from_result=` when you only control the tool layer.

## 2. Perturb the model with `ScriptedChatModel`

`shape.langgraph.ScriptedChatModel` implements `BaseChatModel` and turns a happy-path plan into shrinkable `shape.choose` perturbations:

```python
PLAN = [
    ("lookup_order", {"order_id": "17"}),
    ("issue_refund", {"order_id": "17", "amount_cents": 4200}),
]
model = shape.langgraph.ScriptedChatModel(
    PLAN,
    final="Order 17 is refunded.",
    alternatives={"order_id": ["18"]},
).bind_tools(tools)
```

On nominal runs (all choices `0`), `ScriptedChatModel` emits each planned tool call in order and then returns `final`. On seeded runs it explores six perturbation kinds:

- `repeat`: issues the same tool call twice in a row.
- `drop`: skips a planned tool call.
- `swap`: calls step `N+1` before step `N`.
- `substitute <arg>=<val>`: substitutes a value from `alternatives` (flat `{"order_id": ["18"]}` or per-tool `{"issue_refund": {"order_id": ["18"]}}`).
- `error at call N`: after a tool error, chooses between `retry_1` (nominal), `retry_0` (give up), and `retry_2` (retry twice).
- `after restart`: on a restarted incarnation where step `N` already succeeded in message history, may `reissue` it.

**Tool error detection:** `ScriptedChatModel` treats a `ToolMessage` as an error when `status == "error"` or its content starts with `"Error:"` or `"Error invoking tool"` (and treats `"not approved"` as a rejection). `shape.langgraph.effects` also auto-detects returned `ToolMessage(status="error")` results (and `Command` updates carrying one) as not landed without needing `landed=`. If your tool returns a plain refusal string or error dict such as `{"ok": False}`, pass `landed=lambda res: ...` in `ops` so `shape` logs it as `returned without effect`.

## 3. Drive and resume the graph with `drive` and `adrive`

Compile your graph with a durable checkpointer stored inside `run.dir` (such as `SqliteSaver`), and run it with `shape.langgraph.drive` (or `await shape.langgraph.adrive` for async graphs):

```python
with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as checkpointer:
    graph = builder.compile(checkpointer=checkpointer)
    shape.langgraph.drive(
        graph,
        {"messages": [HumanMessage(content="Please refund order 17.")]},
        {"configurable": {"thread_id": "ticket-17"}},
        run,
        durability="sync",
    )
```

- On incarnation 0 (`run.restarts == 0`), `drive` calls `graph.invoke(input, config)`.
- On restarts (`run.restarts > 0`), `on_restart` controls recovery:
  - `on_restart="resume"` (**default**): calls `graph.invoke(None, config)` to resume from the checkpointer. Resume-from-checkpoint is LangGraph's own recovery behavior, so it stays the LangGraph adapter default.
  - `on_restart="none"`: does nothing and returns `None` (models a graph run without a supervisor or checkpoint recovery).
  - `on_restart="redeliver"`: logs `harness redelivered the request (on_restart='redeliver')` in the trace and resends `input` to `graph.invoke(input, config)` (models an at-least-once queue).
  - `on_restart=callable`: invokes `fn(run)` (sync, or async in `adrive`) for custom recovery logic.
- `durability` is forwarded only when explicitly passed. Leaving `durability=None` tests LangGraph's own default (`"async"`).

### Dynamic `interrupt()` vs. static `interrupt_before`

For dynamic `interrupt()` gates, pass `human=fn` to `drive`. Whenever `graph.invoke` returns a dict containing `"__interrupt__"`, `drive` calls `human(payload)` and resumes with `Command(resume=reply)`. Inside `human`, record approvals with `shape.approval(...)` (or record inside the approval node after `interrupt()` returns, as in [`examples/langgraph/approval.py`](../../examples/langgraph/approval.py)):

```python
shape.langgraph.drive(
    graph,
    INIT_STATE,
    CONFIG,
    run,
    human=lambda payload: shape.choose("human_review", [True, False]),
    durability="sync",
)
```

**Static `interrupt_before` / `interrupt_after`:** `shape.langgraph.drive` and `adrive` handle static breakpoints compiled with `builder.compile(checkpointer=cp, interrupt_before=["sensitive_tools"])` (the LangGraph customer-support tutorial pattern) automatically. After each invoke on a checkpointed graph, `drive` inspects `graph.get_state(config).next`:

```python
def human(snapshot) -> bool:
    if "sensitive_tools" in snapshot.next:
        tc = snapshot.values["messages"][-1].tool_calls[0]
        shape.approval(f"order:{tc['args']['order_id']}", tc["args"])
    return shape.choose("human_review", [True, False])

shape.langgraph.drive(graph, INIT_STATE, CONFIG, run, human=human, durability="sync")
```

- Returning `True` or `None` approves and resumes with `graph.invoke(None, config)`.
- Returning `False` denies pending tool calls (injecting a denial `ToolMessage` when the last message has `tool_calls`, or stopping cleanly otherwise).
- Returning a `dict` or `Command` passes it directly to `graph.invoke(reply, config)`.
- Omitting `human=` fails loudly (`graph paused before node 'sensitive_tools'; pass human=... to approve`) instead of silently returning as finished.

## 4. Real findings in `examples/langgraph/` and their fixes

### Model repeat and wrong-order substitution (`refund_agent.py`)

Run:

```sh
examples/langgraph/.venv/bin/shape test examples/langgraph/refund_agent.py --seeds 200 --jobs 4
```

Without an idempotency key, `refund_repeat_unguarded` fails 37 of 200 seeds across 4 distinct causes: cause 1 needs zero injected faults (the model repeats `issue_refund`), cause 2 groups reply-lost faults (`connection_reset_after_commit`, `timeout_after_commit`), cause 3 isolates `crash_after` (process died after the commit), and cause 4 is `crash_before` + model `'reissue'`:

```text
4 distinct causes across 37 failing seeds

cause 1 of 4: 19 of 37 failing seeds (4, 13, 20, 42, 49, ...)
  needs: model choice
FAIL examples/langgraph/refund_agent.py::refund_repeat_unguarded  seed=4  invariant=at_most_once(payments.refund)
  payments.refund happened 2 times for scope 'order:17' (at most 1 allowed)
  minimized to 3 steps (1 of 1 deviations kept, 2 replays):
    incarnation 0
      1. model chose 'repeat' at 'call 2: issue_refund' (nominal: issue_refund(order_id='17', amount_cents=4200))
      2. issue_refund(order_id='17', amount_cents=4200)
           effect landed
      3. issue_refund(order_id='17', amount_cents=4200)
           effect landed
      finished
```

**Fix (`refund_repeat_guarded`):** Derive a stable idempotency key from thread and order (`f"{THREAD_ID}:refund:{order_id}"`). All 200 seeds pass (`failing=0`).

With `alternatives={"order_id": ["18"]}`, `refund_wrong_order_unguarded` fails 11 of 200 seeds because the model substitutes order `18` when only order `17` was approved:

```text
cause 1 of 1: 11 of 11 failing seeds (43, 50, 58, 113, 130, ...)
  needs: model choice
FAIL examples/langgraph/refund_agent.py::refund_wrong_order_unguarded  seed=50  invariant=approved_before(payments.refund)
  issue_refund(order_id='18', amount_cents=4200) ran with no earlier approval for scope 'order:18'
  minimized to 3 steps (1 of 1 deviations kept, 3 replays):
    incarnation 0
      1. approval recorded for 'order:17'
      2. model chose "substitute order_id='18'" at 'call 2: issue_refund' (nominal: issue_refund(order_id='17', amount_cents=4200))
      3. issue_refund(order_id='18', amount_cents=4200)
           effect landed (idempotency key 'ticket-17:refund:18')
      finished
```

**Fix (`refund_wrong_order_guarded`):** Enforce an allowlist inside `issue_refund` (`if order_id not in allowed_orders: raise PermissionError(...)`). All 200 seeds pass (`failing=0`).

### Default `RetryPolicy()` double charges and retries HTTP 402 (`retry_policy.py`)

Run:

```sh
examples/langgraph/.venv/bin/shape test examples/langgraph/retry_policy.py --seeds 200 --jobs 4
```

LangGraph's default `RetryPolicy()` (`default_retry_on`) retries `ConnectionResetError`, `httpx.ReadTimeout`, and `requests.HTTPError` with status `402`. Without an idempotency key, an after-commit `httpx.ReadTimeout` double-charges inside a single incarnation (`retry_httpx_timeout`, cause 2 of 6):

```text
cause 2 of 6: 8 of 27 failing seeds (7, 32, 79, 86, 87, ...)
  needs: reply lost (commit landed, caller saw an error: connection_reset_after_commit, timeout_after_commit)
FAIL examples/langgraph/retry_policy.py::retry_httpx_timeout  seed=7  invariant=at_most_once(payments.charge)
  payments.charge happened 2 times for scope 'order:ord-17' (at most 1 allowed)
  minimized to 2 steps (1 of 1 deviations kept, 2 replays):
    incarnation 0
      1. charge(order_id='ord-17', amount=5000, idempotency_key=None)
           effect landed, then timeout after commit: the caller saw ReadTimeout
      2. charge(order_id='ord-17', amount=5000, idempotency_key=None)
           effect landed
      finished
```

`retry_requests_402_before_commit` catches `default_retry_on` retrying a non-retryable 402 `requests.HTTPError` (3 of 200 seeds) because `requests.Response.__bool__` is `False` for any status `>= 400`.

**Fix (`retry_fixed`):** Pass `durability="sync"` and a deterministic idempotency key (`f"{state['thread_id']}:charge"`), and configure a custom `retry_on` predicate if you want to avoid retrying 4xx `requests.HTTPError`. `retry_fixed` passes 200 of 200 seeds (`failing=0`).

### Default `durability="async"` loses completed super-steps (`durability.py`)

Run:

```sh
examples/langgraph/.venv/bin/shape test examples/langgraph/durability.py --seeds 200 --jobs 4
```

Under LangGraph's default `durability="async"`, `durability_async` fails 43 of 200 seeds. Beyond mid-node `crash_after` re-runs, `seed=100` catches an async checkpoint loss where `charge` returned cleanly and `crash_before` killed `confirm` before the background thread saved `charge`'s checkpoint:

```text
cause 6 of 7: 3 of 43 failing seeds (82, 100, 109)
FAIL examples/langgraph/durability.py::durability_async  seed=100  invariant=at_most_once(payments.charge)
  payments.charge happened 2 times for scope 'order:ord-42' (at most 1 allowed)
  minimized to 4 steps (1 of 1 deviations kept, 3 replays):
    incarnation 0
      1. charge(order_id='ord-42', amount=5000, idempotency_key=None)
           effect landed
      2. confirm(order_id='ord-42', charge_id=1, idempotency_key=None)
           process killed before the call ran
      -> process died
    incarnation 1 (restarted)
      3. charge(order_id='ord-42', amount=5000, idempotency_key=None)
           effect landed
      4. confirm(order_id='ord-42', charge_id=2, idempotency_key=None)
           effect landed
      finished
```

**Fix (`durability_sync_fixed`):** Combine `durability="sync"` with deterministic idempotency keys derived from durable state on both `charge` and `confirm`. All 200 seeds pass (`failing=0`).

### `interrupt()` approval gate (`approval.py`)

Run:

```sh
examples/langgraph/.venv/bin/shape test examples/langgraph/approval.py --seeds 200 --jobs 4
```

`approved_before(transfers.send)` holds across every scenario (`approval_gate_only`, `approval_default`, `approval_sync_fixed`, and `approval_rejected` with `expect_effects=False`). However, `approval_default` fails `at_most_once(transfers.send)` on 21 of 200 seeds across 2 distinct causes: reply lost (`connection_reset_after_commit`, `timeout_after_commit`) and process crash after commit (`crash_after`):

```text
2 distinct causes across 21 failing seeds

cause 1 of 2: 13 of 21 failing seeds (37, 38, 40, 45, 49, ...)
  needs: reply lost (commit landed, caller saw an error: connection_reset_after_commit, timeout_after_commit)
FAIL examples/langgraph/approval.py::approval_default  seed=37  invariant=at_most_once(transfers.send)
  transfers.send happened 2 times for scope 'order:ord-7' (at most 1 allowed)
  minimized to 3 steps (1 of 1 deviations kept, 2 replays):
    incarnation 0
      1. approval recorded for 'order:ord-7'
      2. send(order_id='ord-7', to='acct-42', amount=50000, idempotency_key=None)
           effect landed, then connection reset after commit: the caller saw ConnectionResetError
      process raised: ConnectionResetError: shape: injected connection_reset_after_commit
    incarnation 1 (restarted)
      3. send(order_id='ord-7', to='acct-42', amount=50000, idempotency_key=None)
           effect landed
      finished

cause 2 of 2: 8 of 21 failing seeds (73, 93, 123, 127, 133, ...)
  needs: crash after commit (process died after the commit: crash_after)
FAIL examples/langgraph/approval.py::approval_default  seed=73  invariant=at_most_once(transfers.send)
  transfers.send happened 2 times for scope 'order:ord-7' (at most 1 allowed)
  minimized to 3 steps (1 of 1 deviations kept, 2 replays):
    incarnation 0
      1. approval recorded for 'order:ord-7'
      2. send(order_id='ord-7', to='acct-42', amount=50000, idempotency_key=None)
           effect landed, then process killed after the effect landed, before the caller saw it
      -> process died
    incarnation 1 (restarted)
      3. send(order_id='ord-7', to='acct-42', amount=50000, idempotency_key=None)
           effect landed
      finished
```

**Fix (`approval_sync_fixed`):** Pass `durability="sync"` and a deterministic idempotency key (`f"{state['thread_id']}:send"`). All 200 seeds pass (`failing=0`).

See [examples/langgraph/README.md](../../examples/langgraph/README.md) for the full version-pinned table and [docs/jepsen/claims/langgraph.md](../jepsen/claims/langgraph.md) for the claim-by-claim LangGraph analysis.
