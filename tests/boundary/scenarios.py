"""Scenarios the boundary tests run. A separate module so spawned children can import it."""

import asyncio
import atexit
import contextlib
import sqlite3
import time
from pathlib import Path

import shape


@shape.effect("t.op", scope=lambda name, **_: f"name:{name}")
def tool(name: str) -> str:
    return name


@shape.effect("t.pay", scope=lambda order, **_: f"order:{order}")
def pay(order: str) -> None:
    return None


@shape.effect("t.apay", scope=lambda order, **_: f"order:{order}")
async def apay(order: str, marker: str | None = None) -> str:
    await asyncio.sleep(0)
    if marker:  # the body's real side effect, visible even if the process dies right after
        Path(marker).write_text("body ran\n")
    return order


@shape.effect("t.mail", scope=lambda order, **_: f"order:{order}", irreversible=True)
def mail(order: str) -> None:
    return None


def _marker(run: shape.Run, name: str) -> None:
    (run.dir / name).write_text("ran")


@shape.scenario(invariants=[shape.exactly_once("t.op")], max_restarts=2)
def cleanup_probe(run: shape.Run) -> None:
    """Writes to disk from `finally` and `atexit` blocks that a real crash must not run."""
    atexit.register(_marker, run, f"atexit-{run.restarts}")
    try:
        tool("a")
    finally:
        _marker(run, f"finally-{run.restarts}")


@shape.scenario(invariants=[shape.at_most_once("t.pay")], max_restarts=3)
def naive_retry(run: shape.Run) -> None:
    """Retries a charge that raised without asking whether it landed."""
    for _ in range(3):
        try:
            return pay("1")
        except (TimeoutError, ConnectionResetError):
            continue
    raise RuntimeError("gave up")


@shape.scenario(invariants=[shape.exactly_once("t.pay")], max_restarts=3)
def mark_before_effect(run: shape.Run) -> None:
    """Persists 'done' before the effect, so a crash in between loses the effect forever."""
    db = sqlite3.connect(run.dir / "progress.db")
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS done (k TEXT PRIMARY KEY)")
        fresh = db.execute("INSERT OR IGNORE INTO done VALUES ('pay')").rowcount
    if fresh:
        pay("1")


@shape.scenario(invariants=[shape.approved_before("t.mail")])
def mail_without_approval(run: shape.Run) -> None:
    mail("1")


@shape.scenario(invariants=[shape.approved_before("t.mail")])
def mail_with_approval(run: shape.Run) -> None:
    shape.approval("order:1", {"order": "1"})
    mail("1")


@shape.scenario(max_restarts=2)
def always_raises(run: shape.Run) -> None:
    raise RuntimeError("cannot recover")


@shape.scenario(max_restarts=1)
def hangs(run: shape.Run) -> None:
    time.sleep(60)


@shape.scenario(max_restarts=1)
def spawned(run: shape.Run) -> None:
    tool("s")


@shape.scenario(invariants=[shape.exactly_once("t.apay")], max_restarts=2)
def async_tool(run: shape.Run) -> None:
    """An `async def` effect awaited from an event loop, with progress saved only after it returns."""
    if run.restarts == 0:
        asyncio.run(apay("1", str(run.dir / "apay-body")))


@shape.scenario(invariants=[], max_restarts=0)
def async_never_awaited(run: shape.Run) -> None:
    """Creates the coroutine and drops it: nothing ran, so nothing may be recorded or faulted."""
    coro = apay("1", str(run.dir / "apay-body"))
    coro.close()


@shape.effect("t.refund", scope=lambda order, **_: f"order:{order}")
def refund(order: str) -> None:
    pass


@shape.scenario(invariants=[shape.at_most_once("t.refund")], max_restarts=0)
def model_may_repeat_a_refund(run: shape.Run) -> None:
    refund("1")
    if shape.choose("after_refund", ["reply_to_customer", "refund_again"]) == "refund_again":
        refund("1")


@shape.effect("t.keyed_refund", scope=lambda order, **_: f"order:{order}",
              key=lambda order, **_: f"refund-{order}")
