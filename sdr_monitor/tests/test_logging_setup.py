from __future__ import annotations

import logging

from app.config import Config
from app.logging_setup import build_logging_config, configure_logging


def test_build_logging_config_includes_service_name() -> None:
    payload = build_logging_config(log_level="INFO", service_name="air-marine")
    formatter = payload["formatters"]["standard"]["format"]
    assert "air-marine" in formatter
    assert payload["root"]["level"] == "INFO"


def test_configure_logging_sets_root_level() -> None:
    config = Config(log_level="DEBUG")
    configure_logging(config)
    assert logging.getLogger().level == logging.DEBUG
