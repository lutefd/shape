"""Cooperative process scheduling for boundary runs."""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection, wait
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import TYPE_CHECKING

from ..choices import ChoiceSource
from .log import PARENT_SEQ, REALIZED, JsonlWriter

if TYPE_CHECKING:
    from .api import Scenario


class Gate:
    """The actor's end of the channel. Only the parent can grant a new turn."""

    def __init__(self, conn: Connection, actor_id: int, name: str) -> None:
        self.conn = conn
        self.actor_id = actor_id
        self.name = name
        self.turn = 0
        self.lock = threading.Lock()

    def point(self, label: str, show: bool = False) -> None:
        with self.lock:
            self.conn.send((label, show))
            self.turn = self.conn.recv()

    def position(self) -> dict:
        return {"actor_id": self.actor_id, "actor_name": self.name, "turn": self.turn}


@dataclass
class _Actor:
    id: int
    name: str
    twin: int
    process: BaseProcess
    conn: Connection
    label: str = "entry"
    show: bool = False
    turn: int = 0
    code: int | None = None

    def event(self, inc: int, seq: int, **fields) -> dict:
        return {"inc": inc, "seq": seq, "turn": self.turn, "twin": self.twin,
                "actor_id": self.id, "actor_name": self.name, **fields}

    def receive(self, deadline: float) -> bool:
        """Read the next point or reap the actor. False means it stopped making progress."""
        ready = wait([self.conn, self.process.sentinel], max(0, deadline - time.monotonic()))
        if not ready:
            return False
        if self.conn in ready:
            try:
                self.label, self.show = self.conn.recv()
                return True
            except EOFError:
                pass
        self.process.join(max(0, deadline - time.monotonic()))
        self.code = self.process.exitcode
        return self.code is not None

    def advance(self, deadline: float) -> bool:
        try:
            self.conn.send(self.turn)
        except BrokenPipeError:
            # An external thread can kill a child after it sent its point.
            pass
        return self.receive(deadline)


def _child(source, spec: dict, workdir: Path, inc: int, actor_id: int, copies: int,
           conn: Connection, inherited: Connection | None) -> None:
    from .runner import child_main

    if inherited is not None:
        inherited.close()
    if isinstance(source, tuple):
        from .child import load_module, scenarios_in

        file, name = source
        source = scenarios_in(load_module(file))[name]
    fn = source.fn if actor_id < copies else source.concurrent[actor_id - copies]
    name = getattr(fn, "__name__", type(fn).__name__)
    gate = Gate(conn, actor_id, name)
    twin = actor_id if actor_id < copies else 0
    actor_slot = max(0, actor_id - copies + 1)
    os._exit(child_main(source, spec, workdir, inc, twin, fn=fn, gate=gate, actor_slot=actor_slot))


def run_processes(scen: Scenario, spec: dict, workdir: Path, inc: int, copies: int, *,
                  use_fork: bool, choices: ChoiceSource, quiet: bool, timeout: float,
                  timeout_exit: int, world: JsonlWriter) -> list[_Actor]:
    """Wait for all entry points, then hand the turn to one actor at a time."""
    context = multiprocessing.get_context("fork" if use_fork else "spawn")
    actors: list[_Actor] = []
    realized = JsonlWriter(workdir / REALIZED)
    deadline = time.monotonic() + timeout
    try:
        for index, fn in enumerate([scen.fn] * copies + list(scen.concurrent)):
            parent, child = context.Pipe()
            source = scen if use_fork else (scen.file, scen.name)
            process = context.Process(target=_child, args=(
                source, spec, workdir, inc, index, copies, child, parent if use_fork else None))
            try:
                process.start()
            except BaseException:
                parent.close()
                raise
            finally:
                child.close()
            name = getattr(fn, "__name__", type(fn).__name__)
            actors.append(_Actor(index, name, index if index < copies else 0, process, parent))
        stalled = _schedule(actors, choices, quiet, inc, deadline, world, realized)
        if stalled is not None:
            waiting = ", ".join(f"{a.name} at {a.label!r}" for a in actors
                                if a is not stalled and a.code is None)
            detail = f"; other actors waiting: {waiting}" if waiting else ""
            world.write(stalled.event(
                inc, PARENT_SEQ - 1, kind="error",
                text=f"possible deadlock or timeout: {stalled.name} made no progress from "
                     f"{stalled.label!r} within {timeout:g}s{detail}"))
            for a in actors:
                if a.code is None:
                    a.code = timeout_exit
        return actors
    finally:
        realized.close()
        for a in actors:
            if a.process.is_alive():
                a.process.kill()
        for a in actors:
            a.process.join()
            a.process.close()
            a.conn.close()


def _schedule(actors: list[_Actor], choices: ChoiceSource, quiet: bool, inc: int,
              deadline: float, world: JsonlWriter, realized: JsonlWriter) -> _Actor | None:
    for a in actors:
        if not a.receive(deadline):
            return a
    turn, picks = 0, 0
    # Run each actor's setup before its first real point, in declaration order.
    for a in actors:
        if a.code is None:
            turn += 1
            a.turn = turn
            if not a.advance(deadline):
                return a
    weight = 1.0 if choices.enabled("interleave") else 0.0
    while ready := [a for a in actors if a.code is None]:
        if time.monotonic() >= deadline:
            return ready[0]
        key = f"sched:actors#{inc}.{picks}"
        index = 0
        if len(ready) > 1 and not quiet:
            index = choices.draw(key, [3.0] + [weight / (len(ready) - 1)] * (len(ready) - 1))
            picks += 1
        a = ready[index]
        turn += 1
        a.turn = turn
        if index:
            nominal = ready[0]
            realized.write(a.event(inc, -2, key=key, value=index, schedule="actors",
                                   chosen=f"actor {a.id} {a.name} at {a.label!r}",
                                   nominal=f"actor {nominal.id} {nominal.name} at {nominal.label!r}"))
        if a.show:
            world.write(a.event(inc, -1, kind="point", label=a.label))
        if not a.advance(deadline):
            return a
    return None
