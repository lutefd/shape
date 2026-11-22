"""SimDB: versioned records with compare-and-set. Not SQL.

Mirrors the semantics of optimistic concurrency control as in DynamoDB conditional writes or
an `UPDATE ... WHERE version = ?`: a write that names a stale version is rejected and changes
nothing. Real Postgres under simulator-controlled interleaving is planned work.
"""

from __future__ import annotations

import copy
from typing import Any

from ..faults import ApiError
from ..world import World
from . import ToolSpec


class SimDB:
    name = "db"
    api = "versioned record store with conditional writes (DynamoDB ConditionExpression semantics)"
    specs = (
        ToolSpec("db", "get", {"id": "str"}),
        ToolSpec("db", "cas", {"id": "str", "expected": "int"}),
    )

    def __init__(self, world: World) -> None:
        self.world = world

    def execute(self, op: str, args: dict[str, Any], key: str | None) -> dict[str, Any]:
        rec = self.world.records.get(args["id"])
        if rec is None:
            raise ApiError("not_found")
        if op == "get":
            return {"value": copy.deepcopy(rec.value), "version": rec.version}
        if rec.version != args["expected"]:
            raise ApiError("version_conflict")
        rec.value = copy.deepcopy(args["value"])
        rec.version += 1
        self.world.effect("db", "cas", f"db:{args['id']}@v{rec.version}", key, True, args)
        return {"version": rec.version}
