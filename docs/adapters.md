# Framework adapters

`shape` core has zero dependencies. Optional adapters for LangGraph (`shape.langgraph`) and the OpenAI Agents SDK (`shape.agents`) remove the harness boilerplate — tool effect wrapping, a perturbed scripted model, and a crash-resuming driver — without modifying your tool bodies.

```sh
pip install 'agent-shape[langgraph]'
pip install 'agent-shape[agents]'
```

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

For graphs that pause at `interrupt()`, pass `human=lambda payload: shape.choose("human", [True, False])` (or return an edit dict) to `shape.langgraph.drive(...)`. Async graphs use `await shape.langgraph.adrive(...)`.

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
- a dict: `{"op": "payments.refund", "scope": ..., "key": ..., "irreversible": False, "errors": ...}`
- an `Effect(...)` dataclass with the same fields.

Unknown tool names in `ops` raise `ValueError`.

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
  1. model chose 'repeat' at 'call 2: issue_refund' (nominal: 'nominal')
  2. issue_refund(order_id='17', amount_cents=4200)
       effect landed
  3. issue_refund(order_id='17', amount_cents=4200)
       effect landed
  finished
```

## Resume policies and assumptions

### `shape.langgraph.drive(graph, input, config, run, *, human=None, durability=None)`

- On incarnation 0 (`run.restarts == 0`), calls `graph.invoke(input, config)`.
- On restarts (`run.restarts > 0`), calls `graph.invoke(None, config)`, assuming the graph was compiled with a durable checkpointer (such as `SqliteSaver` under `run.dir`) and the same `thread_id` in `config`.
- Whenever `graph.invoke` returns an `__interrupt__`, calls `human(payload)` and resumes with `Command(resume=reply)` in a loop. `human` may call `shape.choose(...)` to explore approve/reject/edit paths.
- `durability` is forwarded only when explicitly passed, so `durability=None` tests LangGraph's own default (`"async"`).

### `shape.agents.drive(agent, input, run, *, context=None, session_factory=None, human=None, context_serializer=None, max_turns=10)`

- Persists conversation memory in `SQLiteSession("shape", str(run.dir / "session.db"))` by default, or in the session returned by `session_factory(run.dir)`.
- **No mid-turn resume:** the OpenAI Agents SDK does not checkpoint an in-flight tool call inside `Runner.run`. On restart (`run.restarts > 0`), `drive` redelivers `input` to `Runner.run` with the same session, modeling an at-least-once job queue or client retry.
- **Human-in-the-loop (`needs_approval=True`):** when a run pauses on `result.interruptions`, `drive` persists `result.to_state()` to `run.dir / "paused-run.json"` (defaulting `context_serializer` to `{}` when a `context` is supplied, and reloading with `context_override=context`). On restart, if `paused-run.json` exists, `drive` reloads that `RunState` instead of starting a new turn, asks `human(item)` for each interruption (`True` -> `approve`, `False` -> `reject`), and deletes `paused-run.json` once the run finishes without further interruptions.
- Disables Agents SDK trace export (`RunConfig(tracing_disabled=True)`) so local runs require no API key or network access.
