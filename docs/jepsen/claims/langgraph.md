# LangGraph: claims under test

This file lists what LangGraph's official documentation promises about persistence, durable execution, resume, interrupts and human-in-the-loop, retries, timeouts, and time travel. Each claim is quoted verbatim, with its URL, and mapped to the test that would falsify it. Workload IDs refer to [`../workloads/approval-gate.md`](../workloads/approval-gate.md) (AG, F) and [`../workloads/charge-once.md`](../workloads/charge-once.md) (CO, C).

## Provenance of this file

- Docs fetched 2026-11-22 at 23:44 UTC, from the Markdown form of each page (`<url>.md`). The server reported `last-modified: Sun, 22 Nov 2026 21:12:28 GMT`.
- The OSS docs have no version selector. We pin the claims to the packages that were current on PyPI at fetch time: `langgraph 1.2.24` (uploaded 2026-11-20), `langgraph-checkpoint 4.3.2`, `langgraph-checkpoint-sqlite 3.1.3`, `langgraph-checkpoint-postgres 3.2.1`, `langgraph-prebuilt 1.1.3`, `langchain 1.5.2`. A report re-fetches every quote and fails the claim check if any quote changed.
- `https://docs.langchain.com/oss/python/langgraph/durable-execution` now returns HTTP 308 to `/oss/python/langgraph/persistence`. Older citations of a "Durable execution" page point at content that no longer exists under that name.
- Quotes keep the docs' own Markdown. Where a quote is cut, `...` marks the cut.

Pages:

| Short name | URL |
| --- | --- |
| overview | https://docs.langchain.com/oss/python/langgraph/overview |
| persistence | https://docs.langchain.com/oss/python/langgraph/persistence |
| checkpointers | https://docs.langchain.com/oss/python/langgraph/checkpointers |
| fault-tolerance | https://docs.langchain.com/oss/python/langgraph/fault-tolerance |
| graph-api | https://docs.langchain.com/oss/python/langgraph/graph-api |
| interrupts | https://docs.langchain.com/oss/python/langgraph/interrupts |
| time-travel | https://docs.langchain.com/oss/python/langgraph/use-time-travel |
| functional-api | https://docs.langchain.com/oss/python/langgraph/functional-api |
| hitl | https://docs.langchain.com/oss/python/langchain/human-in-the-loop |
| hitl-overview | https://docs.langchain.com/oss/python/concepts/human-in-the-loop |

## Claims most likely to fail

These are directly testable, and the predicted result contradicts the quoted text.

1. **LG-C24, default retry predicate.** The docs say the default policy does not retry `OSError` or its subclasses, and retries HTTP library exceptions only on 5xx. The 1.2.24 code retries `ConnectionError` (an `OSError` subclass), retries every `requests.HTTPError` whose response is a 4xx or 5xx, and retries every `httpx` transport error. We confirmed this at the predicate level (details under LG-C24). A full graph run has not been done yet. Under F4 kind 3, a non-idempotent call that the docs say will not be retried is retried.
2. **LG-C29, time travel re-triggers interrupts.** The docs say interrupts "are always re-triggered during time travel". In the Graph API approval pattern (T1) and the batched middleware (T2), the gate runs in a node before the tool node. Replaying from the checkpoint between them should run the tool again without a new interrupt, so one approval pays twice (AG4). This matches the "Authority Resurrection" class in the ACRFence paper (section below).
3. **LG-C13 and LG-C16, unkeyed resume.** `Command(resume=True)` names no gate. A redelivered approval sent while the thread waits on the next payment should be consumed by that next gate, so `INV-2` is paid on an approval the human gave for `INV-1` (F6 kind 3, AG4). The batched middleware matches decisions by position, with the same effect. This is predicted from the documented semantics, so it is class 3, and it goes through the security channel because it is an approval bypass.
4. **LG-C18, conditional gate.** The docs say a `when` predicate that flips to `False` on resume runs the tool "even a `reject`". A predicate that reads live data (an allowlist) turns an external edit into an approval bypass (T2w, F7 kind 5, AG2). The behavior is documented, so the finding is class 2.
5. **LG-C06, default durability.** The default mode is `"async"`, which the docs say can lose checkpoints on a crash. Tutorial variants inherit it. Under C3 the model is asked again after restore, a new tool-call id is minted, and a key derived from it no longer deduplicates (CO1).

