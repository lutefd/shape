from collections import Counter

from shape.choices import ExplicitChoices, SeededChoices
from shape.policies import (
    KINDS,
    Action,
    AdversarialPolicy,
    Observation,
    PerturbedReplay,
    RecordingPolicy,
    ReplayPolicy,
    nominal,
)
from shape.runtime import SPECS
from shape.simulation import ORDER, place_order, replay, run_seed

TYPES = {"int": int, "str": str}
BY_NAME = {f"{s.tool}.{s.op}": s for s in SPECS}


def obs(done=()):
    return Observation(0, ORDER, tuple(done), None)


def test_nominal_walks_the_plan_in_order():
    done = []
    names = []
    while (action := nominal(obs(done))).kind == "call":
        names.append(f"{action.tool}.{action.op}")
        done.append(names[-1])
    assert names == ["inventory.reserve", "payments.charge", "email.send"]
    assert nominal(obs(done)).kind == "done"


def test_adversarial_calls_are_valid_against_the_schemas_and_cover_every_kind():
    kinds = Counter()
    for seed in range(400):
        choices = SeededChoices(seed)
        policy = AdversarialPolicy(choices, SPECS)
        for step in range(6):
            action = policy.decide(step, obs())
            kinds[choices.realized.get(f"agent#{step}", 0)] += 1
            if action.kind != "call":
                assert action.kind in ("abort", "done")
                continue
            spec = BY_NAME[f"{action.tool}.{action.op}"]
            assert set(action.args) == set(spec.schema)
            for name, value in action.args.items():
                assert isinstance(value, TYPES[spec.schema[name]])
    assert set(kinds) == set(range(len(KINDS)))


def test_all_zero_choices_is_the_nominal_run():
    result = replay("fixed", {})
    assert result.ok
    assert [s.state for s in result.store.sagas.values()] == ["committed"]
    assert not result.realized


def test_record_then_replay_reproduces_the_run():
    recorded = {}

    def recording(choices):
        policy = RecordingPolicy(AdversarialPolicy(choices, SPECS))
        recorded["policy"] = policy
        return policy

    original = place_order("fixed", SeededChoices(11), policy=recording)
    script = recorded["policy"].script
    assert script and all(Action.from_json(a) for a in script.values())

    non_agent = {k: v for k, v in original.realized.items() if not k.startswith("agent#")}
    again = place_order("fixed", ExplicitChoices(non_agent), policy=lambda c: ReplayPolicy(script))
    assert again.trace.fingerprint() == original.trace.fingerprint()


def test_perturbed_replay_changes_only_the_chosen_step():
    recorded = {}

    def recording(choices):
        recorded["policy"] = RecordingPolicy(AdversarialPolicy(choices, SPECS))
        return recorded["policy"]

    place_order("fixed", ExplicitChoices({}), policy=recording)
    script = recorded["policy"].script

    def perturbed(choices):
        return PerturbedReplay(script, 2, AdversarialPolicy(choices, SPECS))

    same = place_order("fixed", ExplicitChoices({}), policy=perturbed)
    swapped = place_order("fixed", ExplicitChoices({"agent#2": 6}), policy=perturbed)
    nominal_run = place_order("fixed", ExplicitChoices({}), policy=lambda c: ReplayPolicy(script))
    assert same.trace.fingerprint() == nominal_run.trace.fingerprint()
    assert [s.state for s in swapped.store.sagas.values()] == ["compensated"]
    agent_steps = [e.text for e in swapped.trace.events if e.kind == "agent"]
    assert agent_steps[:2] == [e.text for e in nominal_run.trace.events if e.kind == "agent"][:2]


def test_adversarial_policy_cannot_break_the_fixed_runtime_with_wrong_or_repeated_calls():
    for seed in range(300):
        assert run_seed("fixed", seed).ok, seed
