from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.models import ScanBand
from app.radio_v2 import ADSBPipeline, AISPipeline, DSCPipeline, MockBackend, OGNPipeline, ScannerOrchestratorV2
from app.scanner import ScannerConfig
from app.state import LiveState


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "mock_radio"


def _build_scanner(backend: MockBackend, *, mode: str) -> ScannerOrchestratorV2:
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
    scanner.set_scan_mode(mode)
    return scanner


def test_failure_matrix_malformed_frame_then_recovery() -> None:
    backend = MockBackend(fixture_path=FIXTURE_DIR / "malformed_recovery.json", enable_timing_mode=False)
    scanner = _build_scanner(backend, mode="continuous_ais")

    scanner.run_cycle()  # malformed frame can be followed by observation in same cycle
    assert scanner.status()["last_error"] is None
    first_cycle_targets = scanner._state.get_stats()["total_live_targets"]  # noqa: SLF001
    assert first_cycle_targets in {0, 1}

    scanner.run_cycle()  # observation after malformed recovery (if not already ingested)
    assert scanner.status()["last_error"] is None
    assert scanner._state.get_stats()["total_live_targets"] == 1  # noqa: SLF001



def test_failure_matrix_disconnect_reconnect_then_recovery() -> None:
    backend = MockBackend(fixture_path=FIXTURE_DIR / "disconnect_reconnect.json", enable_timing_mode=False)
    scanner = _build_scanner(backend, mode="continuous_adsb")

    scanner.run_cycle()  # disconnect
    assert scanner.status()["last_error"] is None
    assert scanner._state.get_stats()["total_live_targets"] == 0  # noqa: SLF001

    scanner.run_cycle()  # reconnect marker
    assert scanner.status()["last_error"] is None
    assert scanner._state.get_stats()["total_live_targets"] == 0  # noqa: SLF001

    scanner.run_cycle()  # recovered observation
    assert scanner.status()["last_error"] is None
    assert scanner._state.get_stats()["total_live_targets"] == 1  # noqa: SLF001



def test_failure_matrix_timeout_then_recovery() -> None:
    fixture = {
        "seed": 1,
        "sample_rate": 48000,
        "default_band": "ais",
        "controls": {"drop_rate": 0.0, "retune_map": {"162000000": "ais"}},
        "timeline": [
            {"t_ms": 0, "band": "ais", "event_type": "observation", "payload": {}, "fault": "timeout"},
            {
                "t_ms": 10,
                "band": "ais",
                "event_type": "observation",
                "payload": {
                    "observation": {
                        "target_id": "ais:265333333",
                        "source": "ais",
                        "kind": "vessel",
                        "observed_at": "2026-04-08T10:00:00+00:00",
                        "lat": 58.0,
                        "lon": 18.0,
                        "payload_json": {"mock": True},
                    }
                },
            },
        ],
    }
    backend = MockBackend(fixture=fixture, enable_timing_mode=False)
    scanner = _build_scanner(backend, mode="continuous_ais")

    scanner.run_cycle()  # timeout event
    assert scanner.status()["last_error"] is None
    assert scanner._state.get_stats()["total_live_targets"] == 0  # noqa: SLF001

    scanner.run_cycle()  # next observation
    assert scanner.status()["last_error"] is None
    assert scanner._state.get_stats()["total_live_targets"] == 1  # noqa: SLF001
