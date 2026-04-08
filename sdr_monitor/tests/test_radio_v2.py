from __future__ import annotations

from datetime import datetime, timezone
import io
import json
from pathlib import Path

import pytest

from app.models import NormalizedObservation, ScanBand, Source, TargetKind
from app.radio_v2 import (
    ADSBPipeline,
    AISPipeline,
    DSCPipeline,
    InprocBackend,
    LegacyBackend,
    MockBackend,
    OGNPipeline,
    ObservationEvent,
    ExternalBackend,
    ScannerOrchestratorV2,
)
from app.scanner import ScannerConfig
from app.state import LiveState


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "mock_radio"


class _StaticReader:
    def __init__(self, observations: list[NormalizedObservation]) -> None:
        self._observations = list(observations)

    def read_observations(self, **kwargs):  # noqa: ANN003
        return list(self._observations)


def test_mock_backend_deterministic_replay_returns_expected_sequence() -> None:
    backend = MockBackend(fixture_path=FIXTURE_DIR / "mixed_cycle.json", enable_timing_mode=False)
    backend.start()

    ais_events = backend.read(0.1, band=ScanBand.AIS)
    adsb_events = backend.read(0.1, band=ScanBand.ADSB)
    ogn_events = backend.read(0.1, band=ScanBand.OGN)
    dsc_events = backend.read(0.1, band=ScanBand.DSC)

    assert len(ais_events) == 1
    assert len(adsb_events) == 1
    assert len(ogn_events) == 1
    assert len(dsc_events) == 1


def test_mock_backend_timing_mode_adds_jitter_but_keeps_data() -> None:
    backend = MockBackend(fixture_path=FIXTURE_DIR / "retune_mid_window.json", enable_timing_mode=True)
    backend.start()

    events = backend.read(0.1, band=ScanBand.AIS)
    assert events

    event = events[0]
    assert getattr(event, "source_band", None) == ScanBand.AIS


def test_mock_backend_retune_changes_active_band_via_map() -> None:
    backend = MockBackend(fixture_path=FIXTURE_DIR / "retune_mid_window.json", enable_timing_mode=False)
    backend.start()

    backend.retune(1090000000)
    events = backend.read(0.1)

    assert events
    assert getattr(events[0], "source_band", None) == ScanBand.ADSB


def test_mock_backend_handles_disconnect_and_reconnect() -> None:
    backend = MockBackend(fixture_path=FIXTURE_DIR / "disconnect_reconnect.json", enable_timing_mode=False)
    backend.start()

    first = backend.read(0.1, band=ScanBand.ADSB)
    second = backend.read(0.1, band=ScanBand.ADSB)
    third = backend.read(0.1, band=ScanBand.ADSB)

    assert first == []
    assert second == []
    assert third


def test_scanner_orchestrator_v2_ingests_all_sources_with_mock_backend() -> None:
    backend = MockBackend(fixture_path=FIXTURE_DIR / "mixed_cycle.json", enable_timing_mode=False)
    scanner = ScannerOrchestratorV2(
        backend=backend,
        pipelines={
            ScanBand.AIS: AISPipeline(),
            ScanBand.ADSB: ADSBPipeline(),
            ScanBand.OGN: OGNPipeline(),
            ScanBand.DSC: DSCPipeline(),
        },
        state=LiveState(clock=lambda: datetime(2026, 4, 8, tzinfo=timezone.utc)),
        store=None,
        config=ScannerConfig(
            adsb_window_seconds=0.01,
            ogn_window_seconds=0.01,
            ais_window_seconds=0.01,
            dsc_window_seconds=0.01,
            inter_scan_pause_seconds=0.0,
        ),
        sleep_fn=lambda _: None,
        now_fn=lambda: datetime(2026, 4, 8, tzinfo=timezone.utc),
    )

    scanner.run_cycle()

    status = scanner.status()
    assert status["scan_mode"] == "hybrid"
    assert status["last_error"] is None
    stats = scanner._state.get_stats()  # noqa: SLF001
    assert stats["total_live_targets"] >= 4


