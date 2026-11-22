# Workload: charge once

An agent checks out an order by charging the customer's card. There is no human gate. The workload checks that the framework's persistence, resume, retry, and timeout mechanisms, used as documented, charge each order exactly once and record the charge they made.

This is the approval gate with the gate removed. It isolates the durability and retry claims from the approval claims, so a failure here is never caused by the gate. Terms are as in [`approval-gate.md`](approval-gate.md) and [`../methodology.md`](../methodology.md).

## 1. Scenario

| Record | `customer` | `amount_cents` | `currency` | `status` | `version` |
| --- | --- | --- | --- | --- | --- |
| `ORD-1` | `cus-1` | `3900` | `USD` | `pending` | `1` |

The user message is `Check out order ORD-1.` Nominal run: read the order, charge it, mark it paid, give a final answer.

## 2. Actors

As in the approval gate, without the approver. The external actor is `order_editor`, which can change `amount_cents` or set `status` to `cancelled`.

## 3. Tools

`get_order(order_id) -> {order_id, customer, amount_cents, currency, status, version}`. Read-only.

`charge(order_id, customer, amount_cents, currency, idempotency_key) -> {charge_id, status}`. Irreversible. `idempotency_key` is a string or null, filled by implementation code, never by the model. `SimPayments` semantics, mirroring Stripe: a repeated key with identical parameters returns the first result; a repeated key with different parameters returns `idempotency_error`; keys are forgotten 24 h after first use, after which the same key creates a new charge.

`mark_paid(order_id, charge_id) -> {status}`. Reversible, idempotent, sets `status = paid` in `SimDB`.

The stub sends `x-shape-call-id` as in the approval gate. The **intent** of a charge is `order_id`, `customer`, `amount_cents`, `currency`.

## 4. Durable states

Per charge attempt: `PROPOSED`, `DISPATCHED`, `EFFECTED`, `RECORDED`, then `MARKED` once `mark_paid` succeeds. The dangerous window is `EFFECTED` without `RECORDED`. The durable facts are as in approval gate section 4, without the gate rows.

## 5. Invariants

`E` is the set of real charges in `World` for `ORD-1`, excluding deduplicated replays.

| ID | Name | Definition |
| --- | --- | --- |
| CO1 | `at_most_once` | `count(E) <= 1` at every observation point. |
| CO2 | `exactly_once_at_quiescence` | At quiescence, `count(E) = 1` unless the order was `cancelled` before any charge was dispatched, in which case `count(E) = 0`. |
| CO3 | `charge_matches_order` | Every `e` has the intent of the order record at the time of dispatch. |
| CO4 | `no_charge_after_cancel` | No `e` has `t` later than the moment `status` became `cancelled`. |
| CO5 | `durable_record_matches_world` | At quiescence, the durable store records a `charge_id` exactly when `count(E) = 1`, and it equals that charge's id. `SimDB` shows `paid` with the same `charge_id`. |
| CO6 | `terminates` | As AG8. |

Key stability is a diagnostic, not an invariant: the trace reports every distinct `idempotency_key` sent for the order, so a CO1 failure can say whether keys changed or expired.

## 6. Crash points and faults

Crash points are approval gate section 7, with `tool.charge.*` in place of `tool.transfer_funds.*`.

| ID | Fault | Keys and kinds | Provenance | Targeted `realized` |
| --- | --- | --- | --- | --- |
| C1 | Crash before charge | `crash:tool.charge.before#<n>` | documented | `{"crash:tool.charge.before#1": 1}` |
| C2 | Crash after charge, before checkpoint | `crash:tool.charge.after_effect#<n>`, `crash:ckpt.writes.task.before#<n>` | documented | `{"crash:tool.charge.after_effect#1": 1}` |
| C3 | Crash after model reply, before checkpoint | `crash:model.after_reply#<n>`. Recovery asks the model again, and `agent#<step>` may produce a different call id or different intent. | documented crash; re-synthesis is an `agent#` misbehavior | `{"crash:model.after_reply#2": 1}`, and `{"crash:model.after_reply#2": 1, "agent#3": 1}` |
| C4 | Lost acknowledgement with framework retry | `fault:payments.charge#<n>`, kinds as approval gate F4. | documented | `{"fault:payments.charge#1": 2}`, `{...: 3}`, `{...: 4}` |
| C5 | Timeout while the charge is still in flight | `fault:payments.charge#<n>` kind 5 `slow_commit`: the charge commits after the node's `run_timeout` fires, while the retry attempt is already running. | documented: LangGraph timeouts cancel the attempt and retry it. | `{"fault:payments.charge#1": 5}` |
| C6 | Concurrent recovery | `fault:control.recover#<n>` kind 2 `concurrent`: two fresh processes call `recover` on the same thread at once. | theoretical: nothing in the documented API prevents two workers resuming one thread. | `{"crash:tool.charge.after_effect#1": 1, "fault:control.recover#1": 2}` |
| C7 | Late recovery past key expiry | `fault:control.recover#<n>` kind 3: recover 25 h after the crash. | documented: Stripe forgets keys after 24 h. | `{"crash:tool.charge.after_effect#1": 1, "fault:control.recover#1": 3}` |
| C8 | Cancel during charge | `fault:control.cancel#<n>`, kinds as approval gate F5. | documented | `{"fault:control.cancel#2": 1}` |
| C9 | Order edited or cancelled mid-run | `actor:order_editor#<n>`, drawn after `get_order` returns and after a crash: 1 amount `+500`; 2 status `cancelled`. | documented, as approval gate F7 | `{"actor:order_editor#1": 2}` |
| C10 | Operator replay | `fault:control.replay#1` kind 1: replay from the checkpoint before the charge node. | documented | `{"fault:control.replay#1": 1}` |

## 7. Pass and fail

As approval gate section 11. A variant passes when every all-`documented` cell has zero failures.

## 8. Minimal implementation

The adapter is the approval gate adapter without `pending` and `resume`. Implementations live in `frameworks/<name>/charge_once/`, at most 100 lines each.

LangGraph variants:

| Variant | Construction | Source pattern |
| --- | --- | --- |
| T1 | Graph API agent loop with `ToolNode`. `RetryPolicy()` on the tool node. Default durability. `SqliteSaver`. No idempotency key. | fault-tolerance.md retries |
| T2 | The fault tolerance page's error-handler example, verbatim except that the node body calls `charge`: `charge_payment` node with `RetryPolicy(max_attempts=3, retry_on=ConnectionError)` and `payment_error_handler`, which sets `status` to `compensated: ...` and routes to `finalize`. | fault-tolerance.md "Route with Command" |
| H1 | T1 with `durability="sync"`, `PostgresSaver`, async tool node with `timeout=` and `RetryPolicy()`, and `idempotency_key = f"{thread_id}:{tool_call_id}"`. | fault-tolerance.md "Protect side effects with an idempotency key" |
| H2 | H1, but the key is generated and persisted in its own node before the charge node, as the docs recommend when no business identity exists. | fault-tolerance.md, same section |
| FN1 | Functional API: `@entrypoint` with the charge in its own `@task`, as the docs recommend for side effects. | functional-api.md "Handling side effects" |

T2's handler records `compensated` without querying the provider or refunding. Under C4 kind 3 every attempt can commit before the handler runs, so CO1 and CO5 are the invariants to watch. The same page says to "Query the provider for the operation's status before compensating or continuing", so a failure here is class 2: the docs' own example skips the docs' own advice.