def keyed_refund(order: str) -> None:
    pass


@shape.scenario(invariants=[shape.at_most_once("t.keyed_refund")])
def model_never_repeats(run: shape.Run) -> None:
    for _ in range(3):
        try:
            keyed_refund("1")
            break
        except (TimeoutError, ConnectionResetError, shape.RateLimited, shape.ServerError):
            continue
    shape.choose("after_refund", ["reply_to_customer", "ask_a_question", "close_ticket"])


# ---------------------------------------------------------------- outcome semantics and invariants


@shape.effect("t.put", scope=lambda order, **_: f"order:{order}", idempotent=True)
def put(order: str, value: str) -> None:
    pass


@shape.scenario(invariants=[shape.at_most_once("t.put")], max_restarts=0)
def put_same_value_twice(run: shape.Run) -> None:
    put("1", "a")
    put("1", "a")


@shape.scenario(invariants=[shape.at_most_once("t.put")], max_restarts=0)
def put_two_values(run: shape.Run) -> None:
    put("1", "a")
    put("1", "b")


@shape.effect("t.charge", scope=lambda order, **_: f"order:{order}",
              result=lambda reply: {"status": reply["status"]})
def charge(order: str, amount: int = 100) -> dict:
    return {"status": "ok", "internal": "not logged"}


@shape.effect("t.notify", scope=lambda order, **_: f"order:{order}")
def notify(order: str) -> None:
    pass


@shape.scenario(invariants=[shape.requires("t.notify", before="t.charge")], max_restarts=0)
def notify_even_if_charge_failed(run: shape.Run) -> None:
    try:
        charge("1")
    except TimeoutError:
        pass
    notify("1")


@shape.scenario(invariants=[shape.requires("t.notify", before="t.charge")], max_restarts=1)
def notify_only_after_charge(run: shape.Run) -> None:
    try:
        charge("1")
    except Exception:  # noqa: BLE001
        return
    notify("1")


@shape.scenario(invariants=[shape.requires("t.notify", before="t.charge", same_scope=False)],
                max_restarts=0)
def notify_after_charge_of_another_order(run: shape.Run) -> None:
    charge("2")
    notify("1")


@shape.scenario(invariants=[shape.requires("t.notify", before="t.charge")], max_restarts=0)
def notify_after_charge_of_another_order_same_scope(run: shape.Run) -> None:
    charge("2")
    notify("1")


@shape.scenario(invariants=[shape.within_scopes("t.refund", {"order:1"})], max_restarts=0)
def model_may_refund_the_wrong_order(run: shape.Run) -> None:
    refund(shape.choose("which_order", ["1", "2"]))


@shape.scenario(invariants=[shape.within_scopes("t.refund", {"order:1"})], max_restarts=0)
def model_refunds_the_right_order(run: shape.Run) -> None:
    refund(shape.choose("which_order", ["1"]))


def _allowed_from_file(run: shape.Run) -> set[str]:
    return {f"order:{(run.dir / 'allowed').read_text()}"}


@shape.scenario(invariants=[shape.within_scopes("t.refund", _allowed_from_file)], max_restarts=0)
def refund_outside_the_allowed_file(run: shape.Run) -> None:
    (run.dir / "allowed").write_text("1")
    refund("1")
    refund("3")


@shape.scenario(invariants=[shape.max_calls("t.charge", 3)], max_restarts=0)
def unbounded_retries(run: shape.Run) -> None:
    for _ in range(6):
        with contextlib.suppress(Exception):
            return charge("1")


@shape.scenario(invariants=[shape.max_calls("t.charge", 3)], max_restarts=0)
def bounded_retries(run: shape.Run) -> None:
    for _ in range(3):
        with contextlib.suppress(Exception):
            return charge("1")


@shape.effect("t.pay_out", scope=lambda order, **_: f"order:{order}")
def pay_out(order: str, amount: int) -> None:
    pass


