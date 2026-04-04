from __future__ import annotations

import sqlite3
from dataclasses import replace
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
    assert "target_names" in tables
    assert "map_hydro_features" in tables
    assert "map_hydro_bbox_cache" in tables
    assert "map_hydro_bbox_features" in tables


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


def test_fetch_history_honors_observed_interval(tmp_path) -> None:
    seen_at = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "history_cutoff.sqlite3")
    store.initialize()

    store.insert_observation(_observation("adsb:hist", seen_at, altitude=1000.0))
    store.insert_observation(
        _observation("adsb:hist", seen_at + timedelta(seconds=1), altitude=1100.0)
    )
    store.insert_observation(
        _observation("adsb:hist", seen_at + timedelta(seconds=2), altitude=1200.0)
    )

    history = store.fetch_history(
        "adsb:hist",
        limit=10,
        observed_after=seen_at + timedelta(seconds=1),
        observed_before=seen_at + timedelta(seconds=1),
    )

    assert len(history) == 1
    assert [item.altitude for item in history] == [1100.0]


def test_list_historical_targets_returns_counts_and_resolved_labels(tmp_path) -> None:
    seen_at = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "history_targets.sqlite3")
    store.initialize()

    store.insert_observation(_observation("adsb:hist-a", seen_at, altitude=1000.0))
    second = replace(
        _observation("adsb:hist-a", seen_at + timedelta(seconds=1), altitude=1100.0),
        speed=230.0,
    )
    store.insert_observation(second)
    store.insert_observation(_observation("adsb:hist-b", seen_at + timedelta(seconds=2), altitude=1200.0))
    store.upsert_latest_target(_target("adsb:hist-a", seen_at + timedelta(seconds=1), altitude=1100.0))

    summaries = store.list_historical_targets()

    assert [item.target_id for item in summaries] == ["adsb:hist-b", "adsb:hist-a"]
    assert summaries[0].position_count == 1
    assert summaries[1].position_count == 2
    assert summaries[1].label == "SAS123"
    assert summaries[1].max_observed_speed == 230.0


def test_list_historical_targets_honors_observed_interval(tmp_path) -> None:
    seen_at = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "history_targets_cutoff.sqlite3")
    store.initialize()

    store.insert_observation(_observation("adsb:hist-a", seen_at, altitude=1000.0))
    store.insert_observation(
        _observation("adsb:hist-a", seen_at + timedelta(seconds=1), altitude=1100.0)
    )
    store.insert_observation(
        _observation("adsb:hist-b", seen_at + timedelta(seconds=2), altitude=1200.0)
    )

    summaries = store.list_historical_targets(
        observed_after=seen_at + timedelta(seconds=1),
        observed_before=seen_at + timedelta(seconds=1),
    )

    assert [item.target_id for item in summaries] == ["adsb:hist-a"]
    assert summaries[0].position_count == 1


def test_list_historical_target_ids_in_view_returns_only_targets_inside_radar_circle(tmp_path) -> None:
    seen_at = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "history_in_view.sqlite3")
    store.initialize()

    store.insert_observation(
        NormalizedObservation(
            target_id="adsb:inside",
            source=Source.ADSB,
            kind=TargetKind.AIRCRAFT,
            observed_at=seen_at,
            lat=59.0000,
            lon=18.0000,
        )
    )
    store.insert_observation(
        NormalizedObservation(
            target_id="adsb:outside",
            source=Source.ADSB,
            kind=TargetKind.AIRCRAFT,
            observed_at=seen_at,
            lat=59.2000,
            lon=18.2000,
        )
    )

    target_ids = store.list_historical_target_ids_in_view(
        center_lat=59.0,
        center_lon=18.0,
        range_km=5.0,
    )

    assert target_ids == ["adsb:inside"]


def test_list_historical_target_ids_in_view_honors_observed_interval(tmp_path) -> None:
    seen_at = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "history_in_view_cutoff.sqlite3")
    store.initialize()

    store.insert_observation(
        NormalizedObservation(
            target_id="adsb:inside",
            source=Source.ADSB,
            kind=TargetKind.AIRCRAFT,
            observed_at=seen_at,
            lat=59.0000,
            lon=18.0000,
        )
    )
    store.insert_observation(
        NormalizedObservation(
            target_id="adsb:later",
            source=Source.ADSB,
            kind=TargetKind.AIRCRAFT,
            observed_at=seen_at + timedelta(hours=1),
            lat=59.0001,
            lon=18.0001,
        )
    )

    target_ids = store.list_historical_target_ids_in_view(
        center_lat=59.0,
        center_lon=18.0,
        range_km=5.0,
        observed_after=seen_at - timedelta(minutes=1),
        observed_before=seen_at + timedelta(minutes=30),
    )

    assert target_ids == ["adsb:inside"]
