from __future__ import annotations

import logging
from pathlib import Path
from threading import Event
import time

from app.config import Config
from app.map_contours import (
    BackgroundHydroContourProvider,
    CachingMapContourProvider,
    DatabaseHydroContourProvider,
    HydroContourProvider,
    MapContourRequest,
    MapContourResult,
    MarkhojdDirectContourProvider,
    PersistentMapContourProvider,
    Sweref99TmProjection,
    _build_sampling_grid,
    _decode_height_grid,
    _generate_contour_features,
    _project_bbox,
    build_map_contour_service,
)
from app.store import SQLiteStore


class StaticProvider:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, request: MapContourRequest) -> MapContourResult:
        self.calls += 1
        return MapContourResult(
            source=request.source,
            features=(
                {
                    "type": "Feature",
                    "properties": {"calls": self.calls},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[18.0, 59.0], [18.1, 59.1]],
                    },
                },
            ),
        )


def test_caching_map_contour_provider_marks_cache_hits() -> None:
    clock_state = {"now": 100.0}
    provider = StaticProvider()
    cached = CachingMapContourProvider(
        provider,
        ttl_seconds=10,
        clock=lambda: clock_state["now"],
    )
    request = MapContourRequest(source="hydro", bbox=(18.0, 59.0, 18.2, 59.2))

    first = cached.fetch(request)
    second = cached.fetch(request)

    assert provider.calls == 1
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.features[0]["properties"]["calls"] == 1

    clock_state["now"] = 111.0
    third = cached.fetch(request)
    assert provider.calls == 2
    assert third.cache_hit is False


def test_persistent_map_contour_provider_reuses_local_file_cache(tmp_path) -> None:
    provider = StaticProvider()
    persistent = PersistentMapContourProvider(
        provider,
        cache_dir=tmp_path / "map-cache",
    )
    request = MapContourRequest(source="hydro", bbox=(18.0, 59.0, 18.2, 59.2), range_km=10.0)

    first = persistent.fetch(request)
    second = persistent.fetch(request)

    assert provider.calls == 1
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.features[0]["properties"]["calls"] == 1
    assert list((tmp_path / "map-cache" / "hydro").glob("*.json"))


def test_persistent_map_contour_provider_does_not_store_error_results(tmp_path) -> None:
    class ErrorProvider:
        def __init__(self) -> None:
            self.calls = 0

        def fetch(self, request: MapContourRequest) -> MapContourResult:
            self.calls += 1
            return MapContourResult(
                source=request.source,
                features=(),
                status="error",
                error="upstream failed",
            )

    provider = ErrorProvider()
    persistent = PersistentMapContourProvider(
        provider,
        cache_dir=tmp_path / "map-cache",
    )
    request = MapContourRequest(source="hydro", bbox=(18.0, 59.0, 18.2, 59.2), range_km=10.0)

    first = persistent.fetch(request)
    second = persistent.fetch(request)

    assert first.status == "error"
    assert second.status == "error"
    assert provider.calls == 2
    assert not list((tmp_path / "map-cache").rglob("*.json"))


def test_database_hydro_contour_provider_reuses_cached_bbox_and_supports_inspire_lookup(
    tmp_path,
) -> None:
    calls: list[str] = []

    def fake_fetch_json(url: str, headers: dict[str, str]) -> dict:
        calls.append(url)
        assert headers["Authorization"].startswith("Basic ")
        if "LandWaterBoundary" in url:
            return {
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"inspireId": "coast-1"},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[18.0, 59.0], [18.2, 59.1]],
                        },
                    }
                ]
            }
        if "StandingWater" in url:
            return {
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"inspireId": "lake-1"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[18.3, 59.2], [18.4, 59.2], [18.4, 59.3], [18.3, 59.2]]
                            ],
                        },
                    }
                ]
            }
        raise AssertionError(f"Unexpected url: {url}")

    store = SQLiteStore(tmp_path / "hydro.sqlite3")
    store.initialize()
    provider = DatabaseHydroContourProvider(
        HydroContourProvider(
            base_url="https://hydro.example.test/ogc",
            username="user",
            password="pass",
            fetch_json=fake_fetch_json,
        ),
        store=store,
    )
    request = MapContourRequest(source="hydro", bbox=(18.0, 59.0, 18.8, 59.8))

    first = provider.fetch(request)
    second = provider.fetch(request)

    assert len(calls) == 2
    assert first.status == "ok"
    assert second.status == "ok"
    assert len(second.features) == 2
    assert second.features[1]["geometry"]["type"] == "MultiLineString"
    stored = store.load_hydro_feature_by_inspire_id("lake-1")
    assert stored is not None
    assert stored["geometry"]["type"] == "MultiLineString"


