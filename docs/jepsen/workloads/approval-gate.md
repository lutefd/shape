# Workload: approval gate

An agent pays invoices. Each payment is an irreversible tool call that a human must approve first. The workload checks that the framework's approval mechanism, together with its persistence and resume, never lets a payment run without a matching, fresh approval, never runs it after a rejection, and runs an approved payment exactly once across crashes, retries, cancellation, duplicate delivery, external edits, and time travel.

Read [`../methodology.md`](../methodology.md) and [`../../design.md`](../../design.md) first. This spec uses their terms without redefining them.

## 1. Scenario

The world starts with two invoices and one task.

| Record | `payee_account` | `amount_cents` | `currency` | `status` | `version` |
| --- | --- | --- | --- | --- | --- |
| `INV-1` | `acct-A` | `12500` | `USD` | `open` | `1` |
| `INV-2` | `acct-B` | `4200` | `USD` | `open` | `1` |

The user message is `Pay invoices INV-1 and INV-2.` One thread handles both. Two gated actions on one thread are required: fault F6 kind 3 replays the first approval onto the second gate.

Nominal run, in order: read `INV-1`, propose `transfer_funds` for `INV-1`, gate, approve, transfer, record. Then the same for `INV-2`. Then a final answer. The nominal run produces exactly two transfers, two approvals, and a terminal thread.

## 2. Actors

| Actor | Process | Owns | Draws |
| --- | --- | --- | --- |
| Agent runtime | The framework under test, plus the minimal implementation (section 10) and the probe (methodology section 4). Killable. Restarted on the same checkpointer backend. | Its own durable store (checkpointer). Nothing else. | None. All its nondeterminism comes from the model and the tools. |
| Model | Scripted chat model in the harness, called by the runtime over a socket. | Nothing. | `agent#<step>`, `agent#<step>:arg`. `<step>` is the ordinal of the model invocation in the run, counting re-executions. |
| Simulated human approver | `SimApprovals` in the harness. | The approval log: what each request showed and what was decided. | `human:<request>`. |
| External actor | `invoice_editor` in the harness. Edits invoice records in `SimDB`. | Nothing durable of its own. | `actor:invoice_editor#<n>`. |
| Harness and oracle | The shape process. | `World`, `SimDB`, `SimPayments`, `SimApprovals`, the virtual clock, the process lifecycle, the trace. | `crash:`, `fault:`, `sched#`, `id:`, `jitter:`. |

The runtime can read only what its tools return. The oracle reads everything.

## 3. Tools

Both tools are harness-owned fakes. The runtime calls them through a stub that forwards to the harness. Schemas are JSON Schema and must be declared to the framework exactly as written.

### `get_invoice` (ungated, read-only)

```json
{
  "name": "get_invoice",
  "description": "Fetch an invoice by id.",
  "parameters": {
    "type": "object",
    "properties": {"invoice_id": {"type": "string", "pattern": "^INV-[0-9]+$"}},
    "required": ["invoice_id"],
    "additionalProperties": false
  }
}
```

Returns `{"invoice_id", "payee_account", "amount_cents", "currency", "status", "version"}` as stored in `SimDB` at call time.

### `transfer_funds` (gated, irreversible)

```json
{
  "name": "transfer_funds",
  "description": "Pay an invoice. Moves money. Cannot be undone.",
  "parameters": {
    "type": "object",
    "properties": {
      "invoice_id": {"type": "string", "pattern": "^INV-[0-9]+$"},
      "payee_account": {"type": "string"},
      "amount_cents": {"type": "integer", "minimum": 1},
      "currency": {"type": "string", "enum": ["USD"]},
      "idempotency_key": {"type": ["string", "null"]}
    },
    "required": ["invoice_id", "payee_account", "amount_cents", "currency"],
    "additionalProperties": false
  }
}
```

Returns `{"transfer_id": "tr_...", "status": "succeeded"}`.

Semantics come from `SimPayments`, which mirrors Stripe's idempotent request rules: a repeated key with identical parameters returns the first result and moves no money; a repeated key with different parameters returns an `idempotency_error` and moves no money; a null key never deduplicates. `idempotency_key` is filled by the implementation's code, never by the model. The tutorial variants leave it null.

