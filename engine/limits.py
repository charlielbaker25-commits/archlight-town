"""The limits file: the one and only place every operating limit lives.

The engine knows where the file is (the pack tells it) and how to read or change
one value. It never keeps its own copy of a number: callers ask every time,
so a limit Charlie changes takes effect on the next check.
"""
from pathlib import Path
from typing import Any

from engine.yamlio import load_yaml, save_yaml


class LimitNotFound(KeyError):
    pass


class Limits:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def get(self, dotted_key: str) -> Any:
        """Read one limit, e.g. limits.get("money.api_monthly_cap_usd")."""
        node: Any = load_yaml(self.path)
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise LimitNotFound(dotted_key)
            node = node[part]
        return node

    def set(self, dotted_key: str, value: Any) -> Any:
        """Change one existing limit and return the old value.

        Only the executor calls this, when running an approved limit-change action.
        It refuses to invent new keys, so a typo can't create a limit nobody reads.
        """
        data = load_yaml(self.path)
        *parents, leaf = dotted_key.split(".")
        node = data
        for part in parents:
            if not isinstance(node, dict) or part not in node:
                raise LimitNotFound(dotted_key)
            node = node[part]
        if not isinstance(node, dict) or leaf not in node:
            raise LimitNotFound(dotted_key)
        old = node[leaf]
        node[leaf] = value
        save_yaml(self.path, data)
        return old