def test_database_hydro_contour_provider_resumes_from_incomplete_pagination(tmp_path) -> None:
    calls: list[str] = []
    failed_once = {"value": False}

    def fake_fetch_json(url: str, headers: dict[str, str]) -> dict:
        calls.append(url)
        assert headers["Authorization"].startswith("Basic ")
        if "LandWaterBoundary" in url and "page=2" not in url:
            return {
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"inspireId": "coast-1"},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[18.0, 59.0], [18.2, 59.1]],
                        },
                    }
                ],
                "links": [{"rel": "next", "href": "?page=2"}],
            }
        if "LandWaterBoundary" in url and "page=2" in url:
            if not failed_once["value"]:
                failed_once["value"] = True
                raise RuntimeError("temporary upstream failure")
            return {
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"inspireId": "coast-2"},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[18.2, 59.1], [18.3, 59.2]],
                        },
                    }
                ]
            }
        if "StandingWater" in url:
            return {
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"inspireId": "lake-1"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[18.3, 59.2], [18.4, 59.2], [18.4, 59.3], [18.3, 59.2]]
                            ],
                        },
                    }
                ]
            }
        raise AssertionError(f"Unexpected url: {url}")

    store = SQLiteStore(tmp_path / "hydro-resume.sqlite3")
    store.initialize()
    provider = DatabaseHydroContourProvider(
        HydroContourProvider(
            base_url="https://hydro.example.test/ogc",
            username="user",
            password="pass",
            fetch_json=fake_fetch_json,
        ),
        store=store,
    )
    request = MapContourRequest(source="hydro", bbox=(18.0, 59.0, 18.8, 59.8))

    first = provider.fetch(request)
    state = store.load_hydro_bbox_download_state(bbox=request.bbox)
    assert first.status == "error"
    assert state is not None
    assert state.is_complete is False
    assert state.resume_collection == "LandWaterBoundary"
    assert state.resume_url == "https://hydro.example.test/ogc/collections/LandWaterBoundary/items?page=2"
    assert state.feature_count == 1
    assert store.load_hydro_contours_by_bbox(bbox=request.bbox) is None

    second = provider.fetch(request)
    state = store.load_hydro_bbox_download_state(bbox=request.bbox)
    assert second.status == "ok"
    assert len(second.features) == 3
    assert state is not None
    assert state.is_complete is True
    assert state.resume_collection is None
    assert state.resume_url is None
    assert sum("LandWaterBoundary" in url and "page=2" not in url for url in calls) == 1


