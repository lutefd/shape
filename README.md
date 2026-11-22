# shape

Deterministic simulation testing for agent systems, and a Jepsen-style harness for agent frameworks.

The model is an untrusted input. shape asks whether the system around it keeps its safety properties under any sequence of agent decisions and world failures. Everything that varies in a run flows from one seed, so every failure replays exactly and shrinks to a short trace.

See [docs/design.md](docs/design.md).

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