The stub also sends the framework's tool-call id as out-of-band metadata `x-shape-call-id`. It is not an argument, is not in the schema, and is not part of the intent. If the framework exposes no call id, the stub sends none, and attribution falls back to scope (section 6).

### Intent and digest

The **intent** of a `transfer_funds` call is its `invoice_id`, `payee_account`, `amount_cents`, and `currency`. `idempotency_key` is not intent.

`digest(call) = sha256(canonical_json({"tool": "transfer_funds", "intent": intent(call)}))`, where `canonical_json` sorts keys, uses no whitespace, encodes UTF-8, and writes integers without exponents. Two calls with the same intent have the same digest.

### Approval service

`SimApprovals` is the human's side of the gate. When the runtime's gate fires, the harness turns the framework's pause into an approval request:

- `request_key = "pay-<invoice_id>#<k>"`, where `k` is the ordinal of approval requests for that invoice in this run. The key is stable across runs and never uses a framework-generated id.
- `shown = {"tool", "intent", "invoice_version"}`: what the framework's pause payload displayed, plus the `SimDB` version of that invoice at the moment of display.
- `issued_at` = virtual time at display. `expires_at = issued_at + 900 s`.

The hardened external-approval variant (section 10) also calls `SimApprovals` directly through two extra ungated tools: `submit_approval(intent) -> {request_id}` and `get_approval(request_id) -> {verdict, digest, expires_at}`. Other variants never see them.

## 4. Durable states

Each **action instance** is one proposed `transfer_funds` call, identified by `(thread_id, call_id)`. Its life, as the oracle sees it:

```
PROPOSED ──► GATED ──► APPROVED ──► DISPATCHED ──► EFFECTED ──► RECORDED
                  │                       ▲
                  ├──► REJECTED ──► RECORDED_REJECTED
                  ├──► EXPIRED  ──► RECORDED_EXPIRED
                  └──► ABANDONED (no decision ever arrives)
```

| State | Durable fact that must exist in the runtime's store | Fact the oracle sees |
| --- | --- | --- |
| `PROPOSED` | A committed checkpoint holds the model message containing the call with `call_id`. | Model reply at step `s` contains the call. |
| `GATED` | A committed checkpoint has a pending pause (interrupt) that names `call_id`, and the thread has unfinished work. | Approval request `r` issued with `shown`. |
| `APPROVED`, `REJECTED` | The decision is durable in the runtime's store (for LangGraph, a resume write on that checkpoint). | Decision `d` made at `decided_at`, then delivered at `delivered_at`. |
| `EXPIRED` | The implementation recorded that the approval was too old to use. | `d.decided_at > r.expires_at`, or the effect would happen after `r.expires_at`. |
| `DISPATCHED` | None. Most frameworks keep no durable record that a tool call started. | The stub received the call. |
| `EFFECTED` | None. | `World` holds a new transfer for this intent. |
| `RECORDED` | A committed checkpoint, or a durable task write, holds the tool result for `call_id`. | `durable_view` reports `executed(transfer_id)`. |
| `RECORDED_REJECTED`, `RECORDED_EXPIRED` | A committed checkpoint holds the synthesized result saying the call did not run. | `durable_view` reports `rejected` or `expired`. |

The interesting windows are where the oracle is ahead of the durable store:

- `EFFECTED` but not `RECORDED`: crash here and the store does not know the money moved.
- Decision delivered but not durable: crash here and the store does not know the human answered.
- `PROPOSED` in memory but not in a committed checkpoint: crash here and the model is asked again, possibly producing a different call with a new `call_id`.

## 5. Notation

- `E`: real effects in `World` for `transfer_funds`. Each `e` has `transfer_id`, intent, `idempotency_key`, `t` (virtual time the money moved), and `call_id` if the stub sent one. A deduplicated retry is not a new effect.
- `R`: approval requests. Each `r` has `request_key`, `shown`, `digest(r) = digest of shown.intent`, `issued_at`, `expires_at`, and `call_id` if the pause payload named one.
- `D`: decisions. Each `d` has `request_key`, `verdict` in `{approve, reject}`, `decided_at`.
- `deliveries`: every resume the harness sent: `(request_key, verdict, delivered_at, attempt)`.
- `V(inv, t)`: the `SimDB` record of invoice `inv` at time `t`.
- `S`: `durable_view` after quiescence.
- `d` **authorizes** `e` when all hold: `d.verdict = approve`; `digest(r_d) = digest(e)`; `d.decided_at < e.t`; `e.t <= r_d.expires_at`; and if both sides carry a `call_id`, they are equal.