def test_background_hydro_contour_provider_returns_pending_while_worker_runs(tmp_path) -> None:
    started = Event()
    release = Event()

    def fake_fetch_json(url: str, headers: dict[str, str]) -> dict:
        assert headers["Authorization"].startswith("Basic ")
        started.set()
        release.wait(timeout=1.0)
        if "LandWaterBoundary" in url:
            return {
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"inspireId": "coast-1"},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[18.0, 59.0], [18.2, 59.1]],
                        },
                    }
                ]
            }
        if "StandingWater" in url:
            return {"features": []}
        raise AssertionError(f"Unexpected url: {url}")

    store = SQLiteStore(tmp_path / "hydro-background.sqlite3")
    store.initialize()
    background = BackgroundHydroContourProvider(
        DatabaseHydroContourProvider(
            HydroContourProvider(
                base_url="https://hydro.example.test/ogc",
                username="user",
                password="pass",
                fetch_json=fake_fetch_json,
            ),
            store=store,
        ),
        store=store,
        poll_hint_seconds=0.01,
    )
    request = MapContourRequest(source="hydro", bbox=(18.0, 59.0, 18.8, 59.8))

    first = background.fetch(request)
    assert first.status == "pending"
    assert first.details["download_in_progress"] is True
    assert len(first.features) == 0
    assert started.wait(timeout=1.0) is True

    second = background.fetch(request)
    assert second.status == "pending"

    release.set()
    deadline = time.monotonic() + 1.0
    completed = None
    while time.monotonic() < deadline:
        completed = background.fetch(request)
        if completed.status == "ok":
            break
        time.sleep(0.01)

    assert completed is not None
    assert completed.status == "ok"
    assert len(completed.features) == 1


def test_background_hydro_contour_provider_returns_partial_features_while_pending(tmp_path) -> None:
    release = Event()

    def fake_fetch_json(url: str, headers: dict[str, str]) -> dict:
        assert headers["Authorization"].startswith("Basic ")
        if "LandWaterBoundary" in url and "page=2" not in url:
            return {
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"inspireId": "coast-1"},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[18.0, 59.0], [18.2, 59.1]],
                        },
                    }
                ],
                "links": [{"rel": "next", "href": "?page=2"}],
            }
        if "LandWaterBoundary" in url and "page=2" in url:
            release.wait(timeout=1.0)
            return {"features": []}
        if "StandingWater" in url:
            return {"features": []}
        raise AssertionError(f"Unexpected url: {url}")

    store = SQLiteStore(tmp_path / "hydro-background-partial.sqlite3")
    store.initialize()
    background = BackgroundHydroContourProvider(
        DatabaseHydroContourProvider(
            HydroContourProvider(
                base_url="https://hydro.example.test/ogc",
                username="user",
                password="pass",
                fetch_json=fake_fetch_json,
            ),
            store=store,
        ),
        store=store,
        poll_hint_seconds=0.01,
    )
    request = MapContourRequest(source="hydro", bbox=(18.0, 59.0, 18.8, 59.8))

    first = background.fetch(request)
    assert first.status == "pending"

    deadline = time.monotonic() + 1.0
    pending_with_partial = None
    while time.monotonic() < deadline:
        pending_with_partial = background.fetch(request)
        if pending_with_partial.status == "pending" and len(pending_with_partial.features) == 1:
            break
        time.sleep(0.01)

    assert pending_with_partial is not None
    assert pending_with_partial.status == "pending"
    assert len(pending_with_partial.features) == 1
    assert pending_with_partial.features[0]["properties"]["inspireId"] == "coast-1"

    release.set()


def test_hydro_provider_converts_polygons_and_follows_pagination() -> None:
    calls: list[str] = []

    def fake_fetch_json(url: str, headers: dict[str, str]) -> dict:
        calls.append(url)
        assert headers["Authorization"].startswith("Basic ")
        if "LandWaterBoundary" in url and "page=2" not in url:
            return {
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "coast-1"},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[18.0, 59.0], [18.2, 59.1]],
                        },
                    }
                ],
                "links": [{"rel": "next", "href": "?page=2"}],
            }
        if "LandWaterBoundary" in url and "page=2" in url:
            return {
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "coast-2"},
                        "geometry": {
                            "type": "MultiLineString",
                            "coordinates": [
                                [[18.3, 59.2], [18.4, 59.3]],
                                [[18.4, 59.3], [18.5, 59.4]],
                            ],
                        },
                    }
                ]
            }
        if "StandingWater" in url:
            return {
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "lake"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[18.6, 59.4], [18.7, 59.4], [18.7, 59.5], [18.6, 59.4]]
                            ],
                        },
                    }
                ]
            }
        raise AssertionError(f"Unexpected url: {url}")

    provider = HydroContourProvider(
        base_url="https://hydro.example.test/ogc",
        username="user",
        password="pass",
        fetch_json=fake_fetch_json,
    )

    result = provider.fetch(
        MapContourRequest(source="hydro", bbox=(18.0, 59.0, 18.8, 59.8), range_km=12.0)
    )

    assert result.status == "ok"
    assert len(result.features) == 3
    assert result.features[0]["geometry"]["type"] == "LineString"
    assert result.features[1]["geometry"]["type"] == "MultiLineString"
    assert result.features[2]["geometry"]["type"] == "MultiLineString"
    assert result.features[2]["properties"]["collection"] == "StandingWater"
    assert any("page=2" in url for url in calls)


