from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.models import NormalizedObservation, ScanBand, Source, TargetKind
from app.scanner import HybridBandScanner, ScannerConfig
from app.state import LiveState


@dataclass
class FakeReader:
    observations: list[NormalizedObservation]
    should_raise: bool = False
    call_count: int = 0
    last_kwargs: dict | None = None

    def read_observations(self, **kwargs):
        self.call_count += 1
        self.last_kwargs = kwargs
        if self.should_raise:
            raise RuntimeError("reader failed")
        return list(self.observations)


@dataclass
class FakeStore:
    writes: list[tuple[NormalizedObservation, str]]
    should_raise: bool = False
    prune_cutoffs: list[datetime] | None = None

    def persist_observation_and_target(self, observation, target):
        if self.should_raise:
            raise RuntimeError("store write failed")
        self.writes.append((observation, target.target_id))

    def delete_latest_targets_older_than(self, cutoff):  # noqa: ANN001
        if self.prune_cutoffs is not None:
            self.prune_cutoffs.append(cutoff)
        return 0


@dataclass
class FakeSupervisor:
    switches: list[ScanBand]
    stop_calls: int = 0

    def switch_to(self, band: ScanBand) -> None:
        self.switches.append(band)

    def stop_active(self) -> None:
        self.stop_calls += 1

    def status(self):
        return {"switches": [band.value for band in self.switches], "stop_calls": self.stop_calls}


def _obs(target_id: str, source: Source) -> NormalizedObservation:
    return NormalizedObservation(
        target_id=target_id,
        source=source,
        kind=TargetKind.AIRCRAFT if source == Source.ADSB else TargetKind.VESSEL,
        observed_at=datetime(2026, 3, 31, 8, 0, tzinfo=timezone.utc),
        lat=59.0 if source == Source.ADSB else 58.0,
        lon=18.0 if source == Source.ADSB else 17.0,
        course=90.0,
        speed=100.0,
        altitude=1000.0 if source == Source.ADSB else None,
        last_scan_band=ScanBand.ADSB if source == Source.ADSB else ScanBand.AIS,
    )


def test_run_cycle_switches_bands_and_persists_observations() -> None:
    adsb_obs = _obs("adsb:abc123", Source.ADSB)
    ais_obs = _obs("ais:265123456", Source.AIS)

    adsb_reader = FakeReader([adsb_obs])
    ais_reader = FakeReader([ais_obs])
    state = LiveState(clock=lambda: datetime(2026, 3, 31, 8, 0, tzinfo=timezone.utc))
    store = FakeStore(writes=[])
    supervisor = FakeSupervisor(switches=[])
    sleep_calls: list[float] = []

    scanner = HybridBandScanner(
        adsb_reader=adsb_reader,
        ais_reader=ais_reader,
        state=state,
        store=store,
        supervisor=supervisor,  # type: ignore[arg-type]
        config=ScannerConfig(
            adsb_window_seconds=0.01,
            ais_window_seconds=0.01,
            inter_scan_pause_seconds=2.0,
        ),
        sleep_fn=lambda seconds: sleep_calls.append(seconds),
        now_fn=lambda: datetime(2026, 3, 31, 8, 0, tzinfo=timezone.utc),
    )

    scanner.run_cycle()

    assert supervisor.switches == [ScanBand.AIS, ScanBand.ADSB]
    assert supervisor.stop_calls == 2
    assert adsb_reader.call_count == 1
    assert ais_reader.call_count == 1
    assert adsb_reader.last_kwargs == {"timeout_seconds": 0.01}
    assert len(store.writes) == 2
    assert state.get_stats()["total_live_targets"] == 2
    assert sleep_calls == [0.01, 2.0, 0.01, 2.0]
    assert scanner.status()["active_scan_band"] is None


def test_run_cycle_recovers_after_first_band_failure() -> None:
    adsb_reader = FakeReader([_obs("adsb:777", Source.ADSB)])
    ais_reader = FakeReader([], should_raise=True)
    state = LiveState(clock=lambda: datetime(2026, 3, 31, 8, 0, tzinfo=timezone.utc))
    supervisor = FakeSupervisor(switches=[])

    scanner = HybridBandScanner(
        adsb_reader=adsb_reader,
        ais_reader=ais_reader,
        state=state,
        store=None,
        supervisor=supervisor,  # type: ignore[arg-type]
        config=ScannerConfig(
            adsb_window_seconds=0.01,
            ais_window_seconds=0.01,
            inter_scan_pause_seconds=0.0,
        ),
        sleep_fn=lambda seconds: None,
        now_fn=lambda: datetime(2026, 3, 31, 8, 0, tzinfo=timezone.utc),
    )

    scanner.run_cycle()
    assert scanner.last_error is not None
    assert "ais" in scanner.last_error
    assert state.get_stats()["live_aircraft_count"] == 1
    assert supervisor.switches == [ScanBand.AIS, ScanBand.ADSB]


def test_stop_requests_shutdown_and_stops_supervisor() -> None:
    scanner = HybridBandScanner(
        adsb_reader=FakeReader([]),
        ais_reader=FakeReader([]),
        state=LiveState(clock=lambda: datetime(2026, 3, 31, 8, 0, tzinfo=timezone.utc)),
        store=None,
        supervisor=FakeSupervisor(switches=[]),  # type: ignore[arg-type]
        config=ScannerConfig(
            adsb_window_seconds=0.01,
            ais_window_seconds=0.01,
            inter_scan_pause_seconds=0.0,
        ),
        sleep_fn=lambda seconds: None,
        now_fn=lambda: datetime(2026, 3, 31, 8, 0, tzinfo=timezone.utc),
    )

    scanner.stop()
    status = scanner.status()
    assert status["active_scan_band"] is None
    assert status["supervisor"]["stop_calls"] == 1


def test_run_forever_prunes_targets_latest_older_than_five_minutes() -> None:
    now = datetime(2026, 3, 31, 8, 0, tzinfo=timezone.utc)
    store = FakeStore(writes=[], prune_cutoffs=[])
    scanner = HybridBandScanner(
        adsb_reader=FakeReader([]),
        ais_reader=FakeReader([]),
        state=LiveState(clock=lambda: now),
        store=store,
        supervisor=FakeSupervisor(switches=[]),  # type: ignore[arg-type]
        config=ScannerConfig(
            adsb_window_seconds=0.01,
            ais_window_seconds=0.01,
            inter_scan_pause_seconds=0.0,
        ),
        sleep_fn=lambda seconds: None,
        now_fn=lambda: now,
    )

    scanner.run_forever(max_cycles=1)
    assert store.prune_cutoffs is not None
    assert len(store.prune_cutoffs) == 1
    assert store.prune_cutoffs[0] == now - timedelta(minutes=5)