def test_delete_latest_targets_older_than_removes_only_stale_rows(tmp_path) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "prune.sqlite3")
    store.initialize()

    store.upsert_latest_target(_target("adsb:old", now - timedelta(minutes=11), altitude=1000.0))
    store.upsert_latest_target(_target("adsb:new", now - timedelta(minutes=5), altitude=2000.0))

    deleted = store.delete_latest_targets_older_than(now - timedelta(minutes=10))
    assert deleted == 1

    latest = store.load_latest_targets()
    assert [target.target_id for target in latest] == ["adsb:new"]


def test_load_latest_targets_uses_name_mapping_when_latest_row_has_no_callsign(tmp_path) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "names.sqlite3")
    store.initialize()

    first_obs = _observation("adsb:abc", now - timedelta(seconds=30), altitude=9000.0)
    first_target = _target("adsb:abc", now - timedelta(seconds=30), altitude=9000.0)
    store.persist_observation_and_target(first_obs, first_target)

    store.upsert_latest_target(
        Target(
            target_id="adsb:abc",
            source=Source.ADSB,
            kind=TargetKind.AIRCRAFT,
            label=None,
            lat=59.2,
            lon=18.3,
            course=100.0,
            speed=220.0,
            altitude=9100.0,
            first_seen=now - timedelta(minutes=1),
            last_seen=now,
            freshness=Freshness.FRESH,
            last_scan_band=ScanBand.ADSB,
            icao24="ABCDEF",
            callsign=None,
        )
    )

    latest = store.load_latest_targets()
    assert len(latest) == 1
    assert latest[0].target_id == "adsb:abc"
    assert latest[0].label == "SAS123"


def test_populate_target_names_from_observations_backfills_adsb_and_ais(tmp_path) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "backfill.sqlite3")
    store.initialize()

    store.insert_observation(
        NormalizedObservation(
            target_id="adsb:abc123",
            source=Source.ADSB,
            kind=TargetKind.AIRCRAFT,
            observed_at=now - timedelta(seconds=10),
            payload_json={"hex": "abc123", "flight": "SAS900 "},
        )
    )
    store.insert_observation(
        NormalizedObservation(
            target_id="ais:265123456",
            source=Source.AIS,
            kind=TargetKind.VESSEL,
            observed_at=now,
            payload_json={"decoded": {"mmsi": "265123456", "shipname": "VESSEL-X"}},
        )
    )

    result = store.populate_target_names_from_observations()
    assert result["observations_scanned"] == 2
    assert result["names_upserted"] == 2

    with sqlite3.connect(store.sqlite_path) as conn:
        rows = conn.execute("SELECT id, name FROM target_names ORDER BY id ASC").fetchall()
    assert rows == [("265123456", "VESSEL-X"), ("abc123", "SAS900")]


def test_save_hydro_contours_for_bbox_reuses_features_by_inspire_id(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "hydro.sqlite3")
    store.initialize()
    first_bbox = (18.0, 59.0, 18.2, 59.2)
    second_bbox = (18.1, 59.1, 18.3, 59.3)
    shared_feature = {
        "type": "Feature",
        "properties": {
            "collection": "LandWaterBoundary",
            "inspireId": "shared-coast",
        },
        "geometry": {
            "type": "LineString",
            "coordinates": [[18.0, 59.0], [18.2, 59.1]],
        },
    }

    store.save_hydro_contours_for_bbox(bbox=first_bbox, features=[shared_feature])
    store.save_hydro_contours_for_bbox(bbox=second_bbox, features=[shared_feature])

    first_cached = store.load_hydro_contours_by_bbox(bbox=first_bbox)
    second_cached = store.load_hydro_contours_by_bbox(bbox=second_bbox)
    assert first_cached is not None
    assert second_cached is not None
    assert first_cached[0]["properties"]["inspireId"] == "shared-coast"
    assert second_cached[0]["properties"]["inspireId"] == "shared-coast"

    stored_feature = store.load_hydro_feature_by_inspire_id("shared-coast")
    assert stored_feature is not None
    assert stored_feature["geometry"]["type"] == "LineString"

    with sqlite3.connect(store.sqlite_path) as conn:
        feature_count = conn.execute("SELECT COUNT(*) FROM map_hydro_features").fetchone()[0]
        bbox_count = conn.execute("SELECT COUNT(*) FROM map_hydro_bbox_cache").fetchone()[0]
        mapping_count = conn.execute("SELECT COUNT(*) FROM map_hydro_bbox_features").fetchone()[0]

    assert feature_count == 1
    assert bbox_count == 2
    assert mapping_count == 2