@shape.scenario(invariants=[shape.sum_le("t.pay_out", "amount", 100)], max_restarts=0)
def model_may_over_refund(run: shape.Run) -> None:
    pay_out("1", 100)
    if shape.choose("after_refund", ["reply", "top_up"]) == "top_up":
        pay_out("1", 50)


@shape.scenario(invariants=[shape.sum_le("t.pay_out", "amount", 100)], max_restarts=0)
def refunds_within_the_limit(run: shape.Run) -> None:
    pay_out("1", 60)
    pay_out("1", 40)
    pay_out("2", 100)


@shape.scenario(invariants=[shape.claims_true()], max_restarts=0)
def claims_refund_even_if_it_failed(run: shape.Run) -> None:
    with contextlib.suppress(Exception):
        refund("1")
    shape.claim("order:1", "t.refund")


@shape.scenario(invariants=[shape.claims_true()], max_restarts=1)
def claims_only_what_landed(run: shape.Run) -> None:
    try:
        refund("1")
    except Exception:  # noqa: BLE001
        return
    shape.claim("order:1", "t.refund")


@shape.invariant
def charges_stay_small(world: shape.World) -> str | None:
    """Custom: reads effects, results, attempts, approvals, claims and completed."""
    for e in world.effects("t.charge"):
        if e["args"]["amount"] > 100 and e["result"] == {"status": "ok"}:
            return f"charged {e['args']['amount']}, over the 100 cap"
    return None


@shape.scenario(invariants=[charges_stay_small], max_restarts=0)
def model_may_overcharge(run: shape.Run) -> None:
    charge("1", shape.choose("amount", [100, 500]))


@shape.invariant
def broken_invariant(world: shape.World) -> str | None:
    return world.effects("t.charge")[0]["args"]["nope"]


@shape.scenario(invariants=[broken_invariant], max_restarts=0)
def scenario_with_a_broken_invariant(run: shape.Run) -> None:
    charge("1")


@shape.effect("t.send", scope=lambda order, **_: f"order:{order}", irreversible=True)
def send(order: str, amount: int) -> None:
    pass


def edit_amount_choice(run: shape.Run) -> None:
    with (run.dir / "actor.log").open("a") as log:
        log.write("ran\n")
    if shape.choose("user_edits_order", ["leave it", "raise the amount"]) == "raise the amount":
        (run.dir / "amount").write_text("999")


edit_amount = shape.actor(edit_amount_choice)


@shape.scenario(invariants=[shape.approved_before("t.send")], actors=[edit_amount],
                max_restarts=2)
def stale_approval(run: shape.Run) -> None:
    """The human approves amount 100, the worker dies, the user edits the order, the restarted
    worker sends whatever the order says now."""
    amount_file = run.dir / "amount"
    if run.restarts == 0:
        amount_file.write_text("100")
        shape.approval("order:1", {"order": "1", "amount": 100})
        raise RuntimeError("worker restarting")
    send("1", int(amount_file.read_text()))


@shape.effect("t.raw_send", scope=lambda body, **_: f"order:{body.split(':')[1]}")
def raw_send(body: str) -> None:
    pass


def _tool_args(effect_args: dict) -> dict:
    return {"order": effect_args["body"].split(":")[1]}


@shape.scenario(invariants=[shape.approved_before("t.raw_send", match=_tool_args)],
                max_restarts=0)
def raw_body_with_match(run: shape.Run) -> None:
    shape.approval("order:1", {"order": "1"})
    raw_send("send:1")


@shape.scenario(invariants=[shape.approved_before("t.raw_send")], max_restarts=0)
def raw_body_without_match(run: shape.Run) -> None:
    shape.approval("order:1", {"order": "1"})
    raw_send("send:1")


@shape.scenario(invariants=[shape.exactly_once("t.pay")], max_restarts=3)
def naive_retry_exactly_once(run: shape.Run) -> None:
    for _ in range(3):
        try:
            return pay("1")
        except (TimeoutError, ConnectionResetError):
            continue


# -------------------------------------------------------------------------------------- faults


