# Framework adapters reference

> For task-oriented walkthroughs with real findings and fixes, see the [LangGraph guide](guide/langgraph.md) and [OpenAI Agents SDK guide](guide/openai-agents.md).

`shape` core has zero dependencies. Optional adapters for LangGraph (`shape.langgraph`) and the OpenAI Agents SDK (`shape.agents`) remove harness boilerplate (tool effect wrapping, a perturbed scripted model, and a crash-resuming driver) without modifying your tool bodies.

```sh
pip install "agent-shape[langgraph] @ git+https://github.com/lutefd/shape"
pip install "agent-shape[agents] @ git+https://github.com/lutefd/shape"
```

At release on PyPI, these become `pip install 'agent-shape[langgraph]'` and `pip install 'agent-shape[agents]'`.

## LangGraph in under 20 lines

```python
import shape, shape.langgraph
from langgraph.checkpoint.sqlite import SqliteSaver
from langchain_core.messages import HumanMessage

@shape.scenario(invariants=[shape.at_most_once("payments.refund"), shape.approved_before("payments.refund")])
def refund_check(run: shape.Run) -> None:
    shape.approval("order:17", {"order_id": "17", "amount_cents": 4200})
    tools = shape.langgraph.effects(my_tools, ops={
        "issue_refund": {
            "op": "payments.refund",
            "scope": lambda order_id, **_: f"order:{order_id}",
            "key": lambda order_id, **_: f"ticket-17:refund:{order_id}",
        },
    })
    model = shape.langgraph.ScriptedChatModel(
        [("lookup_order", {"order_id": "17"}), ("issue_refund", {"order_id": "17", "amount_cents": 4200})],
        final="Order 17 is refunded.",
        alternatives={"order_id": ["18"]},
    ).bind_tools(tools)
    with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as cp:
        graph = build_graph(model, tools, checkpointer=cp)
        shape.langgraph.drive(graph, {"messages": [HumanMessage(content="Refund order 17.")]},
                              {"configurable": {"thread_id": "ticket-17"}}, run, durability="sync")
```

For graphs that pause at `interrupt()` or static `interrupt_before=` / `interrupt_after=` breakpoints, pass `human=...` to `shape.langgraph.drive(...)`. Async graphs use `await shape.langgraph.adrive(...)`.

## OpenAI Agents SDK in under 20 lines

```python
import shape, shape.agents
from agents import Agent

@shape.scenario(invariants=[shape.at_most_once("payments.refund"), shape.approved_before("payments.refund")])
def refund_check(run: shape.Run) -> None:
    shape.approval("order:17", {"order_id": "17", "amount_cents": 4200})
    tools = shape.agents.effects(my_tools, ops={
        "issue_refund": {
            "op": "payments.refund",
            "scope": lambda order_id, **_: f"order:{order_id}",
            "key": lambda order_id, **_: f"ticket-17:refund:{order_id}",
        },
    })
    model = shape.agents.ScriptedModel(
        [("lookup_order", {"order_id": "17"}), ("issue_refund", {"order_id": "17", "amount_cents": 4200})],
        final="Order 17 is refunded.",
        alternatives={"order_id": ["18"]},
    )
    agent = Agent(name="Support", instructions="Refund orders.", model=model, tools=tools)
    shape.agents.drive(agent, "Please refund order 17.", run, context=my_ctx)
```

For `needs_approval=True` tools, pass `human=approve_fn` (returning `True` to approve or `False` to reject) to `shape.agents.drive(...)`.

## Tool wrapping: `effects(tools, ops)`

`effects(tools, ops={...})` returns a new list of tools with each tool named in `ops` wrapped in `@shape.effect(...)` (preserving sync and async execution, schemas, and error handlers). Unlisted tools pass through untouched. Each value in `ops` can be:

- an op name string: `"payments.refund"`
- a dict: `{"op": "payments.refund", "scope": ..., "key": ..., "key_from_result": ..., "landed": ..., "irreversible": False, "idempotent": False, "result": ..., "hide": (), "errors": ...}`
- an `Effect(...)` dataclass with the same fields.

Unknown tool names in `ops` or unknown effect keys raise `ValueError`.

### Framework context passed to hooks

