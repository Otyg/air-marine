from __future__ import annotations

import logging
import os

from app.config import Config
from app.logging_setup import build_logging_config, configure_logging


def test_build_logging_config_includes_service_name() -> None:
    payload = build_logging_config(log_level="INFO", service_name="air-marine")
    formatter = payload["formatters"]["standard"]["format"]
    assert "air-marine" in formatter
    assert payload["root"]["level"] == "INFO"
    assert payload["handlers"]["console"]["class"] == "app.logging_setup.ResilientStreamHandler"
    assert payload["loggers"]["uvicorn.access"]["level"] == "INFO"


def test_configure_logging_sets_root_level() -> None:
    config = Config(log_level="DEBUG", stderr_log_path=None)
    configure_logging(config)
    assert logging.getLogger().level == logging.DEBUG
    assert logging.raiseExceptions is False


def test_configure_logging_redirects_stderr_when_path_is_configured(
    tmp_path,
    monkeypatch,
) -> None:
    target = tmp_path / "logs" / "stderr.log"
    calls: list[tuple[str, int, int | None]] = []

    def _fake_open(path, flags, mode):  # noqa: ANN001
        assert str(path).endswith("stderr.log")
        assert flags & os.O_APPEND
        calls.append(("open", flags, mode))
        return 123

    def _fake_dup2(src, dst):  # noqa: ANN001
        calls.append(("dup2", src, dst))

    def _fake_close(fd):  # noqa: ANN001
        calls.append(("close", fd, None))

    monkeypatch.setattr("app.logging_setup.os.open", _fake_open)
    monkeypatch.setattr("app.logging_setup.os.dup2", _fake_dup2)
    monkeypatch.setattr("app.logging_setup.os.close", _fake_close)

    configure_logging(Config(stderr_log_path=target))

    assert target.parent.exists()
    assert ("dup2", 123, 2) in calls
    assert ("close", 123, None) in calls


def test_configure_logging_redirects_stdout_when_path_is_configured(
    tmp_path,
    monkeypatch,
) -> None:
    target = tmp_path / "logs" / "stdout.log"
    calls: list[tuple[str, int, int | None]] = []

    def _fake_open(path, flags, mode):  # noqa: ANN001
        assert str(path).endswith("stdout.log")
        assert flags & os.O_APPEND
        calls.append(("open", flags, mode))
        return 456

    def _fake_dup2(src, dst):  # noqa: ANN001
        calls.append(("dup2", src, dst))

    def _fake_close(fd):  # noqa: ANN001
        calls.append(("close", fd, None))

    monkeypatch.setattr("app.logging_setup.os.open", _fake_open)
    monkeypatch.setattr("app.logging_setup.os.dup2", _fake_dup2)
    monkeypatch.setattr("app.logging_setup.os.close", _fake_close)

    configure_logging(Config(stdout_log_path=target, stderr_log_path=None))

    assert target.parent.exists()
    assert ("dup2", 456, 1) in calls
    assert ("close", 456, None) in calls