@shape.effect("t.hook", scope=lambda order, **_: f"order:{order}", hide=("secret",))
def hook(order: str, secret: str, config: dict | None = None, tool_call_id: str | None = None,
         runtime: object = None) -> None:
    pass


@shape.scenario(invariants=[shape.at_most_once("t.hook")], max_restarts=0)
def calls_a_framework_tool(run: shape.Run) -> None:
    hook("1", "hunter2", config={"configurable": {"thread_id": "t"}}, tool_call_id="call_9",
         runtime=object())
    hook("1", "hunter2", config={"configurable": {"thread_id": "t"}})


@shape.scenario(invariants=[shape.approved_before("t.hook")], max_restarts=0)
def hidden_args_do_not_enter_the_approval_hash(run: shape.Run) -> None:
    shape.approval("order:1", {"order": "1"})
    hook("1", "hunter2", config={"configurable": {"thread_id": "t"}})


@shape.scenario(invariants=[], max_restarts=0)
def wrapped_tool_failure(run: shape.Run) -> None:
    """A framework wraps the tool's exception in its own."""
    try:
        refund("1")
    except TimeoutError as exc:
        raise RuntimeError("tool node failed") from exc


@shape.scenario(invariants=[shape.at_most_once("t.refund")], max_restarts=0)
def client_error_uncaught(run: shape.Run) -> None:
    refund("1")


@shape.effect("t.hook_pay", scope=lambda order, **_: f"order:{order}")
def hook_pay(order: str) -> None:
    pass


@shape.effect("t.hook_pay_keyed", scope=lambda order, **_: f"order:{order}",
              key=lambda order, **_: f"hook-{order}")
def hook_pay_keyed(order: str) -> None:
    pass


@shape.scenario(invariants=[shape.at_most_once("t.hook_pay")], max_restarts=1,
                duplicate_trigger=True)
def webhook_handler_without_dedup(run: shape.Run) -> None:
    hook_pay("1")


@shape.scenario(invariants=[shape.at_most_once("t.hook_pay_keyed")], max_restarts=1,
                duplicate_trigger=True)
def webhook_handler_with_dedup(run: shape.Run) -> None:
    hook_pay_keyed("1")


@shape.scenario(invariants=[shape.at_most_once("t.hook_pay")], max_restarts=0)
def one_shot_handler(run: shape.Run) -> None:
    hook_pay("1")


@shape.scenario(invariants=[shape.at_most_once("t.pay")], max_restarts=0)
def scenario_with_no_tool_calls(run: shape.Run) -> None:
    """The agent never reaches its tools, so every invariant passes for no reason."""


@shape.scenario(invariants=[shape.at_most_once("t.pay")], max_restarts=0, expect_effects=False)
def scenario_that_must_do_nothing(run: shape.Run) -> None:
    pass


@shape.scenario(invariants=[shape.at_most_once("t.pay")], max_restarts=0)
def replays_differ_from_seeds(run: shape.Run) -> None:
    """Depends on the run path, so explicit replays in a different directory diverge."""
    for _ in range(1 if run.dir.name.endswith(".min") else 2):
        pay_unkeyed_twice()


@shape.effect("t.pay", scope=lambda **_: "order:1")
def pay_unkeyed_twice() -> None:
    pass


@shape.effect("t.crashy", scope=lambda **_: "order:1")
def crashy() -> None:
    pass


@shape.scenario(invariants=[shape.at_most_once("t.refund"), shape.at_most_once("t.pay")],
                max_restarts=0)
def two_independent_model_bugs(run: shape.Run) -> None:
    """Two duplicate effects triggered by model decisions, with faults still enabled."""
    def attempt(fn, *args):
        with contextlib.suppress(Exception):
            fn(*args)
    attempt(refund, "1")
    if shape.choose("after_refund", ["reply", "refund_again"]) == "refund_again":
        attempt(refund, "1")
    attempt(pay, "1")
    if shape.choose("after_pay", ["reply", "pay_again"]) == "pay_again":
        attempt(pay, "1")
