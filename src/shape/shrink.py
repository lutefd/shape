"""Shrinking: delta debugging (ddmin) over the keys of a `realized` map.

Removing a key returns that decision to nominal, so the result is a minimal set of deviations
from the nominal run that still breaks the invariant. `id:` and `jitter:` keys carry values,
not deviations, so they are never candidates: they ride along unchanged.
"""

from __future__ import annotations

from collections.abc import Callable

from .choices import Realized

VALUE_KEYS = ("id:", "jitter:")


def ddmin(items: list[str], fails: Callable[[list[str]], bool]) -> list[str]:
    """Smallest sublist (1-minimal) of `items` for which `fails` is true."""
    if fails([]):
        return []
    n = 2
    while len(items) >= 2:
        size = -(-len(items) // n)
        chunks = [items[i:i + size] for i in range(0, len(items), size)]
        smaller = next((c for c in chunks if fails(c)), None)
        if smaller is not None:
            items, n = smaller, 2
            continue
        complement = next((c for c in ([x for x in items if x not in set(ch)] for ch in chunks)
                           if c and fails(c)), None)
        if complement is not None:
            items, n = complement, max(n - 1, 2)
            continue
        if n >= len(items):
            break
        n = min(len(items), n * 2)
    return items


def shrink(realized: Realized, fails: Callable[[Realized], bool]) -> Realized:
    """Minimize `realized` while `fails(realized)` stays true. Results are memoized."""
    carried = {k: v for k, v in realized.items() if k.startswith(VALUE_KEYS)}
    candidates = [k for k in realized if k not in carried]
    seen: dict[tuple[str, ...], bool] = {}

    def test(keys: list[str]) -> bool:
        ident = tuple(sorted(keys))
        if ident not in seen:
            seen[ident] = fails({**carried, **{k: realized[k] for k in keys}})
        return seen[ident]

    kept = ddmin(candidates, test)
    return {**carried, **{k: realized[k] for k in kept}}
