# LangGraph Crash-Resume Probe

Jepsen-style boundary probe for **LangGraph** (`langgraph==1.2.24`, `langgraph-checkpoint-sqlite==3.1.3`).

Following Jepsen's rule, this probe tests **what LangGraph's official documentation claims** about persistence, durable execution, `@task`, and human-in-the-loop interrupts under real `SIGKILL` process crashes and fresh-process recovery from `SqliteSaver`.

## Quick Reproduction

One command creates `frameworks/langgraph/.venv` (if missing), installs the pinned dependencies in `requirements.txt`, executes every cell of the results matrix 3 times with real `SIGKILL` crashes in isolated subprocesses, and regenerates `results.md`:

```sh
frameworks/langgraph/run.sh
```

## Official Documentation & Idioms Followed

All workloads are written deterministically (no LLM) using the exact idioms from LangGraph's official Python documentation:

- **Persistence overview**: https://docs.langchain.com/oss/python/langgraph/persistence
  - Uses `SqliteSaver` (`langgraph-checkpoint-sqlite`) with a `thread_id` in `config={"configurable": {"thread_id": ...}}` so checkpoints survive process death.
- **Checkpointers & durability modes**: https://docs.langchain.com/oss/python/langgraph/checkpointers
  - Resume after crash/error via `graph.invoke(None, config)` ("Loads the latest checkpoint and retries unfinished work. Omit `checkpoint_id`.")
  - Compares default `durability="async"` against explicit `durability="sync"` ("persists changes synchronously before the next step starts").
- **Graph API — re-execution, idempotency, and tasks**: https://docs.langchain.com/oss/python/langgraph/graph-api
  - `StateGraph` with typed state, `START`, `END`, and node-level checkpoint boundaries.
  - `@task` inside a node (`graph-api#using-tasks-in-nodes`) to cache completed sub-step outputs on the checkpoint.
- **Fault tolerance — retry boundaries and idempotency**: https://docs.langchain.com/oss/python/langgraph/fault-tolerance
  - Explicitly documents that node execution is **at-least-once** across retries, interrupts, and crash resumes, and that external side effects must be protected with a **deterministic idempotency key derived from durable state** (or reserved in a prior node with `durability="sync"`).
- **Interrupts (Human-in-the-Loop)**: https://docs.langchain.com/oss/python/langgraph/interrupts
  - Follows the canonical two-node approval pattern (`interrupts#approve-or-reject` and `interrupts#side-effects-called-before-interrupt-must-be-idempotent`): `approval_node` calls `interrupt(payload)` and returns `Command(goto="execute" if approved else "cancel")`; the irreversible side effect lives in `execute_node` **after** approval.
  - Resumes via `graph.invoke(Command(resume=True|False), config)`.
  - Explicitly notes that if a post-approval node fails after committing a side effect but before completing, retrying it can repeat the effect unless deduplicated externally.
- **Functional API — `@task` idempotency**: https://docs.langchain.com/oss/python/langgraph/functional-api
  - Documents that completed `@task` results are restored on resume, while a task that started but did not finish may run again and must use idempotency keys.

## Architecture

- [`world.py`](world.py): God's-eye external side-effect log stored in a separate SQLite database (`PRAGMA synchronous=FULL; PRAGMA journal_mode=WAL;`) owned by the harness outside the agent process. Counts real committed effects regardless of what LangGraph's checkpointer believes, and optionally honors Stripe-style `(op, idempotency_key)` deduplication for mitigated variants.
- [`workload_charge_once.py`](workload_charge_once.py): Deterministic `charge -> confirm` workflow in four variants:
  - `naive`: 2-node `StateGraph`, default durability (`"async"`), non-idempotent external calls.
  - `sync`: 2-node `StateGraph`, `durability="sync"`, non-idempotent external calls.
  - `task_async`: single node wrapping `charge` and `confirm` in `@task`, default durability (`"async"`).
  - `idempotent_sync`: 2-node `StateGraph`, `durability="sync"`, deterministic idempotency keys (`charge:{order_id}`, `confirm:{order_id}`).
- [`workload_approval_gate.py`](workload_approval_gate.py): Deterministic human-in-the-loop `approval -> execute | cancel` workflow in three variants:
  - `naive_two_node`: 2-node `interrupt()` + `Command(goto=...)`, default durability (`"async"`), non-idempotent `wire_transfer`.
  - `sync_two_node`: 2-node `interrupt()`, `durability="sync"`, non-idempotent `wire_transfer`.
  - `mitigated`: 2-node `interrupt()`, `durability="sync"`, deterministic idempotency key (`wire:{order_id}`).
- [`harness.py`](harness.py): Runs every `(workload, crash_point, variant)` cell 3 times in isolated subprocesses, injects deterministic `SIGKILL` via the `CRASH_POINT` environment variable, recovers in a fresh process, evaluates invariants against `WorldLog`, and writes [`results.md`](results.md).

## Results

See [`results.md`](results.md) for the full results matrix, verbatim doc quotes, and `(a)`/`(b)`/`(c)` classifications.
