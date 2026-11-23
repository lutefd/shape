"""Entry point for incarnations started by spawn instead of fork: `python -m shape.boundary.child`.
It re-imports the scenario's file, then runs one incarnation exactly like a forked child."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

from . import api, runner


def load_module(path: str | Path):
    path = Path(path).resolve()
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def scenarios_in(module) -> dict[str, api.Scenario]:
    return {v.name: v for v in vars(module).values() if isinstance(v, api.Scenario)}


if __name__ == "__main__":
    job = json.loads(sys.argv[1])
    scen = scenarios_in(load_module(job["file"]))[job["name"]]
    os._exit(runner.child_main(scen, job["spec"], Path(job["workdir"]), job["inc"],
                                   job.get("twin", 0)))
