from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from app.api import APIRuntime, create_api_app
from app.fixed_objects import FixedRadarObject
from app.map_contours import MapContourResult
from app.models import Freshness, NormalizedObservation, ScanBand, Source, Target, TargetKind
from app.state import LiveState
from app.store import SQLiteStore


@dataclass
class FakeScanner:
    payload: dict

    def status(self) -> dict:
        return dict(self.payload)

    def set_scan_mode(self, mode: str) -> None:
        allowed = {"hybrid", "continuous_ais", "continuous_adsb", "continuous_ogn"}
        if mode not in allowed:
            raise ValueError("unsupported scan mode")
        self.payload["scan_mode"] = mode


@dataclass
class FakeMapContourService:
    payload: MapContourResult
    calls: list[dict]

    def get_contours(self, *, bbox, source=None, range_km=None) -> MapContourResult:  # noqa: ANN001
        self.calls.append(
            {
                "bbox": bbox,
                "source": source,
                "range_km": range_km,
            }
        )
        return self.payload


def _request(app, method: str, path: str, **kwargs) -> httpx.Response:
    async def _run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(_run())


def _obs(
    *,
    target_id: str,
    source: Source,
    observed_at: datetime,
    lat: float | None,
    lon: float | None,
) -> NormalizedObservation:
    is_aircraft = source in {Source.ADSB, Source.OGN}
    return NormalizedObservation(
        target_id=target_id,
        source=source,
        kind=TargetKind.AIRCRAFT if is_aircraft else TargetKind.VESSEL,
        observed_at=observed_at,
        lat=lat,
        lon=lon,
        course=90.0,
        speed=120.0,
        altitude=1000.0 if is_aircraft else None,
        last_scan_band=(
            ScanBand.ADSB
            if source == Source.ADSB
            else ScanBand.OGN if source == Source.OGN else ScanBand.AIS
        ),
        icao24="abcdef" if source == Source.ADSB else None,
        mmsi="265123456" if source == Source.AIS else None,
        callsign="FLRABC12" if source == Source.OGN else None,
        label="FLT1" if source == Source.ADSB else "GLIDER1" if source == Source.OGN else "VESSEL1",
    )


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
        first_seen=last_seen - timedelta(minutes=2),
        last_seen=last_seen,
        freshness=Freshness.FRESH,
        last_scan_band=ScanBand.ADSB,
        icao24="abcdef",
    )


def test_health_and_stats_endpoints(tmp_path) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    state = LiveState(clock=lambda: now)
    store = SQLiteStore(tmp_path / "api.sqlite3")
    store.initialize()

    obs = _obs(
        target_id="adsb:abcdef",
        source=Source.ADSB,
        observed_at=now,
        lat=59.0,
        lon=18.0,
    )
    snapshot = state.upsert_observation(obs)
    store.persist_observation_and_target(obs, snapshot.target)

    scanner = FakeScanner(
        payload={
            "active_scan_band": "adsb",
            "last_cycle_start": now,
            "last_scan_switch": now,
            "last_error": None,
            "supervisor": {"last_error": None},
        }
    )
    app = create_api_app(APIRuntime(state=state, store=store, scanner=scanner))

    health = _request(app, "GET", "/health")
    assert health.status_code == 200
    assert health.json()["overall_status"] == "ok"
    assert health.json()["database_available"] is True
    assert health.json()["active_scan_band"] == "adsb"

    stats = _request(app, "GET", "/stats")
    assert stats.status_code == 200
    assert stats.json()["live_aircraft_count"] == 1
    assert stats.json()["total_observations_stored"] == 1


