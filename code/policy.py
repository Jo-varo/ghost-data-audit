"""Deletion policy loaded from dataset/deletion_rules.csv."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from loaders import read_csv

ACTIONS = {"DELETE", "REDACT", "ANONYMIZE", "RETAIN"}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str


class Policy:
    def __init__(self, rules: list[Rule]):
        self.by_category: dict[str, list[Rule]] = defaultdict(list)
        for rule in rules:
            if rule.required_action not in ACTIONS:
                raise ValueError(f"{rule.rule_id}: unknown action {rule.required_action!r}")
            self.by_category[rule.data_category].append(rule)
        for category, rs in self.by_category.items():
            if len({r.required_action for r in rs}) > 1:
                # Conditional rules would need case-specific evaluation; the supplied policy has one per category.
                raise ValueError(f"conflicting actions for {category}: {[r.rule_id for r in rs]}")

    @classmethod
    def load(cls, path: Path) -> "Policy":
        return cls([Rule(r["rule_id"], r["data_category"], r["condition"], r["required_action"])
                    for r in read_csv(path)])

    def action(self, category: str) -> str | None:
        rules = self.by_category.get(category)
        return rules[0].required_action if rules else None

    def restorable(self, category: str) -> bool:
        """A retained archive whose rule is conditioned on blocking restore access."""
        return self.action(category) == "RETAIN" and any(
            "restore" in r.condition.lower() for r in self.by_category[category])

    def permanently_retained(self, category: str) -> bool:
        """Retained records whose state does not change (no restore path to manage)."""
        return self.action(category) == "RETAIN" and not self.restorable(category)
