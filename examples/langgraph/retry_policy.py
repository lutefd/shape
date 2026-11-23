"""LangGraph RetryPolicy() scenarios (claim LG-C24), driven by `shape.langgraph.drive`.

Run with:
    shape test examples/langgraph/retry_policy.py --seeds 200 --jobs 4
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
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
import shape.langgraph

THREAD_ID = "order-17"
INIT_STATE = {"thread_id": THREAD_ID, "order_id": "ord-17", "amount": 5000, "charge_id": None}
CONFIG = {"configurable": {"thread_id": THREAD_ID}}

HTTPX_402 = httpx_status_error(402, "Payment Required")
REQUESTS_402 = requests_http_error(402, "Payment Required")
HTTPX_402_ALL = {
    "server_error": HTTPX_402,
    "timeout": HTTPX_402,
    "connection_reset_after_commit": HTTPX_402,
    "rate_limit": HTTPX_402,
    "client_error": HTTPX_402,
}
REQUESTS_402_ON_SERVER = {**HTTPX_402_ALL, "server_error": REQUESTS_402}


class ChargeState(TypedDict):
    thread_id: str
    order_id: str
    amount: int
    charge_id: int | None


def _run_charge_graph(
    run: shape.Run,
    *,
    errors: Mapping[str, Callable[[], Exception]] | None = None,
    use_idempotency_key: bool = False,
    durability: str | None = None,
    forbid_4xx_retry: bool = False,
) -> None:
    marker = run.dir / "retried_402"
    if forbid_4xx_retry and marker.exists():
        raise RuntimeError("LangGraph retried charge_node after a 402 requests.HTTPError")
    provider = PaymentProvider(run.dir / "provider.db")
    attempts = 0

    @shape.effect(
        "payments.charge",
        scope=lambda order_id, **_: f"order:{order_id}",
        key=lambda idempotency_key=None, **_: idempotency_key,
        errors=errors,
    )
    def charge(order_id: str, amount: int, idempotency_key: str | None = None) -> int:
        return provider.charge(order_id, amount, idempotency_key)

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
                return {"charge_id": charge(state["order_id"], state["amount"], key)}

            builder = StateGraph(ChargeState)
            builder.add_node("charge", charge_node, retry_policy=RetryPolicy())
            builder.add_edge(START, "charge")
            builder.add_edge("charge", END)
            graph = builder.compile(checkpointer=checkpointer)
            try:
                shape.langgraph.drive(graph, INIT_STATE, CONFIG, run, durability=durability)
            except (requests.HTTPError, httpx.HTTPStatusError):
                if not forbid_4xx_retry:
                    raise
    finally:
        provider.close()


@shape.scenario(invariants=[shape.at_most_once("payments.charge")], max_restarts=0)
def retry_connection_reset(run: shape.Run) -> None:
    """ConnectionResetError after commit is retried by default RetryPolicy() -> double charge."""
    _run_charge_graph(run)


@shape.scenario(invariants=[shape.at_most_once("payments.charge")], max_restarts=0)
def retry_httpx_timeout(run: shape.Run) -> None:
    """httpx.ReadTimeout after commit is retried by default RetryPolicy() -> double charge."""
    _run_charge_graph(run, errors={"timeout": httpx_read_timeout})


@shape.scenario(invariants=[shape.at_most_once("payments.charge")], max_restarts=3)
def retry_requests_402_before_commit(run: shape.Run) -> None:
    """Before-commit requests.HTTPError(402) must not be retried per docs."""
    _run_charge_graph(
        run,
        errors=REQUESTS_402_ON_SERVER,
        use_idempotency_key=True,
        durability="sync",
        forbid_4xx_retry=True,
    )


@shape.scenario(invariants=[shape.at_most_once("payments.charge")], max_restarts=3)
def retry_httpx_402_not_retried(run: shape.Run) -> None:
    """Control: httpx.HTTPStatusError(402) is NOT retried by default RetryPolicy() -> PASS."""
    _run_charge_graph(
        run,
        errors=HTTPX_402_ALL,
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
        errors={"timeout": httpx_read_timeout, "server_error": REQUESTS_402},
        use_idempotency_key=True,
        durability="sync",
    )