def test_scanner_orchestrator_v2_survives_store_errors() -> None:
    class FailingStore:
        def persist_observation_and_target(self, observation, target):  # noqa: ANN001
            raise RuntimeError("store write failed")

        def delete_latest_targets_older_than(self, cutoff):  # noqa: ANN001
            return 0

    backend = MockBackend(fixture_path=FIXTURE_DIR / "nominal_adsb.json", enable_timing_mode=False)
    scanner = ScannerOrchestratorV2(
        backend=backend,
        pipelines={ScanBand.ADSB: ADSBPipeline()},
        state=LiveState(clock=lambda: datetime(2026, 4, 8, tzinfo=timezone.utc)),
        store=FailingStore(),  # type: ignore[arg-type]
        config=ScannerConfig(
            adsb_window_seconds=0.01,
            ais_window_seconds=0.01,
            inter_scan_pause_seconds=0.0,
        ),
        sleep_fn=lambda _: None,
        now_fn=lambda: datetime(2026, 4, 8, tzinfo=timezone.utc),
    )

    scanner.set_scan_mode("continuous_adsb")
    scanner.run_cycle()

    assert scanner.status()["last_error"] is not None
    assert "store" in scanner.status()["last_error"]


def test_parity_legacy_inproc_and_mock_for_observation_events() -> None:
    fixture = json.loads((FIXTURE_DIR / "mixed_cycle.json").read_text(encoding="utf-8"))
    by_band: dict[ScanBand, list[NormalizedObservation]] = {}
    for row in fixture["timeline"]:
        if row.get("event_type") != "observation":
            continue
        payload = row.get("payload", {}).get("observation")
        if not isinstance(payload, dict):
            continue
        band = ScanBand(row["band"])
        by_band.setdefault(band, []).append(NormalizedObservation.from_dict(payload))

    readers = {band: _StaticReader(obs) for band, obs in by_band.items()}
    legacy = LegacyBackend(readers)
    inproc = InprocBackend(readers)
    mock = MockBackend(fixture_path=FIXTURE_DIR / "mixed_cycle.json", enable_timing_mode=False)
    legacy.start()
    inproc.start()
    mock.start()

    for band in (ScanBand.AIS, ScanBand.ADSB, ScanBand.OGN, ScanBand.DSC):
        legacy_events = legacy.read(0.1, band=band)
        inproc_events = inproc.read(0.1, band=band)
        mock_events = mock.read(0.1, band=band)

        assert len(legacy_events) == 1
        assert len(inproc_events) == 1
        assert len(mock_events) == 1

        assert isinstance(legacy_events[0], ObservationEvent)
        assert isinstance(inproc_events[0], ObservationEvent)
        assert isinstance(mock_events[0], ObservationEvent)

        assert legacy_events[0].observation.to_dict() == inproc_events[0].observation.to_dict()
        assert legacy_events[0].observation.to_dict() == mock_events[0].observation.to_dict()