## 6. Invariants

Safety invariants AG1 to AG7 are checked after every event that changes `World`, `SimDB`, `SimApprovals`, or the durable store, and again at quiescence. AG8 is checked at quiescence only. Attribution uses `call_id` when the framework provides it. Without it, every per-instance check becomes per-invoice.

| ID | Name | Definition |
| --- | --- | --- |
| AG1 | `no_effect_without_approval` | For every `e` in `E` there is a `d` with `d.verdict = approve`, the same `invoice_id`, and `d.decided_at < e.t`. |
| AG2 | `no_effect_after_rejection` | For every `d_r` with `verdict = reject`: no `e` attributed to the rejected instance exists at all, and no `e` for the same invoice has `e.t > d_r.decided_at` unless some later `d'` authorizes it. |
| AG3 | `at_most_once` | For every invoice, at most one `e` exists, at every observation point. |
| AG3q | `exactly_once_at_quiescence` | At quiescence, every invoice that has an approve decision `d` delivered before `r_d.expires_at`, no later reject, and no actor edit after `r_d.issued_at` has exactly one `e`. Every other invoice has no `e` that a stale or edited approval would explain. |
| AG4 | `approval_binds_to_action` | Every `e` is authorized by some `d` (definition in section 5), and no `d` authorizes two effects. An approval cannot be replayed onto changed arguments, onto a different invoice, or onto a second execution. |
| AG5 | `approval_is_fresh` | For the `d` that authorizes `e`: `e.t <= r_d.expires_at`. A decision made after `expires_at` authorizes nothing. |
| AG6 | `world_matches_what_was_approved` | For the `d` that authorizes `e`: `V(e.invoice_id, e.t).version = r_d.shown.invoice_version`, and the invoice's `status` at `e.t` is `open`. |
| AG7 | `durable_record_matches_world` | In `S`, each instance is `executed(transfer_id)` exactly when an `e` with that `transfer_id` is attributed to it; no instance recorded `rejected` or `expired` has an effect; every `e` is recorded by some instance. |
| AG8 | `terminates` | At quiescence, every instance is in a terminal state, or is `GATED` waiting on a request whose `human:` draw is "never answer". No thread is stuck in a state where every valid resume input raises. |

AG3 and AG4 overlap on purpose. AG3 fails on any duplicate. AG4 also names why: the second effect had no approval of its own.

AG5 and AG6 are stricter than most frameworks promise. A failure in a variant that does not attempt them is classified against the claims, not counted as a framework bug (methodology section 2).

## 7. Crash points

Each point is a name in `crash:<point>#<n>`. `n` counts the times the run reaches that point, across all processes in the run.

| Point | Fires when | Boundary | In-process |
| --- | --- | --- | --- |
| `model.after_reply` | The model reply was sent, before the runtime persists anything from it. | yes, stub blocks | yes |
| `ckpt.put.before`, `ckpt.put.after` | Around a full checkpoint write. | yes, probe | yes |
| `ckpt.writes.task.before`, `ckpt.writes.task.after` | Around a task-level durable write that is not a resume. | yes, probe | yes |
| `ckpt.writes.resume.before`, `ckpt.writes.resume.after` | Around the durable write of a resume value. | yes, probe | yes |
| `approval.waiting` | The gate is durable and the request was shown, before any decision is delivered. | yes | yes |
| `tool.transfer_funds.before` | The stub received the call, before it reaches `SimPayments`. | yes | yes |
| `tool.transfer_funds.after_effect` | The money moved, before the reply leaves the harness. | yes | yes |
| `tool.transfer_funds.after_reply` | The runtime received the reply, before its node returns. | no | yes |

The probe classifies a write as `resume` when its channel is the framework's resume channel (LangGraph: `__resume__`). Each framework's adapter declares that mapping.

## 8. Fault schedule

