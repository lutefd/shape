# shape

Deterministic simulation testing for agent systems, and a Jepsen-style harness for agent frameworks.

The model is an untrusted input. shape asks whether the system around it keeps its safety properties under any sequence of agent decisions and world failures. Everything that varies in a run flows from one seed, so every failure replays exactly and shrinks to a short trace.

See [docs/design.md](docs/design.md).

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

```sh
.venv/bin/shape run --seeds 10000 --saga toy    # finds the planted double charge, shrinks it
.venv/bin/shape run --seeds 10000 --saga fixed  # passes
.venv/bin/shape replay runs/seed-24.at_most_once_effect.min.json
```
