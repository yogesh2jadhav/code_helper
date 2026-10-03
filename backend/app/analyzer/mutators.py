"""Heuristic: which method names mutate their receiver.

Used by control flow and data flow to treat `list.add(x)` or `dto.setName(y)` as a change to
`list` / `dto`. It is name-based because JDK member signatures are not available, so it is a
heuristic and is reported as such.
"""

from __future__ import annotations

MUTATING_METHODS = frozenset(
    {
        "add",
        "addAll",
        "addFirst",
        "addLast",
        "put",
        "putAll",
        "putIfAbsent",
        "remove",
        "removeAll",
        "removeIf",
        "removeFirst",
        "removeLast",
        "retainAll",
        "clear",
        "push",
        "pop",
        "poll",
        "offer",
        "offerFirst",
        "offerLast",
        "append",
        "insert",
        "delete",
        "deleteCharAt",
        "set",
        "sort",
        "replaceAll",
        "merge",
        "compute",
        "computeIfAbsent",
        "computeIfPresent",
        "setLength",
        "reverse",
        "shuffle",
        "fill",
    }
)


def is_mutator(name: str | None) -> bool:
    if not name:
        return False
    if name in MUTATING_METHODS:
        return True
    return len(name) > 3 and name.startswith("set") and name[3].isupper()  # setters
