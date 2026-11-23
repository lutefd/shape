"""pytest entry points for docs/guide/examples/async_agent.py, used by docs/guide/ci.md.

    pytest docs/guide/examples/test_async_agent.py
"""

import pytest
from async_agent import async_per_attempt_key, async_stable_key

import shape


def test_stable_key_charges_once(tmp_path):
    shape.check(async_stable_key, seeds=200, runs_dir=tmp_path)


def test_per_attempt_key_double_charges(tmp_path):
    with pytest.raises(AssertionError, match=r"at_most_once\(payments.charge\)"):
        shape.check(async_per_attempt_key, seeds=200, runs_dir=tmp_path)