def test_hydro_provider_logs_when_calling_lantmateriet(caplog) -> None:
    def fake_fetch_json(url: str, headers: dict[str, str]) -> dict:
        assert headers["Authorization"].startswith("Basic ")
        return {"features": []}

    provider = HydroContourProvider(
        base_url="https://hydro.example.test/ogc",
        username="user",
        password="pass",
        fetch_json=fake_fetch_json,
    )

    with caplog.at_level(logging.INFO, logger="app.map_contours"):
        result = provider.fetch(
            MapContourRequest(source="hydro", bbox=(18.0, 59.0, 18.8, 59.8), range_km=12.0)
        )

    assert result.status == "ok"
    messages = [record.getMessage() for record in caplog.records]
    assert any("Calling Lantmateriet hydro API" in message for message in messages)
    assert any("collection=LandWaterBoundary" in message for message in messages)
    assert any("collection=StandingWater" in message for message in messages)


def test_sweref99tm_projection_roundtrips_coordinates() -> None:
    projection = Sweref99TmProjection()

    projected = projection.to_grid(lat=59.3293, lon=18.0686)
    lat, lon = projection.to_geodetic(
        easting=projected.easting,
        northing=projected.northing,
    )

    assert abs(lat - 59.3293) < 0.00001
    assert abs(lon - 18.0686) < 0.00001


def test_sampling_grid_scales_step_to_respect_point_limit() -> None:
    grid = _build_sampling_grid(
        projected_bounds=(500000.0, 6500000.0, 520000.0, 6520000.0),
        min_step_m=25,
        max_points=1000,
    )

    assert len(grid.points) <= 1000
    assert grid.effective_step_m >= 25
    assert len(grid.eastings) >= 2
    assert len(grid.northings) >= 2


def test_markhojd_direct_provider_requires_credentials() -> None:
    provider = MarkhojdDirectContourProvider(
        base_url="https://api.lantmateriet.se/distribution/produkter/markhojd/v1",
        username="",
        password="",
        srid=3006,
        sample_step_m=25,
        contour_interval_m=10,
        max_points_per_request=1000,
    )

    result = provider.fetch(
        MapContourRequest(source="elevation", bbox=(18.0, 59.0, 18.2, 59.2), range_km=10.0)
    )

    assert result.status == "unavailable"
    assert result.error == "Markhojd Direkt credentials are not configured."