def test_targets_list_filters_and_detail() -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    state = LiveState(clock=lambda: now)
    app = create_api_app(APIRuntime(state=state, store=None, scanner=None))

    stale_aircraft = _obs(
        target_id="adsb:stale1",
        source=Source.ADSB,
        observed_at=now - timedelta(seconds=400),
        lat=59.0,
        lon=18.0,
    )
    fresh_vessel = _obs(
        target_id="ais:265123456",
        source=Source.AIS,
        observed_at=now,
        lat=58.0,
        lon=17.0,
    )
    state.upsert_observation(stale_aircraft)
    state.upsert_observation(fresh_vessel)

    all_targets = _request(app, "GET", "/targets")
    assert all_targets.status_code == 200
    assert all_targets.json()["count"] == 2

    vessels = _request(app, "GET", "/targets", params={"kind": "vessel"})
    assert vessels.status_code == 200
    assert vessels.json()["count"] == 1
    assert vessels.json()["targets"][0]["target_id"] == "ais:265123456"

    fresh_only = _request(app, "GET", "/targets", params={"fresh_only": True})
    assert fresh_only.status_code == 200
    assert fresh_only.json()["count"] == 1
    assert fresh_only.json()["targets"][0]["target_id"] == "ais:265123456"

    detail = _request(app, "GET", "/targets/ais:265123456")
    assert detail.status_code == 200
    assert detail.json()["target"]["target_id"] == "ais:265123456"
    assert len(detail.json()["positions"]) == 1

    not_found = _request(app, "GET", "/targets/does-not-exist")
    assert not_found.status_code == 404


def test_history_endpoint_with_store_and_without_store(tmp_path) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    state = LiveState(clock=lambda: now)
    store = SQLiteStore(tmp_path / "history.sqlite3")
    store.initialize()
    obs = _obs(
        target_id="adsb:abcdef",
        source=Source.ADSB,
        observed_at=now,
        lat=59.0,
        lon=18.0,
    )
    store.persist_observation_and_target(obs, _target("adsb:abcdef", now))
    app = create_api_app(APIRuntime(state=state, store=store))

    history = _request(app, "GET", "/history/adsb:abcdef", params={"limit": 10})
    assert history.status_code == 200
    assert history.json()["count"] == 1
    assert history.json()["observations"][0]["target_id"] == "adsb:abcdef"

    app_without_store = create_api_app(APIRuntime(state=state, store=None))
    unavailable = _request(app_without_store, "GET", "/history/adsb:abcdef")
    assert unavailable.status_code == 503