## Persistence and checkpointing

### LG-C01

> A checkpointer saves a snapshot of graph state at each super-step, organized into **threads**.

checkpointers. Test: with `durability="sync"`, the probe must see one committed `put` for every completed super-step, before any node of the next super-step is dispatched. Falsified by a super-step whose successor runs with no committed checkpoint. Prediction: holds.

### LG-C02

> **Pending writes**: When a graph node fails mid-execution at a given [super-step](#super-steps), LangGraph stores pending checkpoint writes from any other nodes that completed successfully at that super-step. When you resume graph execution from that super-step you don't re-run the successful nodes.

checkpointers. Test: charge-once variant with a fan-out super-step, `charge` and a sibling `notify` that raises. Recover with `invoke(None, config)`. Repeat with `crash:ckpt.writes.task.after#1` on `charge`'s write. Falsified if the `charge` stub is called a second time. Prediction: holds when the task write is durable. With `crash:ckpt.writes.task.before#1` it is expected to re-run, which the claim does not cover.

### LG-C03

> | Continue after an error | `graph.invoke(None, config)` | Loads the latest checkpoint and retries unfinished work. Omit `checkpoint_id`. |

checkpointers. Test: every `recover` call in both workloads uses this form. Falsified if recovery after any crash point leaves unfinished work that never runs (AG8, CO6), or runs a node whose result is in the latest committed checkpoint. Prediction: holds. Re-running work whose effect landed but whose result is not durable is expected, not a violation (LG-C23).

### LG-C04

> Resume returns the saved result without running nodes when the latest checkpoint has no unfinished work.

checkpointers. Test: F6 kind 1. Deliver the last approval again, and call `invoke(None, config)`, after the thread is terminal. Falsified if any stub is called. Prediction: holds.

### LG-C05

> * `"sync"`: LangGraph persists changes synchronously before the next step starts. This ensures that LangGraph writes every checkpoint before continuing execution, providing high durability at the cost of some performance overhead.

checkpointers. Test: hardened variants run with `durability="sync"`. For every crash point, the latest committed checkpoint after recovery must include every super-step that completed before the kill. Falsified by a lost completed super-step. Prediction: holds.

### LG-C06

> * `"async"`: LangGraph persists changes asynchronously while the next step executes. This provides good performance and durability, but there is a small risk that LangGraph does not write checkpoints if the process crashes during execution.

checkpointers. The default comes from the `stream` docstring in `langgraph/pregel/main.py` at 1.2.24: "durability: The durability mode for the graph execution, defaults to `"async"`." Test: run T1, T2, and charge-once T1 under C3 and F1 with the default. Measure the fraction of kills after which a completed super-step is missing, and the fraction that leads to a duplicate effect. Not falsifiable as stated ("small risk"). It is a class 2 candidate: the default mode loses the record of a side effect. Prediction: a measurable rate of lost steps, and duplicates in variants without a durable key.

### LG-C07

> ... This provides the best performance for long-running graphs but means intermediate state is not saved, so you cannot recover from system failures (like process crashes) mid-execution.

checkpointers, about `"exit"`. Test: one confirmation cell per workload with `durability="exit"`. Prediction: holds. Reported to document the mode, not as a finding.

### LG-C08

> In every mode, a crash before the first checkpoint becomes durable leaves the thread indistinguishable from a thread that was never invoked: `get_tuple()` returns `None` in both cases, and `invoke(None, config)` raises `EmptyInputError`.

checkpointers. Test: `crash:ckpt.put.before#1` in each mode, then `recover`. Falsified if `get_tuple()` returns a checkpoint, or `recover` does anything but raise `EmptyInputError`. Prediction: holds. No effect is at risk, because the first node is the model.

## Interrupts and human-in-the-loop

### LG-C09

> When an interrupt is triggered, LangGraph saves the graph state using its [persistence](https://docs.langchain.com/oss/python/langgraph/persistence.md) layer and waits indefinitely until you resume execution.

interrupts. Test: `human:` kind 2 (approve after `expires_at`) and `fault:control.recover` kind 1. Falsified if a pending interrupt is lost or expires on its own. Prediction: holds, and because it holds, AG5 fails in every variant except H2. That is class 3: the framework has no notion of approval age.

### LG-C10

> * **Checkpointing keeps your place:** the checkpointer writes the exact graph state so you can resume later, even when in an error state.

interrupts. Test: F2 and F3 in every variant. Falsified if, after a kill, the gate disappears, a different gate appears for the same call, or a durable decision is lost. Prediction: holds with SQLite and Postgres. Fails trivially with `InMemorySaver`, which the docs already warn about.

### LG-C11

> 3. The [checkpointer](https://docs.langchain.com/oss/python/langgraph/persistence.md) saves the agent's state, so the run can stop and resume later, even in another process.

hitl-overview. Test: F2 in boundary mode. The decision is delivered to a fresh process. Falsified if the decision is not applied, the human is asked again, or the tool runs twice. Prediction: holds.

### LG-C12

> * The node restarts from the beginning of the node where the [`interrupt`](https://reference.langchain.com/python/langgraph/types/interrupt) was called when resumed, so any code before the [`interrupt`](https://reference.langchain.com/python/langgraph/types/interrupt) runs again

interrupts. Test: T3 counts executions of the code before `interrupt()` inside `transfer_funds`. Falsified if the count after one resume is not 2. Prediction: holds. It matters for F7: in T3 the restarted node still uses the model's original arguments, so an invoice edited during the wait is paid with stale data (AG6).

### LG-C13

> When a node contains multiple interrupt calls, LangGraph keeps a list of resume values specific to the task executing the node. Whenever execution resumes, it starts at the beginning of the node. For each interrupt encountered, LangGraph checks if a matching value exists in the task's resume list. Matching is **strictly index-based**, so the order of interrupt calls within the node is important.

interrupts. Together with "The value passed to `Command(resume=...)` becomes the return value of the [`interrupt`](https://reference.langchain.com/python/langgraph/types/interrupt) call". Test: F6 kind 3 in T1 and T3. Falsified (in the safe direction) if the stale `Command(resume=True)` is rejected or ignored. In 1.2.24, `map_command` in `langgraph/pregel/_io.py` writes an unkeyed resume as a `(NULL_TASK_ID, RESUME, value)` write, not bound to any interrupt id. Prediction: the stale approval answers the `INV-2` gate, and AG4 fails. Class 3, security channel.

### LG-C14

> * Keep authorization and request edits at their own boundaries. A changed request needs a new governed job, not a resume payload that rewrites the original.

interrupts, section "When approval lives outside the graph". The same page's full "Interrupts in tools" example does the opposite: it reads `final_to = response.get("to", to)` from the resume payload, under the comment "Resume with approval and optionally edited arguments". Test: none needed for the inconsistency itself. It is reported as a docs finding. H2 implements the recommended pattern, and F6 kind 3 and F7 test whether it holds. Prediction: H2 passes AG4 and AG6.

### LG-C15

> * ✅ After a long interrupt, re-read external state (price, inventory, policy) before the mutating side effect. Do not assume the pre-pause world is still valid.

interrupts. Test: F7 at `#3k+2` and `#3k+3` in every variant. T1, T2, and T3 do not re-read. Prediction: they pay with the old intent after the invoice changed (AG6 fails, class 2, since the docs advise the re-read but their examples omit it). H1 and H2 re-read and refuse. The docs do not say what to do when the re-read differs. H1 refusing leaves the invoice unpaid, which AG3q accepts because the invoice was edited.

### LG-C16

> | ✅ `approve` | Execute the tool with the original arguments as proposed by the agent. |

hitl and hitl-overview. Test: AG4 under F6 kind 3 in T2 (batched, decisions matched by position) and H1 (per-call, keyed by interrupt id). Falsified if any effect's intent differs from what the approver was shown for the decision that released it. Prediction: T2 fails, because a stale positional decision list answers the next gate. H1 is unknown: what LangGraph does with a resume keyed by an interrupt id that is no longer pending is not documented. Either rejecting or ignoring it passes.

### LG-C17

> | ❌ `reject` | Skip executing this tool call entirely and return rejection feedback to the agent. |

hitl and hitl-overview. Test: `human:pay-INV-1#1 = 1` combined with F1, F3, F5, F6 kind 2, and F8 kind 1. Falsified if a rejected call has any effect (AG2). Prediction: holds in T2 and H1. F8 kind 1 is the risk: replaying from a checkpoint before the tool node may re-run the decision path.

### LG-C18

> The predicate runs again when the agent resumes, so it must return the same result for the same call. If it returns `False` on resume, the tool runs and the reviewer's answer is ignored, even a `reject`.

hitl, section "Conditional interrupts". Test: T2w with `human:pay-INV-1#1 = 1` and `actor:invoice_editor#1 = 5`, which adds `acct-A` to the allowlist while the gate waits. Expected: the tool runs after the reject (AG2 fails). The behavior is documented, so the finding is class 2: a predicate that reads mutable data is a natural use of `when`, and the result is an approval bypass.

### LG-C19

> LangGraph checks each answer against the interrupt's `response_schema` before it saves the answer. An invalid answer raises a `ValidationError` and nothing is saved, so you can send a corrected answer to the same interrupt ID.

hitl, per-call mode. Test: H1 delivers a malformed decision, then a valid one. The probe must see no resume write for the malformed one. Falsified if a resume write appears before the valid decision, or the tool runs twice. Prediction: holds.

### LG-C20

> | An answer is saved before it is checked | One decision for two calls raises `ValueError: Number of human decisions (1) does not match number of hanging tool calls (2).` The corrected answer then fails with the same error, so the thread stays stuck. | The answer is checked first. Fix it and answer again. |

hitl-overview, describing batched mode. Test: T2 with `agent#2 = 2` (two calls in one message) and a one-element decision list, then the corrected list. Prediction: AG8 fails as described. Reported as a class 3 confirmation. The docs already name it.

### LG-C21

> You can also answer interrupts one at a time. An interrupt you do not answer stays pending, and its tool runs once it gets an answer.

hitl, per-call mode. Test: H1 with `human:` kind 2. Prediction: holds, so the tool runs on an answer of any age (AG5 fails, class 3). This is the per-call form of LG-C09.

### LG-C22

> * List `HumanInTheLoopMiddleware` before tool retry or error-handling middleware, such as [ToolRetryMiddleware](https://reference.langchain.com/python/langchain/agents/middleware/tool_retry/ToolRetryMiddleware) and [ToolErrorMiddleware](https://reference.langchain.com/python/langchain/agents/middleware/tool_error/ToolErrorMiddleware), so it wraps them. Otherwise a retried call asks the reviewer again, and an invalid answer is retried or returned to the model as a tool error.

hitl. Test: H1 with `ToolRetryMiddleware` listed before and after `HumanInTheLoopMiddleware`, under F4 kinds 1 and 3. Falsified if the correct order asks the reviewer again on retry. Prediction: holds. With the correct order, the retry re-executes the payment without asking again, which is safe only because of the key.

## Retries, timeouts, and error handling

### LG-C23

> A [retry](#retries) re-runs the entire node function from the start. That gives **at-least-once** semantics for node execution: when a policy matches, LangGraph invokes the node again. It does not make external side effects happen exactly once.

fault-tolerance. Test: F4 and C4 with `RetryPolicy()`. Duplicates are expected in variants without a durable key. Prediction: holds. Duplicates in T1 are class 2 (the tutorial approval example has no key), not class 1.

### LG-C24

> By default, `retry_on` uses `default_retry_on`, which retries on **any** exception except the following (and their subclasses):

followed by a list that ends with `OSError`, then:

> For exceptions from popular HTTP libraries such as `requests` and `httpx`, it only retries on 5xx status codes.

fault-tolerance. The code at 1.2.24, `langgraph/_internal/_retry.py`, checks `isinstance(exc, ConnectionError)` and returns `True` before it checks the exclusion list. For `requests.HTTPError` it returns `500 <= exc.response.status_code < 600 if exc.response else True`. A `requests.Response` is falsy when its status is 400 or above, so every 4xx or 5xx `requests.HTTPError` takes the `True` branch. `httpx` transport errors are neither `HTTPStatusError` nor `OSError`, so they reach the final `return True`.

Predicate-level check: we copied `default_retry_on` from the 1.2.24 wheel and called it under Python 3.14.7, `requests 2.32.3`, `httpx 0.28.1`.

| Exception | Docs predict | `default_retry_on` returned |
| --- | --- | --- |
| `ConnectionResetError()` | not retried (`OSError` subclass) | `True` |
| `TimeoutError()` | not retried | `False` |
| `requests.HTTPError` with a 402 response | not retried (not 5xx) | `True` |
| `requests.HTTPError` with a 503 response | retried | `True` |
| `requests.ConnectionError()` | not retried | `False` |
| `requests.ReadTimeout()` | not retried | `False` |
| `httpx.ReadTimeout` | not retried | `True` |
| `httpx.ConnectError` | not retried | `True` |
| `httpx.HTTPStatusError` with a 402 response | not retried | `False` |

Test: F4 kinds 3 and 4, and charge-once C4, with `RetryPolicy()` and a node that records `execution_info.node_attempt`. Falsified if the attempt count after a `ConnectionResetError` or `httpx.ReadTimeout` is greater than 1. Prediction: falsified, a class 1 finding against the docs. Which side is intended is for the maintainers to say. The `NodeTimeoutError` docstring shows that retrying some timeouts is deliberate. The `requests` 4xx case looks unintended.

### LG-C25

> When the limit is exceeded, LangGraph raises [`NodeTimeoutError`](https://reference.langchain.com/python/langgraph/errors/NodeTimeoutError), clears any writes from the failed attempt, and lets the retry policy decide whether to retry.

fault-tolerance, together with "Node timeouts only apply to **async** nodes" and "To wrap blocking I/O, use `asyncio.to_thread` inside an async node." Test: C5 and F4 kind 2 with the tool wrapped in `asyncio.to_thread`. Cancelling the awaiting coroutine does not stop the thread. Prediction: the timed-out attempt's charge lands while the retry is running, giving two concurrent calls. With no key, CO1 fails (class 3). With a key, the result depends on the provider's handling of concurrent requests with the same key.

### LG-C26

> Failure provenance is checkpointed. If the graph is interrupted or the process crashes after a node fails but before the handler completes, the handler sees the same `NodeError` context when the graph resumes from its checkpoint.

fault-tolerance. Test: charge-once T2, with C4 kind 3 to exhaust retries, then `crash:ckpt.put.before#<n>` on the handler's super-step. On recovery, compare the handler's `error.node` and the exception type and message with the first run. Falsified if they differ, the handler does not run, or the charge node runs again. Prediction: unknown.

### LG-C27

> | Node mid-execution | Runs to completion. Drain takes effect on the next superstep. |
> ...
> | More supersteps remain | Raises `GraphDrained(reason)`. Checkpoint is saved and resumable. |

fault-tolerance, "Graceful shutdown". Test: F5 kind 3 and C8 kind 3. Falsified if the in-flight tool is cut short, its result is not in the saved checkpoint, or resuming runs it again. Prediction: holds.

### LG-C28

> 1. **The key is reproducible from durable state.** A retry or a resume in a fresh process reconstructs the same key.

fault-tolerance, "Protect side effects with an idempotency key". Test: H1 and H2 in charge-once under C2, C3, C6, and C7. Falsified (for the recommendation, not the framework) if CO1 fails while the key was derived as the docs describe. Prediction: H1 fails under C3 with async durability, because a lost model step mints a new tool-call id. H1 passes with sync durability. C7 fails in every variant, because the provider forgets the key after 24 h. The docs do not mention key expiry.

## Time travel

### LG-C29

> Both work by resuming from a prior checkpoint. Nodes before the checkpoint are not re-executed (results are already saved). Nodes after the checkpoint re-execute, including any LLM calls, API requests, and [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts.md) (which may produce different results).

and

> If your graph uses [`interrupt`](https://reference.langchain.com/python/langgraph/types/interrupt) for [human-in-the-loop](https://docs.langchain.com/oss/python/langgraph/interrupts.md) workflows, interrupts are always re-triggered during time travel. The node containing the interrupt re-executes, and `interrupt()` pauses for a new `Command(resume=...)`.

time-travel. Test: F8 kind 1 in T1, T2, T3, and H1. Falsified (the second sentence) if a gated tool runs during replay without a new decision. Prediction: in T1 and T2 the gate node is before the replay checkpoint, so it is not re-executed, and the tool pays again on the old approval (AG3, AG4). In T3 and H1 the interrupt is inside the tool call, so it re-triggers. The first quote predicts the T1 and T2 behavior. The second reads as a stronger promise. We expect class 3, and report the second sentence as misleading for gates that live in their own node.

## Functional API

### LG-C30

> On resume, LangGraph restores completed **task** results. A **task** that started but did not finish may run again, so use idempotency keys or check existing results before writing.

functional-api. Test: charge-once FN1 under C2. Prediction: the charge task runs again (CO1 fails without a key). This holds as documented. The same page's "Common pitfalls" says: "Put side effects (writing a file, sending an email) in tasks so resume does not run them again." A reader who follows only that sentence gets the duplicate. Class 2.

## ACRFence

The paper exists: Yusheng Zheng, Yiwei Yang, Wei Zhang, Andi Quinn, "ACRFence: Preventing Semantic Rollback Attacks in Agent Checkpoint-Restore", arXiv:2603.20625 [cs.CR], v1 submitted 2026-03-21, https://arxiv.org/abs/2603.20625. We read the v1 HTML full text.

What it found:

- It names "semantic rollback attacks": after a checkpoint restore, an LLM agent re-synthesizes a slightly different tool call, so idempotency keys and request-ID dedupe do not match it, and the server performs the effect again.
- Two attack classes. **Action Replay**: a crash after a payment, then restore, then a second payment with a new reference ID. **Authority Resurrection**: restore to just after a single-use approval was granted, then use it on a different target.
- Its "survey of 12 frameworks" is Table 1, a count of public issue reports per framework (LangGraph 8+, CrewAI 5, Google ADK 4, AutoGen 3, OpenAI Agents 3, Claude Code 5, OpenClaw 6, Cursor 4, OpenHands 1, Vercel AI 1, LiveKit 2, n8n 3), with one key finding each. For LangGraph the key finding is "Tools re-fire on resume", citing langgraph issue #6208, which is still open as of 2026-11-22. The survey is a literature and issue-tracker review. It did not run tests against the frameworks.
- Its experiments use Claude Code CLI with Qwen3-32B and its built-in checkpoint restore, against simulated MCP servers. Action Replay produced duplicates in 10 of 10 checkpoint-restore trials and 0 of 10 without restore. Authority Resurrection succeeded 2 of 2 times against stateless token validation, and failed against stateful validation.
- The mitigation, ACRFence, is a proposal: an effect log at the tool boundary plus an analyzer LLM that classifies post-restore calls as replay or fork. The paper says its evaluation "does not yet include an implementation of ACRFence itself".
- The abstract on the arXiv listing says the problem was "independently acknowledged by framework maintainers". The v1 HTML says "independently reported across framework communities".

What this work adds:

- **Each framework's own claims.** We test quoted promises at pinned versions, so a result is a statement about that framework's guarantee, not about agents in general.
- **Systematic fault schedules.** Every fault is a named choice key with provenance, and the same schedule runs against every variant. ACRFence's experiments ran one fault (crash then restore) in one agent, Claude Code, with one model.
- **Reproducible.** Every failing run is a `realized` map that replays exactly, is shrunk to its minimal set of deviations, and is verified against the real framework, a real checkpointer, and real process death.
- **Quantified.** Each workload and fault cell reports a failure rate with an interval and a run count, including cells that pass.
- **Version-pinned.** Results name exact package versions and docs fetch dates. They are re-run when either changes.
- **Beyond restore.** We also test retry predicates, timeouts, cancellation, duplicate resume delivery, external edits during a wait, approval freshness, and approval-to-argument binding. ACRFence's two attack classes correspond to our F1 with `agent#` re-synthesis (Action Replay) and F8 kind 1 (Authority Resurrection).
