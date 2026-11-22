"""Addressable choices: the single source of everything that varies in a run.

Every nondeterministic decision in a simulation (the policy's next action, whether a
fault fires, which ready task the scheduler runs, an ID, a jitter) is a `draw` at a
stable string key such as ``"fault:payments.charge#3"`` or ``"agent#7"``.

Value 0 always means "the nominal thing": no fault, the correct action, FIFO order.
A run is therefore identified by the map of keys whose drawn value was non-zero.
That map is the replay file and the unit the shrinker minimizes: dropping a key
returns that decision to nominal.

Two sources share one interface:

* ``SeededChoices`` derives each key's value from ``hash(seed, key)``. The value does
  not depend on draw order, so unrelated changes elsewhere in a run do not reshuffle it.
* ``ExplicitChoices`` reads values from a map, and every unlisted key is 0. Replaying a
  seeded run's ``realized`` map through it must reproduce the run exactly.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Protocol

Realized = dict[str, int]


class ChoiceSource(Protocol):
    realized: Realized

    def enabled(self, kind: str) -> bool:
        """Swarm testing: whether this run may inject faults of ``kind``."""
        ...

    def draw(self, key: str, weights: Sequence[float]) -> int:
        """Return an index into ``weights``. Index 0 is the nominal outcome."""
        ...


def _unit(seed: int, key: str) -> float:
    digest = hashlib.blake2b(f"{seed}\x00{key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


class SeededChoices:
    def __init__(self, seed: int, *, swarm: Mapping[str, bool] | None = None) -> None:
        self.seed = seed
        self.realized: Realized = {}
        # Swarm testing: the set of enabled fault kinds is a property of the generator,
        # not of the run. It shapes seeded draws and never appears in `realized`.
        self.swarm = dict(swarm or {})

    def enabled(self, kind: str) -> bool:
        if kind not in self.swarm:
            self.swarm[kind] = _unit(self.seed, f"swarm:{kind}") < 0.5
        return self.swarm[kind]

    def draw(self, key: str, weights: Sequence[float]) -> int:
        total = sum(weights)
        point = _unit(self.seed, key) * total
        acc = 0.0
        value = len(weights) - 1
        for i, w in enumerate(weights):
            acc += w
            if point < acc:
                value = i
                break
        if value:
            self.realized[key] = value
        return value


class ExplicitChoices:
    def __init__(self, values: Mapping[str, int]) -> None:
        self.values = dict(values)
        self.realized: Realized = {}

    def enabled(self, kind: str) -> bool:
        return True

    def draw(self, key: str, weights: Sequence[float]) -> int:
        value = self.values.get(key, 0)
        if not 0 <= value < len(weights):
            value = 0
        if value:
            self.realized[key] = value
        return value