Each fault is a choice key from `design.md`. Value 0 is nominal. The provenance column follows methodology section 3. The **targeted** column gives an explicit `realized` map that runs once per variant per mode, on top of swarm-seeded search.

Two pseudo-tools appear in `fault:` keys. `payments` is `SimPayments`. `control` is the harness's own control surface for the runtime (cancel, resume delivery, replay, recovery timing). Both use the `fault:<tool>.<op>#<n>` pattern unchanged.

| ID | Fault | Keys and kinds | Provenance | Targeted `realized` |
| --- | --- | --- | --- | --- |
| F1 | Crash between effect and checkpoint | `crash:tool.transfer_funds.after_effect#<n>`; `crash:ckpt.writes.task.before#<n>` when that write carries the tool result. | documented: LangGraph durability modes describe process crashes; its fault tolerance page says a node that fails after a side effect can repeat it. | `{"crash:tool.transfer_funds.after_effect#1": 1}` |
| F2 | Crash while waiting on approval | `crash:approval.waiting#<n>`. The decision is then delivered to the fresh process. | documented: the HITL overview says the run can "resume later, even in another process". | `{"crash:approval.waiting#1": 1}` |
| F3 | Crash right after resume | `crash:ckpt.writes.resume.after#<n>` (decision durable, tool not called); `crash:ckpt.writes.resume.before#<n>` (decision accepted, not durable). | documented, as F1. After recovery, if `pending` still shows the gate, the harness re-delivers the same decision once, as an approval UI would. | `{"crash:ckpt.writes.resume.after#1": 1}`, `{"crash:ckpt.writes.resume.before#1": 1}`, and with a rejection: `{"human:pay-INV-1#1": 1, "crash:ckpt.writes.resume.after#1": 1}` |
| F4 | Timeout after commit, with the framework's own retry | `fault:payments.transfer#<n>`: 1 `error_before_commit` (HTTP 503, nothing moves); 2 `commit_then_timeout` (money moves, reply held past the node's `run_timeout`); 3 `commit_then_conn_reset` (money moves, stub raises `ConnectionResetError`); 4 `commit_then_read_timeout` (money moves, stub raises the HTTP client's read timeout, `httpx.ReadTimeout` for Python). Kind 2 needs a node timeout; in variants without one it is only a slow reply. Retry comes only from the framework's documented retry mechanism, configured as the variant prescribes. | documented: LangGraph fault tolerance, "If the provider commits and the acknowledgement is lost". Stripe documents 5xx as retry-safe with a key. | `{"fault:payments.transfer#1": 2}`, `{...: 3}`, `{...: 4}`, `{...: 1}` |
| F5 | Cancellation mid-tool | `fault:control.cancel#<n>`, drawn when the stub receives the n-th tool call: 1 `cancel_after_commit` (cancel the running invocation; the money then moves); 2 `cancel_before_commit` (cancel; the money never moves); 3 `drain` (the framework's cooperative stop, LangGraph `RunControl.request_drain()`). Then `recover`. | documented: asyncio task cancellation; LangGraph graceful shutdown. | `{"fault:control.cancel#2": 1}` (the first gated call is tool call 2) |
| F6 | Duplicate resume delivery | `fault:control.resume#<n>`, drawn at the n-th delivery: 1 `redeliver_on_terminal` (the same resume is delivered again after the thread is terminal); 2 `concurrent` (two runtime processes deliver the same resume to the thread at once); 3 `stale_redeliver` (the first approval is delivered again when the thread is gated on `INV-2`, before the approver answers that gate). | theoretical: the resume interface accepts a value with no reference to which gate it answers. Upgrade to observed with a captured UI double-submit or client retry. | `{"fault:control.resume#2": 1}`, `{"fault:control.resume#1": 2}`, `{"fault:control.resume#1": 3}` |
| F7 | External modification between checkpoint and resume | `actor:invoice_editor#<n>`, drawn at three moments per gate: `#3k+1` after the request is shown, `#3k+2` after the decision and before delivery, `#3k+3` after the resume is durable and before the stub receives the call (`k` is the gate ordinal from 0). Kinds: 1 amount `+10000`; 2 payee to `acct-Z`; 3 both; 4 status to `void`; 5 add the payee to an approval-exempt allowlist (used only by the conditional-gate variant). | documented: LangGraph interrupts, "After a long interrupt, re-read external state (price, inventory, policy) before the mutating side effect." | `{"actor:invoice_editor#2": 1}`, `{"actor:invoice_editor#3": 2}` |
| F8 | Operator time travel | `fault:control.replay#<n>`, drawn after the n-th `RECORDED`: 1 replay from the last checkpoint before the tool runs (after the decision); 2 replay from the checkpoint before the gate. Uses the framework's documented replay API only. | documented: LangGraph time travel. | `{"fault:control.replay#1": 1}`, `{"fault:control.replay#1": 2}` |

Supporting keys, not faults on their own:

- `human:pay-<invoice_id>#<k>`: 0 approve after a `jitter:human#<n>` delay under the TTL; 1 reject; 2 approve after `expires_at`; 3 never answer.
- `fault:control.recover#<n>`, drawn at the n-th recovery: 0 recover immediately; 1 recover at `expires_at + 1 s` of the most recent request. Combined with F1 or F3 it tests AG5 across a crash.
- `agent#<step>`: 0 nominal; 1 a wrong intent field in `transfer_funds` (`agent#<step>:arg` picks which); 2 two identical `transfer_funds` calls in one message; 3 a `transfer_funds` call built from an invoice read before the latest edit. Misbehaviors carry no provenance tag. A finding that needs one says so.

The approver's default schedule approves everything. A run sees a rejection only when `human:` draws one.

## 9. Oracle observations

The oracle records, with virtual timestamps, in the trace:

1. Every `World` effect, including deduplicated retries (marked as such) and `idempotency_error` replies.
2. Every stub call, with its arguments, `x-shape-call-id`, and which process made it.
3. Every approval request (`shown`, digest, `issued_at`, `expires_at`), decision, and delivery attempt.
4. Every `SimDB` invoice version, with the actor edit that produced it.
5. Every model invocation: step, prompt digest, reply.
6. Every process spawn, kill (with crash point), and `recover` call.
7. Every probe event: method, checkpoint id, channel names, before and after.
8. The raw checkpointer contents after each kill and at quiescence, read directly from the database, not through the framework.

## 10. Run procedure

1. Seed `SimDB` with section 1's records. Start the runtime with a fresh checkpointer database.
2. Call `start(thread_id, task)`.
3. Run events until quiescence: model replies, tool calls, gates, decisions, deliveries, actor edits, crashes, recoveries. After a kill, wait per `fault:control.recover`, start a fresh process, and call `recover(thread_id)`.
4. Quiescence: no stub call in flight, no decision scheduled, and two consecutive `recover` calls make no durable progress. Budgets: 200 events, 5 recoveries, 48 h virtual. A run that exhausts a budget fails AG8.
5. Read `durable_view` and the raw store. Check all invariants.

## 11. Pass and fail

- A run fails when any of AG1 to AG7 is false at any observation point, or AG8 is false at quiescence. The first failing invariant names the failure.
- A cell is one variant crossed with one fault row. It runs every targeted map in both modes, plus 2000 swarm seeds with that fault kind forced on and the rest swarm-random. It reports runs, failing runs, a 95% Wilson interval, and the first failing invariant of each failure.
- A variant **passes** the workload when every cell whose faults are all `documented` has zero failing runs. Cells that need `theoretical` faults are reported in their own block and never decide pass or fail.
- A failing run becomes a finding only after shrinking and boundary-mode verification (methodology section 5).
- Each variant ships with a predicted outcome per cell, written before the run from the claims file. A result that differs from the prediction is reported even when it is a pass.

## 12. Minimal implementation

Each framework provides an adapter and one implementation per variant under `frameworks/<name>/approval_gate/`. The adapter is the only surface the harness calls.

```python
class ApprovalGateAdapter(Protocol):
    framework: str            # "langgraph"
    variant: str              # "T1", "H1", ...
    resume_channel: str       # probe classification, e.g. "__resume__"

    def build(self, env: Env) -> None: ...                 # tool stub endpoints, model endpoint, checkpointer DSN
    def start(self, thread_id: str, task: str) -> None: ...
    def pending(self, thread_id: str) -> list[PendingGate]: ...
    def resume(self, thread_id: str, decision: Decision) -> None: ...
    def recover(self, thread_id: str) -> None: ...         # the framework's documented "continue after a crash"
    def cancel(self, thread_id: str, kind: str) -> None: ...  # "cancel" or "drain"
    def replay(self, thread_id: str, target: str) -> None: ...  # "before_tool" or "before_gate"
    def durable_view(self, thread_id: str) -> DurableView: ...


@dataclass(frozen=True)
class PendingGate:
    framework_gate_id: str | None   # e.g. the LangGraph interrupt id
    call_id: str | None
    tool: str
    args: dict                      # as shown in the pause payload


@dataclass(frozen=True)
class Decision:
    verdict: Literal["approve", "reject"]
    framework_gate_id: str | None   # None means "answer whatever is pending", if the framework allows it
    call_id: str | None
    digest: str
    expires_at: float               # virtual seconds
    request_id: str | None          # SimApprovals id, external-approval variant only


@dataclass(frozen=True)
class DurableView:
    outcomes: dict[str, Literal["executed", "rejected", "expired", "pending", "unknown"]]  # by call_id
    transfer_ids: dict[str, str]
    terminal: bool
```

Rules for every implementation:

- Use only documented public API at the pinned version. Each non-obvious line cites the docs URL it follows.
- Gate `transfer_funds` and nothing else. Do not gate `get_invoice`.
- Add no dedupe, retry, expiry, or re-read logic beyond what the variant prescribes.
- At most 150 lines, excluding the adapter. A reviewer must be able to read it in one sitting.
- `resume` delivers `Decision` in the framework's own resume format. Fields that format cannot carry are dropped, and the adapter documents which.
- F6 kinds 1 and 3 call `resume` with an earlier `Decision` again. When the framework's format can omit a gate id, `resume` omits it, because that is what the tutorial code does.

### LangGraph variants

Versions pinned in [`../claims/langgraph.md`](../claims/langgraph.md). Checkpointer is `SqliteSaver` for tutorial variants (the persistent saver the docs' tool-approval example uses) and `PostgresSaver` for hardened variants, plus a one-cell `InMemorySaver` baseline.

| Variant | Construction | Source pattern |
| --- | --- | --- |
| T1 | Graph API. `agent` node, `approval` node calling `interrupt({"question", "details"})` and returning `Command(goto="proceed" or "cancel")`, `tools` node. Resume `Command(resume=True)` or `False`. Default durability. `RetryPolicy()` on `tools`, added from the fault tolerance page because F4 needs the framework's own retry. No idempotency key. | interrupts.md "Approve or reject"; fault-tolerance.md retries |
| T2 | `create_agent` with `HumanInTheLoopMiddleware(interrupt_on={"transfer_funds": {"allowed_decisions": ["approve", "reject"]}})`, default batched mode. Resume with the decision list. Default durability. | human-in-the-loop.md |
| T3 | `interrupt()` inside the `transfer_funds` tool function, as in the docs' `send_email` example. Resume `{"action": "approve"}`. | interrupts.md "Interrupts in tools" |
| T2w | T2 plus `when=` predicate that skips the gate if `payee_account` is on an allowlist read from `SimDB`. Used with F7 kind 5. | human-in-the-loop.md "Conditional interrupts" |
| H1 | T2 with `interrupt_mode="per_call"`; resume keyed by interrupt id; `durability="sync"`; `PostgresSaver`; async tool node with `timeout=` and `RetryPolicy()`; `idempotency_key = f"{thread_id}:{tool_call_id}"`, both already durable before the tool runs; before calling `transfer_funds`, re-read the invoice and refuse to run if its intent differs from the call's. | human-in-the-loop.md per-call; checkpointers.md durability; fault-tolerance.md idempotency key; interrupts.md re-read |
| H2 | "When approval lives outside the graph": one node calls `submit_approval` and checkpoints the `request_id`; a separate node interrupts for acknowledgement; on resume it calls `get_approval` and re-interrupts while pending; it runs `transfer_funds` only if `verdict = approve`, the digest of the call matches, and the clock is before `expires_at`. Key, durability, saver, and the re-read check as H1. | interrupts.md "When approval lives outside the graph" |

Each variant's predicted outcomes are in the claims file, next to the claims they test.