def test_hydro_bbox_download_state_tracks_incomplete_and_resume_pointer(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "hydro-state.sqlite3")
    store.initialize()
    bbox = (18.0, 59.0, 18.2, 59.2)

    store.begin_hydro_bbox_download(
        bbox=bbox,
        resume_collection="LandWaterBoundary",
        resume_url="https://hydro.example.test/page-1",
        reset=True,
    )
    state = store.load_hydro_bbox_download_state(bbox=bbox)
    assert state is not None
    assert state.is_complete is False
    assert state.resume_collection == "LandWaterBoundary"
    assert state.resume_url == "https://hydro.example.test/page-1"
    assert state.feature_count == 0
    assert store.load_hydro_contours_by_bbox(bbox=bbox) is None

    store.append_hydro_contour_page(
        bbox=bbox,
        features=[
            {
                "type": "Feature",
                "properties": {
                    "collection": "LandWaterBoundary",
                    "inspireId": "coast-1",
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[18.0, 59.0], [18.1, 59.1]],
                },
            }
        ],
        next_collection="StandingWater",
        next_url="https://hydro.example.test/page-2",
        is_complete=False,
    )
    state = store.load_hydro_bbox_download_state(bbox=bbox)
    assert state is not None
    assert state.is_complete is False
    assert state.resume_collection == "StandingWater"
    assert state.resume_url == "https://hydro.example.test/page-2"
    assert state.feature_count == 1

    store.append_hydro_contour_page(
        bbox=bbox,
        features=[],
        next_collection=None,
        next_url=None,
        is_complete=True,
    )
    state = store.load_hydro_bbox_download_state(bbox=bbox)
    assert state is not None
    assert state.is_complete is True
    assert state.resume_collection is None
    assert state.resume_url is None
    assert state.feature_count == 1
    cached = store.load_hydro_contours_by_bbox(bbox=bbox)
    assert cached is not None
    assert cached[0]["properties"]["inspireId"] == "coast-1"


def test_load_hydro_partial_contours_by_bbox_returns_features_for_incomplete_bbox(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "hydro-partial.sqlite3")
    store.initialize()
    bbox = (18.0, 59.0, 18.2, 59.2)

    store.begin_hydro_bbox_download(
        bbox=bbox,
        resume_collection="StandingWater",
        resume_url="https://hydro.example.test/page-2",
        reset=True,
    )
    store.append_hydro_contour_page(
        bbox=bbox,
        features=[
            {
                "type": "Feature",
                "properties": {
                    "collection": "LandWaterBoundary",
                    "inspireId": "coast-1",
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[18.0, 59.0], [18.1, 59.1]],
                },
            }
        ],
        next_collection="StandingWater",
        next_url="https://hydro.example.test/page-2",
        is_complete=False,
    )

    assert store.load_hydro_contours_by_bbox(bbox=bbox) is None
    partial = store.load_hydro_partial_contours_by_bbox(bbox=bbox)
    assert partial is not None
    assert partial[0]["properties"]["inspireId"] == "coast-1"


def test_load_hydro_contours_by_bbox_returns_empty_tuple_for_cached_empty_bbox(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "hydro-empty.sqlite3")
    store.initialize()

    store.save_hydro_contours_for_bbox(
        bbox=(18.0, 59.0, 18.2, 59.2),
        features=[],
    )

    cached = store.load_hydro_contours_by_bbox(bbox=(18.0, 59.0, 18.2, 59.2))
    assert cached == ()
