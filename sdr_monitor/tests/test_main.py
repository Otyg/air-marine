from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import httpx

from app.config import Config
from app.env_utils import load_local_dotenv
from app.main import (
    build_decoder_process_config,
    create_service_components,
    is_radio_connected,
    recover_state_from_latest_targets,
    resolve_adsb_snapshot_path,
)
from app.models import Freshness, ScanBand, Source, Target, TargetKind
from app.radio_v2 import ScannerOrchestratorV2
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


def _request(app, method: str, path: str, **kwargs) -> httpx.Response:
    async def _run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(_run())


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


def test_load_local_dotenv_prefers_cwd_then_project_root(tmp_path) -> None:
    cwd_dir = tmp_path / "cwd"
    project_root = tmp_path / "project"
    cwd_dir.mkdir()
    project_root.mkdir()
    (cwd_dir / ".env").write_text("A=1\n", encoding="utf-8")
    (project_root / ".env").write_text("B=2\n", encoding="utf-8")

    loaded_calls: list[Path] = []

    def _fake_load_dotenv(*, dotenv_path, override):  # noqa: ANN001
        assert override is False
        loaded_calls.append(Path(dotenv_path))
        return True

    loaded = load_local_dotenv(
        _fake_load_dotenv,
        project_root=project_root,
        cwd=cwd_dir,
    )

    assert loaded == (cwd_dir / ".env", project_root / ".env")
    assert loaded_calls == [cwd_dir / ".env", project_root / ".env"]


def test_create_service_components_without_background_scanner(tmp_path) -> None:
    fixed_objects_path = tmp_path / "fixed_objects.json"
    fixed_objects_path.write_text(
        json.dumps(
            [
                {
                    "name": "Harbor",
                    "latitude": 56.1619519,
                    "longitude": 15.5940978,
                }
            ]
        ),
        encoding="utf-8",
    )
    config = Config(
        sqlite_path=tmp_path / "service.sqlite3",
        adsb_window_seconds=0.5,
        ais_window_seconds=0.5,
        fixed_objects_path=fixed_objects_path,
    )

    components = create_service_components(
        config=config,
        start_scanner=False,
        recover_latest_targets=False,
    )

    assert components.config.sqlite_path == config.sqlite_path
    assert components.store.sqlite_path == config.sqlite_path
    assert components.scanner_worker.status()["is_alive"] is False
    response = _request(components.app, "GET", "/")
    assert response.status_code == 200
    assert "Harbor" in response.text
    assert components.app is not None
    assert components.scanner.status()["ogn_window_seconds"] == 0.0


def test_create_service_components_with_mock_backend_uses_v2_scanner(tmp_path) -> None:
    fixture_path = (
        Path(__file__).resolve().parent / "fixtures" / "mock_radio" / "mixed_cycle.json"
    )
    config = Config(
        sqlite_path=tmp_path / "service.sqlite3",
        adsb_window_seconds=0.01,
        ogn_window_seconds=0.01,
        ais_window_seconds=0.01,
        dsc_window_seconds=0.01,
        radio_backend="mock",
        mock_radio_fixture_path=fixture_path,
    )

    components = create_service_components(
        config=config,
        start_scanner=False,
        recover_latest_targets=False,
    )

    assert isinstance(components.scanner, ScannerOrchestratorV2)


def test_build_decoder_process_config_matches_ingestors() -> None:
    decoder_config = build_decoder_process_config(
        adsb_snapshot_path=Path("/tmp/readsb/aircraft.json"),
        ais_tcp_port=10110,
    )
    assert decoder_config.adsb_command[0] == "readsb"
    assert decoder_config.ogn_command is None
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


def test_startup_prunes_targets_latest_older_than_ten_minutes_when_radio_connected(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
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
    components.store.upsert_latest_target(_target("adsb:old", now - timedelta(minutes=11)))
    components.store.upsert_latest_target(_target("adsb:new", now - timedelta(minutes=5)))

    monkeypatch.setattr("app.main.is_radio_connected", lambda **kwargs: True)

    start_calls = {"count": 0}

    def _start() -> None:
        start_calls["count"] += 1

    monkeypatch.setattr(components.scanner_worker, "start", _start)
    monkeypatch.setattr("app.main.datetime", type("FrozenDateTime", (), {"now": staticmethod(lambda tz: now)}))

    asyncio.run(components.app.router.startup())
    assert start_calls["count"] == 1
    latest_target_ids = {target.target_id for target in components.store.load_latest_targets()}
    assert "adsb:new" in latest_target_ids
    assert "adsb:old" not in latest_target_ids
    asyncio.run(components.app.router.shutdown())