def test_invalid_retune_is_rejected_and_surfaces_in_scanner_status() -> None:
    backend = MockBackend(fixture_path=FIXTURE_DIR / "nominal_adsb.json", enable_timing_mode=False)
    scanner = ScannerOrchestratorV2(
        backend=backend,
        pipelines={ScanBand.ADSB: ADSBPipeline()},
        state=LiveState(clock=lambda: datetime(2026, 4, 8, tzinfo=timezone.utc)),
        store=None,
        config=ScannerConfig(adsb_window_seconds=0.01, ais_window_seconds=0.01),
        sleep_fn=lambda _: None,
        now_fn=lambda: datetime(2026, 4, 8, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="Invalid frequency"):
        backend.retune(-1)

    status = scanner.status()
    assert status["supervisor"]["last_error"] is not None
    assert "Invalid frequency" in status["supervisor"]["last_error"]


def test_invalid_gain_is_rejected_in_backend_status() -> None:
    backend = MockBackend(fixture_path=FIXTURE_DIR / "nominal_ais.json", enable_timing_mode=False)
    backend.start()

    with pytest.raises(ValueError, match="Invalid gain"):
        backend.set_gain(200)

    assert backend.status().last_error is not None
    assert "Invalid gain" in backend.status().last_error


def test_mock_backend_supports_payload_ref_catalog() -> None:
    fixture = {
        "seed": 1,
        "sample_rate": 48000,
        "default_band": "ais",
        "payloads": {
            "ais_obs": {
                "observation": {
                    "target_id": "ais:265555555",
                    "source": "ais",
                    "kind": "vessel",
                    "observed_at": "2026-04-08T10:00:00+00:00",
                    "lat": 58.0,
                    "lon": 18.0,
                    "payload_json": {},
                }
            }
        },
        "timeline": [
            {
                "t_ms": 0,
                "band": "ais",
                "event_type": "observation",
                "payload_ref": "ais_obs",
            }
        ],
    }
    backend = MockBackend(fixture=fixture, enable_timing_mode=False)
    backend.start()
    events = backend.read(0.1, band=ScanBand.AIS)

    assert len(events) == 1
    assert isinstance(events[0], ObservationEvent)
    assert events[0].observation.target_id == "ais:265555555"


def test_mock_backend_rejects_unknown_payload_ref() -> None:
    fixture = {
        "seed": 1,
        "sample_rate": 48000,
        "default_band": "ais",
        "payloads": {},
        "timeline": [
            {
                "t_ms": 0,
                "band": "ais",
                "event_type": "observation",
                "payload_ref": "missing_ref",
            }
        ],
    }

    with pytest.raises(ValueError, match="payload_ref"):
        MockBackend(fixture=fixture, enable_timing_mode=False)


def test_mock_backend_rejects_payload_and_payload_ref_together() -> None:
    fixture = {
        "seed": 1,
        "sample_rate": 48000,
        "default_band": "ais",
        "payloads": {"a": {"x": 1}},
        "timeline": [
            {
                "t_ms": 0,
                "band": "ais",
                "event_type": "observation",
                "payload_ref": "a",
                "payload": {"observation": {}},
            }
        ],
    }

    with pytest.raises(ValueError, match="both payload and payload_ref"):
        MockBackend(fixture=fixture, enable_timing_mode=False)


def test_external_backend_worker_mode_reads_events_and_sends_commands(monkeypatch) -> None:
    obs = NormalizedObservation(
        target_id="ais:265123456",
        source=Source.AIS,
        kind=TargetKind.VESSEL,
        observed_at=datetime(2026, 4, 8, tzinfo=timezone.utc),
        lat=58.0,
        lon=18.0,
    )
    commands: list[dict] = []

    class _FakeSocket:
        def __init__(self, mode: str):
            self._mode = mode
            self._buffer = io.BytesIO()
            if mode == "data":
                payload = (
                    json.dumps(
                        {
                            "type": "observation",
                            "source_band": "ais",
                            "observation": obs.to_dict(),
                        }
                    )
                    + "\n"
                )
                self._stream = io.StringIO(payload)
            else:
                self._stream = io.StringIO("")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def sendall(self, data: bytes) -> None:
            self._buffer.write(data)
            text = data.decode("utf-8", errors="replace").strip()
            if text:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    commands.append(parsed)

        def recv(self, size: int) -> bytes:
            return b'{"ok": true}\n'

        def settimeout(self, value: float) -> None:
            return None

        def makefile(self, mode: str, encoding: str = "utf-8", errors: str = "replace"):
            return self._stream

    def _fake_create_connection(address, timeout=1.0):  # noqa: ANN001, ARG001
        host, port = address
        if port == 17601:
            return _FakeSocket("control")
        if port == 17602:
            return _FakeSocket("data")
        raise ConnectionRefusedError(f"unexpected address: {address}")

    monkeypatch.setattr("app.radio_v2.socket.create_connection", _fake_create_connection)

    backend = ExternalBackend(
        readers={},
        use_worker=True,
        control_host="127.0.0.1",
        control_port=17601,
        data_host="127.0.0.1",
        data_port=17602,
    )
    backend.start()
    backend.retune(162000000)
    backend.set_gain(20)

    events = backend.read(0.2, band=ScanBand.AIS)
    assert len(events) == 1
    assert isinstance(events[0], ObservationEvent)
    assert events[0].observation.target_id == "ais:265123456"
    assert any(command.get("cmd") == "retune" for command in commands)
    assert any(command.get("cmd") == "set_gain" for command in commands)
    assert backend.status().connected is True


def test_external_backend_worker_failure_falls_back_to_reader() -> None:
    observation = NormalizedObservation(
        target_id="adsb:abcdef",
        source=Source.ADSB,
        kind=TargetKind.AIRCRAFT,
        observed_at=datetime(2026, 4, 8, tzinfo=timezone.utc),
        lat=59.0,
        lon=18.0,
    )
    backend = ExternalBackend(
        readers={ScanBand.ADSB: _StaticReader([observation])},
        use_worker=True,
        control_host="127.0.0.1",
        control_port=19991,
        data_host="127.0.0.1",
        data_port=19992,
    )
    backend.start()
    events = backend.read(0.1, band=ScanBand.ADSB)

    assert len(events) == 1
    assert isinstance(events[0], ObservationEvent)
    assert events[0].observation.target_id == "adsb:abcdef"
    assert backend.status().last_error is not None
    assert "external worker read failed" in backend.status().last_error
