"""Centralized logging setup utilities."""

from __future__ import annotations

import logging
import logging.config
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
    logging.config.dictConfig(
        build_logging_config(log_level=resolved.log_level, service_name=resolved.service_name)
    )


def get_logger(name: str) -> logging.Logger:
    """Get an application logger."""

    return logging.getLogger(name)
