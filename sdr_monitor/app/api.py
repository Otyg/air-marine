"""HTTP API endpoints for service health, live targets, stats, and history."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from app.fixed_objects import FixedRadarObject
from app.health import build_health_report
from app.logging_setup import get_logger
from app.map_contours import BBox, MapContourService
from app.models import TargetKind
from app.scanner import SCAN_MODE_VALUES, SCAN_VALUES, HybridBandScanner
from app.state import LiveState
from app.store import SQLiteStore

LOGGER = get_logger(__name__)


@dataclass(slots=True)
class APIRuntime:
    state: LiveState
    store: SQLiteStore | None = None
    scanner: HybridBandScanner | None = None
    map_contour_service: MapContourService | None = None
    service_name: str = "sdr-monitor"
    radar_center_lat: float = 0.0
    radar_center_lon: float = 0.0
    radio_connected: bool = False
    fixed_objects: list[FixedRadarObject] = field(default_factory=list)
    default_map_source: str = "hydro"


def create_api_app(runtime: APIRuntime) -> FastAPI:
    """Create the phase-9 FastAPI application."""

    app = FastAPI(title=runtime.service_name)

    def _default_reception_status_payload() -> dict[str, Any]:
        return {
            "threshold_hours": 2,
            "adsb_last_position_at": None,
            "ais_last_position_at": None,
        }

    def _build_reception_status_payload() -> dict[str, Any]:
        if runtime.store is None:
            return _default_reception_status_payload()

        try:
            latest_by_source = runtime.store.latest_position_timestamps_by_source()
        except Exception:
            LOGGER.exception("Failed to build reception status payload.")
            return _default_reception_status_payload()

        payload = _default_reception_status_payload()
        payload["adsb_last_position_at"] = _to_iso(latest_by_source.get("adsb"))
        payload["ais_last_position_at"] = _to_iso(latest_by_source.get("ais"))
        return payload

    @app.get("/", response_class=HTMLResponse)
    async def get_radar_screen() -> str:
        if not runtime.radio_connected:
            return RedirectResponse(url="/history-radar", status_code=307)
        return _build_radar_html(
            center_lat=runtime.radar_center_lat,
            center_lon=runtime.radar_center_lon,
            service_name=runtime.service_name,
            fixed_objects=runtime.fixed_objects,
            default_map_source=runtime.default_map_source,
        )

    @app.get("/history-radar", response_class=HTMLResponse)
    async def get_history_radar_screen() -> str:
        return _build_history_radar_html(
            center_lat=runtime.radar_center_lat,
            center_lon=runtime.radar_center_lon,
            service_name=runtime.service_name,
            fixed_objects=runtime.fixed_objects,
            default_map_source=runtime.default_map_source,
        )

    @app.get("/ui/live-config")
    async def get_live_ui_config() -> dict[str, Any]:
        return {
            "service_name": runtime.service_name,
            "center_lat": runtime.radar_center_lat,
            "center_lon": runtime.radar_center_lon,
            "fixed_objects": [item.to_dict() for item in runtime.fixed_objects],
            "default_map_source": runtime.default_map_source,
        }

    @app.get("/ui/targets-latest")
    async def get_targets_latest() -> dict[str, Any]:
        scanner_status = runtime.scanner.status() if runtime.scanner else {}
        scanner_payload = {
            "active_scan_band": scanner_status.get("active_scan_band"),
            "last_cycle_start": _to_iso(scanner_status.get("last_cycle_start")),
            "last_scan_switch": _to_iso(scanner_status.get("last_scan_switch")),
            "last_error": scanner_status.get("last_error"),
            "cycle_count": scanner_status.get("cycle_count"),
            "scan_mode": scanner_status.get("scan_mode"),
            "scan": scanner_status.get("scan"),
            "supported_scan": scanner_status.get("supported_scan"),
            "adsb_window_seconds": scanner_status.get("adsb_window_seconds"),
            "ogn_window_seconds": scanner_status.get("ogn_window_seconds"),
            "ais_window_seconds": scanner_status.get("ais_window_seconds"),
            "inter_scan_pause_seconds": scanner_status.get("inter_scan_pause_seconds"),
        }
        if runtime.store is None:
            return {
                "count": 0,
                "targets": [],
                "radio_connected": runtime.radio_connected,
                "scanner": scanner_payload,
                "reception_status": _build_reception_status_payload(),
            }

        try:
            targets = runtime.store.load_latest_targets()
        except Exception as exc:
            LOGGER.exception("Failed to load latest targets.")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to load latest targets: {exc}",
            ) from exc

        serialized: list[dict[str, Any]] = []
        trail_cutoff = runtime.state.now() - timedelta(seconds=120)
        for target in targets:
            item = target.to_dict()
            item["recent_positions"] = []
            speed = target.speed
            if runtime.radio_connected and speed is not None and speed > 1:
                state_snapshot = runtime.state.get_target_state(target.target_id)
                if state_snapshot is not None:
                    item["recent_positions"] = [
                        sample.to_dict()
                        for sample in state_snapshot.positions
                        if sample.ts >= trail_cutoff
                    ]
            serialized.append(item)

        return {
            "count": len(serialized),
            "targets": serialized,
            "radio_connected": runtime.radio_connected,
            "scanner": scanner_payload,
            "reception_status": _build_reception_status_payload(),
        }

    @app.get("/ui/history-targets")
    async def get_history_targets(
        observed_after: datetime | None = Query(default=None),
        observed_before: datetime | None = Query(default=None),
    ) -> dict[str, Any]:
        if runtime.store is None:
            return {
                "count": 0,
                "targets": [],
                "reception_status": _build_reception_status_payload(),
            }

        try:
            targets = runtime.store.list_historical_targets(
                observed_after=observed_after,
                observed_before=observed_before,
            )
        except Exception as exc:
            LOGGER.exception("Failed to load historical targets.")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to load historical targets: {exc}",
            ) from exc

        serialized = [target.to_dict() for target in targets]
        return {
            "count": len(serialized),
            "targets": serialized,
            "reception_status": _build_reception_status_payload(),
        }

    @app.get("/ui/history-targets-in-view")
    async def get_history_targets_in_view(
        center_lat: float = Query(..., ge=-90, le=90),
        center_lon: float = Query(..., ge=-180, le=180),
        range_km: float = Query(..., gt=0),
        observed_after: datetime | None = Query(default=None),
        observed_before: datetime | None = Query(default=None),
    ) -> dict[str, Any]:
        if runtime.store is None:
            return {
                "count": 0,
                "target_ids": [],
            }

        try:
            target_ids = runtime.store.list_historical_target_ids_in_view(
                center_lat=center_lat,
                center_lon=center_lon,
                range_km=range_km,
                observed_after=observed_after,
                observed_before=observed_before,
            )
        except ValueError as exc:
            LOGGER.exception("Invalid request for historical targets in view.")
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("Failed to load historical targets in view.")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to load historical targets in view: {exc}",
            ) from exc

        return {
            "count": len(target_ids),
            "target_ids": target_ids,
        }

    @app.get("/ui/history-tracks-in-view")
    async def get_history_tracks_in_view(
        center_lat: float = Query(..., ge=-90, le=90),
        center_lon: float = Query(..., ge=-180, le=180),
        range_km: float = Query(..., gt=0),
        observed_after: datetime | None = Query(default=None),
        observed_before: datetime | None = Query(default=None),
    ) -> dict[str, Any]:
        if runtime.store is None:
            return {
                "count": 0,
                "target_count": 0,
                "tracks": [],
            }

        try:
            tracks_by_target_id = runtime.store.fetch_historical_tracks_in_view(
                center_lat=center_lat,
                center_lon=center_lon,
                range_km=range_km,
                observed_after=observed_after,
                observed_before=observed_before,
            )
        except ValueError as exc:
            LOGGER.exception("Invalid request for historical tracks in view.")
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("Failed to load historical tracks in view.")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to load historical tracks in view: {exc}",
            ) from exc

        tracks = [
            {
                "target_id": target_id,
                "observations": [observation.to_dict() for observation in observations],
            }
            for target_id, observations in tracks_by_target_id.items()
        ]
        return {
            "count": sum(len(item["observations"]) for item in tracks),
            "target_count": len(tracks),
            "tracks": tracks,
        }

    @app.get("/ui/map-contours")
    async def get_map_contours(
        bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat"),
        range_km: float | None = Query(default=None, gt=0),
        source: str | None = Query(default=None),
    ) -> dict[str, Any]:
        try:
            parsed_bbox = _parse_bbox(bbox)
        except ValueError as exc:
            LOGGER.exception("Invalid bbox for map contours request.")
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        contour_service = runtime.map_contour_service
        if contour_service is None:
            resolved_source = (source or runtime.default_map_source).strip().lower()
            if resolved_source not in {"hydro", "elevation"}:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unsupported map source: {resolved_source!r}. Expected one of elevation, hydro.",
                )
            return {
                "type": "FeatureCollection",
                "features": [],
                "source": resolved_source,
                "status": "unavailable",
                "error": "Map contour service is not configured.",
                "cache_hit": False,
                "bbox": list(parsed_bbox),
                "range_km": range_km,
            }

        try:
            result = contour_service.get_contours(
                bbox=parsed_bbox,
                source=source,
                range_km=range_km,
            )
        except ValueError as exc:
            LOGGER.exception("Failed to resolve map contours.")
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return result.to_payload(
            bbox=parsed_bbox,
            range_km=range_km,
        )

    @app.get("/scanner/mode")
    async def get_scanner_mode() -> dict[str, Any]:
        if runtime.scanner is None:
            raise HTTPException(status_code=503, detail="Scanner is not configured.")
        scanner_status = runtime.scanner.status()
        return {
            "scan_mode": scanner_status.get("scan_mode"),
            "supported_scan_modes": list(SCAN_MODE_VALUES),
        }

    @app.get("/scanner/scan")
    async def get_scanner_scan_targets() -> dict[str, Any]:
        if runtime.scanner is None:
            raise HTTPException(status_code=503, detail="Scanner is not configured.")
        scanner_status = runtime.scanner.status()
        return {
            "scan": list(scanner_status.get("scan") or []),
            "supported_scan": list(scanner_status.get("supported_scan") or SCAN_VALUES),
        }

    @app.post("/scanner/mode")
    async def set_scanner_mode(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        if runtime.scanner is None:
            raise HTTPException(status_code=503, detail="Scanner is not configured.")

        requested_mode = str(payload.get("scan_mode", "")).strip().lower()
        if not requested_mode:
            raise HTTPException(status_code=422, detail="scan_mode is required.")

        try:
            runtime.scanner.set_scan_mode(requested_mode)
        except ValueError as exc:
            LOGGER.exception("Failed to set scanner mode.")
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        scanner_status = runtime.scanner.status()
        return {
            "scan_mode": scanner_status.get("scan_mode"),
            "supported_scan_modes": list(SCAN_MODE_VALUES),
        }

    @app.post("/scanner/scan")
    async def set_scanner_scan_targets(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        if runtime.scanner is None:
            raise HTTPException(status_code=503, detail="Scanner is not configured.")

        requested_scan = payload.get("scan")
        if not isinstance(requested_scan, list):
            raise HTTPException(status_code=422, detail="scan must be an array.")

        try:
            runtime.scanner.set_scan_targets(requested_scan)
        except ValueError as exc:
            LOGGER.exception("Failed to set scanner scan targets.")
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        scanner_status = runtime.scanner.status()
        return {
            "scan": list(scanner_status.get("scan") or []),
            "supported_scan": list(scanner_status.get("supported_scan") or SCAN_VALUES),
        }

    @app.get("/health")
    async def get_health() -> dict[str, Any]:
        return build_health_report(
            service_name=runtime.service_name,
            scanner=runtime.scanner,
            store=runtime.store,
        )

    @app.get("/targets")
    async def get_targets(
        kind: TargetKind | None = Query(default=None),
        fresh_only: bool = Query(default=False),
    ) -> dict[str, Any]:
        targets = runtime.state.list_targets(kind=kind, fresh_only=fresh_only)
        serialized = [target.to_dict() for target in targets]
        return {"count": len(serialized), "targets": serialized}

    @app.get("/targets/{target_id}")
    async def get_target_detail(target_id: str) -> dict[str, Any]:
        state_snapshot = runtime.state.get_target_state(target_id)
        if state_snapshot is None:
            raise HTTPException(status_code=404, detail=f"Target not found: {target_id}")
        return state_snapshot.to_dict()

    @app.get("/stats")
    async def get_stats() -> dict[str, Any]:
        state_stats = runtime.state.get_stats()
        scanner_status = runtime.scanner.status() if runtime.scanner else {}

        total_observations_stored: int | None
        if runtime.store is None:
            total_observations_stored = None
        else:
            try:
                total_observations_stored = runtime.store.count_observations()
            except Exception:
                LOGGER.exception("Failed to count observations for stats.")
                total_observations_stored = None

        return {
            "live_aircraft_count": state_stats["live_aircraft_count"],
            "live_vessel_count": state_stats["live_vessel_count"],
            "total_live_targets": state_stats["total_live_targets"],
            "total_observations_stored": total_observations_stored,
            "last_scan_switch": _to_iso(scanner_status.get("last_scan_switch")),
            "last_error": scanner_status.get("last_error"),
        }

    @app.get("/history/{target_id}")
    async def get_history(
        target_id: str,
        limit: int = Query(default=100, gt=0),
        observed_after: datetime | None = Query(default=None),
        observed_before: datetime | None = Query(default=None),
    ) -> dict[str, Any]:
        if runtime.store is None:
            raise HTTPException(status_code=503, detail="History store is not configured.")

        try:
            observations = runtime.store.fetch_history(
                target_id=target_id,
                limit=limit,
                observed_after=observed_after,
                observed_before=observed_before,
            )
        except ValueError as exc:
            LOGGER.exception("Invalid history lookup request.")
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception(exc)
            LOGGER.exception(f"History lookup failed. Target: {target_id}, limit: {limit},observed_after: {observed_after}, observed_before: {observed_before}")
            raise HTTPException(status_code=500, detail=f"History lookup failed: {exc}") from exc

        serialized = [observation.to_dict() for observation in observations]
        return {
            "target_id": target_id,
            "count": len(serialized),
            "observations": serialized,
        }

    return app


def _to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse_bbox(raw_bbox: str) -> BBox:
    parts = [part.strip() for part in raw_bbox.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must contain exactly four comma-separated coordinates.")
    try:
        min_lon, min_lat, max_lon, max_lat = (float(part) for part in parts)
    except ValueError as exc:
        LOGGER.exception("Failed to parse bbox coordinates.")
        raise ValueError("bbox must contain valid floating-point coordinates.") from exc
    if min_lon >= max_lon:
        raise ValueError("bbox min_lon must be smaller than max_lon.")
    if min_lat >= max_lat:
        raise ValueError("bbox min_lat must be smaller than max_lat.")
    if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
        raise ValueError("bbox longitude values must be within -180..180.")
    if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
        raise ValueError("bbox latitude values must be within -90..90.")
    return (min_lon, min_lat, max_lon, max_lat)

_TEMPLATE_DIR = Path(__file__).with_name("templates")


@lru_cache(maxsize=None)
def _load_html_template(template_name: str) -> str:
    template_path = _TEMPLATE_DIR / template_name
    return template_path.read_text(encoding="utf-8")


def _build_radar_html(
    *,
    center_lat: float,
    center_lon: float,
    service_name: str,
    fixed_objects: list[FixedRadarObject],
    default_map_source: str,
) -> str:
    fixed_objects_json = json.dumps(
        [item.to_dict() for item in fixed_objects],
        ensure_ascii=False,
    ).replace("</", "<\\/")
    return _load_html_template("radar.html").format(
        service_name=service_name,
        center_lat=center_lat,
        center_lon=center_lon,
        default_map_source_json=json.dumps(default_map_source),
        fixed_objects_json=fixed_objects_json,
    )


def _build_history_radar_html(
    *,
    center_lat: float,
    center_lon: float,
    service_name: str,
    fixed_objects: list[FixedRadarObject],
    default_map_source: str,
) -> str:
    fixed_objects_json = json.dumps(
        [item.to_dict() for item in fixed_objects],
        ensure_ascii=False,
    ).replace("</", "<\\/")
    return _load_html_template("history_radar.html").format(
        service_name=service_name,
        center_lat=center_lat,
        center_lon=center_lon,
        default_map_source_json=json.dumps(default_map_source),
        fixed_objects_json=fixed_objects_json,
    )
