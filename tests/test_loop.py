import asyncio
import os
import random
import socket
import time
import uuid

import pytest

from shape.choices import ExplicitChoices, SeededChoices
from shape.loop import Deadlock, SimLoop, TimeLimit
from shape.seal import Escape, sealed
from shape.trace import Trace


def run(coro_fn, choices=None, **kw):
    trace = Trace()
    loop = SimLoop(choices or ExplicitChoices({}), trace, **kw)
    try:
        with sealed():
            return loop.run_until_complete(coro_fn()), loop, trace
    finally:
        loop.close()


def test_sleep_advances_virtual_time_instantly():
    async def main():
        await asyncio.sleep(3600)
        return asyncio.get_running_loop().time()

    start = time.monotonic()
    (t, _, _) = run(main)
    assert t == 3600
    assert time.monotonic() - start < 1


def test_ready_queue_is_fifo_when_nominal():
    order = []

    async def worker(name):
        order.append(name)

    async def main():
        await asyncio.gather(*(worker(i) for i in range(5)))

    run(main)
    assert order == [0, 1, 2, 3, 4]


def test_sched_draw_reorders_ready_callbacks():
    def go(realized):
        order = []

        async def worker(name):
            order.append(name)

        async def main():
            await asyncio.gather(worker("a"), worker("b"))

        run(main, ExplicitChoices(realized))
        return order

    nominal = go({})
    assert nominal == ["a", "b"]
    swapped = [go({f"sched#{n}": 1}) for n in range(6)]
    assert ["b", "a"] in swapped


def test_deadlock_when_nothing_can_run():
    async def main():
        await asyncio.get_running_loop().create_future()

    with pytest.raises(Deadlock):
        run(main)


def test_time_limit():
    async def main():
        while True:
            await asyncio.sleep(1)

    with pytest.raises(TimeLimit):
        run(main, max_time=50)


def test_timeout_fires_in_virtual_time():
    async def main():
        try:
            async with asyncio.timeout(5):
                await asyncio.sleep(100)
        except TimeoutError:
            return asyncio.get_running_loop().time()

    assert run(main)[0] == 5


@pytest.mark.parametrize(
    "call",
    [
        lambda: time.time(),
        lambda: time.monotonic(),
        lambda: time.perf_counter(),
        lambda: time.sleep(1),
        lambda: random.random(),
        lambda: random.randint(1, 2),
        lambda: uuid.uuid4(),
        lambda: uuid.uuid1(),
        lambda: os.urandom(4),
        lambda: socket.create_connection(("127.0.0.1", 9)),
        lambda: socket.socket().connect(("127.0.0.1", 9)),
    ],
)
def test_real_nondeterminism_escapes(call):
    with pytest.raises(Escape):
        with sealed():
            call()


def test_escape_inside_a_run_is_not_swallowed_by_except_exception():
    async def main():
        try:
            time.time()
        except Exception:
            pass

    with pytest.raises(Escape):
        run(main)


def test_swallowed_escape_still_fails_the_run():
    async def main():
        try:
            time.time()
        except BaseException:
            pass

    with pytest.raises(Escape):
        run(main)


def test_seal_restores_real_functions():
    with pytest.raises(Escape):
        with sealed():
            time.time()
    assert time.time() > 0
    assert isinstance(random.random(), float)
    assert SeededChoices(1).draw("x", [1, 1]) in (0, 1)