def test_markhojd_direct_provider_generates_contour_features_from_sampled_points() -> None:
    projection = Sweref99TmProjection()
    bbox = (18.05, 59.30, 18.07, 59.32)
    projected_bounds = _project_bbox(bbox, projection=projection)
    grid = _build_sampling_grid(
        projected_bounds=projected_bounds,
        min_step_m=500,
        max_points=1000,
    )

    def fake_post_json(url: str, headers: dict[str, str], body: dict) -> dict:
        assert url.endswith("/hojd")
        coords = body["coordinates"]
        response_coords = []
        for easting, northing in coords:
            z_value = round((easting - coords[0][0]) / 100.0 + (northing - coords[0][1]) / 100.0, 3)
            response_coords.append([easting, northing, z_value])
        return {
            "type": "Feature",
            "geometry": {
                "type": "MultiPoint",
                "coordinates": response_coords,
            },
            "properties": {"nodatavalue": -9999},
        }

    provider = MarkhojdDirectContourProvider(
        base_url="https://markhojd.example.test",
        username="user",
        password="pass",
        srid=3006,
        sample_step_m=500,
        contour_interval_m=5,
        max_points_per_request=1000,
        projection=projection,
        post_json=fake_post_json,
    )

    result = provider.fetch(MapContourRequest(source="elevation", bbox=bbox, range_km=5.0))

    assert result.status == "ok"
    assert result.details["service"] == "markhojd-direkt"
    assert result.details["sample_point_count"] == len(grid.points)
    assert result.details["segment_count"] > 0
    assert len(result.features) > 0
    assert result.features[0]["geometry"]["type"] == "LineString"
    assert "elevation_m" in result.features[0]["properties"]


def test_markhojd_direct_provider_logs_when_calling_lantmateriet(caplog) -> None:
    def fake_post_json(url: str, headers: dict[str, str], body: dict) -> dict:
        assert url.endswith("/hojd")
        assert headers["Authorization"].startswith("Basic ")
        response_coords = []
        for index, (easting, northing) in enumerate(body["coordinates"]):
            response_coords.append([easting, northing, 10.0 + float(index)])
        return {
            "type": "Feature",
            "geometry": {
                "type": "MultiPoint",
                "coordinates": response_coords,
            },
            "properties": {"nodatavalue": -9999},
        }

    provider = MarkhojdDirectContourProvider(
        base_url="https://markhojd.example.test",
        username="user",
        password="pass",
        srid=3006,
        sample_step_m=500,
        contour_interval_m=5,
        max_points_per_request=1000,
        post_json=fake_post_json,
    )

    with caplog.at_level(logging.INFO, logger="app.map_contours"):
        result = provider.fetch(
            MapContourRequest(
                source="elevation",
                bbox=(18.05, 59.30, 18.07, 59.32),
                range_km=5.0,
            )
        )

    assert result.status == "ok"
    messages = [record.getMessage() for record in caplog.records]
    assert any("Calling Lantmateriet Markhojd Direkt" in message for message in messages)
    assert any("sample_points=" in message for message in messages)


def test_decode_height_grid_respects_nodata_value() -> None:
    rows = _decode_height_grid(
        response_feature={
            "geometry": {
                "type": "MultiPoint",
                "coordinates": [
                    [500000.0, 6500000.0, 10.0],
                    [500100.0, 6500000.0, -9999],
                    [500000.0, 6500100.0, 12.0],
                    [500100.0, 6500100.0, 14.0],
                ],
            }
        },
        x_count=2,
        y_count=2,
        nodata_value=-9999,
    )

    assert rows == [[10.0, None], [12.0, 14.0]]


def test_generate_contour_features_emits_line_features() -> None:
    projection = Sweref99TmProjection()
    features = _generate_contour_features(
        eastings=(500000.0, 500100.0),
        northings=(6500000.0, 6500100.0),
        heights=[[0.0, 10.0], [10.0, 20.0]],
        contour_interval_m=5,
        projection=projection,
    )

    assert len(features) > 0
    assert features[0]["geometry"]["type"] == "LineString"
    assert features[0]["properties"]["source"] == "markhojd-direkt"


def test_build_map_contour_service_supports_both_sources() -> None:
    service = build_map_contour_service(Config())
    bbox = (18.0, 59.0, 18.2, 59.2)

    hydro = service.get_contours(bbox=bbox, source="hydro", range_km=10.0)
    elevation = service.get_contours(bbox=bbox, source="elevation", range_km=10.0)

    assert hydro.source == "hydro"
    assert hydro.status == "unavailable"
    assert elevation.source == "elevation"
    assert elevation.status == "unavailable"
