from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.models import ScanBand
from app.radio_v2 import ADSBPipeline, AISPipeline, DSCPipeline, MockBackend, OGNPipeline, ScannerOrchestratorV2
from app.scanner import ScannerConfig
from app.state import LiveState


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "mock_radio"


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
