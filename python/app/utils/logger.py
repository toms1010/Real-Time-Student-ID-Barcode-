"""Structured logging.

Two formatters are provided:

``text``
    Human readable single line per event - the format shown in the brief::

        2026-09-30 09:42:17 INFO  Camera initialized

``json``
    One JSON object per event, for log shipping during evaluation.

Both include the process name and thread name, which matters because the camera
thread, the decode thread and the request workers are separate threads in the
same process.

A :class:`RedactingFilter` strips anything that looks like a secret from the
formatted record: ``password``, ``token``, ``secret``, ``authorization``,
``cookie`` and the Argon2 hash prefix.  This is a defence in depth - the code
paths below also avoid logging these values at all.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_LOG_CONFIGURED = False
_CONFIG_LOCK = threading.Lock()
_SENSITIVE_KEYS = re.compile(
    r"(password|passwd|token|secret|authorization|cookie|session|api[_-]?key)",
    re.IGNORECASE,
)
_REDACTED = "***REDACTED***"


class RedactingFilter(logging.Filter):
    """Replace sensitive values in ``args`` before they reach a handler."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - stdlib API
        if isinstance(record.args, dict):
            record.args = {k: (_REDACTED if _SENSITIVE_KEYS.search(str(k)) else v)
                           for k, v in record.args.items()}
        return True


class _TextFormatter(logging.Formatter):
    default_time_format = "%Y-%m-%d %H:%M:%S"
    default_msec_format = "%s.%03d"

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-5s %(threadName)s [%(name)s] %(message)s",
            datefmt=self.default_time_format,
        )

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        stamp = datetime.fromtimestamp(record.created).strftime(datefmt or self.default_time_format)
        return f"{stamp}.{int(record.msecs):03d}"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "thread": record.threadName,
            "process": record.process,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "event_data", None)
        if isinstance(extra, dict):
            payload["data"] = extra
        return json.dumps(payload, default=str)


def setup_logging(
    level: str = "INFO",
    fmt: str = "text",
    *,
    log_file: str | os.PathLike[str] | None = None,
    force: bool = False,
) -> None:
    """Configure the root logger once per process."""
    global _LOG_CONFIGURED
    with _CONFIG_LOCK:
        if _LOG_CONFIGURED and not force:
            return
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)

        root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
        formatter = _JsonFormatter() if fmt.lower() == "json" else _TextFormatter()

        console = logging.StreamHandler(stream=sys.stdout)
        console.setFormatter(formatter)
        console.addFilter(RedactingFilter())
        root.addHandler(console)

        if log_file:
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            rotating = logging.handlers.RotatingFileHandler(
                path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            rotating.setFormatter(formatter)
            rotating.addFilter(RedactingFilter())
            root.addHandler(rotating)

        # Third-party libraries are noisy at DEBUG; keep the app's own logs clean.
        for noisy in ("uvicorn.access", "PIL", "multipart", "httpx", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        _LOG_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger.  The redacting filter is attached lazily so that
    modules imported before :func:`setup_logging` still get filtered."""
    logger = logging.getLogger(name)
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger


def log_event(logger: logging.Logger, level: int, message: str, **data: Any) -> None:
    """Log ``message`` with a structured ``data`` payload attached.

    ``data`` is shown by the JSON formatter and, for text mode, appended as
    ``key=value`` pairs.  Sensitive keys are redacted.
    """
    safe = {k: (_REDACTED if _SENSITIVE_KEYS.search(str(k)) else v) for k, v in data.items()}
    logger.log(level, message, extra={"event_data": safe})
    if safe and not logger.isEnabledFor(logging.DEBUG):
        return
