"""Token budgeting without a tokenizer: a conservative character-based estimate."""

from __future__ import annotations

import math

CHARS_PER_TOKEN = 3.5  # source code tokenizes denser than prose; err on the side of too many


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN)) if text else 0


def truncate_to_tokens(text: str, tokens: int, marker: str = "[... truncated]") -> tuple[str, bool]:
    """Cut `text` at a line boundary so it fits `tokens`; returns (text, was_truncated)."""
    if estimate_tokens(text) <= tokens:
        return text, False
    budget_chars = max(0, int(tokens * CHARS_PER_TOKEN) - len(marker) - 1)
    lines = text.split("\n")
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget_chars:
            break
        kept.append(line)
        used += len(line) + 1
    omitted = len(lines) - len(kept)
    return "\n".join([*kept, f"{marker} ({omitted} more lines)"]), True
