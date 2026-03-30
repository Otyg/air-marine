from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Config
from app.main import create_service_components, recover_state_from_latest_targets
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
