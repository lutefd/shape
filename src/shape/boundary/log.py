"""The on-disk logs. Each entry is one JSON line, appended and fsynced before the call returns,
so a process that dies on the very next instruction still left the entry behind."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

WORLD = "world.jsonl"
REALIZED = "realized.jsonl"
PARENT_SEQ = 10**9  # the runner's own entries sort after everything an incarnation wrote


def event_order(entry: dict) -> tuple[int, int, int]:
    seq = entry["seq"]
    turn = PARENT_SEQ if seq > PARENT_SEQ else entry.get("turn", entry.get("twin", 0))
    return entry["inc"], turn, seq


def args_hash(args: Any) -> str:
    text = json.dumps(args, sort_keys=True, default=repr)
    return hashlib.blake2b(text.encode(), digest_size=8).hexdigest()


class JsonlWriter:
    def __init__(self, path: Path, *, fsync: bool = True) -> None:
        self.fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        self.fsync = fsync

    def write(self, entry: dict) -> None:
        os.write(self.fd, (json.dumps(entry, default=repr) + "\n").encode())
        if self.fsync:
            os.fsync(self.fd)

    def close(self) -> None:
        os.close(self.fd)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            break  # a torn last line from a killed writer
    return entries
