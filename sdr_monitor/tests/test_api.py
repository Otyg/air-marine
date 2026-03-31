from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from app.api import APIRuntime, create_api_app
from app.fixed_objects import FixedRadarObject
from app.models import Freshness, NormalizedObservation, ScanBand, Source, Target, TargetKind
from app.state import LiveState
from app.store import SQLiteStore


@dataclass
class FakeScanner:
    payload: dict

    def status(self) -> dict:
        return dict(self.payload)


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
    return NormalizedObservation(
        target_id=target_id,
        source=source,
        kind=TargetKind.AIRCRAFT if source == Source.ADSB else TargetKind.VESSEL,
        observed_at=observed_at,
        lat=lat,
        lon=lon,
        course=90.0,
        speed=120.0,
        altitude=1000.0 if source == Source.ADSB else None,
        last_scan_band=ScanBand.ADSB if source == Source.ADSB else ScanBand.AIS,
        icao24="abcdef" if source == Source.ADSB else None,
        mmsi="265123456" if source == Source.AIS else None,
        label="FLT1" if source == Source.ADSB else "VESSEL1",
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
    assert "id=\"showFixedNames\"" in response.text
    assert "id=\"objectsList\"" in response.text
    assert "id=\"outsideObjectsList\"" in response.text
    assert "id=\"showLowSpeed\"" in response.text
    assert "drawCourseVector" in response.text
    assert "const basePollMs = 2000;" in response.text
    assert "renderObjectsPanel" in response.text
    assert "Objekt utanför aktivt område" in response.text
    assert "const fixedObjects =" in response.text
    assert "Lighthouse" in response.text
    assert "drawFixedObjects" in response.text
    assert "drawRecentPositions(trackedTarget, cx, cy, pxPerKm, radius);" in response.text
    assert "const trailPointWindowSeconds = 120;" in response.text
    assert "mergeTrailPoints" in response.text
    assert "getTrailFadeProgress" in response.text
    assert "trailOpacityForAgeRank" in response.text
    assert "updateTrailCacheFromTargets" in response.text
    assert "retainedTrailTargets" in response.text
    assert "#39FF14" in response.text
    assert "last_seen:" in response.text
    assert "59.32930000" in response.text
    assert "18.06860000" in response.text


def test_targets_latest_ui_endpoint_returns_store_rows(tmp_path) -> None:
    now = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    state = LiveState(clock=lambda: now)
    store = SQLiteStore(tmp_path / "radar.sqlite3")
    store.initialize()
    store.upsert_latest_target(_target("adsb:abcdef", now))

    app = create_api_app(APIRuntime(state=state, store=store))
    response = _request(app, "GET", "/ui/targets-latest")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["radio_connected"] is False
    assert response.json()["targets"][0]["target_id"] == "adsb:abcdef"
    assert response.json()["targets"][0]["recent_positions"] == []


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