Both adapters pass framework context into `scope`, `key`, `result`, `landed`, and `key_from_result` without exposing it in rendered calls, world log `args`, or `approved_before` argument hashes:

- **`shape.agents.effects`** (verified on `openai-agents` 0.25.0): passes the `ToolContext` (a `RunContextWrapper`, so `ctx.context` holds your run context object) under `ctx`, `context`, and your `@function_tool`'s own leading context parameter name (for example `wrapper`).
- **`shape.langgraph.effects`** (verified on `langgraph` 1.2.24): passes `config` (`RunnableConfig`, even when your `@tool` does not declare a `config` parameter) plus any `InjectedState`, `InjectedStore`, and `ToolRuntime` arguments declared by the tool. All injected parameters are hidden from rendered calls and approval hashes.

If a hook (`scope`, `key`, `result`, `landed`, `key_from_result`) raises, shape raises `shape.HarnessError` (a `BaseException`) so neither LangGraph's `ToolNode(handle_tool_errors=True)` nor the Agents SDK's failure-handling tool invoker can swallow it into an error string. The run fails immediately pointing at your hook.

### Failure results and `landed=` / `key_from_result=`

- **Auto-detected framework failures:** `shape.langgraph.effects` treats a returned `ToolMessage(status="error")` (or a `Command` updating `messages` with one) as not landed automatically; `shape.agents.effects` treats a converted `"An error occurred while running the tool..."` string the same way. You do not need to write `landed=` for these.
- **Domain refusals returned as values:** if your tool returns a refusal string (`"not the owner"`) or error dict (`{"ok": False}`) instead of raising, pass `landed=lambda res: ...`. Calls where `landed` returns `False` are logged as attempts that `returned without effect`, not effects.
- **State-deduped tools:** when you wrap an agent-facing tool that dedupes through agent state and returns an existing resource id on repeat calls (e.g. `"CMP-5170"`), pass `key_from_result=lambda res: ...` so repeat calls yielding the same id count as one effect. Whenever possible, still prefer placing `@shape.effect` on the underlying API client call that touches the outside world.

## Scripted models and perturbations

`ScriptedChatModel` (LangGraph `BaseChatModel`) and `ScriptedModel` (Agents SDK `Model`) take a plain plan of `(tool_name, args)` tuples (or `Call(...)` objects) plus a `final` message (or trailing string in the plan). Nominal execution (all choices `0`) runs the plan verbatim and emits `final`.

On seeded runs, each perturbation is drawn through `shape.choose`, so failures replay deterministically and shrink to the exact model decision that broke the invariant:

| Perturbation | Choice label | Options (index 0 is nominal) | What it tests |
| --- | --- | --- | --- |
| Repeat | `call N: <tool>` | `'repeat'` | Model calls the same tool twice in a row (catches missing idempotency keys or dedupe guards). |
| Drop | `call N: <tool>` | `'drop'` | Model skips a planned tool call and moves on (catches missing postconditions / lost effects). |
| Swap | `call N: <tool>` | `'swap'` | Model calls step `N+1` before step `N` (catches unguarded ordering assumptions). |
| Substitute arg | `call N: <tool>` | `"substitute <arg>=<val>"` | Model substitutes a value from `alternatives` (catches missing allowlists / authorization checks). |
| Error retry | `error at call N: <tool>` | `'retry_1'`, `'retry_0'`, `'retry_2'` | After a tool returns an error, model retries once (nominal), gives up (`retry_0`), or retries twice (`retry_2`). |
| Post-restart re-issue | `after restart: call N: <tool>` | `'nominal'`, `'reissue'` | On a restarted incarnation where step `N` already succeeded in history, model re-issues it anyway. |

`alternatives` accepts either flat argument alternatives (`{"order_id": ["18"]}`, applied to any call carrying that argument) or per-tool alternatives (`{"issue_refund": {"order_id": ["18"]}}`).

A failure caused purely by a model perturbation (zero injected faults) renders as:

```text
incarnation 0
  1. model chose 'repeat' at 'call 2: issue_refund' (nominal: issue_refund(order_id='17', amount_cents=4200))
  2. issue_refund(order_id='17', amount_cents=4200)
       effect landed
  3. issue_refund(order_id='17', amount_cents=4200)
       effect landed
  finished
```

