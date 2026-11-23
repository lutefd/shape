"""A toy saga runtime for "place order": reserve inventory -> charge -> send confirmation.

The agent (a Policy) proposes tool calls. The runtime decides what actually happens:

* a durable intent is written before every call, then the call is audited, then made;
* every call carries an idempotency key; retries after a timeout reuse it when
  `retry_key="stable"` and mint a new one when `retry_key="fresh"` (the planted bug);
* the confirmation email is irreversible: it needs an approval for that exact action first,
  and sending it commits the saga;
* if the saga cannot finish it compensates in reverse order. Releasing inventory is
  conflict-aware: it applies a delta by compare-and-set instead of overwriting a record that
  someone else changed, and flags a conflict it cannot resolve;
* an order is accepted by its first durable write, and only the ack that follows tells the client
  to stop resubmitting; submitting again is idempotent by order id;
* after a crash, `recover()` resumes every unfinished saga from the durable store alone. A new
  process has no call in flight, so every intent still `started` was cut off by the crash: its
  effect may exist, and `recover()` records that durably (`Intent.unknown`) before anything else;
* the confirmation email is the pivot. Once it may have taken effect the saga can only commit or
  escalate with an audit entry. It never compensates, because a sent email cannot be undone.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from ..policies import Action, Observation, Policy
from ..sim.faults import ApiError, RateLimited, ServerError, ToolTimeout
from ..sim.host import Host, render_args
from ..sim.tools import SimApprovals, ToolSpec
from .store import (
    COMMITTED,
    COMPENSATED,
    COMPENSATING,
    ESCALATED,
    RUNNING,
    Intent,
    SagaRecord,
    Store,
)

SPECS = [
    ToolSpec("inventory", "reserve", {"sku": "str", "qty": "int"}),
    ToolSpec("payments", "charge", {"order": "int", "amount": "int"}),
    ToolSpec("email", "send", {"order": "int", "to": "str"}, irreversible=True),
]
STEPS = {"inventory.reserve": "reserve", "payments.charge": "charge", "email.send": "email"}
NAMES = {step: name for name, step in STEPS.items()}


@dataclass(frozen=True)
class RuntimeConfig:
    retry_key: str = "stable"  # "stable": one key per (saga, step). "fresh": new key per retry
    max_attempts: int = 4
    backoff: float = 0.5
    approval_timeout: float = 60.0
    max_steps: int = 12


SAGAS = {"toy": RuntimeConfig(retry_key="fresh"), "fixed": RuntimeConfig(retry_key="stable")}


class StepFailed(Exception):
    def __init__(self, code: str, unknown: bool = False) -> None:
        super().__init__(code)
        self.code, self.unknown = code, unknown


def make_agent(config: RuntimeConfig) -> Callable[[Store, Host, SimApprovals], Runtime]:
    return lambda store, host, approver: Runtime(store, host, approver, config)


class Runtime:
    def __init__(self, store: Store, host: Host, approver: SimApprovals,
                 config: RuntimeConfig) -> None:
        self.store, self.host, self.approver, self.cfg = store, host, approver, config

    # ---- entry points -------------------------------------------------------------------

    async def submit(self, order: dict[str, Any]) -> str:
        """Accept an order: the first durable write. Idempotent by order id.

        Returning is the ack. Until the client has it the order may not exist anywhere durable,
        so the client must submit again; a retry after the write landed finds the row and keeps it.
        """
        saga_id = f"saga-{order['order']}"
        if not self.store.has_saga(saga_id):
            self.store.put_saga(SagaRecord(saga_id, order))
        return saga_id

    async def run(self, order: dict[str, Any], policy: Policy,
                  ack: Callable[[str], None] = lambda saga_id: None) -> None:
        saga_id = await self.submit(order)
        ack(saga_id)  # the client now knows the order is durable and stops resubmitting
        await self._drive(saga_id, policy)

    async def recover(self, policy: Policy) -> None:
        for saga_id in self.store.open_sagas():
            await self._mark_in_doubt(saga_id)
            await self._drive(saga_id, policy)

    async def _mark_in_doubt(self, saga_id: str) -> None:
        """Record durably that every intent the crash interrupted may have taken effect."""
        for intent in self.store.intents_of(saga_id):
            if intent.state == "started" and not intent.unknown:
                intent.unknown = True
                self.store.put_intent(intent)

    async def compensate_again(self) -> None:
        """Run every finished compensation once more (for the idempotence check)."""
        for saga_id, saga in list(self.store.sagas.items()):
            if saga.state == COMPENSATED:
                await self._compensate(saga_id, force=True)

    # ---- the saga loop ------------------------------------------------------------------

    async def _drive(self, saga_id: str, policy: Policy) -> None:
        saga = self.store.saga(saga_id)
        if saga.state == RUNNING and saga.pending:
            await self._handle(saga_id, Action.from_json(saga.pending))
        while (saga := self.store.saga(saga_id)).state == RUNNING:
            if saga.cursor >= self.cfg.max_steps:
                await self._settle(saga_id, state=COMPENSATING, why="step budget exhausted")
                break
            done = tuple(
                NAMES[i.step] for i in self.store.intents_of(saga_id)
                if i.state == "done" and i.step in NAMES
            )
            action = policy.decide(saga.cursor, Observation(saga.cursor, saga.order, done, saga.last))
            self.host.trace.add("agent", action.render(), step=saga.cursor)
            saga.cursor += 1
            saga.pending = asdict(action)
            self.store.put_saga(saga)
            await self._handle(saga_id, action)
        if self.store.saga(saga_id).state == COMPENSATING:
            await self._compensate(saga_id)

    async def _handle(self, saga_id: str, action: Action) -> None:
        saga = self.store.saga(saga_id)
        if saga.state != RUNNING:
            return
        if action.kind == "abort":
            return await self._settle(saga_id, state=COMPENSATING, why="agent aborted")
        if action.kind == "done":
            return await self._settle(saga_id, state=COMPENSATING, why="agent stopped early")
        step = STEPS.get(f"{action.tool}.{action.op}")
        if step is None or action.args != self._args(step, saga.order):
            return await self._reject(saga_id, "invalid_action")
        if step != "reserve" and not self._done(saga_id, "reserve"):
            return await self._reject(saga_id, "out_of_order")
        if step == "email" and not self._done(saga_id, "charge"):
            return await self._reject(saga_id, "out_of_order")
        try:
            result = await self._do(saga_id, step, saga.order)
        except StepFailed as failed:
            self.store.audit(saga=saga_id, kind="step_failed", step=step, code=failed.code)
            if step == "email" and failed.unknown:
                return await self._escalate(saga_id, "confirmation outcome unknown")
            return await self._settle(saga_id, state=COMPENSATING, why=f"{step} failed")
        state = COMMITTED if step == "email" else RUNNING
        await self._settle(saga_id, state=state, last=result)

    def _args(self, step: str, order: dict[str, Any]) -> dict[str, Any]:
        if step == "reserve":
            return {"sku": order["sku"], "qty": order["qty"]}
        if step == "charge":
            return {"order": order["order"], "amount": order["amount"]}
        return {"order": order["order"], "to": order["to"]}

    def _done(self, saga_id: str, step: str) -> bool:
        intent = self.store.intent(saga_id, step)
        return intent is not None and intent.state == "done"

    async def _reject(self, saga_id: str, error: str) -> None:
        self.store.audit(saga=saga_id, kind="rejected", error=error)
        await self._settle(saga_id, last={"error": error})

    async def _settle(self, saga_id: str, *, state: str | None = None,
                      last: dict[str, Any] | None = None, why: str = "") -> None:
        saga = self.store.saga(saga_id)
        saga.pending = None
        if last is not None:
            saga.last = last
        if state is not None:
            saga.state = state
            self.store.audit(saga=saga_id, kind="state", state=state, why=why)
        self.store.put_saga(saga)

    async def _escalate(self, saga_id: str, why: str) -> None:
        self.store.audit(saga=saga_id, kind="escalate", why=why)
        await self._settle(saga_id, state=ESCALATED, why=why)

    # ---- forward steps ------------------------------------------------------------------

    async def _do(self, saga_id: str, step: str, order: dict[str, Any]) -> dict[str, Any]:
        done = self.store.intent(saga_id, step)
        if done and done.state == "done":
            self.store.audit(saga=saga_id, kind="dedupe", step=step)
            return done.result or {}
        if step == "reserve":
            return await self._reserve(saga_id, order)
        if step == "charge":
            args = self._args("charge", order)
            await self._ensure(Intent(saga_id, "charge", "payments", "charge", args,
                                      f"{saga_id}:charge"))
            return await self._call_step(saga_id, "charge")
        args = self._args("email", order)
        if not await self._gate(saga_id, "email", "send", args):
            raise StepFailed("approval_refused")
        await self._ensure(Intent(saga_id, "email", "email", "send", args, f"{saga_id}:email"))
        return await self._call_step(saga_id, "email")

    async def _gate(self, saga_id: str, tool: str, op: str, args: dict[str, Any]) -> bool:
        request = f"{saga_id}:{tool}"
        self.store.audit(saga=saga_id, kind="gate_requested", request=request)
        try:
            ok = await asyncio.wait_for(
                self.approver.request(request, tool, op, args), self.cfg.approval_timeout)
        except TimeoutError:
            ok = False
        self.store.audit(saga=saga_id, kind="gate_decision", request=request, approved=ok)
        return ok

    async def _reserve(self, saga_id: str, order: dict[str, Any]) -> dict[str, Any]:
        record_id, hold = f"inv:{order['sku']}", str(order["order"])
        for _ in range(3):
            got = await self._read(record_id)
            held, stock = got["value"]["held"], got["value"]["stock"]
            if hold in held:  # an earlier attempt of ours already landed
                return await self._mark(saga_id, "reserve", "done", {"version": got["version"]})
            if stock < order["qty"]:
                raise StepFailed("insufficient_stock")
            value = {"stock": stock - order["qty"], "held": {**held, hold: order["qty"]}}
            args = {"id": record_id, "expected": got["version"], "value": value}
            self.store.put_intent(Intent(saga_id, "reserve", "db", "cas", args, None))
            try:
                return await self._call_step(saga_id, "reserve")
            except StepFailed as failed:
                if failed.code != "version_conflict":
                    raise
        raise StepFailed("version_conflict")

    # ---- the call machinery -------------------------------------------------------------

    async def _ensure(self, intent: Intent) -> None:
        if self.store.intent(intent.saga, intent.step) is None:
            self.store.put_intent(intent)

    async def _mark(self, saga_id: str, step: str, state: str,
                    result: dict[str, Any]) -> dict[str, Any]:
        intent = self.store.intent(saga_id, step) or Intent(saga_id, step, "db", "cas", {}, None)
        intent.state, intent.result = state, result
        self.store.put_intent(intent)
        return result

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(self.cfg.backoff * 2**attempt)

    async def _read(self, record_id: str) -> dict[str, Any]:
        for attempt in range(self.cfg.max_attempts):
            try:
                return await self.host.call("db", "get", {"id": record_id})
            except RateLimited as limited:
                await asyncio.sleep(limited.retry_after)
            except (ToolTimeout, ServerError):
                await self._backoff(attempt)
            except ApiError as error:
                raise StepFailed(error.code) from error
        raise StepFailed("read_failed")

    async def _call_step(self, saga_id: str, step: str, *, compensating: bool = False,
                         ok_codes: tuple[str, ...] = ()) -> dict[str, Any]:
        """Make the intent's call, retrying transient failures, and record the outcome."""
        intent = self.store.intent(saga_id, step)
        assert intent is not None
        if intent.state == "done":
            return intent.result or {}
        for attempt in range(self.cfg.max_attempts):
            await self._audit_call(saga_id, intent)
            try:
                result = await self.host.call(intent.tool, intent.op, intent.args, intent.key,
                                              compensating)
            except ToolTimeout:
                intent.unknown = True
                await self._backoff(attempt)
                if self.cfg.retry_key == "fresh":
                    intent.key = f"{saga_id}:{step}:r{attempt + 1}"
                self.store.put_intent(intent)
                note = "  # new key on retry" if self.cfg.retry_key == "fresh" else ""
                self.host.trace.add(
                    "retry", f"retry {intent.tool}.{intent.op}({render_args(intent.args)})  "
                    f"key={intent.key}{note}", tool=intent.tool, op=intent.op, key=intent.key)
            except RateLimited as limited:
                await asyncio.sleep(limited.retry_after)
            except ServerError:
                await self._backoff(attempt)
            except ApiError as error:
                if error.code in ok_codes:
                    return await self._mark(saga_id, step, "done", {"already": error.code})
                if compensating and error.code != "version_conflict":
                    await self._backoff(attempt)
                    continue
                intent.state = "failed"
                self.store.put_intent(intent)
                raise StepFailed(error.code, intent.unknown) from error
            else:
                return await self._mark(saga_id, step, "done", result)
        intent.state = "failed"
        self.store.put_intent(intent)
        raise StepFailed("attempts_exhausted", intent.unknown)

    async def _audit_call(self, saga_id: str, intent: Intent) -> None:
        self.store.audit(saga=saga_id, kind="call", tool=intent.tool, op=intent.op,
                         args=intent.args, key=intent.key)

    # ---- compensation -------------------------------------------------------------------

    def _pivot_may_have_taken_effect(self, saga_id: str) -> bool:
        email = self.store.intent(saga_id, "email")
        return email is not None and (email.state != "failed" or email.unknown)

    async def _compensate(self, saga_id: str, *, force: bool = False) -> None:
        if not force and self._pivot_may_have_taken_effect(saga_id):
            return await self._escalate(saga_id, "confirmation may have been sent; not compensating")
        for intent in reversed(self.store.intents_of(saga_id)):
            if intent.step == "charge":
                ok = await self._refund(saga_id, intent, force)
            elif intent.step == "reserve":
                ok = await self._release(saga_id, force)
            else:
                continue
            if not ok:
                return await self._escalate(saga_id, f"could not compensate {intent.step}")
        if not force:
            await self._settle(saga_id, state=COMPENSATED, why="all steps compensated")

    async def _comp_done(self, saga_id: str, step: str, force: bool) -> bool:
        comp = self.store.intent(saga_id, step)
        return bool(comp and comp.state == "done" and not force)

    async def _refund(self, saga_id: str, charge: Intent, force: bool) -> bool:
        if await self._comp_done(saga_id, "comp:charge", force):
            return True
        if charge.state == "failed" and not charge.unknown:
            return True  # the charge was rejected outright: nothing to undo
        if charge.state != "done":  # outcome unknown: ask again under the same key to find out
            try:
                await self._call_step(saga_id, "charge")
            except StepFailed as failed:
                return not failed.unknown
            charge = self.store.intent(saga_id, "charge")  # type: ignore[assignment]
        refund = Intent(saga_id, "comp:charge", "payments", "refund",
                        {"charge_id": (charge.result or {})["charge_id"]}, f"{saga_id}:refund")
        self.store.put_intent(refund)
        try:
            await self._call_step(saga_id, "comp:charge", compensating=True,
                                  ok_codes=("charge_already_refunded",))
        except StepFailed:
            return False
        return True

    async def _release(self, saga_id: str, force: bool) -> bool:
        if await self._comp_done(saga_id, "comp:reserve", force):
            return True
        order = self.store.saga(saga_id).order
        record_id, hold = f"inv:{order['sku']}", str(order["order"])
        reserved = self.store.intent(saga_id, "reserve")
        for _ in range(3):
            try:
                got = await self._read(record_id)
            except StepFailed:
                return False
            value = got["value"]
            if hold not in value["held"]:
                await self._mark(saga_id, "comp:reserve", "done", {"released": False})
                return True
            if reserved and reserved.result and got["version"] != reserved.result["version"]:
                self.store.audit(saga=saga_id, kind="conflict", record=record_id,
                                 note="record changed since the reserve; releasing by delta")
            held = {k: v for k, v in value["held"].items() if k != hold}
            restored = {"stock": value["stock"] + value["held"][hold], "held": held}
            args = {"id": record_id, "expected": got["version"], "value": restored}
            self.store.put_intent(Intent(saga_id, "comp:reserve", "db", "cas", args, None))
            try:
                await self._call_step(saga_id, "comp:reserve", compensating=True)
                return True
            except StepFailed as failed:
                if failed.code != "version_conflict":
                    return False
        saga = self.store.saga(saga_id)
        saga.conflicts.append(record_id)
        self.store.put_saga(saga)
        self.store.audit(saga=saga_id, kind="conflict", record=record_id,
                         note="could not release: record kept changing")
        return False
