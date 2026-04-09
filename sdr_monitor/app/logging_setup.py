"""Centralized logging setup utilities."""

from __future__ import annotations

import logging
import logging.config
import os
from pathlib import Path
from typing import Any

from app.config import Config, load_config


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
                "class": "logging.StreamHandler",
                "formatter": "standard",
            }
        },
        "root": {
            "handlers": ["console"],
            "level": log_level,
        },
    }


def configure_logging(config: Config | None = None) -> None:
    """Apply global logging configuration."""

    resolved = config or load_config()
    if resolved.stderr_log_path is not None:
        _redirect_stderr_to_file(resolved.stderr_log_path)
    logging.config.dictConfig(
        build_logging_config(log_level=resolved.log_level, service_name=resolved.service_name)
    )


def get_logger(name: str) -> logging.Logger:
    """Get an application logger."""

    return logging.getLogger(name)


def _redirect_stderr_to_file(log_path: Path) -> None:
    """Redirect process-level stderr to a file in append mode."""

    resolved = log_path.expanduser().resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor = os.open(
        resolved,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o644,
    )
    try:
        os.dup2(file_descriptor, 2)
    finally:
        os.close(file_descriptor)
