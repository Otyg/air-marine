from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from app.models import Freshness, NormalizedObservation, ScanBand, Source, Target, TargetKind
from app.store import SQLiteStore


def _observation(target_id: str, seen_at: datetime, altitude: float | None) -> NormalizedObservation:
    return NormalizedObservation(
        target_id=target_id,
        source=Source.ADSB,
        kind=TargetKind.AIRCRAFT,
        observed_at=seen_at,
        lat=59.1,
        lon=18.2,
        course=88.0,
        speed=210.0,
        altitude=altitude,
        payload_json={"seen_at": seen_at.isoformat()},
        icao24="abcdef",
    )


def _target(target_id: str, seen_at: datetime, altitude: float | None) -> Target:
    return Target(
        target_id=target_id,
        source=Source.ADSB,
        kind=TargetKind.AIRCRAFT,
        label="SAS123",
        lat=59.1,
        lon=18.2,
        course=88.0,
        speed=210.0,
        altitude=altitude,
        first_seen=seen_at - timedelta(minutes=2),
        last_seen=seen_at,
        freshness=Freshness.FRESH,
        last_scan_band=ScanBand.ADSB,
        icao24="abcdef",
        callsign="SAS123",
    )


def test_initialize_creates_schema(tmp_path) -> None:
    db_path = tmp_path / "state.sqlite3"
    store = SQLiteStore(db_path)
    store.initialize()

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "observations" in tables
    assert "targets_latest" in tables


def test_persist_observation_and_target_and_count(tmp_path) -> None:
    seen_at = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "persist.sqlite3")
    store.initialize()

    store.persist_observation_and_target(
        _observation("adsb:one", seen_at, altitude=10000.0),
        _target("adsb:one", seen_at, altitude=10000.0),
    )

    assert store.count_observations() == 1
    latest = store.load_latest_targets()
    assert len(latest) == 1
    assert latest[0].target_id == "adsb:one"
    assert latest[0].altitude == 10000.0


def test_upsert_latest_target_updates_existing_row(tmp_path) -> None:
    seen_at = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "latest.sqlite3")
    store.initialize()

    store.upsert_latest_target(_target("adsb:two", seen_at, altitude=11000.0))
    store.upsert_latest_target(_target("adsb:two", seen_at + timedelta(minutes=1), altitude=12000.0))

    latest = store.load_latest_targets()
    assert len(latest) == 1
    assert latest[0].target_id == "adsb:two"
    assert latest[0].altitude == 12000.0


def test_fetch_history_returns_descending_with_limit(tmp_path) -> None:
    seen_at = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "history.sqlite3")
    store.initialize()

    store.insert_observation(_observation("adsb:hist", seen_at, altitude=1000.0))
    store.insert_observation(
        _observation("adsb:hist", seen_at + timedelta(seconds=1), altitude=1100.0)
    )
    store.insert_observation(
        _observation("adsb:hist", seen_at + timedelta(seconds=2), altitude=1200.0)
    )

    history = store.fetch_history("adsb:hist", limit=2)
    assert len(history) == 2
    assert history[0].altitude == 1200.0
    assert history[1].altitude == 1100.0
