"""LangGraph RetryPolicy() scenarios (claim LG-C24).

LangGraph's fault-tolerance docs state that `RetryPolicy()` (using `default_retry_on`) does not
retry `OSError` or its subclasses, and for HTTP libraries such as `requests` and `httpx` only
retries 5xx status codes.

In `langgraph==1.2.24` (`langgraph/_internal/_retry.py`), `default_retry_on` actually:
1. Checks `isinstance(exc, ConnectionError)` and returns `True` BEFORE the `OSError` exclusion
   (so `ConnectionResetError`, an `OSError` subclass, IS retried).
2. Evaluates `500 <= exc.response.status_code < 600 if exc.response else True` for
   `requests.HTTPError`. Because `requests.Response.__bool__` is `False` for any status >= 400,
   every 4xx `requests.HTTPError` (e.g. 402 Payment Required, 409 Conflict) takes the `True`
   branch and IS retried.
3. Reaches the final `return True` for `httpx` transport errors such as `httpx.ReadTimeout`
   (which are neither `httpx.HTTPStatusError` nor `OSError`).

Run with:
    shape test examples/langgraph/retry_policy.py --seeds 200 --jobs 4
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import httpx
import requests
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy
from payments import (
    PaymentProvider,
    httpx_read_timeout,
    httpx_status_error,
    requests_http_error,
)

import shape

THREAD_ID = "order-17"
ORDER_ID = "ord-17"
AMOUNT = 5000


class ChargeState(TypedDict):
    thread_id: str
    order_id: str
    amount: int
    charge_id: int | None


class StdlibPaymentClient:
    """Default boundary errors: `connection_reset_after_commit` raises `ConnectionResetError`."""

    def __init__(self, db_path: Path) -> None:
        self.provider = PaymentProvider(db_path)

    @shape.effect(
        "payments.charge",
        scope=lambda order_id, **_: f"order:{order_id}",
        key=lambda idempotency_key=None, **_: idempotency_key,
    )
    def charge(self, order_id: str, amount: int, idempotency_key: str | None = None) -> int:
        return self.provider.charge(order_id, amount, idempotency_key)

    def close(self) -> None:
        self.provider.close()


class HttpxPaymentClient:
    """`timeout` and `timeout_after_commit` raise `httpx.ReadTimeout`."""

    def __init__(self, db_path: Path) -> None:
        self.provider = PaymentProvider(db_path)

    @shape.effect(
        "payments.charge",
        scope=lambda order_id, **_: f"order:{order_id}",
        key=lambda idempotency_key=None, **_: idempotency_key,
        errors={"timeout": httpx_read_timeout},
    )
    def charge(self, order_id: str, amount: int, idempotency_key: str | None = None) -> int:
        return self.provider.charge(order_id, amount, idempotency_key)

    def close(self) -> None:
        self.provider.close()



class Requests402BeforeCommitClient:
    """Maps `server_error` (before-commit) to `requests.HTTPError` (402 Payment Required).

    All other non-crash faults raise `httpx.HTTPStatusError(402)`, which `default_retry_on`
    correctly refuses to retry. This isolates the `requests.HTTPError` 4xx retry bug on
    `server_error`.
    """

    def __init__(self, db_path: Path) -> None:
        self.provider = PaymentProvider(db_path)

    @shape.effect(
        "payments.charge",
        scope=lambda order_id, **_: f"order:{order_id}",
        key=lambda idempotency_key=None, **_: idempotency_key,
        errors={
            "server_error": requests_http_error(402, "Payment Required"),
            "timeout": httpx_status_error(402, "Payment Required"),
            "connection_reset_after_commit": httpx_status_error(402, "Payment Required"),
            "rate_limit": httpx_status_error(402, "Payment Required"),
        },
    )
    def charge(self, order_id: str, amount: int, idempotency_key: str | None = None) -> int:
        return self.provider.charge(order_id, amount, idempotency_key)

    def close(self) -> None:
        self.provider.close()


class Httpx402ControlClient:
    """Control: every non-crash fault raises `httpx.HTTPStatusError` (402 Payment Required).

    LangGraph's `default_retry_on` returns `False` for 4xx `httpx.HTTPStatusError` (matching
    the docs), so the node is never retried.
    """

    def __init__(self, db_path: Path) -> None:
        self.provider = PaymentProvider(db_path)

    @shape.effect(
        "payments.charge",
        scope=lambda order_id, **_: f"order:{order_id}",
        key=lambda idempotency_key=None, **_: idempotency_key,
        errors={
            "server_error": httpx_status_error(402, "Payment Required"),
            "timeout": httpx_status_error(402, "Payment Required"),
            "connection_reset_after_commit": httpx_status_error(402, "Payment Required"),
            "rate_limit": httpx_status_error(402, "Payment Required"),
        },
    )
    def charge(self, order_id: str, amount: int, idempotency_key: str | None = None) -> int:
        return self.provider.charge(order_id, amount, idempotency_key)

    def close(self) -> None:
        self.provider.close()


class HardenedPaymentClient:
    """Exercises ConnectionResetError, httpx.ReadTimeout, and requests.HTTPError(402) together."""

    def __init__(self, db_path: Path) -> None:
        self.provider = PaymentProvider(db_path)

    @shape.effect(
        "payments.charge",
        scope=lambda order_id, **_: f"order:{order_id}",
        key=lambda idempotency_key=None, **_: idempotency_key,
        errors={
            "timeout": httpx_read_timeout,
            "server_error": requests_http_error(402, "Payment Required"),
        },
    )
    def charge(self, order_id: str, amount: int, idempotency_key: str | None = None) -> int:
        return self.provider.charge(order_id, amount, idempotency_key)

    def close(self) -> None:
        self.provider.close()


def _run_charge_graph(
    run: shape.Run,
    client_cls: type,
    *,
    use_idempotency_key: bool = False,
    durability: str | None = None,
    forbid_4xx_retry: bool = False,
) -> None:
    marker = run.dir / "retried_402"
    if forbid_4xx_retry and marker.exists():
        raise RuntimeError("LangGraph retried charge_node after a 402 requests.HTTPError")
    client = client_cls(run.dir / "provider.db")
    attempts = 0
    try:
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as checkpointer:
            def charge_node(state: ChargeState) -> dict[str, int]:
                nonlocal attempts
                attempts += 1
                if forbid_4xx_retry and attempts > 1:
                    marker.write_text("retried")
                    raise RuntimeError(
                        "LangGraph retried charge_node after a 402 requests.HTTPError"
                    )
                key = f"{state['thread_id']}:charge" if use_idempotency_key else None
                charge_id = client.charge(
                    order_id=state["order_id"],
                    amount=state["amount"],
                    idempotency_key=key,
                )
                return {"charge_id": charge_id}

            builder = StateGraph(ChargeState)
            builder.add_node("charge", charge_node, retry_policy=RetryPolicy())
            builder.add_edge(START, "charge")
            builder.add_edge("charge", END)
            graph = builder.compile(checkpointer=checkpointer)

            config = {"configurable": {"thread_id": THREAD_ID}}
            kwargs = {"durability": durability} if durability is not None else {}
            try:
                if run.restarts == 0:
                    graph.invoke(
                        {
                            "thread_id": THREAD_ID,
                            "order_id": ORDER_ID,
                            "amount": AMOUNT,
                            "charge_id": None,
                        },
                        config,
                        **kwargs,
                    )
                else:
                    graph.invoke(None, config, **kwargs)
            except (requests.HTTPError, httpx.HTTPStatusError):
                if not forbid_4xx_retry:
                    raise
                # A 402 Payment Required is a terminal business decline, not a process crash.
    finally:
        client.close()


@shape.scenario(invariants=[shape.at_most_once("payments.charge")], max_restarts=0)
def retry_connection_reset(run: shape.Run) -> None:
    """ConnectionResetError after commit is retried by default RetryPolicy() -> double charge."""
    _run_charge_graph(run, StdlibPaymentClient)


@shape.scenario(invariants=[shape.at_most_once("payments.charge")], max_restarts=0)
def retry_httpx_timeout(run: shape.Run) -> None:
    """httpx.ReadTimeout after commit is retried by default RetryPolicy() -> double charge."""
    _run_charge_graph(run, HttpxPaymentClient)



@shape.scenario(invariants=[shape.at_most_once("payments.charge")], max_restarts=3)
def retry_requests_402_before_commit(run: shape.Run) -> None:
    """Before-commit requests.HTTPError(402) (via server_error) must not be retried per docs."""
    _run_charge_graph(
        run,
        Requests402BeforeCommitClient,
        use_idempotency_key=True,
        durability="sync",
        forbid_4xx_retry=True,
    )


@shape.scenario(invariants=[shape.at_most_once("payments.charge")], max_restarts=3)
def retry_httpx_402_not_retried(run: shape.Run) -> None:
    """Control: httpx.HTTPStatusError(402) is NOT retried by default RetryPolicy() -> PASS."""
    _run_charge_graph(
        run,
        Httpx402ControlClient,
        use_idempotency_key=True,
        durability="sync",
        forbid_4xx_retry=True,
    )


@shape.scenario(
    invariants=[shape.at_most_once("payments.charge"), shape.exactly_once("payments.charge")],
    max_restarts=3,
)
def retry_fixed(run: shape.Run) -> None:
    """Fixed variant: durability='sync' + idempotency key derived from durable state."""
    _run_charge_graph(
        run,
        HardenedPaymentClient,
        use_idempotency_key=True,
        durability="sync",
    )
