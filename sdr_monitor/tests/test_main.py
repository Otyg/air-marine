from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

from app.config import Config
from app.main import (
    build_decoder_process_config,
    create_service_components,
    is_radio_connected,
    recover_state_from_latest_targets,
    resolve_adsb_snapshot_path,
)
from app.models import Freshness, ScanBand, Source, Target, TargetKind
from app.state import LiveState
from app.store import SQLiteStore


def _target(target_id: str, last_seen: datetime) -> Target:
    return Target(
        target_id=target_id,
        source=Source.ADSB,
        kind=TargetKind.AIRCRAFT,
        label="FLT1",
        lat=59.0,
        lon=18.0,
        course=90.0,
        speed=120.0,
        altitude=1000.0,
        first_seen=last_seen - timedelta(minutes=1),
        last_seen=last_seen,
        freshness=Freshness.FRESH,
        last_scan_band=ScanBand.ADSB,
        icao24="abcdef",
    )


def test_recover_state_from_latest_targets(tmp_path) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "recover.sqlite3")
    store.initialize()
    store.upsert_latest_target(_target("adsb:abcdef", now))

    state = LiveState(clock=lambda: now)
    recovered_count = recover_state_from_latest_targets(state=state, store=store)

    assert recovered_count == 1
    recovered = state.get_target_state("adsb:abcdef")
    assert recovered is not None
    assert recovered.target.target_id == "adsb:abcdef"


def test_create_service_components_without_background_scanner(tmp_path) -> None:
    config = Config(
        sqlite_path=tmp_path / "service.sqlite3",
        adsb_window_seconds=0.5,
        ais_window_seconds=0.5,
    )

    components = create_service_components(
        config=config,
        start_scanner=False,
        recover_latest_targets=False,
    )

    assert components.config.sqlite_path == config.sqlite_path
    assert components.store.sqlite_path == config.sqlite_path
    assert components.scanner_worker.status()["is_alive"] is False
    assert components.app is not None


def test_build_decoder_process_config_matches_ingestors() -> None:
    decoder_config = build_decoder_process_config(
        adsb_snapshot_path=Path("/tmp/readsb/aircraft.json"),
        ais_tcp_port=10110,
    )
    assert decoder_config.adsb_command[0] == "readsb"
    assert "--device-type" in decoder_config.adsb_command
    device_type_index = decoder_config.adsb_command.index("--device-type")
    assert decoder_config.adsb_command[device_type_index + 1] == "rtlsdr"
    assert "--write-json" in decoder_config.adsb_command
    assert "/tmp/readsb" in decoder_config.adsb_command
    assert decoder_config.ais_command == ("rtl_ais", "-T", "-P", "10110", "-n")


def test_resolve_adsb_snapshot_path_falls_back_on_unwritable_dir(tmp_path) -> None:
    @dataclass
    class FakeLogger:
        messages: list[str]

        def warning(self, message, *args):  # noqa: ANN001
            self.messages.append(message % args)

    logger = FakeLogger(messages=[])
    resolved = resolve_adsb_snapshot_path(Path("/proc/readsb/aircraft.json"), logger=logger)
    assert resolved == Path("./data/readsb/aircraft.json")
    assert logger.messages


def test_is_radio_connected_returns_true_when_probe_succeeds() -> None:
    @dataclass
    class FakeLogger:
        messages: list[str]

        def warning(self, message, *args):  # noqa: ANN001
            self.messages.append(message % args)

    logger = FakeLogger(messages=[])

    def _run_command(*args, **kwargs):  # noqa: ANN002, ARG001
        return subprocess.CompletedProcess(args=["rtl_test"], returncode=0, stdout="", stderr="")

    assert is_radio_connected(logger=logger, run_command=_run_command) is True
    assert logger.messages == []


def test_is_radio_connected_returns_false_when_probe_fails() -> None:
    @dataclass
    class FakeLogger:
        messages: list[str]

        def warning(self, message, *args):  # noqa: ANN001
            self.messages.append(message % args)

    logger = FakeLogger(messages=[])

    def _run_command(*args, **kwargs):  # noqa: ANN002, ARG001
        return subprocess.CompletedProcess(
            args=["rtl_test"],
            returncode=1,
            stdout="",
            stderr="No supported devices found.",
        )

    assert is_radio_connected(logger=logger, run_command=_run_command) is False
    assert logger.messages
    assert "no radio was detected" in logger.messages[0].lower()


def test_scanner_does_not_start_on_startup_when_no_radio(tmp_path, monkeypatch) -> None:
    config = Config(
        sqlite_path=tmp_path / "service.sqlite3",
        adsb_window_seconds=0.01,
        ais_window_seconds=0.01,
    )
    components = create_service_components(
        config=config,
        start_scanner=True,
        recover_latest_targets=False,
    )

    probe_calls = {"count": 0}

    def _probe(**kwargs):  # noqa: ANN003, ARG001
        probe_calls["count"] += 1
        return False

    monkeypatch.setattr("app.main.is_radio_connected", _probe)

    asyncio.run(components.app.router.startup())
    assert probe_calls["count"] == 1
    assert components.scanner_worker.status()["is_alive"] is False
    asyncio.run(components.app.router.shutdown())
