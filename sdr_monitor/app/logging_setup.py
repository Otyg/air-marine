"""Centralized logging setup utilities."""

from __future__ import annotations

import errno
import logging
import logging.config
import os
from pathlib import Path
import sys
from typing import Any

from app.config import Config, load_config


class ResilientStreamHandler(logging.StreamHandler):
    """Stream handler that tolerates broken stdio targets in long-running services."""

    _BROKEN_STREAM_ERRNOS = {errno.EIO, errno.EPIPE, errno.EBADF}

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError) and exc.errno in self._BROKEN_STREAM_ERRNOS:
            # Terminal/pipe disappeared; switch to /dev/null to avoid repeated tracebacks.
            try:
                self.setStream(open(os.devnull, "a", encoding="utf-8"))  # noqa: SIM115
            except Exception:
                pass
            return
        super().handleError(record)


def build_logging_config(log_level: str, service_name: str) -> dict[str, Any]:
    """Build a dictConfig payload for app logging."""

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": (
                    "%(asctime)s | %(levelname)s | %(name)s | "
                    f"{service_name} | %(message)s"
                )
            }
        },
        "handlers": {
            "console": {
                "class": "app.logging_setup.ResilientStreamHandler",
                "formatter": "standard",
            }
        },
        "root": {
            "handlers": ["console"],
            "level": log_level,
        },
        "loggers": {
            "uvicorn": {"handlers": ["console"], "level": log_level, "propagate": False},
            "uvicorn.error": {"handlers": ["console"], "level": log_level, "propagate": False},
            "uvicorn.access": {"handlers": ["console"], "level": log_level, "propagate": False},
        },
    }


def configure_logging(config: Config | None = None) -> None:
    """Apply global logging configuration."""

    resolved = config or load_config()
    logging.raiseExceptions = False
    if resolved.stdout_log_path is not None:
        _redirect_fd_to_file(resolved.stdout_log_path, fd=1)
    if resolved.stderr_log_path is not None:
        _redirect_fd_to_file(resolved.stderr_log_path, fd=2)
    logging.config.dictConfig(
        build_logging_config(log_level=resolved.log_level, service_name=resolved.service_name)
    )


def get_logger(name: str) -> logging.Logger:
    """Get an application logger."""

    return logging.getLogger(name)


def _redirect_fd_to_file(log_path: Path, *, fd: int) -> None:
    """Redirect process-level file descriptor to a file in append mode."""

    resolved = log_path.expanduser().resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor = os.open(
        resolved,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o644,
    )
    try:
        os.dup2(file_descriptor, fd)
    finally:
        os.close(file_descriptor)
