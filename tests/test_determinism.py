import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from shape.choices import ExplicitChoices, SeededChoices
from shape.runtime import SAGAS, Runtime
from shape.seal import Escape
from shape.simulation import place_order, replay, run_seed

SEEDS = range(150)


@pytest.mark.parametrize("saga", ["toy", "fixed"])
def test_same_seed_twice_gives_identical_trace(saga):
    for seed in SEEDS:
        a, b = run_seed(saga, seed), run_seed(saga, seed)
        assert a.trace.fingerprint() == b.trace.fingerprint(), seed
        assert a.realized == b.realized


@pytest.mark.parametrize("saga", ["toy", "fixed"])
def test_realized_replayed_through_explicit_choices_gives_identical_trace(saga):
    for seed in SEEDS:
        seeded = run_seed(saga, seed)
        replayed = replay(saga, seeded.realized)
        assert replayed.trace.fingerprint() == seeded.trace.fingerprint(), seed
        assert replayed.realized == seeded.realized
        assert [v.message for v in replayed.violations] == [v.message for v in seeded.violations]


SRC = str(Path(__file__).resolve().parents[1] / "src")


def test_runs_do_not_depend_on_python_hash_randomization():
    code = (
        "from shape.simulation import run_seed\n"
        "import hashlib\n"
        "print(hashlib.sha256(repr([run_seed('toy', n).trace.fingerprint()"
        " for n in range(40)]).encode()).hexdigest())\n"
    )
    outs = {
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                       env={**os.environ, "PYTHONHASHSEED": str(h), "PYTHONPATH": SRC}).stdout.splitlines()[-1]
        for h in (1, 2, 3)
    }
    assert len(outs) == 1


def test_real_clock_read_inside_a_run_raises_escape():
    class ReadsTheClock(Runtime):
        async def _gate(self, saga_id, tool, op, args):
            time.time()
            return True

    config = SAGAS["fixed"]
    with pytest.raises(Escape):
        place_order("fixed", ExplicitChoices({}),
                    agent=lambda s, h, a: ReadsTheClock(s, h, a, config))


def test_real_randomness_inside_a_run_raises_escape_even_if_swallowed():
    import random

    class Swallows(Runtime):
        async def _gate(self, saga_id, tool, op, args):
            try:
                random.random()
            except BaseException:  # noqa: BLE001, S110
                pass
            return True

    config = SAGAS["fixed"]
    with pytest.raises(Escape):
        place_order("fixed", SeededChoices(1), agent=lambda s, h, a: Swallows(s, h, a, config))


def test_a_run_takes_no_real_time_for_virtual_hours():
    # 50 external restocks' worth of virtual time and approval delays still run in milliseconds
    start = time.perf_counter()
    result = replay("fixed", {"human:saga-17:email": 2})
    assert result.ok
    assert result.trace.events[-1].t >= 30
    assert time.perf_counter() - start < 1
