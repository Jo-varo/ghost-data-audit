"""Allowed step operations, loaded from code/config/operations.json."""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent / "config" / "operations.json"


class OperationCatalog:
    def __init__(self, phases: list[str], operations: list[dict]):
        self.phases = list(phases)
        self._phase_rank = {p: i for i, p in enumerate(self.phases)}
        self._by_name: dict[str, dict] = {}
        self._rank: dict[str, int] = {}
        for i, op in enumerate(operations):
            name = op["operation"]
            if name in self._by_name:
                raise ValueError(f"duplicate operation {name!r}")
            if op["phase"] not in self._phase_rank:
                raise ValueError(f"operation {name!r} has unknown phase {op['phase']!r}")
            self._by_name[name] = op
            self._rank[name] = i
        empty = [p for p in self.phases if not any(o["phase"] == p for o in operations)]
        if empty:
            raise ValueError(f"phases without operations: {empty}")

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> "OperationCatalog":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["phases"], data["operations"])

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def phase(self, name: str) -> str:
        return self._get(name)["phase"]

    def state(self, name: str) -> str:
        return self._get(name)["state"]

    def sort_key(self, name: str) -> tuple[int, int]:
        """Phase order, then the operation's row order in the catalog."""
        return self._phase_rank[self.phase(name)], self._rank[name]

    def _get(self, name: str) -> dict:
        try:
            return self._by_name[name]
        except KeyError:
            raise ValueError(f"operation {name!r} is not in the allowed operations catalog") from None
