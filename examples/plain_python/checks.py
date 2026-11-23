"""shape scenarios for the plain-python agent.

    shape test examples/plain_python/checks.py::checkout_buggy --seeds 200
    shape test examples/plain_python/checks.py::checkout_fixed --seeds 200
"""

from agent import charge_with_retry, charge_with_retry_fixed, process_order

import shape

CRASH_SAFE = [shape.at_most_once("payments.charge"), shape.exactly_once("payments.charge")]


@shape.scenario(invariants=CRASH_SAFE, max_restarts=3)
def checkout_buggy(run: shape.Run) -> None:
    process_order(run.dir, "o1", 500, charge_with_retry)


@shape.scenario(invariants=CRASH_SAFE, max_restarts=3)
def checkout_fixed(run: shape.Run) -> None:
    process_order(run.dir, "o1", 500, charge_with_retry_fixed)