def test_radar_ui_root_renders_html_with_center_coordinates() -> None:
    state = LiveState(clock=lambda: datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc))
    app = create_api_app(
        APIRuntime(
            state=state,
            store=None,
            radar_center_lat=59.3293,
            radar_center_lon=18.0686,
            fixed_objects=[
                FixedRadarObject(
                    name="Lighthouse",
                    lat=59.3201,
                    lon=18.0711,
                    symbol="*",
                    max_visible_range_km=10.0,
                )
            ],
        )
    )

    response = _request(app, "GET", "/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "id=\"radar\"" in response.text
    assert "id=\"zoomIn\"" in response.text
    assert "id=\"zoomOut\"" in response.text
    assert "id=\"rangeInput\"" in response.text
    assert "id=\"scanModeSelect\"" in response.text
    assert "id=\"showFixedNames\"" in response.text
    assert "id=\"showTargetLabels\"" in response.text
    assert "id=\"showMapContours\"" in response.text
    assert "id=\"objectsList\"" in response.text
    assert "id=\"outsideObjectsList\"" in response.text
    assert "id=\"targetTypeFilter\"" in response.text
    assert "href=\"/history-radar\"" in response.text
    assert "id=\"showLowSpeed\"" in response.text
    assert "drawCourseVector" in response.text
    assert "const defaultPollMs = 2000;" in response.text
    assert "const minPollMs = 700;" in response.text
    assert "function computeAdaptivePollMs()" in response.text
    assert "function computeBandAwarePollMs(scannerState)" in response.text
    assert "function setScanMode(nextMode)" in response.text
    assert "scheduleNextLoad(nextPollMs);" in response.text
    assert "void loadTargets();" in response.text
    assert "renderObjectsPanel" in response.text
    assert "Objekt utanför aktivt område" in response.text
    assert "const fixedObjects =" in response.text
    assert "Lighthouse" in response.text
    assert "\"max_visible_range_km\": 10.0" in response.text
    assert "drawFixedObjects" in response.text
    assert "drawMapContours" in response.text
    assert "async function loadMapContoursForView(rangeKm)" in response.text
    assert "const defaultMapSource = \"hydro\";" in response.text
    assert "drawRecentPositions(trackedTarget, cx, cy, pxPerKm, radius);" in response.text
    assert "const trailPointWindowSeconds = 120;" in response.text
    assert "mergeTrailPoints" in response.text
    assert "getTrailFadeProgress" in response.text
    assert "trailOpacityForAgeRank" in response.text
    assert "updateTrailCacheFromTargets" in response.text
    assert "retainedTrailTargets" in response.text
    assert "let selectedTargetId = null;" in response.text
    assert "const selectedHistoryByTargetId = new Map();" in response.text
    assert "let pendingFitTargetId = null;" in response.text
    assert "const selectedHistoryPositionCount = 15;" in response.text
    assert "const selectedHistoryRequestLimit = selectedHistoryPositionCount + 1;" in response.text
    assert "function fitSelectionToView(targetId, options = {})" in response.text
    assert "function limitSelectedHistoryPoints(targetId, historyPoints)" in response.text
    assert "function drawSelectedHistoryPath(targetId, cx, cy, pxPerKm, radius)" in response.text
    assert "const currentTarget = findTargetById(targetId);" in response.text
    assert "function trailColorForAge(ageRank, targetColor)" in response.text
    assert "lastHistoryPoint," in response.text
    assert "currentCanvasPoint," in response.text
    assert "function clipSegmentToCircle(start, end, cx, cy, radius)" in response.text
    assert "const clippedSegment = clipSegmentToCircle(" in response.text
    assert "limit=${selectedHistoryRequestLimit}" in response.text
    assert "data-target-id" in response.text
    assert "objectsList.addEventListener(\"click\"" in response.text
    assert "outsideObjectsList.addEventListener(\"click\"" in response.text
    assert ".object-item.selected" in response.text
    assert "object-type-icon" in response.text
    assert "function targetTypeIcon(kind)" in response.text
    assert "function targetDisplayLabel(target)" in response.text
    assert "function drawMapTargetLabel(label, x, y, color)" in response.text
    assert "function matchesTargetTypeFilter(target, filterValue)" in response.text
    assert "#ff4d4d" in response.text
    assert "#39FF14" in response.text
    assert "last_seen:" in response.text
    assert "59.32930000" in response.text
    assert "18.06860000" in response.text


def test_history_radar_ui_renders_html_with_history_panel() -> None:
    state = LiveState(clock=lambda: datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc))
    app = create_api_app(
        APIRuntime(
            state=state,
            store=None,
            radar_center_lat=59.3293,
            radar_center_lon=18.0686,
            fixed_objects=[],
        )
    )

    response = _request(app, "GET", "/history-radar")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "HISTORY VIEW" in response.text
    assert "id=\"historyObjectsList\"" in response.text
    assert "id=\"historyTargetTypeFilter\"" in response.text
    assert "id=\"historyMinSpeedFilter\"" in response.text
    assert "id=\"historyTimeFilterButton\"" in response.text
    assert "id=\"historyTimeFilterModal\"" in response.text
    assert "id=\"historyObservedAfterInput\"" in response.text
    assert "id=\"historyObservedBeforeInput\"" in response.text
    assert "id=\"showTargetLabels\"" in response.text
    assert "Historiska objekt" in response.text
    assert "Alla spår" in response.text
    assert 'const requestUrl = params.size > 0' in response.text
    assert '?${params.toString()}' in response.text
    assert "fetch(`/ui/history-targets-in-view?${params.toString()}`" in response.text
    assert "object-type-icon" in response.text
    assert "object-view-badge" in response.text
    assert "function targetTypeIcon(kind)" in response.text
    assert "function setHistoryTimeFilterModalOpen(isOpen)" in response.text
    assert "function applyHistoryTimeFilter(nextObservedAfterIso, nextObservedBeforeIso)" in response.text
    assert "observed_after" in response.text
    assert "function drawMapTargetLabel(label, x, y, color)" in response.text
    assert "function matchesTargetTypeFilter(target, filterValue)" in response.text
    assert "function matchesHistorySpeedFilter(target, minSpeed)" in response.text
    assert "function ensureHistoryTargetsInView(rangeKm)" in response.text
    assert "if (leftInView !== rightInView)" in response.text
    assert "function drawSelectedHistoryPath(cx, cy, pxPerKm, radius)" in response.text
    assert "function fitHistoryToView(points)" in response.text
    assert "fitHistoryToView(historyPoints);" not in response.text
    assert "href=\"/\"" in response.text


