from shape.choices import ExplicitChoices, SeededChoices


def test_seeded_draw_is_order_independent():
    a, b = SeededChoices(7), SeededChoices(7)
    keys = [f"k{i}" for i in range(50)]
    first = [a.draw(k, [0.5, 0.5]) for k in keys]
    second = [b.draw(k, [0.5, 0.5]) for k in reversed(keys)][::-1]
    assert first == second


def test_explicit_replays_seeded_realized():
    seeded = SeededChoices(42)
    keys = [f"k{i}" for i in range(200)]
    values = [seeded.draw(k, [0.7, 0.2, 0.1]) for k in keys]
    explicit = ExplicitChoices(seeded.realized)
    assert [explicit.draw(k, [0.7, 0.2, 0.1]) for k in keys] == values
    assert explicit.realized == seeded.realized


def test_unlisted_keys_are_nominal():
    assert ExplicitChoices({}).draw("anything", [0.01, 0.99]) == 0
