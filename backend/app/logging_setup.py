"""Structured (key=value) logging. Source code is never logged by default."""

from __future__ import annotations

import logging
from typing import Any

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class KeyValueFormatter(logging.Formatter):
    """`ts level logger event key=value ...` — extras passed via `extra={}` are appended."""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{self.formatTime(record, '%Y-%m-%dT%H:%M:%S')} {record.levelname} "
            f"{record.name} {record.getMessage()}"
        )
        extras = " ".join(f"{k}={v}" for k, v in record.__dict__.items() if k not in _RESERVED)
        text = f"{base} {extras}".rstrip()
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(KeyValueFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit a pipeline event such as `repository_scan_started` with structured fields."""
    logger.info(event, extra=fields)