## Resume policies and assumptions

A [scenario recovery preset](guide/recovery.md) controls both the runner and the adapter. Omit `on_restart=` from the driver when using a preset.

### `shape.langgraph.drive(graph, input, config, run, *, human=None, durability=None, on_restart=None)`

- On incarnation 0 (`run.restarts == 0`), calls `graph.invoke(input, config)`.
- On restarts (`run.restarts > 0`), `on_restart` controls what happens:
  - `"resume"` (**default**): calls `graph.invoke(None, config)` to resume from the checkpointer (LangGraph's own checkpoint recovery behavior, assuming a durable checkpointer such as `SqliteSaver` under `run.dir` and the same `thread_id` in `config`).
  - `"none"`: does nothing and returns `None` (models an app without a supervisor or checkpointer recovery).
  - `"redeliver"`: resends `input` to `graph.invoke(input, config)` (models an at-least-once queue/supervisor) and logs `harness redelivered the request (on_restart='redeliver')` as its own line in the trace.
  - callable `fn(run)`: calls your custom recovery function with `run` and returns its result.
- **Dynamic `interrupt()`:** whenever `graph.invoke` returns an `__interrupt__`, calls `human(payload)` and resumes with `Command(resume=reply)` in a loop. `human` may call `shape.choose(...)` to explore approve/reject/edit paths.
- **Static `interrupt_before=` / `interrupt_after=`:** after each invoke on a checkpointed graph, `drive` checks `graph.get_state(config).next`. When the graph is paused before a node, `drive` calls `human(snapshot)` and resumes: `True` or `None` approves (`graph.invoke(None, config)`), `False` denies pending tool calls (injecting a denial `ToolMessage` when the last message has `tool_calls`, or stopping cleanly otherwise), and a `dict` or `Command` is passed directly to `graph.invoke(reply, config)`.
- **Loud pause failures:** if the graph pauses with no `human=` callback, `drive` raises `RuntimeError("graph paused before node '<name>'; pass human=... to approve")` (or the dynamic `interrupt()` equivalent) instead of returning as finished. It also raises if resuming leaves the graph stuck at the same checkpoint.
- `durability` is forwarded only when explicitly passed, so `durability=None` tests LangGraph's own default (`"async"`). `adrive` is the `async` counterpart (supporting the same `on_restart` values, including async callables).

### `shape.agents.drive(agent, input, run, *, context=None, session_id=None, session_factory=None, human=None, context_serializer=None, max_turns=10, on_restart=None)`

- **Distinct session per job by default:** Each `drive()` call in a scenario gets its own `SQLiteSession("shape-job-<n>", str(run.dir / "session.db"))` (`shape-job-0`, `shape-job-1`, ...), so two independent jobs in the same scenario do not share conversation history or skip each other's tool calls. To share a session deliberately across turns, pass the same `session_id="..."` (or `session_factory=...`).
- **Explicit restart policy (`on_restart`):** The OpenAI Agents SDK does not checkpoint an in-flight tool call inside `Runner.run`, so `drive` never invents supervisor recovery you did not opt into:
  - `"none"` (**default**): on `run.restarts > 0`, does nothing and returns `None` (models an app without a supervisor).
  - `"redeliver"`: on `run.restarts > 0`, logs `harness redelivered the request (on_restart='redeliver')` as its own trace line, then reloads any saved approval `RunState` or resends `input` into the same session (models an at-least-once job queue or supervisor).
  - callable `fn(run)`: calls your custom recovery function (sync or async) with `run` and returns its result.
- **Human-in-the-loop (`needs_approval=True`):** when a run pauses on `result.interruptions`, `drive` persists `result.to_state()` under `run.dir` (defaulting `context_serializer` to `{}` when a `context` is supplied, and reloading with `context_override=context`), asks `human(item)` for each interruption (`True` -> `approve`, `False` -> `reject`), and deletes the saved state file once the run finishes without further interruptions. With `on_restart="redeliver"`, a restarted incarnation reloads that saved `RunState` instead of starting a new turn.
- Disables Agents SDK trace export (`RunConfig(tracing_disabled=True)`) so local runs require no API key or network access.