def test_history_targets_ui_endpoint_returns_history_summaries(tmp_path) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    state = LiveState(clock=lambda: now)
    store = SQLiteStore(tmp_path / "history_targets.sqlite3")
    store.initialize()
    obs = _obs(
        target_id="adsb:abcdef",
        source=Source.ADSB,
        observed_at=now,
        lat=59.0,
        lon=18.0,
    )
    store.persist_observation_and_target(obs, _target("adsb:abcdef", now))

    app = create_api_app(APIRuntime(state=state, store=store))
    response = _request(app, "GET", "/ui/history-targets")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["targets"][0]["target_id"] == "adsb:abcdef"
    assert payload["targets"][0]["label"] == "FLT1"
    assert payload["targets"][0]["position_count"] == 1
    assert payload["targets"][0]["max_observed_speed"] == 120.0
    assert payload["targets"][0]["last_seen"] == now.isoformat()


def test_history_targets_ui_endpoint_honors_observed_interval(tmp_path) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    state = LiveState(clock=lambda: now)
    store = SQLiteStore(tmp_path / "history_targets_cutoff.sqlite3")
    store.initialize()
    early = _obs(
        target_id="adsb:early",
        source=Source.ADSB,
        observed_at=now - timedelta(hours=2),
        lat=59.0,
        lon=18.0,
    )
    late = _obs(
        target_id="adsb:late",
        source=Source.ADSB,
        observed_at=now,
        lat=59.1,
        lon=18.1,
    )
    store.persist_observation_and_target(early, _target("adsb:early", now - timedelta(hours=2)))
    store.persist_observation_and_target(late, _target("adsb:late", now))

    app = create_api_app(APIRuntime(state=state, store=store))
    response = _request(
        app,
        "GET",
        "/ui/history-targets",
        params={
            "observed_after": (now - timedelta(hours=3)).isoformat(),
            "observed_before": (now - timedelta(hours=1)).isoformat(),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["targets"][0]["target_id"] == "adsb:early"


def test_history_targets_in_view_endpoint_returns_matching_target_ids(tmp_path) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    state = LiveState(clock=lambda: now)
    store = SQLiteStore(tmp_path / "history_targets_in_view.sqlite3")
    store.initialize()
    store.insert_observation(
        NormalizedObservation(
            target_id="adsb:inside",
            source=Source.ADSB,
            kind=TargetKind.AIRCRAFT,
            observed_at=now,
            lat=59.0,
            lon=18.0,
        )
    )
    store.insert_observation(
        NormalizedObservation(
            target_id="adsb:outside",
            source=Source.ADSB,
            kind=TargetKind.AIRCRAFT,
            observed_at=now,
            lat=59.3,
            lon=18.3,
        )
    )

    app = create_api_app(APIRuntime(state=state, store=store))
    response = _request(
        app,
        "GET",
        "/ui/history-targets-in-view",
        params={"center_lat": 59.0, "center_lon": 18.0, "range_km": 5.0},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["target_ids"] == ["adsb:inside"]


def test_history_targets_in_view_endpoint_honors_observed_interval(tmp_path) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    state = LiveState(clock=lambda: now)
    store = SQLiteStore(tmp_path / "history_targets_in_view_cutoff.sqlite3")
    store.initialize()
    store.insert_observation(
        NormalizedObservation(
            target_id="adsb:inside-now",
            source=Source.ADSB,
            kind=TargetKind.AIRCRAFT,
            observed_at=now,
            lat=59.0,
            lon=18.0,
        )
    )
    store.insert_observation(
        NormalizedObservation(
            target_id="adsb:inside-earlier",
            source=Source.ADSB,
            kind=TargetKind.AIRCRAFT,
            observed_at=now - timedelta(hours=2),
            lat=59.0002,
            lon=18.0002,
        )
    )

    app = create_api_app(APIRuntime(state=state, store=store))
    response = _request(
        app,
        "GET",
        "/ui/history-targets-in-view",
        params={
            "center_lat": 59.0,
            "center_lon": 18.0,
            "range_km": 5.0,
            "observed_after": (now - timedelta(hours=3)).isoformat(),
            "observed_before": (now - timedelta(hours=1)).isoformat(),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["target_ids"] == ["adsb:inside-earlier"]


def test_history_endpoint_honors_observed_interval(tmp_path) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    state = LiveState(clock=lambda: now)
    store = SQLiteStore(tmp_path / "history_interval.sqlite3")
    store.initialize()
    store.insert_observation(_obs(
        target_id="adsb:abcdef",
        source=Source.ADSB,
        observed_at=now - timedelta(hours=2),
        lat=59.0,
        lon=18.0,
    ))
    store.insert_observation(_obs(
        target_id="adsb:abcdef",
        source=Source.ADSB,
        observed_at=now - timedelta(hours=1),
        lat=59.1,
        lon=18.1,
    ))
    store.insert_observation(_obs(
        target_id="adsb:abcdef",
        source=Source.ADSB,
        observed_at=now,
        lat=59.2,
        lon=18.2,
    ))

    app = create_api_app(APIRuntime(state=state, store=store))
    history = _request(
        app,
        "GET",
        "/history/adsb:abcdef",
        params={
            "limit": 10,
            "observed_after": (now - timedelta(hours=1, minutes=30)).isoformat(),
            "observed_before": (now - timedelta(minutes=30)).isoformat(),
        },
    )

    assert history.status_code == 200
    payload = history.json()
    assert payload["count"] == 1
    assert payload["observations"][0]["lat"] == 59.1


def test_targets_latest_ui_endpoint_returns_store_rows(tmp_path) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    state = LiveState(clock=lambda: now)
    store = SQLiteStore(tmp_path / "radar.sqlite3")
    store.initialize()
    store.upsert_latest_target(_target("adsb:abcdef", now))

    app = create_api_app(APIRuntime(state=state, store=store))
    response = _request(app, "GET", "/ui/targets-latest")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["radio_connected"] is False
    assert payload["targets"][0]["target_id"] == "adsb:abcdef"
    assert payload["targets"][0]["recent_positions"] == []
    assert payload["scanner"]["active_scan_band"] is None
    assert payload["scanner"]["last_scan_switch"] is None
    assert payload["scanner"]["scan_mode"] is None


def test_map_contours_endpoint_uses_runtime_service() -> None:
    state = LiveState(clock=lambda: datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc))
    contour_service = FakeMapContourService(
        payload=MapContourResult(
            source="hydro",
            status="ok",
            features=(
                {
                    "type": "Feature",
                    "properties": {"collection": "LandWaterBoundary"},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[18.0, 59.0], [18.1, 59.1]],
                    },
                },
            ),
        ),
        calls=[],
    )
    app = create_api_app(
        APIRuntime(
            state=state,
            store=None,
            map_contour_service=contour_service,
            default_map_source="hydro",
        )
    )

    response = _request(
        app,
        "GET",
        "/ui/map-contours",
        params={
            "bbox": "17.9,58.9,18.2,59.2",
            "range_km": "10",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "FeatureCollection"
    assert payload["source"] == "hydro"
    assert payload["status"] == "ok"
    assert len(payload["features"]) == 1
    assert payload["bbox"] == [17.9, 58.9, 18.2, 59.2]
    assert contour_service.calls == [
        {
            "bbox": (17.9, 58.9, 18.2, 59.2),
            "source": None,
            "range_km": 10.0,
        }
    ]


def test_map_contours_endpoint_rejects_invalid_bbox() -> None:
    state = LiveState(clock=lambda: datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc))
    app = create_api_app(APIRuntime(state=state, store=None))

    response = _request(
        app,
        "GET",
        "/ui/map-contours",
        params={"bbox": "18.2,59.2,17.9,58.9"},
    )

    assert response.status_code == 422
    assert "min_lon" in response.json()["detail"]


def test_targets_latest_ui_endpoint_includes_scanner_timing_fields(tmp_path) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    state = LiveState(clock=lambda: now)
    store = SQLiteStore(tmp_path / "radar_scanner.sqlite3")
    store.initialize()
    store.upsert_latest_target(_target("adsb:abcdef", now))
    scanner = FakeScanner(
        payload={
            "active_scan_band": "ais",
            "last_cycle_start": now,
            "last_scan_switch": now,
            "last_error": None,
            "cycle_count": 17,
            "scan_mode": "continuous_ais",
            "adsb_window_seconds": 7.0,
            "ogn_window_seconds": 4.0,
            "ais_window_seconds": 9.0,
            "inter_scan_pause_seconds": 0.25,
        }
    )

    app = create_api_app(APIRuntime(state=state, store=store, scanner=scanner))
    response = _request(app, "GET", "/ui/targets-latest")
    assert response.status_code == 200
    payload = response.json()
    assert payload["scanner"]["active_scan_band"] == "ais"
    assert payload["scanner"]["last_scan_switch"] == now.isoformat()
    assert payload["scanner"]["cycle_count"] == 17
    assert payload["scanner"]["scan_mode"] == "continuous_ais"
    assert payload["scanner"]["adsb_window_seconds"] == 7.0
    assert payload["scanner"]["ogn_window_seconds"] == 4.0
    assert payload["scanner"]["ais_window_seconds"] == 9.0
    assert payload["scanner"]["inter_scan_pause_seconds"] == 0.25


def test_scanner_mode_endpoints_get_and_set() -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    state = LiveState(clock=lambda: now)
    scanner = FakeScanner(payload={"scan_mode": "hybrid"})
    app = create_api_app(APIRuntime(state=state, store=None, scanner=scanner))

    mode_before = _request(app, "GET", "/scanner/mode")
    assert mode_before.status_code == 200
    assert mode_before.json()["scan_mode"] == "hybrid"
    assert "continuous_adsb" in mode_before.json()["supported_scan_modes"]
    assert "continuous_ogn" in mode_before.json()["supported_scan_modes"]

    updated = _request(
        app,
        "POST",
        "/scanner/mode",
        json={"scan_mode": "continuous_adsb"},
    )
    assert updated.status_code == 200
    assert updated.json()["scan_mode"] == "continuous_adsb"

    mode_after = _request(app, "GET", "/scanner/mode")
    assert mode_after.status_code == 200
    assert mode_after.json()["scan_mode"] == "continuous_adsb"


def test_targets_latest_ui_endpoint_includes_recent_positions_when_radio_connected(tmp_path) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    state = LiveState(clock=lambda: now)
    store = SQLiteStore(tmp_path / "radar_positions.sqlite3")
    store.initialize()

    first = _obs(
        target_id="adsb:abcdef",
        source=Source.ADSB,
        observed_at=now - timedelta(seconds=10),
        lat=59.0001,
        lon=18.0001,
    )
    second = _obs(
        target_id="adsb:abcdef",
        source=Source.ADSB,
        observed_at=now,
        lat=59.0002,
        lon=18.0002,
    )
    state.upsert_observation(first)
    snapshot = state.upsert_observation(second)
    store.upsert_latest_target(snapshot.target)

    app = create_api_app(APIRuntime(state=state, store=store, radio_connected=True))
    response = _request(app, "GET", "/ui/targets-latest")
    assert response.status_code == 200
    payload = response.json()
    assert payload["radio_connected"] is True
    assert payload["count"] == 1
    recent_positions = payload["targets"][0]["recent_positions"]
    assert len(recent_positions) == 2
    assert recent_positions[-1]["lat"] == 59.0002
