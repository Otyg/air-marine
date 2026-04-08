"""HTTP API endpoints for service health, live targets, stats, and history."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.fixed_objects import FixedRadarObject
from app.health import build_health_report
from app.map_contours import BBox, MapContourService
from app.models import TargetKind
from app.scanner import SCAN_MODE_VALUES, HybridBandScanner
from app.state import LiveState
from app.store import SQLiteStore


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

    @app.get("/", response_class=HTMLResponse)
    async def get_radar_screen() -> str:
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
            }

        try:
            targets = runtime.store.load_latest_targets()
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to load latest targets: {exc}",
            ) from exc

        serialized: list[dict[str, Any]] = []
        for target in targets:
            item = target.to_dict()
            item["recent_positions"] = []
            if runtime.radio_connected:
                state_snapshot = runtime.state.get_target_state(target.target_id)
                if state_snapshot is not None:
                    item["recent_positions"] = [
                        sample.to_dict()
                        for sample in list(state_snapshot.positions)[-5:]
                    ]
            serialized.append(item)

        return {
            "count": len(serialized),
            "targets": serialized,
            "radio_connected": runtime.radio_connected,
            "scanner": scanner_payload,
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
            }

        try:
            targets = runtime.store.list_historical_targets(
                observed_after=observed_after,
                observed_before=observed_before,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to load historical targets: {exc}",
            ) from exc

        serialized = [target.to_dict() for target in targets]
        return {
            "count": len(serialized),
            "targets": serialized,
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
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to load historical targets in view: {exc}",
            ) from exc

        return {
            "count": len(target_ids),
            "target_ids": target_ids,
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
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        scanner_status = runtime.scanner.status()
        return {
            "scan_mode": scanner_status.get("scan_mode"),
            "supported_scan_modes": list(SCAN_MODE_VALUES),
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
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
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
    return f"""<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{service_name} Radar</title>
  <style>
    :root {{
      --radar-bg: #000000;
      --radar-fg: #E9FCE9;
      --radar-ring: #2c7a2c;
      --radar-center: #d3d3d3;
      --panel-fg: #9be89b;
      --panel-dim: #5b9e5b;
    }}
    html, body {{
      margin: 0;
      width: 100%;
      height: 100%;
      background: var(--radar-bg);
      color: var(--radar-fg);
      font-family: "Courier New", Courier, monospace;
    }}
    .layout {{
      display: flex;
      flex-direction: column;
      height: 100%;
    }}
    .hud {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      padding: 10px 14px;
      border-bottom: 1px solid #154815;
      color: var(--panel-fg);
      font-size: 14px;
      letter-spacing: 0.03em;
    }}
    .hud-title {{
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .view-links {{
      display: inline-flex;
      gap: 10px;
      font-size: 12px;
      letter-spacing: normal;
    }}
    .view-links a {{
      color: var(--panel-dim);
      text-decoration: none;
    }}
    .view-links a[aria-current="page"] {{
      color: var(--panel-fg);
    }}
    .view-links a:hover {{
      color: var(--radar-fg);
    }}
    .hud .dim {{
      color: var(--panel-dim);
    }}
    .hud-right {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .scan-mode-control {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--panel-dim);
      font-size: 12px;
      white-space: nowrap;
    }}
    .scan-mode-control select {{
      border: 1px solid #226322;
      background: #041104;
      color: var(--panel-fg);
      font: inherit;
      padding: 2px 6px;
      height: 28px;
      min-width: 170px;
    }}
    .scan-mode-control select:focus {{
      outline: none;
      border-color: #2f8b2f;
      background: #0a1f0a;
    }}
    .zoom-controls {{
      display: inline-flex;
      border: 1px solid #226322;
      align-items: stretch;
      background: #051805;
    }}
    .zoom-controls button {{
      background: transparent;
      color: var(--panel-fg);
      border: 0;
      border-right: 1px solid #226322;
      min-width: 40px;
      height: 28px;
      cursor: pointer;
      font: inherit;
    }}
    .zoom-controls button:hover {{
      background: #0a260a;
    }}
    .zoom-controls input {{
      width: 62px;
      border: 0;
      border-right: 1px solid #226322;
      background: #020b02;
      color: var(--panel-fg);
      text-align: right;
      padding: 0 8px;
      font: inherit;
    }}
    .zoom-controls input:focus {{
      outline: none;
      background: #0a1f0a;
    }}
    .zoom-controls .range-unit {{
      display: inline-flex;
      align-items: center;
      padding: 0 8px;
      color: var(--panel-dim);
      border-right: 1px solid #226322;
      font-size: 12px;
    }}
    .zoom-controls > *:last-child {{
      border-right: 0;
    }}
    .toggle-control {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--panel-dim);
      font-size: 12px;
      user-select: none;
      white-space: nowrap;
    }}
    .screen {{
      flex: 1;
      min-height: 0;
      padding: 12px;
      display: flex;
      gap: 12px;
    }}
    .radar-wrap {{
      flex: 1;
      min-width: 0;
    }}
    .side-panel {{
      width: 360px;
      max-width: 44vw;
      border: 1px solid #154815;
      background: #020b02;
      display: flex;
      flex-direction: column;
      min-height: 0;
    }}
    .side-panel-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      padding: 10px;
      border-bottom: 1px solid #154815;
      color: var(--panel-fg);
      font-size: 13px;
    }}
    .filter-toggle {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--panel-dim);
      font-size: 12px;
      user-select: none;
    }}
    .panel-filter-control {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--panel-dim);
      font-size: 12px;
      white-space: nowrap;
    }}
    .panel-filter-control select {{
      border: 1px solid #226322;
      background: #041104;
      color: var(--panel-fg);
      font: inherit;
      padding: 2px 6px;
      height: 28px;
      min-width: 96px;
    }}
    .panel-filter-control select:focus {{
      outline: none;
      border-color: #2f8b2f;
      background: #0a1f0a;
    }}
    .side-panel-summary {{
      padding: 8px 10px;
      color: var(--panel-dim);
      border-bottom: 1px solid #103810;
      font-size: 12px;
    }}
    .side-panel-subhead {{
      padding: 8px 10px;
      color: var(--panel-fg);
      border-top: 1px solid #154815;
      border-bottom: 1px solid #103810;
      font-size: 12px;
    }}
    .objects-list {{
      flex: 1;
      overflow: auto;
      padding: 8px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 34vh;
    }}
    .object-item {{
      border: 1px solid #124212;
      background: #041104;
      padding: 8px;
      color: var(--panel-fg);
      font-size: 12px;
      line-height: 1.45;
      cursor: pointer;
      transition: border-color 120ms ease, background 120ms ease;
    }}
    .object-item:hover {{
      border-color: #2f8b2f;
    }}
    .object-item.selected {{
      border-color: #9e2f2f;
      background: #200808;
    }}
    .object-label {{
      color: var(--radar-fg);
      font-size: 13px;
      margin-bottom: 4px;
    }}
    .object-label-row {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 4px;
    }}
    .object-type-icon {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 18px;
      color: var(--panel-dim);
      font-size: 14px;
      line-height: 1;
    }}
    .object-view-badge {{
      margin-left: auto;
      border: 1px solid #1b5e8b;
      color: #8fd3ff;
      background: rgba(16, 50, 72, 0.45);
      padding: 1px 6px;
      font-size: 12px;
      line-height: 1.2;
    }}
    .object-item.in-view {{
      box-shadow: inset 0 0 0 1px rgba(55, 170, 235, 0.38);
    }}
    .object-item.selected .object-label {{
      color: #ff9c9c;
    }}
    .objects-empty {{
      padding: 10px;
      color: var(--panel-dim);
      font-size: 12px;
    }}
    @media (max-width: 1000px) {{
      .screen {{
        flex-direction: column;
      }}
      .side-panel {{
        width: auto;
        max-width: none;
        min-height: 240px;
      }}
    }}
    canvas {{
      display: block;
      width: 100%;
      height: 100%;
      background: var(--radar-bg);
      border: 1px solid #154815;
    }}
  </style>
</head>
<body>
  <div class="layout">
    <div class="hud">
      <div class="hud-title">
        <div>{service_name} / RADAR VIEW</div>
        <div class="view-links">
          <a href="./" aria-current="page">Live radar</a>
          <a href="history-radar">Historiska spår</a>
        </div>
      </div>
      <div class="hud-right">
        <div class="zoom-controls">
          <button id="zoomOut" type="button" aria-label="Zooma ut">-</button>
          <input id="rangeInput" type="text" inputmode="decimal" value="10" aria-label="Range km" />
          <span class="range-unit">km</span>
          <button id="zoomIn" type="button" aria-label="Zooma in">+</button>
          <button id="zoomReset" type="button" aria-label="Reset range">Hem</button>
        </div>
        <label class="scan-mode-control" for="scanModeSelect">
          Mottagning
          <select id="scanModeSelect" aria-label="Mottagningsläge">
            <option value="hybrid">Scan AIS + ADS-B + OGN/FLARM/ADS-L</option>
            <option value="continuous_ais">Kontinuerlig AIS</option>
            <option value="continuous_adsb">Kontinuerlig ADS-B</option>
            <option value="continuous_ogn">Kontinuerlig OGN/FLARM/ADS-L</option>
          </select>
        </label>
        <label class="toggle-control" for="showFixedNames">
          <input id="showFixedNames" type="checkbox" checked />
          Visa namn fasta punkter
        </label>
        <label class="toggle-control" for="showTargetLabels">
          <input id="showTargetLabels" type="checkbox" />
          Visa labels objekt
        </label>
        <label class="toggle-control" for="showMapContours">
          <input id="showMapContours" type="checkbox" checked />
          Visa kust/sjö-konturer
        </label>
        <div id="meta" class="dim">Center: {center_lat:.6f}, {center_lon:.6f}</div>
      </div>
    </div>
    <div class="screen">
      <div class="radar-wrap">
        <canvas id="radar"></canvas>
      </div>
      <aside class="side-panel">
        <div class="side-panel-head">
          <div>Synliga objekt</div>
          <label class="filter-toggle" for="showLowSpeed">
            <input id="showLowSpeed" type="checkbox" />
            Visa `last_speed<1`
          </label>
        </div>
        <div class="side-panel-summary">
          <label class="panel-filter-control" for="targetTypeFilter">
            Typ
            <select id="targetTypeFilter" aria-label="Filtrera objekttyp">
              <option value="all">Alla</option>
              <option value="aircraft">Flygplan</option>
              <option value="vessel">Båtar</option>
            </select>
          </label>
        </div>
        <div id="objectsSummary" class="side-panel-summary">0 synliga objekt</div>
        <div id="objectsList" class="objects-list">
          <div class="objects-empty">Inga objekt i aktuell vy.</div>
        </div>
        <div class="side-panel-subhead">Objekt utanför aktivt område</div>
        <div id="outsideObjectsSummary" class="side-panel-summary">0 objekt utanför aktivt område</div>
        <div id="outsideObjectsList" class="objects-list">
          <div class="objects-empty">Inga objekt utanför aktivt område.</div>
        </div>
      </aside>
    </div>
  </div>
  <script>
    const homeCenter = {{ lat: {center_lat:.8f}, lon: {center_lon:.8f} }};
    const kmPerDegLat = 110.574;
    const minAutoRefreshMs = 2000;
    const defaultPollMs = minAutoRefreshMs;
    const minPollMs = minAutoRefreshMs;
    const maxPollMs = 12000;
    const pollBackoffFactor = 1.25;
    const observedIntervalLimit = 12;
    const defaultRangeKm = 10.0;
    const radarRingCount = 5;
    const minRangeKm = 0.2;
    const maxRangeKm = 500.0;
    const trailPointWindowSeconds = 120;
    const trailStaleStartSeconds = 30;
    const trailStaleFadeSeconds = 270;
    const liveTrailAgeColors = [
      "#C1F5C1",
      "#90EE90",
      "#72E972",
      "#4AE34A",
      "#22DD22",
      "#1CB51C",
      "#168D16",
      "#106510",
      "#0A3E0A",
      "#031603",
    ];
    const radarRingColor = "#2c7a2c";
    const liveTargetColor = "#E9FCE9";
    const defaultMapSource = {json.dumps(default_map_source)};
    const fixedObjects = {fixed_objects_json};
    const canvas = document.getElementById("radar");
    const meta = document.getElementById("meta");
    const zoomInButton = document.getElementById("zoomIn");
    const zoomOutButton = document.getElementById("zoomOut");
    const zoomResetButton = document.getElementById("zoomReset");
    const scanModeSelect = document.getElementById("scanModeSelect");
    const rangeInput = document.getElementById("rangeInput");
    const showFixedNamesCheckbox = document.getElementById("showFixedNames");
    const showTargetLabelsCheckbox = document.getElementById("showTargetLabels");
    const showMapContoursCheckbox = document.getElementById("showMapContours");
    const showLowSpeedCheckbox = document.getElementById("showLowSpeed");
    const targetTypeFilterSelect = document.getElementById("targetTypeFilter");
    const objectsSummary = document.getElementById("objectsSummary");
    const objectsList = document.getElementById("objectsList");
    const outsideObjectsSummary = document.getElementById("outsideObjectsSummary");
    const outsideObjectsList = document.getElementById("outsideObjectsList");
    const ctx = canvas.getContext("2d");
    let targets = [];
    let retainedTrailTargets = [];
    const trailCache = new Map();
    let error = null;
    let radioConnected = false;
    let viewCenter = {{ ...homeCenter }};
    let manualRangeKm = defaultRangeKm;
    let dragStart = null;
    let dragCurrent = null;
    let showLowSpeed = false;
    let targetTypeFilter = "all";
    let showFixedNames = true;
    let showTargetLabels = false;
    let showMapContours = true;
    let selectedTargetId = null;
    const selectedHistoryByTargetId = new Map();
    let pendingFitTargetId = null;
    const selectedHistoryPositionCount = 15;
    const selectedHistoryRequestLimit = selectedHistoryPositionCount + 1;
    const selectedTargetColor = "#ff4d4d";
    let pollTimerId = null;
    let nextPollMs = defaultPollMs;
    let requestInFlight = false;
    let lastSeenWatermarkMs = Number.NaN;
    let lastDataChangeAtMs = Date.now();
    const observedUpdateIntervalsMs = [];
    let lastScannerState = null;
    let scanMode = "hybrid";
    let scanModeUpdateInFlight = false;
    let mapContours = [];
    let mapContourSource = defaultMapSource;
    let mapContourStatus = "idle";
    let mapContourError = null;
    let mapContourRequestKey = null;
    let mapContourLoadedKey = null;
    let mapContourPendingKey = null;
    let mapContourRequestInFlight = false;
    let mapContourRetryTimer = null;

    function clampUnitInterval(value) {{
      if (!Number.isFinite(value)) return 0;
      return Math.max(0, Math.min(1, value));
    }}

    function parseHexColor(hexColor) {{
      if (typeof hexColor !== "string") return null;
      const normalized = hexColor.trim();
      const match = /^#([0-9a-f]{6})$/i.exec(normalized);
      if (!match) return null;
      return {{
        red: Number.parseInt(match[1].slice(0, 2), 16),
        green: Number.parseInt(match[1].slice(2, 4), 16),
        blue: Number.parseInt(match[1].slice(4, 6), 16),
      }};
    }}

    function toHexChannel(value) {{
      return Math.round(clampUnitInterval(value / 255) * 255)
        .toString(16)
        .padStart(2, "0");
    }}

    function blendHexColors(fromColor, toColor, amount) {{
      const from = parseHexColor(fromColor);
      const to = parseHexColor(toColor);
      if (!from || !to) return toColor || fromColor || radarRingColor;
      const mix = clampUnitInterval(amount);
      return `#${{toHexChannel(from.red + ((to.red - from.red) * mix))}}${{toHexChannel(from.green + ((to.green - from.green) * mix))}}${{toHexChannel(from.blue + ((to.blue - from.blue) * mix))}}`;
    }}

    function trailColorForAge(ageRank, targetColor) {{
      const clampedAge = clampUnitInterval(ageRank);
      const emphasis = Math.pow(1 - clampedAge, 0.85);
      return blendHexColors(radarRingColor, targetColor || liveTargetColor, emphasis);
    }}

    function liveTrailColorForAge(ageRank) {{
      const clampedAge = clampUnitInterval(ageRank);
      const paletteIndex = Math.min(
        liveTrailAgeColors.length - 1,
        Math.floor(clampedAge * liveTrailAgeColors.length),
      );
      return liveTrailAgeColors[paletteIndex];
    }}

    function clampPollMs(value) {{
      if (!Number.isFinite(value)) return defaultPollMs;
      return Math.max(minPollMs, Math.min(maxPollMs, Math.round(value)));
    }}

    function pushObservedUpdateInterval(intervalMs) {{
      if (!Number.isFinite(intervalMs)) return;
      if (intervalMs <= 0 || intervalMs > 15 * 60 * 1000) return;
      observedUpdateIntervalsMs.push(intervalMs);
      if (observedUpdateIntervalsMs.length > observedIntervalLimit) {{
        observedUpdateIntervalsMs.shift();
      }}
    }}

    function median(values) {{
      if (!Array.isArray(values) || values.length === 0) return Number.NaN;
      const sorted = [...values].sort((a, b) => a - b);
      const middle = Math.floor(sorted.length / 2);
      if ((sorted.length % 2) === 0) {{
        return (sorted[middle - 1] + sorted[middle]) / 2;
      }}
      return sorted[middle];
    }}

    function deriveLatestLastSeenMs(items) {{
      let latest = Number.NaN;
      for (const item of items) {{
        if (!item || typeof item !== "object") continue;
        const tsMs = parseTimestampMs(item.last_seen);
        if (!Number.isFinite(tsMs)) continue;
        if (!Number.isFinite(latest) || tsMs > latest) {{
          latest = tsMs;
        }}
      }}
      return latest;
    }}

    function computeAdaptivePollMs() {{
      const medianObservedIntervalMs = median(observedUpdateIntervalsMs);
      if (!Number.isFinite(medianObservedIntervalMs)) {{
        return clampPollMs(defaultPollMs);
      }}
      return clampPollMs(medianObservedIntervalMs * 0.55);
    }}

    function toPositiveMs(secondsValue) {{
      const seconds = Number(secondsValue);
      if (!Number.isFinite(seconds) || seconds <= 0) return Number.NaN;
      return seconds * 1000;
    }}

    function normalizeScannerState(rawScanner) {{
      if (!rawScanner || typeof rawScanner !== "object") return null;
      const activeScanBand = typeof rawScanner.active_scan_band === "string"
        ? rawScanner.active_scan_band
        : null;
      const lastScanSwitchMs = parseTimestampMs(rawScanner.last_scan_switch);
      const adsbWindowMs = toPositiveMs(rawScanner.adsb_window_seconds);
      const ognWindowMs = toPositiveMs(rawScanner.ogn_window_seconds);
      const aisWindowMs = toPositiveMs(rawScanner.ais_window_seconds);
      const pauseMs = toPositiveMs(rawScanner.inter_scan_pause_seconds);
      return {{
        active_scan_band: activeScanBand,
        last_scan_switch_ms: lastScanSwitchMs,
        adsb_window_ms: adsbWindowMs,
        ogn_window_ms: ognWindowMs,
        ais_window_ms: aisWindowMs,
        inter_scan_pause_ms: Number.isFinite(pauseMs) ? pauseMs : 0,
      }};
    }}

    function computeBandAwarePollMs(scannerState) {{
      if (!scannerState || typeof scannerState !== "object") return Number.NaN;
      const activeBand = scannerState.active_scan_band;
      if (activeBand !== "adsb" && activeBand !== "ais" && activeBand !== "ogn") return Number.NaN;

      const lastSwitchMs = scannerState.last_scan_switch_ms;
      if (!Number.isFinite(lastSwitchMs)) return Number.NaN;

      const adsbWindowMs = scannerState.adsb_window_ms;
      const ognWindowMs = scannerState.ogn_window_ms;
      const aisWindowMs = scannerState.ais_window_ms;
      const pauseMs = Number.isFinite(scannerState.inter_scan_pause_ms)
        ? scannerState.inter_scan_pause_ms
        : 0;

      const currentWindowMs = activeBand === "adsb"
        ? adsbWindowMs
        : activeBand === "ogn"
          ? ognWindowMs
          : aisWindowMs;
      if (!Number.isFinite(currentWindowMs) || currentWindowMs <= 0) return Number.NaN;

      const elapsedMs = Math.max(0, Date.now() - lastSwitchMs);
      const remainingInBandMs = Math.max(0, currentWindowMs - elapsedMs);

      // Poll around the next likely band handover where fresh rows usually arrive.
      const handoverSafetyMs = 180;
      const targetMs = remainingInBandMs + Math.max(0, pauseMs) + handoverSafetyMs;
      return clampPollMs(targetMs);
    }}

    function blendPollMsWithBandTiming(baseMs, bandAwareMs) {{
      if (!Number.isFinite(baseMs)) return clampPollMs(bandAwareMs);
      if (!Number.isFinite(bandAwareMs)) return clampPollMs(baseMs);
      return clampPollMs((baseMs * 0.65) + (bandAwareMs * 0.35));
    }}

    function scanModeLabel(mode) {{
      if (mode === "continuous_ais") return "Kontinuerlig AIS";
      if (mode === "continuous_adsb") return "Kontinuerlig ADS-B";
      if (mode === "continuous_ogn") return "Kontinuerlig OGN/FLARM/ADS-L";
      return "Scan AIS + ADS-B + OGN/FLARM/ADS-L";
    }}

    function syncScanModeSelect() {{
      if (!(scanModeSelect instanceof HTMLSelectElement)) return;
      if (document.activeElement === scanModeSelect) return;
      scanModeSelect.value = scanMode;
    }}

    async function setScanMode(nextMode) {{
      if (!(scanModeSelect instanceof HTMLSelectElement)) return;
      if (scanModeUpdateInFlight) return;
      scanModeUpdateInFlight = true;
      scanModeSelect.disabled = true;
      try {{
        const response = await fetch("scanner/mode", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ scan_mode: nextMode }}),
        }});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        const payload = await response.json();
        if (payload && typeof payload.scan_mode === "string") {{
          scanMode = payload.scan_mode;
        }} else {{
          scanMode = nextMode;
        }}
        error = null;
      }} catch (err) {{
        error = err instanceof Error ? err.message : String(err);
      }} finally {{
        scanModeUpdateInFlight = false;
        scanModeSelect.disabled = false;
        syncScanModeSelect();
        draw();
      }}
    }}

    function scheduleNextLoad(delayMs = nextPollMs) {{
      if (pollTimerId !== null) {{
        clearTimeout(pollTimerId);
      }}
      const safeDelayMs = clampPollMs(delayMs);
      pollTimerId = window.setTimeout(() => {{
        void loadTargets();
      }}, safeDelayMs);
    }}

    function clampRangeKm(value) {{
      return Math.max(minRangeKm, Math.min(maxRangeKm, value));
    }}

    function kmPerDegLon(lat) {{
      return 111.320 * Math.cos((lat * Math.PI) / 180);
    }}

    function toOffsetKm(lat, lon, referenceCenter) {{
      const dy = (lat - referenceCenter.lat) * kmPerDegLat;
      const dx = (lon - referenceCenter.lon) * kmPerDegLon(referenceCenter.lat);
      return {{ dx, dy }};
    }}

    function offsetKmToLatLon(dxKm, dyKm, referenceCenter) {{
      const lat = referenceCenter.lat + (dyKm / kmPerDegLat);
      const lon = referenceCenter.lon + (dxKm / kmPerDegLon(referenceCenter.lat));
      return {{ lat, lon }};
    }}

    function computeMapContourBBox(rangeKm) {{
      const latPadding = rangeKm / kmPerDegLat;
      const lonPadding = rangeKm / kmPerDegLon(viewCenter.lat);
      return [
        viewCenter.lon - lonPadding,
        viewCenter.lat - latPadding,
        viewCenter.lon + lonPadding,
        viewCenter.lat + latPadding,
      ];
    }}

    function mapContourRequestKeyForView(rangeKm) {{
      const bbox = computeMapContourBBox(rangeKm);
      const bboxKey = bbox.map((value) => value.toFixed(4)).join(",");
      return {{
        bbox,
        key: `${{mapContourSource}}|${{bboxKey}}`,
      }};
    }}

    function computeRangeKm(items, referenceCenter) {{
      let maxDistance = 3;
      for (const item of items) {{
        if (typeof item.lat !== "number" || typeof item.lon !== "number") continue;
        const {{ dx, dy }} = toOffsetKm(item.lat, item.lon, referenceCenter);
        const distance = Math.sqrt((dx * dx) + (dy * dy));
        if (distance > maxDistance) maxDistance = distance;
      }}
      return clampRangeKm(Math.max(3, Math.ceil(maxDistance + 1)));
    }}

    function resizeCanvas() {{
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      const w = Math.max(1, Math.floor(rect.width * dpr));
      const h = Math.max(1, Math.floor(rect.height * dpr));
      if (canvas.width !== w || canvas.height !== h) {{
        canvas.width = w;
        canvas.height = h;
      }}
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }}

    function getViewMetrics() {{
      resizeCanvas();
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      const cx = width / 2;
      const cy = height / 2;
      const radius = Math.max(30, Math.min(width, height) * 0.45);
      const autoRangeKm = computeRangeKm(targets, viewCenter);
      const rangeKm = clampRangeKm(manualRangeKm ?? defaultRangeKm);
      const pxPerKm = radius / rangeKm;
      return {{ width, height, cx, cy, radius, autoRangeKm, rangeKm, pxPerKm }};
    }}

    function formatRangeValue(value) {{
      return Number.isInteger(value)
        ? String(value)
        : value.toFixed(2).replace(/0+$/, "").replace(/\\.$/, "");
    }}

    function syncRangeInput(rangeKm) {{
      if (document.activeElement === rangeInput) return;
      rangeInput.value = formatRangeValue(rangeKm);
    }}

    function parseRangeInputValue(value) {{
      const normalized = String(value).trim().replace(",", ".");
      if (!normalized) return NaN;
      return Number(normalized);
    }}

    function setRangeKm(nextRangeKm) {{
      manualRangeKm = clampRangeKm(nextRangeKm);
      draw();
    }}

    function increaseRange() {{
      const rangeKm = getViewMetrics().rangeKm;
      setRangeKm(rangeKm + 1);
    }}

    function decreaseRange() {{
      const rangeKm = getViewMetrics().rangeKm;
      setRangeKm(rangeKm - 1);
    }}

    function resetZoom() {{
      viewCenter = {{ ...homeCenter }};
      manualRangeKm = defaultRangeKm;
      draw();
    }}

    function applyRangeInput() {{
      const parsed = parseRangeInputValue(rangeInput.value);
      if (!Number.isFinite(parsed)) {{
        syncRangeInput(getViewMetrics().rangeKm);
        return;
      }}
      setRangeKm(parsed);
    }}

    function canvasPointFromEvent(event) {{
      const rect = canvas.getBoundingClientRect();
      const x = Math.max(0, Math.min(rect.width, event.clientX - rect.left));
      const y = Math.max(0, Math.min(rect.height, event.clientY - rect.top));
      return {{ x, y }};
    }}

    function beginSelection(event) {{
      if (event.button !== 0) return;
      dragStart = canvasPointFromEvent(event);
      dragCurrent = dragStart;
      draw();
    }}

    function updateSelection(event) {{
      if (!dragStart) return;
      dragCurrent = canvasPointFromEvent(event);
      draw();
    }}

    function applySelectionZoom() {{
      if (!dragStart || !dragCurrent) return;
      const {{ cx, cy, pxPerKm }} = getViewMetrics();
      const x1 = Math.min(dragStart.x, dragCurrent.x);
      const x2 = Math.max(dragStart.x, dragCurrent.x);
      const y1 = Math.min(dragStart.y, dragCurrent.y);
      const y2 = Math.max(dragStart.y, dragCurrent.y);
      const widthPx = x2 - x1;
      const heightPx = y2 - y1;
      if (widthPx < 10 || heightPx < 10) return;

      const centerX = x1 + (widthPx / 2);
      const centerY = y1 + (heightPx / 2);
      const dxKm = (centerX - cx) / pxPerKm;
      const dyKm = (cy - centerY) / pxPerKm;
      viewCenter = offsetKmToLatLon(dxKm, dyKm, viewCenter);

      const halfWidthKm = (widthPx / 2) / pxPerKm;
      const halfHeightKm = (heightPx / 2) / pxPerKm;
      const nextRangeKm = clampRangeKm(Math.max(halfWidthKm, halfHeightKm) * 1.2);
      manualRangeKm = nextRangeKm;
    }}

    function endSelection(event) {{
      if (!dragStart) return;
      dragCurrent = canvasPointFromEvent(event);
      applySelectionZoom();
      dragStart = null;
      dragCurrent = null;
      draw();
    }}

    function cancelSelection() {{
      if (!dragStart) return;
      dragStart = null;
      dragCurrent = null;
      draw();
    }}

    function drawSelectionBox() {{
      if (!dragStart || !dragCurrent) return;
      const x = Math.min(dragStart.x, dragCurrent.x);
      const y = Math.min(dragStart.y, dragCurrent.y);
      const width = Math.abs(dragCurrent.x - dragStart.x);
      const height = Math.abs(dragCurrent.y - dragStart.y);
      ctx.save();
      ctx.setLineDash([6, 4]);
      ctx.strokeStyle = "#7cff7c";
      ctx.lineWidth = 1;
      ctx.strokeRect(x, y, width, height);
      ctx.fillStyle = "rgba(124, 255, 124, 0.08)";
      ctx.fillRect(x, y, width, height);
      ctx.restore();
    }}

    function pointerLengthPx(speed) {{
      if (!Number.isFinite(speed) || speed <= 0) return 10;
      return Math.max(8, Math.min(26, 8 + (Math.sqrt(speed) * 1.5)));
    }}

    function drawCourseVector(x, y, course, speed, color) {{
      if (!Number.isFinite(course) || !Number.isFinite(speed) || speed <= 0) return;
      const bearing = ((course % 360) + 360) % 360;
      const radians = (bearing * Math.PI) / 180;
      const vx = Math.sin(radians);
      const vy = -Math.cos(radians);
      const length = pointerLengthPx(speed);
      const endX = x + (vx * length);
      const endY = y + (vy * length);

      const headLength = 4;
      const headBaseX = endX - (vx * headLength);
      const headBaseY = endY - (vy * headLength);
      const perpX = -vy;
      const perpY = vx;

      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(endX, endY);
      ctx.stroke();

      ctx.beginPath();
      ctx.moveTo(endX, endY);
      ctx.lineTo(headBaseX + (perpX * 2.2), headBaseY + (perpY * 2.2));
      ctx.moveTo(endX, endY);
      ctx.lineTo(headBaseX - (perpX * 2.2), headBaseY - (perpY * 2.2));
      ctx.stroke();
      ctx.restore();
    }}

    function isInsideRadarCircle(x, y, cx, cy, radius) {{
      return ((x - cx) * (x - cx)) + ((y - cy) * (y - cy)) <= (radius * radius);
    }}

    function clipSegmentToCircle(start, end, cx, cy, radius) {{
      if (!start || !end) return null;
      const dx = end.x - start.x;
      const dy = end.y - start.y;
      const a = (dx * dx) + (dy * dy);
      if (a <= 0.000001) {{
        return isInsideRadarCircle(start.x, start.y, cx, cy, radius)
          ? {{ start, end }}
          : null;
      }}

      const startInside = isInsideRadarCircle(start.x, start.y, cx, cy, radius);
      const endInside = isInsideRadarCircle(end.x, end.y, cx, cy, radius);
      if (startInside && endInside) {{
        return {{ start, end }};
      }}

      const fx = start.x - cx;
      const fy = start.y - cy;
      const b = 2 * ((fx * dx) + (fy * dy));
      const c = (fx * fx) + (fy * fy) - (radius * radius);
      const discriminant = (b * b) - (4 * a * c);
      if (discriminant < 0) return null;

      const sqrtDiscriminant = Math.sqrt(discriminant);
      const t1 = (-b - sqrtDiscriminant) / (2 * a);
      const t2 = (-b + sqrtDiscriminant) / (2 * a);
      const enterT = Math.max(0, Math.min(t1, t2));
      const exitT = Math.min(1, Math.max(t1, t2));
      if (enterT > exitT) return null;

      const clippedStartT = startInside ? 0 : enterT;
      const clippedEndT = endInside ? 1 : exitT;
      return {{
        start: {{
          x: start.x + (dx * clippedStartT),
          y: start.y + (dy * clippedStartT),
        }},
        end: {{
          x: start.x + (dx * clippedEndT),
          y: start.y + (dy * clippedEndT),
        }},
      }};
    }}

    function normalizeRecentPositions(value) {{
      if (!Array.isArray(value)) return [];
      return value;
    }}

    function parseTimestampMs(value) {{
      if (typeof value === "number" && Number.isFinite(value)) return value;
      if (typeof value !== "string" || !value.trim()) return NaN;
      const parsed = Date.parse(value);
      return Number.isFinite(parsed) ? parsed : NaN;
    }}

    function buildTrailPoint(sample, fallbackTsMs = NaN) {{
      if (!sample || typeof sample !== "object") return null;
      const lat = toOptionalNumber(sample.lat);
      const lon = toOptionalNumber(sample.lon);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
      let tsMs = parseTimestampMs(sample.ts);
      if (!Number.isFinite(tsMs)) tsMs = parseTimestampMs(sample.last_seen);
      if (!Number.isFinite(tsMs)) tsMs = fallbackTsMs;
      if (!Number.isFinite(tsMs)) return null;
      return {{ ts_ms: tsMs, lat, lon }};
    }}

    function mergeTrailPoints(existingPoints, incomingPoints, nowMs) {{
      const cutoffMs = nowMs - (trailPointWindowSeconds * 1000);
      const candidates = []
        .concat(Array.isArray(existingPoints) ? existingPoints : [])
        .concat(Array.isArray(incomingPoints) ? incomingPoints : [])
        .filter(
          (point) =>
            point
            && Number.isFinite(point.ts_ms)
            && Number.isFinite(point.lat)
            && Number.isFinite(point.lon)
            && point.ts_ms >= cutoffMs,
        )
        .sort((a, b) => a.ts_ms - b.ts_ms);

      const deduped = [];
      for (const point of candidates) {{
        const previous = deduped[deduped.length - 1];
        if (
          previous
          && Math.abs(previous.ts_ms - point.ts_ms) < 1000
          && Math.abs(previous.lat - point.lat) < 0.000001
          && Math.abs(previous.lon - point.lon) < 0.000001
        ) {{
          continue;
        }}
        deduped.push(point);
      }}
      return deduped;
    }}

    function updateTrailCacheFromTargets(activeTargets) {{
      const nowMs = Date.now();
      const activeIds = new Set();
      for (const target of activeTargets) {{
        const targetId = typeof target.target_id === "string" ? target.target_id : "";
        if (!targetId) continue;
        activeIds.add(targetId);
        const cached = trailCache.get(targetId) || {{}};
        const targetLastSeenMs = parseTimestampMs(target.last_seen);
        const incomingTrailPoints = [];

        const currentPoint = buildTrailPoint(target, targetLastSeenMs);
        if (currentPoint) incomingTrailPoints.push(currentPoint);
        for (const sample of normalizeRecentPositions(target.recent_positions)) {{
          const trailPoint = buildTrailPoint(sample, targetLastSeenMs);
          if (trailPoint) incomingTrailPoints.push(trailPoint);
        }}

        const nextTrailPoints = mergeTrailPoints(
          cached.trail_points,
          incomingTrailPoints,
          nowMs,
        );
        const nextLastSeen = target.last_seen || cached.last_seen || null;
        trailCache.set(targetId, {{
          ...cached,
          ...target,
          last_seen: nextLastSeen,
          trail_points: nextTrailPoints,
        }});
      }}

      const maxInactiveSeconds = trailStaleStartSeconds + trailStaleFadeSeconds;
      retainedTrailTargets = [];
      for (const [targetId, cached] of trailCache.entries()) {{
        const lastSeenMs = Date.parse(String(cached.last_seen || ""));
        const inactiveSeconds = Number.isFinite(lastSeenMs)
          ? (nowMs - lastSeenMs) / 1000
          : Number.POSITIVE_INFINITY;
        if (inactiveSeconds > maxInactiveSeconds) {{
          trailCache.delete(targetId);
          continue;
        }}
        if (!activeIds.has(targetId)) {{
          retainedTrailTargets.push(cached);
        }}
      }}
    }}

    function findTargetById(targetId) {{
      if (!targetId) return null;
      const inCache = trailCache.get(targetId);
      if (inCache) return inCache;
      const inActive = targets.find((item) => item && item.target_id === targetId);
      if (inActive) return inActive;
      const inRetained = retainedTrailTargets.find((item) => item && item.target_id === targetId);
      return inRetained || null;
    }}

    function normalizeHistoryPoints(observations) {{
      if (!Array.isArray(observations)) return [];
      return observations
        .map((item, index) => {{
          if (!item || typeof item !== "object") return null;
          const lat = toOptionalNumber(item.lat);
          const lon = toOptionalNumber(item.lon);
          if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
          const observedAtMs = parseTimestampMs(item.observed_at);
          return {{
            lat,
            lon,
            ts_ms: Number.isFinite(observedAtMs) ? observedAtMs : index,
          }};
        }})
        .filter(Boolean)
        .sort((a, b) => a.ts_ms - b.ts_ms);
    }}

    function pointsMatch(left, right) {{
      if (!left || !right) return false;
      const leftLat = toOptionalNumber(left.lat);
      const leftLon = toOptionalNumber(left.lon);
      const rightLat = toOptionalNumber(right.lat);
      const rightLon = toOptionalNumber(right.lon);
      if (
        !Number.isFinite(leftLat)
        || !Number.isFinite(leftLon)
        || !Number.isFinite(rightLat)
        || !Number.isFinite(rightLon)
      ) {{
        return false;
      }}
      return Math.abs(leftLat - rightLat) < 0.000001 && Math.abs(leftLon - rightLon) < 0.000001;
    }}

    function limitSelectedHistoryPoints(targetId, historyPoints) {{
      if (!Array.isArray(historyPoints) || historyPoints.length === 0) return [];

      let points = historyPoints.slice();
      const selectedTarget = findTargetById(targetId);
      if (selectedTarget) {{
        const currentPoint = {{
          lat: toOptionalNumber(selectedTarget.lat),
          lon: toOptionalNumber(selectedTarget.lon),
        }};
        const latestHistoryPoint = points[points.length - 1];
        if (pointsMatch(latestHistoryPoint, currentPoint)) {{
          points = points.slice(0, -1);
        }}
      }}

      if (points.length <= selectedHistoryPositionCount) return points;
      return points.slice(-selectedHistoryPositionCount);
    }}

    function collectSelectionPoints(targetId, includeHistory = false) {{
      const points = [];
      const selectedTarget = findTargetById(targetId);
      if (selectedTarget) {{
        const lat = toOptionalNumber(selectedTarget.lat);
        const lon = toOptionalNumber(selectedTarget.lon);
        if (Number.isFinite(lat) && Number.isFinite(lon)) {{
          points.push({{ lat, lon }});
        }}
      }}

      if (includeHistory) {{
        const historyPoints = selectedHistoryByTargetId.get(targetId);
        if (Array.isArray(historyPoints)) {{
          for (const point of historyPoints) {{
            const lat = toOptionalNumber(point.lat);
            const lon = toOptionalNumber(point.lon);
            if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
            points.push({{ lat, lon }});
          }}
        }}
      }}

      return points;
    }}

    function fitSelectionToView(targetId, options = {{}}) {{
      const includeHistory = Boolean(options.includeHistory);
      const points = collectSelectionPoints(targetId, includeHistory);
      if (points.length === 0) return false;

      let minLat = points[0].lat;
      let maxLat = points[0].lat;
      let minLon = points[0].lon;
      let maxLon = points[0].lon;
      for (const point of points) {{
        minLat = Math.min(minLat, point.lat);
        maxLat = Math.max(maxLat, point.lat);
        minLon = Math.min(minLon, point.lon);
        maxLon = Math.max(maxLon, point.lon);
      }}

      const nextCenter = {{
        lat: (minLat + maxLat) / 2,
        lon: (minLon + maxLon) / 2,
      }};
      let maxDxKm = 0;
      let maxDyKm = 0;
      for (const point of points) {{
        const {{ dx, dy }} = toOffsetKm(point.lat, point.lon, nextCenter);
        maxDxKm = Math.max(maxDxKm, Math.abs(dx));
        maxDyKm = Math.max(maxDyKm, Math.abs(dy));
      }}
      const requiredRangeKm = Math.max(maxDxKm, maxDyKm) * 1.15;
      const currentRangeKm = getViewMetrics().rangeKm;
      viewCenter = nextCenter;
      manualRangeKm = clampRangeKm(Math.max(currentRangeKm, requiredRangeKm, minRangeKm));
      return true;
    }}

    async function loadSelectedHistory(targetId) {{
      if (!targetId) return [];
      if (selectedHistoryByTargetId.has(targetId)) {{
        return selectedHistoryByTargetId.get(targetId) || [];
      }}

      try {{
        const response = await fetch(
          `history/${{encodeURIComponent(targetId)}}?limit=${{selectedHistoryRequestLimit}}`,
          {{ cache: "no-store" }},
        );
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        const payload = await response.json();
        const observations = Array.isArray(payload.observations) ? payload.observations : [];
        const historyPoints = limitSelectedHistoryPoints(
          targetId,
          normalizeHistoryPoints(observations),
        );
        selectedHistoryByTargetId.set(targetId, historyPoints);
        return historyPoints;
      }} catch (err) {{
        return [];
      }}
    }}

    async function selectTarget(targetId, shouldFitOnSelect = false) {{
      if (!targetId) return;
      if (selectedTargetId === targetId) {{
        selectedTargetId = null;
        pendingFitTargetId = null;
        draw();
        return;
      }}

      selectedTargetId = targetId;
      if (shouldFitOnSelect) {{
        pendingFitTargetId = targetId;
        fitSelectionToView(targetId, {{ includeHistory: false }});
      }} else {{
        pendingFitTargetId = null;
      }}
      draw();

      const activeSelection = targetId;
      await loadSelectedHistory(targetId);
      if (selectedTargetId !== activeSelection) return;
      if (pendingFitTargetId === activeSelection) {{
        fitSelectionToView(activeSelection, {{ includeHistory: true }});
        pendingFitTargetId = null;
      }}
      draw();
    }}

    function getTargetIdFromPanelEvent(event) {{
      if (!(event.target instanceof Element)) return null;
      const card = event.target.closest(".object-item[data-target-id]");
      if (!(card instanceof HTMLElement)) return null;
      const targetId = card.dataset.targetId;
      if (typeof targetId !== "string" || !targetId) return null;
      return targetId;
    }}

    function onObjectPanelClick(event, shouldFitOnSelect) {{
      const targetId = getTargetIdFromPanelEvent(event);
      if (!targetId) return;
      selectTarget(targetId, shouldFitOnSelect);
    }}

    function onObjectPanelKeyDown(event, shouldFitOnSelect) {{
      if (event.key !== "Enter" && event.key !== " ") return;
      const targetId = getTargetIdFromPanelEvent(event);
      if (!targetId) return;
      event.preventDefault();
      selectTarget(targetId, shouldFitOnSelect);
    }}

    function getTrailFadeProgress(lastSeenValue) {{
      if (!lastSeenValue) return 0;
      const lastSeenMs = Date.parse(String(lastSeenValue));
      if (!Number.isFinite(lastSeenMs)) return 0;
      const inactiveSeconds = (Date.now() - lastSeenMs) / 1000;
      if (!Number.isFinite(inactiveSeconds) || inactiveSeconds <= trailStaleStartSeconds) {{
        return 0;
      }}
      return Math.max(
        0,
        Math.min(1, (inactiveSeconds - trailStaleStartSeconds) / trailStaleFadeSeconds),
      );
    }}

    function trailOpacityForAgeRank(ageRank, fadeProgress) {{
      if (fadeProgress <= 0) return 1;
      const clampedRank = Math.max(0, Math.min(1, ageRank));
      const fadeStart = (1 - clampedRank) * 0.65;
      const localProgress = Math.max(0, Math.min(1, (fadeProgress - fadeStart) / (1 - fadeStart)));
      return 1 - localProgress;
    }}

    function drawRecentPositions(target, cx, cy, pxPerKm, radius, currentX = null, currentY = null) {{
      if (!radioConnected) return;

      const fallbackLastSeenMs = parseTimestampMs(target.last_seen);
      const rawTrailPoints = Array.isArray(target.trail_points) && target.trail_points.length > 0
        ? target.trail_points
        : normalizeRecentPositions(target.recent_positions)
          .map((sample) => buildTrailPoint(sample, fallbackLastSeenMs))
          .filter(Boolean);
      if (rawTrailPoints.length === 0) return;

      const cutoffMs = Date.now() - (trailPointWindowSeconds * 1000);
      const orderedTrailPoints = rawTrailPoints
        .filter(
          (point) =>
            point
            && Number.isFinite(point.ts_ms)
            && Number.isFinite(point.lat)
            && Number.isFinite(point.lon)
            && point.ts_ms >= cutoffMs,
        )
        .sort((a, b) => a.ts_ms - b.ts_ms);
      if (orderedTrailPoints.length === 0) return;

      let effectiveCurrentPoint = null;
      if (Number.isFinite(currentX) && Number.isFinite(currentY)) {{
        effectiveCurrentPoint = {{ x: currentX, y: currentY }};
      }} else {{
        const currentLat = toOptionalNumber(target.lat);
        const currentLon = toOptionalNumber(target.lon);
        if (Number.isFinite(currentLat) && Number.isFinite(currentLon)) {{
          const {{ dx, dy }} = toOffsetKm(currentLat, currentLon, viewCenter);
          effectiveCurrentPoint = {{
            x: cx + (dx * pxPerKm),
            y: cy - (dy * pxPerKm),
          }};
        }}
      }}

      const points = [];
      for (const sample of orderedTrailPoints) {{
        const {{ dx, dy }} = toOffsetKm(sample.lat, sample.lon, viewCenter);
        const x = cx + (dx * pxPerKm);
        const y = cy - (dy * pxPerKm);
        const insideRadar = isInsideRadarCircle(x, y, cx, cy, radius);
        if (!insideRadar) continue;
        if (effectiveCurrentPoint) {{
          const sameAsCurrent =
            ((x - effectiveCurrentPoint.x) * (x - effectiveCurrentPoint.x))
            + ((y - effectiveCurrentPoint.y) * (y - effectiveCurrentPoint.y)) < 1;
          if (sameAsCurrent) continue;
        }}
        points.push({{ x, y }});
      }}

      if (points.length === 0) return;

      points.reverse();
      const fadeProgress = getTrailFadeProgress(target.last_seen);

      ctx.save();
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      if (effectiveCurrentPoint) {{
        const newestOpacity = trailOpacityForAgeRank(0, fadeProgress);
        if (newestOpacity > 0.02) {{
          const clippedSegment = clipSegmentToCircle(
            effectiveCurrentPoint,
            points[0],
            cx,
            cy,
            radius,
          );
          if (clippedSegment) {{
            ctx.globalAlpha = newestOpacity;
            ctx.strokeStyle = liveTrailColorForAge(0);
            ctx.beginPath();
            ctx.moveTo(clippedSegment.start.x, clippedSegment.start.y);
            ctx.lineTo(clippedSegment.end.x, clippedSegment.end.y);
            ctx.stroke();
          }}
        }}
      }}
      for (let i = 0; i < points.length - 1; i += 1) {{
        const segmentAgeRank = points.length <= 1 ? 1 : (i + 1) / (points.length - 1);
        const segmentOpacity = trailOpacityForAgeRank(segmentAgeRank, fadeProgress);
        if (segmentOpacity <= 0.02) continue;
        ctx.globalAlpha = segmentOpacity;
        const color = liveTrailColorForAge(segmentAgeRank);
        const from = points[i];
        const to = points[i + 1];
        ctx.beginPath();
        ctx.moveTo(from.x, from.y);
        ctx.lineTo(to.x, to.y);
        ctx.strokeStyle = color;
        ctx.stroke();
      }}
      ctx.setLineDash([]);
      points.forEach((point, index) => {{
        const pointAgeRank = points.length <= 1 ? 1 : index / (points.length - 1);
        const pointOpacity = trailOpacityForAgeRank(pointAgeRank, fadeProgress);
        if (pointOpacity <= 0.02) return;
        ctx.globalAlpha = pointOpacity;
        const color = liveTrailColorForAge(pointAgeRank);
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(point.x, point.y, 1.6, 0, Math.PI * 2);
        ctx.fill();
      }});
      ctx.globalAlpha = 1;
      ctx.restore();
    }}

    function drawSelectedHistoryPath(targetId, cx, cy, pxPerKm, radius) {{
      if (!targetId) return;
      const historyPoints = selectedHistoryByTargetId.get(targetId);
      if (!Array.isArray(historyPoints) || historyPoints.length === 0) return;

      const canvasPoints = [];
      for (const point of historyPoints) {{
        const lat = toOptionalNumber(point.lat);
        const lon = toOptionalNumber(point.lon);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
        const {{ dx, dy }} = toOffsetKm(lat, lon, viewCenter);
        const x = cx + (dx * pxPerKm);
        const y = cy - (dy * pxPerKm);
        if (!isInsideRadarCircle(x, y, cx, cy, radius)) continue;
        canvasPoints.push({{ x, y }});
      }}
      if (canvasPoints.length === 0) return;

      let currentCanvasPoint = null;
      const currentTarget = findTargetById(targetId);
      if (currentTarget) {{
        const currentLat = toOptionalNumber(currentTarget.lat);
        const currentLon = toOptionalNumber(currentTarget.lon);
        if (Number.isFinite(currentLat) && Number.isFinite(currentLon)) {{
          const {{ dx, dy }} = toOffsetKm(currentLat, currentLon, viewCenter);
          const currentX = cx + (dx * pxPerKm);
          const currentY = cy - (dy * pxPerKm);
          if (isInsideRadarCircle(currentX, currentY, cx, cy, radius)) {{
            currentCanvasPoint = {{ x: currentX, y: currentY }};
          }}
        }}
      }}

      ctx.save();
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 3]);
      if (canvasPoints.length > 1) {{
        for (let i = 1; i < canvasPoints.length; i += 1) {{
          const ageRank = canvasPoints.length <= 1 ? 1 : 1 - (i / (canvasPoints.length - 1));
          const clippedSegment = clipSegmentToCircle(
            canvasPoints[i - 1],
            canvasPoints[i],
            cx,
            cy,
            radius,
          );
          if (!clippedSegment) continue;
          ctx.globalAlpha = 0.95;
          ctx.strokeStyle = trailColorForAge(ageRank, selectedTargetColor);
          ctx.beginPath();
          ctx.moveTo(clippedSegment.start.x, clippedSegment.start.y);
          ctx.lineTo(clippedSegment.end.x, clippedSegment.end.y);
          ctx.stroke();
        }}
      }}
      const lastHistoryPoint = canvasPoints[canvasPoints.length - 1];
      const shouldConnectToCurrent =
        currentCanvasPoint
        && lastHistoryPoint
        && (
          Math.abs(lastHistoryPoint.x - currentCanvasPoint.x) >= 1
          || Math.abs(lastHistoryPoint.y - currentCanvasPoint.y) >= 1
        );
      if (shouldConnectToCurrent) {{
        const clippedSegment = clipSegmentToCircle(
          lastHistoryPoint,
          currentCanvasPoint,
          cx,
          cy,
          radius,
        );
        if (clippedSegment) {{
          ctx.globalAlpha = 0.95;
          ctx.strokeStyle = trailColorForAge(0, selectedTargetColor);
          ctx.beginPath();
          ctx.moveTo(clippedSegment.start.x, clippedSegment.start.y);
          ctx.lineTo(clippedSegment.end.x, clippedSegment.end.y);
          ctx.stroke();
        }}
      }}
      canvasPoints.forEach((point, index) => {{
        const ageRank = canvasPoints.length <= 1 ? 0 : 1 - (index / (canvasPoints.length - 1));
        ctx.globalAlpha = 0.95;
        ctx.fillStyle = trailColorForAge(ageRank, selectedTargetColor);
        ctx.beginPath();
        ctx.arc(point.x, point.y, 1.9, 0, Math.PI * 2);
        ctx.fill();
      }});
      ctx.globalAlpha = 1;
      ctx.restore();
    }}

    function fixedObjectMarkerFontPx(rangeKm) {{
      const effectiveRange = Number.isFinite(rangeKm) ? Math.max(0, rangeKm) : 10;
      const zoomOutSteps = Math.max(0, Math.floor((effectiveRange - 10) / 10));
      return Math.max(7, 13 - zoomOutSteps);
    }}

    function drawFixedObjects(cx, cy, pxPerKm, radius, rangeKm) {{
      if (!Array.isArray(fixedObjects) || fixedObjects.length === 0) return;
      ctx.save();
      ctx.textBaseline = "middle";
      const markerFontPx = fixedObjectMarkerFontPx(rangeKm);
      const markerTextOffsetPx = Math.max(6, Math.round(markerFontPx * 0.65));
      for (const item of fixedObjects) {{
        const lat = toOptionalNumber(item.lat);
        const lon = toOptionalNumber(item.lon);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;

        const {{ dx, dy }} = toOffsetKm(lat, lon, viewCenter);
        const x = cx + (dx * pxPerKm);
        const y = cy - (dy * pxPerKm);
        const insideRadar = ((x - cx) * (x - cx)) + ((y - cy) * (y - cy)) <= (radius * radius);
        if (!insideRadar) continue;

        const maxVisibleRangeKm = toOptionalNumber(item.max_visible_range_km);
        if (Number.isFinite(maxVisibleRangeKm) && rangeKm > maxVisibleRangeKm) continue;

        const rawSymbol = typeof item.symbol === "string" ? item.symbol.trim() : "";
        const symbol = rawSymbol ? rawSymbol[0] : "O";
        const name = typeof item.name === "string" ? item.name.trim() : "";
        const nameLines = name ? name.split(/\\s+/).filter(Boolean) : [];

        ctx.fillStyle = radarRingColor;
        ctx.font = `${{markerFontPx}}px Courier New, monospace`;
        ctx.textAlign = "center";
        ctx.fillText(symbol, x, y);
        if (showFixedNames && nameLines.length > 0) {{
          const lineHeight = 12;
          const startY = y - ((nameLines.length - 1) * lineHeight * 0.5);
          ctx.fillStyle = "#9be89b";
          ctx.font = "12px Courier New, monospace";
          ctx.textAlign = "left";
          nameLines.forEach((line, index) => {{
            ctx.fillText(line, x + markerTextOffsetPx, startY + (index * lineHeight));
          }});
        }}
      }}
      ctx.restore();
    }}

    function normalizeMapContourFeatures(features) {{
      if (!Array.isArray(features)) return [];
      return features.filter((feature) => feature && typeof feature === "object");
    }}

    function drawLineCoordinates(coordinates, cx, cy, pxPerKm) {{
      if (!Array.isArray(coordinates) || coordinates.length < 2) return;
      let segmentOpen = false;
      for (const coordinate of coordinates) {{
        if (!Array.isArray(coordinate) || coordinate.length < 2) {{
          segmentOpen = false;
          continue;
        }}
        const lon = toOptionalNumber(coordinate[0]);
        const lat = toOptionalNumber(coordinate[1]);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) {{
          segmentOpen = false;
          continue;
        }}
        const {{ dx, dy }} = toOffsetKm(lat, lon, viewCenter);
        const x = cx + (dx * pxPerKm);
        const y = cy - (dy * pxPerKm);
        if (!segmentOpen) {{
          ctx.moveTo(x, y);
          segmentOpen = true;
        }} else {{
          ctx.lineTo(x, y);
        }}
      }}
    }}

    function drawMapContours(cx, cy, pxPerKm, radius) {{
      if (!showMapContours) return;
      if (!Array.isArray(mapContours) || mapContours.length === 0) return;

      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, radius, 0, Math.PI * 2);
      ctx.clip();
      ctx.strokeStyle = "#143314";
      ctx.lineWidth = 1;
      ctx.globalAlpha = 0.9;
      for (const feature of mapContours) {{
        const geometry = feature.geometry;
        if (!geometry || typeof geometry !== "object") continue;
        const geometryType = geometry.type;
        const coordinates = geometry.coordinates;
        ctx.beginPath();
        if (geometryType === "LineString") {{
          drawLineCoordinates(coordinates, cx, cy, pxPerKm);
        }} else if (geometryType === "MultiLineString" && Array.isArray(coordinates)) {{
          for (const line of coordinates) {{
            drawLineCoordinates(line, cx, cy, pxPerKm);
          }}
        }} else {{
          continue;
        }}
        ctx.stroke();
      }}
      ctx.globalAlpha = 1;
      ctx.restore();
    }}

    function toOptionalNumber(value) {{
      if (typeof value === "number") return value;
      if (typeof value === "string" && value.trim() !== "") {{
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : NaN;
      }}
      return NaN;
    }}

    function formatOptional(value, decimals = 2) {{
      if (!Number.isFinite(value)) return "-";
      return value.toFixed(decimals);
    }}

    function escapeHtml(value) {{
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }}

    function targetTypeIcon(kind) {{
      return kind === "vessel" ? "⛵" : "✈";
    }}

    function targetDisplayLabel(target) {{
      if (!target || typeof target !== "object") return "";
      const label = typeof target.label === "string" ? target.label.trim() : "";
      if (label) return label;
      const targetId = typeof target.target_id === "string" ? target.target_id.trim() : "";
      return targetId;
    }}

    function drawMapTargetLabel(label, x, y, color) {{
      if (typeof label !== "string" || !label.trim()) return;
      ctx.save();
      ctx.font = "12px Courier New, monospace";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillStyle = color || "#9be89b";
      ctx.fillText(label, x + 8, y - 10);
      ctx.restore();
    }}

    function matchesTargetTypeFilter(target, filterValue) {{
      if (filterValue === "all") return true;
      if (!target || typeof target !== "object") return false;
      return target.kind === filterValue;
    }}

    function renderObjectCards(items, emptyText) {{
      if (items.length === 0) {{
        return `<div class="objects-empty">${{escapeHtml(emptyText)}}</div>`;
      }}

      return items
        .map((target) => {{
          const targetId = typeof target.target_id === "string" ? target.target_id : "";
          const label = target.label || target.target_id || "okänt";
          const kind = typeof target.kind === "string" ? target.kind : "";
          const lat = toOptionalNumber(target.lat);
          const lon = toOptionalNumber(target.lon);
          const speed = toOptionalNumber(target.speed);
          const altitude = toOptionalNumber(target.altitude);
          const lastSeen = target.last_seen ? String(target.last_seen) : "-";
          const positionText =
            Number.isFinite(lat) && Number.isFinite(lon)
              ? `${{lat.toFixed(6)}}, ${{lon.toFixed(6)}}`
              : "-";
          const detailLines = [];
          if (positionText !== "-") {{
            detailLines.push(`<div>position: ${{escapeHtml(positionText)}}</div>`);
          }}
          const speedText = formatOptional(speed);
          if (speedText !== "-") {{
            detailLines.push(`<div>last_speed: ${{escapeHtml(speedText)}}</div>`);
          }}
          const altitudeText = formatOptional(altitude);
          if (altitudeText !== "-") {{
            detailLines.push(`<div>last_altitude: ${{escapeHtml(altitudeText)}}</div>`);
          }}
          if (lastSeen !== "-") {{
            detailLines.push(`<div>last_seen: ${{escapeHtml(lastSeen)}}</div>`);
          }}
          const selectedClass = targetId && selectedTargetId === targetId ? " selected" : "";
          const targetAttr = targetId
            ? ` data-target-id="${{escapeHtml(targetId)}}" role="button" tabindex="0"`
            : "";

          return `
            <div class="object-item${{selectedClass}}"${{targetAttr}}>
              <div class="object-label-row">
                <span class="object-type-icon" aria-hidden="true">${{escapeHtml(targetTypeIcon(kind))}}</span>
                <div class="object-label">${{escapeHtml(label)}}</div>
              </div>
              ${{detailLines.join("")}}
            </div>
          `;
        }})
        .join("");
    }}

    function renderObjectsPanel(visibleTargets, outsideTargets) {{
      const filteredVisibleTargets = visibleTargets.filter((target) =>
        matchesTargetTypeFilter(target, targetTypeFilter)
      );
      const filteredOutsideTargets = outsideTargets.filter((target) =>
        matchesTargetTypeFilter(target, targetTypeFilter)
      );
      objectsSummary.textContent = `${{filteredVisibleTargets.length}} synliga objekt`;
      outsideObjectsSummary.textContent = `${{filteredOutsideTargets.length}} objekt utanför aktivt område`;
      objectsList.innerHTML = renderObjectCards(filteredVisibleTargets, "Inga objekt i aktuell vy.");
      outsideObjectsList.innerHTML = renderObjectCards(
        filteredOutsideTargets,
        "Inga objekt utanför aktivt område.",
      );
    }}

    function clearMapContourRetryTimer() {{
      if (mapContourRetryTimer !== null) {{
        window.clearTimeout(mapContourRetryTimer);
        mapContourRetryTimer = null;
      }}
    }}

    function scheduleMapContourRetry() {{
      clearMapContourRetryTimer();
      mapContourRetryTimer = window.setTimeout(() => {{
        mapContourRetryTimer = null;
        mapContourPendingKey = null;
        void loadMapContoursForView(getViewMetrics().rangeKm);
      }}, minAutoRefreshMs);
    }}

    async function loadMapContoursForView(rangeKm) {{
      if (!showMapContours || mapContourRequestInFlight) return;
      const request = mapContourRequestKeyForView(rangeKm);
      if (
        request.key === mapContourLoadedKey
        || request.key === mapContourRequestKey
        || request.key === mapContourPendingKey
      ) return;

      mapContourRequestInFlight = true;
      mapContourRequestKey = request.key;
      try {{
        const params = new URLSearchParams({{
          source: mapContourSource,
          bbox: request.bbox.map((value) => value.toFixed(6)).join(","),
          range_km: rangeKm.toFixed(3),
        }});
        const response = await fetch(`ui/map-contours?${{params.toString()}}`, {{
          cache: "no-store",
        }});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        const payload = await response.json();
        mapContours = normalizeMapContourFeatures(payload.features);
        mapContourSource = typeof payload.source === "string" ? payload.source : mapContourSource;
        mapContourStatus = typeof payload.status === "string" ? payload.status : "ok";
        mapContourError = typeof payload.error === "string" && payload.error
          ? payload.error
          : null;
        clearMapContourRetryTimer();
        if (mapContourStatus === "pending") {{
          mapContourPendingKey = request.key;
          mapContourLoadedKey = null;
          scheduleMapContourRetry();
        }} else if (mapContourStatus === "ok") {{
          mapContourPendingKey = null;
          mapContourLoadedKey = request.key;
        }} else {{
          mapContourPendingKey = null;
          mapContourLoadedKey = null;
        }}
      }} catch (err) {{
        mapContours = [];
        mapContourStatus = "error";
        mapContourError = err instanceof Error ? err.message : String(err);
        mapContourPendingKey = null;
        mapContourLoadedKey = null;
        clearMapContourRetryTimer();
      }} finally {{
        mapContourRequestInFlight = false;
        mapContourRequestKey = null;
        draw();
      }}
    }}

    function ensureMapContoursForView(rangeKm) {{
      if (!showMapContours) return;
      void loadMapContoursForView(rangeKm);
    }}

    function isDynamicTrackTarget(target) {{
      if (!target || typeof target !== "object") return false;
      const source = typeof target.source === "string" ? target.source : "";
      const kind = typeof target.kind === "string" ? target.kind : "";
      const validSource = source === "adsb" || source === "ais" || source === "ogn";
      const validKind = kind === "aircraft" || kind === "vessel";
      return validSource && validKind;
    }}

    function draw() {{
      const {{
        width,
        height,
        cx,
        cy,
        radius,
        autoRangeKm,
        rangeKm,
        pxPerKm,
      }} = getViewMetrics();

      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#000000";
      ctx.fillRect(0, 0, width, height);

      ctx.strokeStyle = radarRingColor;
      ctx.lineWidth = 1;
      const ringSpacingKm = rangeKm / radarRingCount;
      for (let i = 1; i <= radarRingCount; i += 1) {{
        ctx.beginPath();
        ctx.arc(cx, cy, i * ringSpacingKm * pxPerKm, 0, Math.PI * 2);
        ctx.stroke();
      }}

      ctx.strokeStyle = radarRingColor;
      ctx.beginPath();
      ctx.moveTo(cx - radius, cy);
      ctx.lineTo(cx + radius, cy);
      ctx.moveTo(cx, cy - radius);
      ctx.lineTo(cx, cy + radius);
      ctx.stroke();

      ctx.fillStyle = "#d3d3d3";
      ctx.beginPath();
      ctx.arc(cx, cy, 5, 0, Math.PI * 2);
      ctx.fill();

      drawMapContours(cx, cy, pxPerKm, radius);
      drawFixedObjects(cx, cy, pxPerKm, radius, rangeKm);
      drawSelectedHistoryPath(selectedTargetId, cx, cy, pxPerKm, radius);

      ctx.font = "bold 16px Courier New, monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";

      let visible = 0;
      const visibleTargets = [];
      const outsideTargets = [];
      for (const target of targets) {{
        if (typeof target.lat !== "number" || typeof target.lon !== "number") continue;
        const targetId = typeof target.target_id === "string" ? target.target_id : "";
        const isSelected = selectedTargetId !== null && targetId === selectedTargetId;
        const trackedTarget = trailCache.get(target.target_id) || target;
        const speed = toOptionalNumber(target.speed);
        if (!showLowSpeed && Number.isFinite(speed) && speed < 1) continue;
        const {{ dx, dy }} = toOffsetKm(target.lat, target.lon, viewCenter);
        const x = cx + (dx * pxPerKm);
        const y = cy - (dy * pxPerKm);
        const insideRadar = ((x - cx) * (x - cx)) + ((y - cy) * (y - cy)) <= (radius * radius);
        if (!insideRadar) {{
          drawRecentPositions(trackedTarget, cx, cy, pxPerKm, radius);
          outsideTargets.push(target);
          continue;
        }}
        const course = toOptionalNumber(target.course);
        drawRecentPositions(trackedTarget, cx, cy, pxPerKm, radius, x, y);
        const markerColor = isSelected ? selectedTargetColor : liveTargetColor;
        drawCourseVector(x, y, course, speed, markerColor);
        ctx.fillStyle = markerColor;
        const symbol = target.kind === "vessel" ? "*" : "+";
        ctx.fillText(symbol, x, y);
        if (showTargetLabels) {{
          drawMapTargetLabel(targetDisplayLabel(target), x, y, markerColor);
        }}
        visibleTargets.push(target);
        visible += 1;
      }}
      for (const retainedTarget of retainedTrailTargets) {{
        drawRecentPositions(retainedTarget, cx, cy, pxPerKm, radius);
      }}

      renderObjectsPanel(visibleTargets, outsideTargets);
      drawSelectionBox();
      syncRangeInput(rangeKm);
      ensureMapContoursForView(rangeKm);
      const status = error ? `Error: ${{error}}` : `${{visible}} visible / ${{targets.length}} total`;
      const contourErrorText = showMapContours && mapContourError ? ` | Konturer: ${{mapContourError}}` : "";
      meta.textContent = `View: ${{viewCenter.lat.toFixed(6)}}, ${{viewCenter.lon.toFixed(6)}} | Ringavstand: ${{ringSpacingKm.toFixed(2)}} km | ${{status}}${{contourErrorText}}`;
    }}

    async function loadTargets() {{
      if (requestInFlight) return;
      requestInFlight = true;
      try {{
        const response = await fetch("ui/targets-latest", {{ cache: "no-store" }});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        const payload = await response.json();
        lastScannerState = normalizeScannerState(payload.scanner);
        if (
          payload
          && payload.scanner
          && typeof payload.scanner === "object"
          && typeof payload.scanner.scan_mode === "string"
        ) {{
          scanMode = payload.scanner.scan_mode;
          syncScanModeSelect();
        }}
        const loadedTargets = Array.isArray(payload.targets) ? payload.targets : [];
        targets = loadedTargets.filter(isDynamicTrackTarget);
        updateTrailCacheFromTargets(targets);
        radioConnected = Boolean(payload.radio_connected);
        const latestLastSeenMs = deriveLatestLastSeenMs(targets);
        if (Number.isFinite(latestLastSeenMs)) {{
          if (Number.isFinite(lastSeenWatermarkMs) && latestLastSeenMs > lastSeenWatermarkMs) {{
            pushObservedUpdateInterval(latestLastSeenMs - lastSeenWatermarkMs);
            nextPollMs = computeAdaptivePollMs();
            lastDataChangeAtMs = Date.now();
          }} else if (!Number.isFinite(lastSeenWatermarkMs)) {{
            lastDataChangeAtMs = Date.now();
          }}
          lastSeenWatermarkMs = Number.isFinite(lastSeenWatermarkMs)
            ? Math.max(lastSeenWatermarkMs, latestLastSeenMs)
            : latestLastSeenMs;
        }} else if (targets.length === 0) {{
          nextPollMs = clampPollMs(Math.max(nextPollMs, defaultPollMs * 1.5));
        }}
        nextPollMs = blendPollMsWithBandTiming(nextPollMs, computeBandAwarePollMs(lastScannerState));
        error = null;
      }} catch (err) {{
        error = err instanceof Error ? err.message : String(err);
        radioConnected = false;
        nextPollMs = clampPollMs(nextPollMs * pollBackoffFactor);
      }} finally {{
        requestInFlight = false;
        const idleMs = Date.now() - lastDataChangeAtMs;
        if (idleMs >= nextPollMs * 2) {{
          nextPollMs = clampPollMs(nextPollMs * pollBackoffFactor);
        }}
        const bandAwareAfterIdleMs = computeBandAwarePollMs(lastScannerState);
        if (Number.isFinite(bandAwareAfterIdleMs)) {{
          nextPollMs = Math.min(nextPollMs, bandAwareAfterIdleMs);
        }}
        draw();
        scheduleNextLoad(nextPollMs);
      }}
    }}

    window.addEventListener("resize", draw);
    zoomInButton.addEventListener("click", decreaseRange);
    zoomOutButton.addEventListener("click", increaseRange);
    zoomResetButton.addEventListener("click", resetZoom);
    if (scanModeSelect instanceof HTMLSelectElement) {{
      scanModeSelect.addEventListener("change", () => {{
        void setScanMode(scanModeSelect.value);
      }});
    }}
    rangeInput.addEventListener("change", applyRangeInput);
    rangeInput.addEventListener("blur", applyRangeInput);
    rangeInput.addEventListener("keydown", (event) => {{
      if (event.key === "Enter") {{
        event.preventDefault();
        applyRangeInput();
      }}
    }});
    objectsList.addEventListener("click", (event) => {{
      onObjectPanelClick(event, false);
    }});
    outsideObjectsList.addEventListener("click", (event) => {{
      onObjectPanelClick(event, true);
    }});
    objectsList.addEventListener("keydown", (event) => {{
      onObjectPanelKeyDown(event, false);
    }});
    outsideObjectsList.addEventListener("keydown", (event) => {{
      onObjectPanelKeyDown(event, true);
    }});
    showLowSpeedCheckbox.addEventListener("change", () => {{
      showLowSpeed = showLowSpeedCheckbox.checked;
      draw();
    }});
    if (targetTypeFilterSelect instanceof HTMLSelectElement) {{
      targetTypeFilterSelect.addEventListener("change", () => {{
        targetTypeFilter = targetTypeFilterSelect.value;
        if (selectedTargetId) {{
          const selectedTarget = findTargetById(selectedTargetId);
          if (!matchesTargetTypeFilter(selectedTarget, targetTypeFilter)) {{
            selectedTargetId = null;
            pendingFitTargetId = null;
          }}
        }}
        draw();
      }});
    }}
    showFixedNamesCheckbox.addEventListener("change", () => {{
      showFixedNames = showFixedNamesCheckbox.checked;
      draw();
    }});
    showTargetLabelsCheckbox.addEventListener("change", () => {{
      showTargetLabels = showTargetLabelsCheckbox.checked;
      draw();
    }});
    showMapContoursCheckbox.addEventListener("change", () => {{
      showMapContours = showMapContoursCheckbox.checked;
      if (!showMapContours) {{
        mapContourError = null;
        mapContourPendingKey = null;
        clearMapContourRetryTimer();
      }}
      draw();
    }});
    canvas.addEventListener(
      "wheel",
      (event) => {{
        event.preventDefault();
        if (event.deltaY < 0) {{
          decreaseRange();
        }} else {{
          increaseRange();
        }}
      }},
      {{ passive: false }},
    );
    canvas.addEventListener("mousedown", beginSelection);
    canvas.addEventListener("mousemove", updateSelection);
    canvas.addEventListener("mouseleave", cancelSelection);
    window.addEventListener("mouseup", endSelection);
    document.addEventListener("visibilitychange", () => {{
      if (document.visibilityState === "visible") {{
        nextPollMs = Math.min(nextPollMs, defaultPollMs);
        scheduleNextLoad(minPollMs);
      }}
    }});
    draw();
    void loadTargets();
  </script>
</body>
</html>
"""


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
    return f"""<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{service_name} Historiska Spår</title>
  <style>
    :root {{
      --radar-bg: #000000;
      --radar-fg: #E9FCE9;
      --radar-ring: #2c7a2c;
      --radar-center: #d3d3d3;
      --panel-fg: #9be89b;
      --panel-dim: #5b9e5b;
    }}
    html, body {{
      margin: 0;
      width: 100%;
      height: 100%;
      background: var(--radar-bg);
      color: var(--radar-fg);
      font-family: "Courier New", Courier, monospace;
    }}
    .layout {{
      display: flex;
      flex-direction: column;
      height: 100%;
    }}
    .hud {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      padding: 10px 14px;
      border-bottom: 1px solid #154815;
      color: var(--panel-fg);
      font-size: 14px;
      letter-spacing: 0.03em;
    }}
    .hud-title {{
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .view-links {{
      display: inline-flex;
      gap: 10px;
      font-size: 12px;
      letter-spacing: normal;
    }}
    .view-links a {{
      color: var(--panel-dim);
      text-decoration: none;
    }}
    .view-links a[aria-current="page"] {{
      color: var(--panel-fg);
    }}
    .view-links a:hover {{
      color: var(--radar-fg);
    }}
    .hud .dim {{
      color: var(--panel-dim);
    }}
    .hud-right {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .zoom-controls {{
      display: inline-flex;
      border: 1px solid #226322;
      align-items: stretch;
      background: #051805;
    }}
    .zoom-controls button {{
      background: transparent;
      color: var(--panel-fg);
      border: 0;
      border-right: 1px solid #226322;
      min-width: 40px;
      height: 28px;
      cursor: pointer;
      font: inherit;
    }}
    .zoom-controls button:hover {{
      background: #0a260a;
    }}
    .zoom-controls input {{
      width: 62px;
      border: 0;
      border-right: 1px solid #226322;
      background: #020b02;
      color: var(--panel-fg);
      text-align: right;
      padding: 0 8px;
      font: inherit;
    }}
    .zoom-controls input:focus {{
      outline: none;
      background: #0a1f0a;
    }}
    .zoom-controls .range-unit {{
      display: inline-flex;
      align-items: center;
      padding: 0 8px;
      color: var(--panel-dim);
      border-right: 1px solid #226322;
      font-size: 12px;
    }}
    .zoom-controls > *:last-child {{
      border-right: 0;
    }}
    .toggle-control {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--panel-dim);
      font-size: 12px;
      user-select: none;
      white-space: nowrap;
    }}
    .screen {{
      flex: 1;
      min-height: 0;
      padding: 12px;
      display: flex;
      gap: 12px;
    }}
    .radar-wrap {{
      flex: 1;
      min-width: 0;
    }}
    .side-panel {{
      width: 360px;
      max-width: 44vw;
      border: 1px solid #154815;
      background: #020b02;
      display: flex;
      flex-direction: column;
      min-height: 0;
    }}
    .side-panel-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      padding: 10px;
      border-bottom: 1px solid #154815;
      color: var(--panel-fg);
      font-size: 13px;
    }}
    .side-panel-summary {{
      padding: 8px 10px;
      color: var(--panel-dim);
      border-bottom: 1px solid #103810;
      font-size: 12px;
    }}
    .objects-list {{
      flex: 1;
      overflow: auto;
      padding: 8px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .object-item {{
      border: 1px solid #124212;
      background: #041104;
      padding: 8px;
      color: var(--panel-fg);
      font-size: 12px;
      line-height: 1.45;
      cursor: pointer;
      transition: border-color 120ms ease, background 120ms ease;
    }}
    .object-item:hover {{
      border-color: #2f8b2f;
    }}
    .object-item.selected {{
      border-color: #9e2f2f;
      background: #200808;
    }}
    .object-label {{
      color: var(--radar-fg);
      font-size: 13px;
      margin-bottom: 4px;
    }}
    .object-label-row {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 4px;
    }}
    .object-type-icon {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 18px;
      color: var(--panel-dim);
      font-size: 14px;
      line-height: 1;
    }}
    .object-item.selected .object-label {{
      color: #ff9c9c;
    }}
    .objects-empty {{
      padding: 10px;
      color: var(--panel-dim);
      font-size: 12px;
    }}
    @media (max-width: 1000px) {{
      .screen {{
        flex-direction: column;
      }}
      .side-panel {{
        width: auto;
        max-width: none;
        min-height: 240px;
      }}
    }}
    canvas {{
      display: block;
      width: 100%;
      height: 100%;
      background: var(--radar-bg);
      border: 1px solid #154815;
    }}
  </style>
</head>
<body>
  <div class="layout">
    <div class="hud">
      <div class="hud-title">
        <div>{service_name} / HISTORY VIEW</div>
        <div class="view-links">
          <a href="./">Live radar</a>
          <a href="history-radar" aria-current="page">Historiska spår</a>
        </div>
      </div>
      <div class="hud-right">
        <div class="zoom-controls">
          <button id="zoomOut" type="button" aria-label="Zooma ut">-</button>
          <input id="rangeInput" type="text" inputmode="decimal" value="10" aria-label="Range km" />
          <span class="range-unit">km</span>
          <button id="zoomIn" type="button" aria-label="Zooma in">+</button>
          <button id="zoomReset" type="button" aria-label="Reset range">Hem</button>
        </div>
        <label class="toggle-control" for="showFixedNames">
          <input id="showFixedNames" type="checkbox" checked />
          Visa namn fasta punkter
        </label>
        <label class="toggle-control" for="showTargetLabels">
          <input id="showTargetLabels" type="checkbox" />
          Visa labels objekt
        </label>
        <label class="toggle-control" for="showMapContours">
          <input id="showMapContours" type="checkbox" checked />
          Visa kust/sjö-konturer
        </label>
        <div id="meta" class="dim">Center: {center_lat:.6f}, {center_lon:.6f}</div>
      </div>
    </div>
    <div class="screen">
      <div class="radar-wrap">
        <canvas id="radar"></canvas>
      </div>
      <aside class="side-panel">
        <div class="side-panel-head">
          <div>Historiska objekt</div>
          <button id="historyTimeFilterButton" class="panel-action-button" type="button">
            Tidsfilter
          </button>
        </div>
        <div class="side-panel-summary">
          <div class="panel-filter-row">
            <label class="panel-filter-control" for="historyTargetTypeFilter">
              Typ
              <select id="historyTargetTypeFilter" aria-label="Filtrera historisk objekttyp">
                <option value="all">Alla</option>
                <option value="aircraft">Flygplan</option>
                <option value="vessel">Båtar</option>
              </select>
            </label>
            <label class="panel-filter-control" for="historyMinSpeedFilter">
              Min topphastighet
              <input
                id="historyMinSpeedFilter"
                type="number"
                min="0"
                step="1"
                value="0"
                inputmode="decimal"
                aria-label="Filtrera historiska objekt på högsta rapporterade hastighet"
              />
            </label>
          </div>
        </div>
        <div id="historyTimeFilterSummary" class="side-panel-summary panel-filter-status">
          Tidsfilter: Alla spår
        </div>
        <div id="historyObjectsSummary" class="side-panel-summary">0 objekt med historik</div>
        <div id="historyObjectsList" class="objects-list">
          <div class="objects-empty">Inga historiska objekt.</div>
        </div>
      </aside>
    </div>
  </div>
  <div id="historyTimeFilterModal" class="modal-backdrop" hidden aria-hidden="true">
    <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="historyTimeFilterTitle">
      <div class="modal-head">
        <div id="historyTimeFilterTitle" class="modal-title">Globalt tidsfilter</div>
        <div class="modal-copy">
          Välj ett globalt tidsintervall som ska gälla för alla historiska spår och för objektlistan.
        </div>
      </div>
      <div class="modal-body">
        <label class="panel-filter-control" for="historyObservedAfterInput">
          Visa spår fr.o.m.
          <input
            id="historyObservedAfterInput"
            type="datetime-local"
            step="60"
            aria-label="Välj starttid för historiska spår"
          />
        </label>
        <label class="panel-filter-control" for="historyObservedBeforeInput">
          Visa spår t.o.m.
          <input
            id="historyObservedBeforeInput"
            type="datetime-local"
            step="60"
            aria-label="Välj sluttid för historiska spår"
          />
        </label>
        <div class="modal-actions">
          <button id="historyTimeFilterCancelButton" class="panel-action-button" type="button">
            Avbryt
          </button>
          <button id="historyTimeFilterClearButton" class="panel-action-button" type="button">
            Alla spår
          </button>
          <button id="historyTimeFilterApplyButton" class="panel-action-button" type="button">
            Använd
          </button>
        </div>
      </div>
    </div>
  </div>
  <script>
    const homeCenter = {{ lat: {center_lat:.8f}, lon: {center_lon:.8f} }};
    const kmPerDegLat = 110.574;
    const minAutoRefreshMs = 2000;
    const defaultRangeKm = 10.0;
    const radarRingCount = 5;
    const minRangeKm = 0.2;
    const maxRangeKm = 500.0;
    const radarRingColor = "#2c7a2c";
    const selectedTargetColor = "#ff4d4d";
    const defaultMapSource = {json.dumps(default_map_source)};
    const fixedObjects = {fixed_objects_json};
    const canvas = document.getElementById("radar");
    const meta = document.getElementById("meta");
    const zoomInButton = document.getElementById("zoomIn");
    const zoomOutButton = document.getElementById("zoomOut");
    const zoomResetButton = document.getElementById("zoomReset");
    const rangeInput = document.getElementById("rangeInput");
    const showFixedNamesCheckbox = document.getElementById("showFixedNames");
    const showTargetLabelsCheckbox = document.getElementById("showTargetLabels");
    const showMapContoursCheckbox = document.getElementById("showMapContours");
    const historyTargetTypeFilterSelect = document.getElementById("historyTargetTypeFilter");
    const historyMinSpeedFilterInput = document.getElementById("historyMinSpeedFilter");
    const historyTimeFilterButton = document.getElementById("historyTimeFilterButton");
    const historyTimeFilterSummary = document.getElementById("historyTimeFilterSummary");
    const historyObjectsSummary = document.getElementById("historyObjectsSummary");
    const historyObjectsList = document.getElementById("historyObjectsList");
    const historyTimeFilterModal = document.getElementById("historyTimeFilterModal");
    const historyObservedAfterInput = document.getElementById("historyObservedAfterInput");
    const historyObservedBeforeInput = document.getElementById("historyObservedBeforeInput");
    const historyTimeFilterApplyButton = document.getElementById("historyTimeFilterApplyButton");
    const historyTimeFilterClearButton = document.getElementById("historyTimeFilterClearButton");
    const historyTimeFilterCancelButton = document.getElementById("historyTimeFilterCancelButton");
    const ctx = canvas.getContext("2d");

    let historyTargets = [];
    let historyTargetsInView = new Set();
    let selectedTargetId = null;
    let selectedTargetKind = null;
    let historyTargetTypeFilter = "all";
    let historyMinSpeedFilter = 0;
    let selectedTargetLabel = null;
    let selectedPositionCount = 0;
    let selectedLastSeen = null;
    let selectedHistoryPoints = [];
    let historyObservedAfterIso = null;
    let historyObservedBeforeIso = null;
    let error = null;
    let viewCenter = {{ ...homeCenter }};
    let manualRangeKm = defaultRangeKm;
    let dragStart = null;
    let dragCurrent = null;
    let showFixedNames = true;
    let showTargetLabels = false;
    let showMapContours = true;
    let mapContours = [];
    let mapContourSource = defaultMapSource;
    let mapContourStatus = "idle";
    let mapContourError = null;
    let mapContourRequestKey = null;
    let mapContourLoadedKey = null;
    let mapContourPendingKey = null;
    let mapContourRequestInFlight = false;
    let mapContourRetryTimer = null;
    let historyTargetsInViewRequestKey = null;
    let historyTargetsInViewLoadedKey = null;
    let historyTargetsInViewRequestInFlight = false;

    function clampUnitInterval(value) {{
      if (!Number.isFinite(value)) return 0;
      return Math.max(0, Math.min(1, value));
    }}

    function parseHexColor(hexColor) {{
      if (typeof hexColor !== "string") return null;
      const normalized = hexColor.trim();
      const match = /^#([0-9a-f]{6})$/i.exec(normalized);
      if (!match) return null;
      return {{
        red: Number.parseInt(match[1].slice(0, 2), 16),
        green: Number.parseInt(match[1].slice(2, 4), 16),
        blue: Number.parseInt(match[1].slice(4, 6), 16),
      }};
    }}

    function toHexChannel(value) {{
      return Math.round(clampUnitInterval(value / 255) * 255)
        .toString(16)
        .padStart(2, "0");
    }}

    function blendHexColors(fromColor, toColor, amount) {{
      const from = parseHexColor(fromColor);
      const to = parseHexColor(toColor);
      if (!from || !to) return toColor || fromColor || radarRingColor;
      const mix = clampUnitInterval(amount);
      return `#${{toHexChannel(from.red + ((to.red - from.red) * mix))}}${{toHexChannel(from.green + ((to.green - from.green) * mix))}}${{toHexChannel(from.blue + ((to.blue - from.blue) * mix))}}`;
    }}

    function trailColorForAge(ageRank, targetColor = selectedTargetColor) {{
      const clampedAge = clampUnitInterval(ageRank);
      const emphasis = Math.pow(1 - clampedAge, 0.85);
      return blendHexColors(radarRingColor, targetColor, emphasis);
    }}
    function clampRangeKm(value) {{
      return Math.max(minRangeKm, Math.min(maxRangeKm, value));
    }}

    function kmPerDegLon(lat) {{
      return 111.320 * Math.cos((lat * Math.PI) / 180);
    }}

    function toOffsetKm(lat, lon, referenceCenter) {{
      const dy = (lat - referenceCenter.lat) * kmPerDegLat;
      const dx = (lon - referenceCenter.lon) * kmPerDegLon(referenceCenter.lat);
      return {{ dx, dy }};
    }}

    function offsetKmToLatLon(dxKm, dyKm, referenceCenter) {{
      const lat = referenceCenter.lat + (dyKm / kmPerDegLat);
      const lon = referenceCenter.lon + (dxKm / kmPerDegLon(referenceCenter.lat));
      return {{ lat, lon }};
    }}

    function toOptionalNumber(value) {{
      if (typeof value === "number") return value;
      if (typeof value === "string" && value.trim() !== "") {{
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : NaN;
      }}
      return NaN;
    }}

    function parseTimestampMs(value) {{
      if (typeof value === "number" && Number.isFinite(value)) return value;
      if (typeof value !== "string" || !value.trim()) return NaN;
      const parsed = Date.parse(value);
      return Number.isFinite(parsed) ? parsed : NaN;
    }}

    function formatHistoryFilterDateLabel(isoValue) {{
      if (typeof isoValue !== "string" || !isoValue.trim()) return "";
      const parsed = new Date(isoValue);
      if (!Number.isFinite(parsed.getTime())) return "";
      return parsed.toLocaleString("sv-SE", {{
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      }});
    }}

    function toDatetimeLocalValue(isoValue) {{
      if (typeof isoValue !== "string" || !isoValue.trim()) return "";
      const parsed = new Date(isoValue);
      if (!Number.isFinite(parsed.getTime())) return "";
      const pad = (value) => String(value).padStart(2, "0");
      return [
        parsed.getFullYear(),
        pad(parsed.getMonth() + 1),
        pad(parsed.getDate()),
      ].join("-") + `T${{pad(parsed.getHours())}}:${{pad(parsed.getMinutes())}}`;
    }}

    function parseObservedBeforeInputValue(value) {{
      if (typeof value !== "string") return null;
      const trimmed = value.trim();
      if (!trimmed) return null;
      const parsed = new Date(trimmed);
      if (!Number.isFinite(parsed.getTime())) return null;
      return parsed.toISOString();
    }}

    function normalizeHistoryTimeFilterRange(observedAfterIso, observedBeforeIso) {{
      const afterMs = parseTimestampMs(observedAfterIso);
      const beforeMs = parseTimestampMs(observedBeforeIso);
      if (Number.isFinite(afterMs) && Number.isFinite(beforeMs) && afterMs > beforeMs) {{
        return {{
          observedAfterIso: observedBeforeIso,
          observedBeforeIso: observedAfterIso,
        }};
      }}
      return {{
        observedAfterIso: observedAfterIso || null,
        observedBeforeIso: observedBeforeIso || null,
      }};
    }}

    function escapeHtml(value) {{
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }}

    function targetTypeIcon(kind) {{
      return kind === "vessel" ? "⛵" : "✈";
    }}

    function drawMapTargetLabel(label, x, y, color) {{
      if (typeof label !== "string" || !label.trim()) return;
      ctx.save();
      ctx.font = "12px Courier New, monospace";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillStyle = color || "#9be89b";
      ctx.fillText(label, x + 8, y - 10);
      ctx.restore();
    }}

    function matchesTargetTypeFilter(target, filterValue) {{
      if (filterValue === "all") return true;
      if (!target || typeof target !== "object") return false;
      return target.kind === filterValue;
    }}

    function matchesHistorySpeedFilter(target, minSpeed) {{
      if (!Number.isFinite(minSpeed) || minSpeed <= 0) return true;
      if (!target || typeof target !== "object") return false;
      const maxObservedSpeed = toOptionalNumber(target.max_observed_speed);
      return Number.isFinite(maxObservedSpeed) && maxObservedSpeed >= minSpeed;
    }}

    function clearSelectedTargetState() {{
      selectedTargetId = null;
      selectedTargetKind = null;
      selectedTargetLabel = null;
      selectedPositionCount = 0;
      selectedLastSeen = null;
      selectedHistoryPoints = [];
    }}

    function updateHistoryTimeFilterSummary() {{
      const observedAfterLabel = formatHistoryFilterDateLabel(historyObservedAfterIso);
      const observedBeforeLabel = formatHistoryFilterDateLabel(historyObservedBeforeIso);
      let summary = "Alla spår";
      if (observedAfterLabel && observedBeforeLabel) {{
        summary = `${{observedAfterLabel}} -> ${{observedBeforeLabel}}`;
      }} else if (observedAfterLabel) {{
        summary = `Från ${{observedAfterLabel}}`;
      }} else if (observedBeforeLabel) {{
        summary = `Till ${{observedBeforeLabel}}`;
      }}
      historyTimeFilterSummary.textContent = `Tidsfilter: ${{summary}}`;
    }}

    function setHistoryTimeFilterModalOpen(isOpen) {{
      historyTimeFilterModal.hidden = !isOpen;
      historyTimeFilterModal.setAttribute("aria-hidden", isOpen ? "false" : "true");
      if (isOpen) {{
        historyObservedAfterInput.value = toDatetimeLocalValue(historyObservedAfterIso);
        historyObservedBeforeInput.value = toDatetimeLocalValue(historyObservedBeforeIso);
        historyObservedAfterInput.focus();
      }}
    }}

    function appendHistoryTimeFilterParams(params) {{
      if (historyObservedAfterIso) {{
        params.set("observed_after", historyObservedAfterIso);
      }}
      if (!historyObservedBeforeIso) return;
      params.set("observed_before", historyObservedBeforeIso);
    }}

    function formatRangeValue(value) {{
      return Number.isInteger(value)
        ? String(value)
        : value.toFixed(2).replace(/0+$/, "").replace(/\\.$/, "");
    }}

    function computeHistoryRangeKm(points, referenceCenter) {{
      let maxDistance = 3;
      for (const point of points) {{
        if (!point) continue;
        const lat = toOptionalNumber(point.lat);
        const lon = toOptionalNumber(point.lon);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
        const {{ dx, dy }} = toOffsetKm(lat, lon, referenceCenter);
        const distance = Math.sqrt((dx * dx) + (dy * dy));
        if (distance > maxDistance) maxDistance = distance;
      }}
      return clampRangeKm(Math.max(3, Math.ceil(maxDistance + 1)));
    }}

    function resizeCanvas() {{
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      const w = Math.max(1, Math.floor(rect.width * dpr));
      const h = Math.max(1, Math.floor(rect.height * dpr));
      if (canvas.width !== w || canvas.height !== h) {{
        canvas.width = w;
        canvas.height = h;
      }}
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }}

    function getViewMetrics() {{
      resizeCanvas();
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      const cx = width / 2;
      const cy = height / 2;
      const radius = Math.max(30, Math.min(width, height) * 0.45);
      const autoRangeKm = computeHistoryRangeKm(selectedHistoryPoints, viewCenter);
      const rangeKm = clampRangeKm(manualRangeKm ?? autoRangeKm ?? defaultRangeKm);
      const pxPerKm = radius / rangeKm;
      return {{ width, height, cx, cy, radius, autoRangeKm, rangeKm, pxPerKm }};
    }}

    function syncRangeInput(rangeKm) {{
      if (document.activeElement === rangeInput) return;
      rangeInput.value = formatRangeValue(rangeKm);
    }}

    function parseRangeInputValue(value) {{
      const normalized = String(value).trim().replace(",", ".");
      if (!normalized) return NaN;
      return Number(normalized);
    }}

    function setRangeKm(nextRangeKm) {{
      manualRangeKm = clampRangeKm(nextRangeKm);
      draw();
    }}

    function increaseRange() {{
      const rangeKm = getViewMetrics().rangeKm;
      setRangeKm(rangeKm + 1);
    }}

    function decreaseRange() {{
      const rangeKm = getViewMetrics().rangeKm;
      setRangeKm(rangeKm - 1);
    }}

    function resetZoom() {{
      if (selectedHistoryPoints.length > 0) {{
        fitHistoryToView(selectedHistoryPoints);
      }} else {{
        viewCenter = {{ ...homeCenter }};
        manualRangeKm = defaultRangeKm;
      }}
      draw();
    }}

    function applyRangeInput() {{
      const parsed = parseRangeInputValue(rangeInput.value);
      if (!Number.isFinite(parsed)) {{
        syncRangeInput(getViewMetrics().rangeKm);
        return;
      }}
      setRangeKm(parsed);
    }}

    function canvasPointFromEvent(event) {{
      const rect = canvas.getBoundingClientRect();
      const x = Math.max(0, Math.min(rect.width, event.clientX - rect.left));
      const y = Math.max(0, Math.min(rect.height, event.clientY - rect.top));
      return {{ x, y }};
    }}

    function beginSelection(event) {{
      if (event.button !== 0) return;
      dragStart = canvasPointFromEvent(event);
      dragCurrent = dragStart;
      draw();
    }}

    function updateSelection(event) {{
      if (!dragStart) return;
      dragCurrent = canvasPointFromEvent(event);
      draw();
    }}

    function applySelectionZoom() {{
      if (!dragStart || !dragCurrent) return;
      const {{ cx, cy, pxPerKm }} = getViewMetrics();
      const x1 = Math.min(dragStart.x, dragCurrent.x);
      const x2 = Math.max(dragStart.x, dragCurrent.x);
      const y1 = Math.min(dragStart.y, dragCurrent.y);
      const y2 = Math.max(dragStart.y, dragCurrent.y);
      const widthPx = x2 - x1;
      const heightPx = y2 - y1;
      if (widthPx < 10 || heightPx < 10) return;

      const centerX = x1 + (widthPx / 2);
      const centerY = y1 + (heightPx / 2);
      const dxKm = (centerX - cx) / pxPerKm;
      const dyKm = (cy - centerY) / pxPerKm;
      viewCenter = offsetKmToLatLon(dxKm, dyKm, viewCenter);

      const halfWidthKm = (widthPx / 2) / pxPerKm;
      const halfHeightKm = (heightPx / 2) / pxPerKm;
      manualRangeKm = clampRangeKm(Math.max(halfWidthKm, halfHeightKm) * 1.2);
    }}

    function endSelection(event) {{
      if (!dragStart) return;
      dragCurrent = canvasPointFromEvent(event);
      applySelectionZoom();
      dragStart = null;
      dragCurrent = null;
      draw();
    }}

    function cancelSelection() {{
      if (!dragStart) return;
      dragStart = null;
      dragCurrent = null;
      draw();
    }}

    function drawSelectionBox() {{
      if (!dragStart || !dragCurrent) return;
      const x = Math.min(dragStart.x, dragCurrent.x);
      const y = Math.min(dragStart.y, dragCurrent.y);
      const width = Math.abs(dragCurrent.x - dragStart.x);
      const height = Math.abs(dragCurrent.y - dragStart.y);
      ctx.save();
      ctx.setLineDash([6, 4]);
      ctx.strokeStyle = "#7cff7c";
      ctx.lineWidth = 1;
      ctx.strokeRect(x, y, width, height);
      ctx.fillStyle = "rgba(124, 255, 124, 0.08)";
      ctx.fillRect(x, y, width, height);
      ctx.restore();
    }}

    function computeMapContourBBox(rangeKm) {{
      const latPadding = rangeKm / kmPerDegLat;
      const lonPadding = rangeKm / kmPerDegLon(viewCenter.lat);
      return [
        viewCenter.lon - lonPadding,
        viewCenter.lat - latPadding,
        viewCenter.lon + lonPadding,
        viewCenter.lat + latPadding,
      ];
    }}

    function mapContourRequestKeyForView(rangeKm) {{
      const bbox = computeMapContourBBox(rangeKm);
      const bboxKey = bbox.map((value) => value.toFixed(4)).join(",");
      return {{
        bbox,
        key: `${{mapContourSource}}|${{bboxKey}}`,
      }};
    }}

    function historyTargetsInViewRequestKeyForView(rangeKm) {{
      return [
        viewCenter.lat.toFixed(4),
        viewCenter.lon.toFixed(4),
        rangeKm.toFixed(3),
        historyObservedAfterIso || "all",
        historyObservedBeforeIso || "all",
      ].join("|");
    }}

    function normalizeMapContourFeatures(features) {{
      if (!Array.isArray(features)) return [];
      return features.filter((feature) => feature && typeof feature === "object");
    }}

    function drawLineCoordinates(coordinates, cx, cy, pxPerKm) {{
      if (!Array.isArray(coordinates) || coordinates.length < 2) return;
      let segmentOpen = false;
      for (const coordinate of coordinates) {{
        if (!Array.isArray(coordinate) || coordinate.length < 2) {{
          segmentOpen = false;
          continue;
        }}
        const lon = toOptionalNumber(coordinate[0]);
        const lat = toOptionalNumber(coordinate[1]);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) {{
          segmentOpen = false;
          continue;
        }}
        const {{ dx, dy }} = toOffsetKm(lat, lon, viewCenter);
        const x = cx + (dx * pxPerKm);
        const y = cy - (dy * pxPerKm);
        if (!segmentOpen) {{
          ctx.moveTo(x, y);
          segmentOpen = true;
        }} else {{
          ctx.lineTo(x, y);
        }}
      }}
    }}

    function drawMapContours(cx, cy, pxPerKm, radius) {{
      if (!showMapContours) return;
      if (!Array.isArray(mapContours) || mapContours.length === 0) return;

      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, radius, 0, Math.PI * 2);
      ctx.clip();
      ctx.strokeStyle = "#143314";
      ctx.lineWidth = 1;
      ctx.globalAlpha = 0.9;
      for (const feature of mapContours) {{
        const geometry = feature.geometry;
        if (!geometry || typeof geometry !== "object") continue;
        const geometryType = geometry.type;
        const coordinates = geometry.coordinates;
        ctx.beginPath();
        if (geometryType === "LineString") {{
          drawLineCoordinates(coordinates, cx, cy, pxPerKm);
        }} else if (geometryType === "MultiLineString" && Array.isArray(coordinates)) {{
          for (const line of coordinates) {{
            drawLineCoordinates(line, cx, cy, pxPerKm);
          }}
        }} else {{
          continue;
        }}
        ctx.stroke();
      }}
      ctx.globalAlpha = 1;
      ctx.restore();
    }}

    function clearMapContourRetryTimer() {{
      if (mapContourRetryTimer !== null) {{
        window.clearTimeout(mapContourRetryTimer);
        mapContourRetryTimer = null;
      }}
    }}

    function scheduleMapContourRetry() {{
      clearMapContourRetryTimer();
      mapContourRetryTimer = window.setTimeout(() => {{
        mapContourRetryTimer = null;
        mapContourPendingKey = null;
        void loadMapContoursForView(getViewMetrics().rangeKm);
      }}, minAutoRefreshMs);
    }}

    async function loadMapContoursForView(rangeKm) {{
      if (!showMapContours || mapContourRequestInFlight) return;
      const request = mapContourRequestKeyForView(rangeKm);
      if (
        request.key === mapContourLoadedKey
        || request.key === mapContourRequestKey
        || request.key === mapContourPendingKey
      ) return;

      mapContourRequestInFlight = true;
      mapContourRequestKey = request.key;
      try {{
        const params = new URLSearchParams({{
          source: mapContourSource,
          bbox: request.bbox.map((value) => value.toFixed(6)).join(","),
          range_km: rangeKm.toFixed(3),
        }});
        const response = await fetch(`ui/map-contours?${{params.toString()}}`, {{
          cache: "no-store",
        }});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        const payload = await response.json();
        mapContours = normalizeMapContourFeatures(payload.features);
        mapContourSource = typeof payload.source === "string" ? payload.source : mapContourSource;
        mapContourStatus = typeof payload.status === "string" ? payload.status : "ok";
        mapContourError = typeof payload.error === "string" && payload.error
          ? payload.error
          : null;
        clearMapContourRetryTimer();
        if (mapContourStatus === "pending") {{
          mapContourPendingKey = request.key;
          mapContourLoadedKey = null;
          scheduleMapContourRetry();
        }} else if (mapContourStatus === "ok") {{
          mapContourPendingKey = null;
          mapContourLoadedKey = request.key;
        }} else {{
          mapContourPendingKey = null;
          mapContourLoadedKey = null;
        }}
      }} catch (err) {{
        mapContours = [];
        mapContourStatus = "error";
        mapContourError = err instanceof Error ? err.message : String(err);
        mapContourPendingKey = null;
        mapContourLoadedKey = null;
        clearMapContourRetryTimer();
      }} finally {{
        mapContourRequestInFlight = false;
        mapContourRequestKey = null;
        draw();
      }}
    }}

    function ensureMapContoursForView(rangeKm) {{
      if (!showMapContours) return;
      void loadMapContoursForView(rangeKm);
    }}

    async function loadHistoryTargetsInView(rangeKm) {{
      if (dragStart) return;
      if (historyTargetsInViewRequestInFlight) return;
      const requestKey = historyTargetsInViewRequestKeyForView(rangeKm);
      if (
        requestKey === historyTargetsInViewLoadedKey
        || requestKey === historyTargetsInViewRequestKey
      ) {{
        return;
      }}

      historyTargetsInViewRequestInFlight = true;
      historyTargetsInViewRequestKey = requestKey;
      try {{
        const params = new URLSearchParams({{
          center_lat: viewCenter.lat.toFixed(6),
          center_lon: viewCenter.lon.toFixed(6),
          range_km: rangeKm.toFixed(3),
        }});
        appendHistoryTimeFilterParams(params);
        const response = await fetch(`ui/history-targets-in-view?${{params.toString()}}`, {{
          cache: "no-store",
        }});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        const payload = await response.json();
        const nextIds = Array.isArray(payload.target_ids) ? payload.target_ids : [];
        historyTargetsInView = new Set(
          nextIds.filter((targetId) => typeof targetId === "string" && targetId),
        );
        historyTargetsInViewLoadedKey = requestKey;
      }} catch (err) {{
        historyTargetsInView = new Set();
      }} finally {{
        historyTargetsInViewRequestInFlight = false;
        historyTargetsInViewRequestKey = null;
        renderHistoryPanel();
        draw();
      }}
    }}

    function ensureHistoryTargetsInView(rangeKm) {{
      void loadHistoryTargetsInView(rangeKm);
    }}

    function fixedObjectMarkerFontPx(rangeKm) {{
      const effectiveRange = Number.isFinite(rangeKm) ? Math.max(0, rangeKm) : 10;
      const zoomOutSteps = Math.max(0, Math.floor((effectiveRange - 10) / 10));
      return Math.max(7, 13 - zoomOutSteps);
    }}

    function drawFixedObjects(cx, cy, pxPerKm, radius, rangeKm) {{
      if (!Array.isArray(fixedObjects) || fixedObjects.length === 0) return;
      ctx.save();
      ctx.textBaseline = "middle";
      const markerFontPx = fixedObjectMarkerFontPx(rangeKm);
      const markerTextOffsetPx = Math.max(6, Math.round(markerFontPx * 0.65));
      for (const item of fixedObjects) {{
        const lat = toOptionalNumber(item.lat);
        const lon = toOptionalNumber(item.lon);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;

        const {{ dx, dy }} = toOffsetKm(lat, lon, viewCenter);
        const x = cx + (dx * pxPerKm);
        const y = cy - (dy * pxPerKm);
        const insideRadar = ((x - cx) * (x - cx)) + ((y - cy) * (y - cy)) <= (radius * radius);
        if (!insideRadar) continue;

        const maxVisibleRangeKm = toOptionalNumber(item.max_visible_range_km);
        if (Number.isFinite(maxVisibleRangeKm) && rangeKm > maxVisibleRangeKm) continue;

        const rawSymbol = typeof item.symbol === "string" ? item.symbol.trim() : "";
        const symbol = rawSymbol ? rawSymbol[0] : "O";
        const name = typeof item.name === "string" ? item.name.trim() : "";
        const nameLines = name ? name.split(/\\s+/).filter(Boolean) : [];

        ctx.fillStyle = radarRingColor;
        ctx.font = `${{markerFontPx}}px Courier New, monospace`;
        ctx.textAlign = "center";
        ctx.fillText(symbol, x, y);
        if (showFixedNames && nameLines.length > 0) {{
          const lineHeight = 12;
          const startY = y - ((nameLines.length - 1) * lineHeight * 0.5);
          ctx.fillStyle = "#9be89b";
          ctx.font = "12px Courier New, monospace";
          ctx.textAlign = "left";
          nameLines.forEach((line, index) => {{
            ctx.fillText(line, x + markerTextOffsetPx, startY + (index * lineHeight));
          }});
        }}
      }}
      ctx.restore();
    }}

    function isInsideRadarCircle(x, y, cx, cy, radius) {{
      return ((x - cx) * (x - cx)) + ((y - cy) * (y - cy)) <= (radius * radius);
    }}

    function clipSegmentToCircle(start, end, cx, cy, radius) {{
      if (!start || !end) return null;
      const dx = end.x - start.x;
      const dy = end.y - start.y;
      const a = (dx * dx) + (dy * dy);
      if (a <= 0.000001) {{
        return isInsideRadarCircle(start.x, start.y, cx, cy, radius)
          ? {{ start, end }}
          : null;
      }}

      const startInside = isInsideRadarCircle(start.x, start.y, cx, cy, radius);
      const endInside = isInsideRadarCircle(end.x, end.y, cx, cy, radius);
      if (startInside && endInside) {{
        return {{ start, end }};
      }}

      const fx = start.x - cx;
      const fy = start.y - cy;
      const b = 2 * ((fx * dx) + (fy * dy));
      const c = (fx * fx) + (fy * fy) - (radius * radius);
      const discriminant = (b * b) - (4 * a * c);
      if (discriminant < 0) return null;

      const sqrtDiscriminant = Math.sqrt(discriminant);
      const t1 = (-b - sqrtDiscriminant) / (2 * a);
      const t2 = (-b + sqrtDiscriminant) / (2 * a);
      const enterT = Math.max(0, Math.min(t1, t2));
      const exitT = Math.min(1, Math.max(t1, t2));
      if (enterT > exitT) return null;

      const clippedStartT = startInside ? 0 : enterT;
      const clippedEndT = endInside ? 1 : exitT;
      return {{
        start: {{
          x: start.x + (dx * clippedStartT),
          y: start.y + (dy * clippedStartT),
        }},
        end: {{
          x: start.x + (dx * clippedEndT),
          y: start.y + (dy * clippedEndT),
        }},
      }};
    }}

    function normalizeHistoryPoints(observations) {{
      if (!Array.isArray(observations)) return [];
      return observations
        .map((item, index) => {{
          if (!item || typeof item !== "object") return null;
          const lat = toOptionalNumber(item.lat);
          const lon = toOptionalNumber(item.lon);
          if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
          const observedAtMs = parseTimestampMs(item.observed_at);
          return {{
            lat,
            lon,
            ts_ms: Number.isFinite(observedAtMs) ? observedAtMs : index,
          }};
        }})
        .filter(Boolean)
        .sort((a, b) => a.ts_ms - b.ts_ms);
    }}

    function fitHistoryToView(points) {{
      if (!Array.isArray(points) || points.length === 0) return false;

      let minLat = points[0].lat;
      let maxLat = points[0].lat;
      let minLon = points[0].lon;
      let maxLon = points[0].lon;
      for (const point of points) {{
        minLat = Math.min(minLat, point.lat);
        maxLat = Math.max(maxLat, point.lat);
        minLon = Math.min(minLon, point.lon);
        maxLon = Math.max(maxLon, point.lon);
      }}

      const nextCenter = {{
        lat: (minLat + maxLat) / 2,
        lon: (minLon + maxLon) / 2,
      }};
      let maxDxKm = 0;
      let maxDyKm = 0;
      for (const point of points) {{
        const {{ dx, dy }} = toOffsetKm(point.lat, point.lon, nextCenter);
        maxDxKm = Math.max(maxDxKm, Math.abs(dx));
        maxDyKm = Math.max(maxDyKm, Math.abs(dy));
      }}

      viewCenter = nextCenter;
      manualRangeKm = clampRangeKm(Math.max(maxDxKm, maxDyKm, minRangeKm) * 1.15);
      return true;
    }}

    function drawSelectedHistoryPath(cx, cy, pxPerKm, radius) {{
      if (!Array.isArray(selectedHistoryPoints) || selectedHistoryPoints.length === 0) return;

      const canvasPoints = [];
      for (const point of selectedHistoryPoints) {{
        const {{ dx, dy }} = toOffsetKm(point.lat, point.lon, viewCenter);
        const x = cx + (dx * pxPerKm);
        const y = cy - (dy * pxPerKm);
        canvasPoints.push({{ x, y }});
      }}
      if (canvasPoints.length === 0) return;

      ctx.save();
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 3]);
      if (canvasPoints.length > 1) {{
        for (let i = 1; i < canvasPoints.length; i += 1) {{
          const ageRank = canvasPoints.length <= 1 ? 1 : 1 - (i / (canvasPoints.length - 1));
          const clippedSegment = clipSegmentToCircle(
            canvasPoints[i - 1],
            canvasPoints[i],
            cx,
            cy,
            radius,
          );
          if (!clippedSegment) continue;
          ctx.globalAlpha = 0.95;
          ctx.strokeStyle = trailColorForAge(ageRank);
          ctx.beginPath();
          ctx.moveTo(clippedSegment.start.x, clippedSegment.start.y);
          ctx.lineTo(clippedSegment.end.x, clippedSegment.end.y);
          ctx.stroke();
        }}
      }}
      canvasPoints.forEach((point, index) => {{
        if (!isInsideRadarCircle(point.x, point.y, cx, cy, radius)) return;
        const ageRank = canvasPoints.length <= 1 ? 0 : 1 - (index / (canvasPoints.length - 1));
        ctx.globalAlpha = 0.95;
        ctx.fillStyle = trailColorForAge(ageRank);
        ctx.beginPath();
        ctx.arc(point.x, point.y, 1.9, 0, Math.PI * 2);
        ctx.fill();
      }});
      ctx.globalAlpha = 1;
      ctx.restore();
    }}

    function drawSelectedTargetMarker(cx, cy, pxPerKm, radius) {{
      if (!Array.isArray(selectedHistoryPoints) || selectedHistoryPoints.length === 0) return;
      const latestPoint = selectedHistoryPoints[selectedHistoryPoints.length - 1];
      if (!latestPoint) return;
      const {{ dx, dy }} = toOffsetKm(latestPoint.lat, latestPoint.lon, viewCenter);
      const x = cx + (dx * pxPerKm);
      const y = cy - (dy * pxPerKm);
      if (!isInsideRadarCircle(x, y, cx, cy, radius)) return;

      ctx.save();
      ctx.font = "bold 16px Courier New, monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = selectedTargetColor;
      const symbol = selectedTargetKind === "vessel" ? "*" : "+";
      ctx.fillText(symbol, x, y);
      if (showTargetLabels) {{
        drawMapTargetLabel(selectedTargetLabel || selectedTargetId || "", x, y, selectedTargetColor);
      }}
      ctx.restore();
    }}

    function renderHistoryCards(items) {{
      if (items.length === 0) {{
        const emptyText = historyObservedBeforeIso
          || historyObservedAfterIso
          ? "Inga historiska objekt för valt tidsfilter."
          : "Inga historiska objekt.";
        return `<div class="objects-empty">${{escapeHtml(emptyText)}}</div>`;
      }}

      return items
        .map((target) => {{
          const targetId = typeof target.target_id === "string" ? target.target_id : "";
          const label = target.label || target.target_id || "okänt";
          const kind = typeof target.kind === "string" ? target.kind : "";
          const lastSeen = target.last_seen ? String(target.last_seen) : "-";
          const positionCount = Number.isFinite(Number(target.position_count))
            ? Number(target.position_count)
            : 0;
          const maxObservedSpeed = toOptionalNumber(target.max_observed_speed);
          const maxObservedSpeedText = Number.isFinite(maxObservedSpeed)
            ? String(maxObservedSpeed)
            : "-";
          const inView = targetId ? historyTargetsInView.has(targetId) : false;
          const selectedClass = targetId && selectedTargetId === targetId ? " selected" : "";
          const inViewClass = inView ? " in-view" : "";
          const targetAttr = targetId
            ? ` data-target-id="${{escapeHtml(targetId)}}" role="button" tabindex="0"`
            : "";

          return `
            <div class="object-item${{selectedClass}}${{inViewClass}}"${{targetAttr}}>
              <div class="object-label-row">
                <span class="object-type-icon" aria-hidden="true">${{escapeHtml(targetTypeIcon(kind))}}</span>
                <div class="object-label">${{escapeHtml(label)}}</div>
                ${{inView ? '<span class="object-view-badge" aria-label="I vy" title="I vy">◉</span>' : ""}}
              </div>
              <div>positioner: ${{escapeHtml(String(positionCount))}}</div>
              <div>max_speed: ${{escapeHtml(maxObservedSpeedText)}}</div>
              <div>last_seen: ${{escapeHtml(lastSeen)}}</div>
            </div>
          `;
        }})
        .join("");
    }}

    function renderHistoryPanel() {{
      const filteredTargets = historyTargets
        .filter((target) => matchesTargetTypeFilter(target, historyTargetTypeFilter))
        .filter((target) => matchesHistorySpeedFilter(target, historyMinSpeedFilter))
        .sort((left, right) => {{
          const leftInView = left && typeof left.target_id === "string" && historyTargetsInView.has(left.target_id);
          const rightInView = right && typeof right.target_id === "string" && historyTargetsInView.has(right.target_id);
          if (leftInView !== rightInView) {{
            return leftInView ? -1 : 1;
          }}

          const leftLastSeenMs = parseTimestampMs(left && left.last_seen);
          const rightLastSeenMs = parseTimestampMs(right && right.last_seen);
          if (Number.isFinite(leftLastSeenMs) || Number.isFinite(rightLastSeenMs)) {{
            return (Number.isFinite(rightLastSeenMs) ? rightLastSeenMs : Number.NEGATIVE_INFINITY)
              - (Number.isFinite(leftLastSeenMs) ? leftLastSeenMs : Number.NEGATIVE_INFINITY);
          }}
          return 0;
        }});
      historyObjectsSummary.textContent = `${{filteredTargets.length}} objekt med historik`;
      historyObjectsList.innerHTML = renderHistoryCards(filteredTargets);
    }}

    function findHistoryTargetById(targetId) {{
      if (!targetId) return null;
      return historyTargets.find((item) => item && item.target_id === targetId) || null;
    }}

    async function loadHistoryTargets() {{
      try {{
        const params = new URLSearchParams();
        appendHistoryTimeFilterParams(params);
        const requestUrl = params.size > 0
          ? `ui/history-targets?${{params.toString()}}`
          : "ui/history-targets";
        const response = await fetch(requestUrl, {{ cache: "no-store" }});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        const payload = await response.json();
        historyTargets = Array.isArray(payload.targets) ? payload.targets : [];
        if (selectedTargetId && !findHistoryTargetById(selectedTargetId)) {{
          clearSelectedTargetState();
        }}
        error = null;
      }} catch (err) {{
        historyTargets = [];
        error = err instanceof Error ? err.message : String(err);
      }} finally {{
        renderHistoryPanel();
        draw();
      }}
    }}

    async function loadSelectedHistory(targetId, positionCount) {{
      if (!targetId) return [];
      const safeLimit = Math.max(1, Number(positionCount) || 1);
      const params = new URLSearchParams({{
        limit: String(safeLimit),
      }});
      appendHistoryTimeFilterParams(params);
      const response = await fetch(
        `history/${{encodeURIComponent(targetId)}}?${{params.toString()}}`,
        {{ cache: "no-store" }},
      );
      if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
      const payload = await response.json();
      const observations = Array.isArray(payload.observations) ? payload.observations : [];
      return normalizeHistoryPoints(observations);
    }}

    async function refreshSelectedTargetHistory() {{
      if (!selectedTargetId) return;
      const selectedTarget = findHistoryTargetById(selectedTargetId);
      if (!selectedTarget) {{
        clearSelectedTargetState();
        renderHistoryPanel();
        draw();
        return;
      }}

      selectedTargetKind = typeof selectedTarget.kind === "string" ? selectedTarget.kind : null;
      selectedTargetLabel = selectedTarget.label || selectedTarget.target_id || null;
      selectedPositionCount = Number(selectedTarget.position_count) || 0;
      selectedLastSeen = selectedTarget.last_seen || null;
      selectedHistoryPoints = [];
      renderHistoryPanel();
      draw();

      const activeSelection = selectedTargetId;
      try {{
        const historyPoints = await loadSelectedHistory(activeSelection, selectedPositionCount);
        if (selectedTargetId !== activeSelection) return;
        selectedHistoryPoints = historyPoints;
        error = null;
      }} catch (err) {{
        if (selectedTargetId !== activeSelection) return;
        selectedHistoryPoints = [];
        error = err instanceof Error ? err.message : String(err);
      }}
      draw();
    }}

    async function selectTarget(targetId) {{
      if (!targetId) return;
      if (selectedTargetId === targetId) {{
        clearSelectedTargetState();
        draw();
        renderHistoryPanel();
        return;
      }}

      const selectedTarget = findHistoryTargetById(targetId);
      if (!selectedTarget) return;
      selectedTargetId = targetId;
      await refreshSelectedTargetHistory();
    }}

    async function applyHistoryTimeFilter(nextObservedAfterIso, nextObservedBeforeIso) {{
      const normalized = normalizeHistoryTimeFilterRange(
        nextObservedAfterIso,
        nextObservedBeforeIso,
      );
      historyObservedAfterIso = normalized.observedAfterIso;
      historyObservedBeforeIso = normalized.observedBeforeIso;
      updateHistoryTimeFilterSummary();
      historyTargetsInView = new Set();
      historyTargetsInViewLoadedKey = null;
      historyTargetsInViewRequestKey = null;
      await loadHistoryTargets();
      if (selectedTargetId) {{
        await refreshSelectedTargetHistory();
      }} else {{
        draw();
      }}
    }}

    function getTargetIdFromPanelEvent(event) {{
      if (!(event.target instanceof Element)) return null;
      const card = event.target.closest(".object-item[data-target-id]");
      if (!(card instanceof HTMLElement)) return null;
      const targetId = card.dataset.targetId;
      if (typeof targetId !== "string" || !targetId) return null;
      return targetId;
    }}

    function onHistoryPanelClick(event) {{
      const targetId = getTargetIdFromPanelEvent(event);
      if (!targetId) return;
      void selectTarget(targetId);
    }}

    function onHistoryPanelKeyDown(event) {{
      if (event.key !== "Enter" && event.key !== " ") return;
      const targetId = getTargetIdFromPanelEvent(event);
      if (!targetId) return;
      event.preventDefault();
      void selectTarget(targetId);
    }}

    function draw() {{
      const {{
        width,
        height,
        cx,
        cy,
        radius,
        rangeKm,
        pxPerKm,
      }} = getViewMetrics();

      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#000000";
      ctx.fillRect(0, 0, width, height);

      ctx.strokeStyle = radarRingColor;
      ctx.lineWidth = 1;
      const ringSpacingKm = rangeKm / radarRingCount;
      for (let i = 1; i <= radarRingCount; i += 1) {{
        ctx.beginPath();
        ctx.arc(cx, cy, i * ringSpacingKm * pxPerKm, 0, Math.PI * 2);
        ctx.stroke();
      }}

      ctx.strokeStyle = radarRingColor;
      ctx.beginPath();
      ctx.moveTo(cx - radius, cy);
      ctx.lineTo(cx + radius, cy);
      ctx.moveTo(cx, cy - radius);
      ctx.lineTo(cx, cy + radius);
      ctx.stroke();

      ctx.fillStyle = "#d3d3d3";
      ctx.beginPath();
      ctx.arc(cx, cy, 5, 0, Math.PI * 2);
      ctx.fill();

      drawMapContours(cx, cy, pxPerKm, radius);
      drawFixedObjects(cx, cy, pxPerKm, radius, rangeKm);
      drawSelectedHistoryPath(cx, cy, pxPerKm, radius);
      drawSelectedTargetMarker(cx, cy, pxPerKm, radius);
      drawSelectionBox();
      syncRangeInput(rangeKm);
      ensureMapContoursForView(rangeKm);
      ensureHistoryTargetsInView(rangeKm);

      const contourErrorText = showMapContours && mapContourError ? ` | Konturer: ${{mapContourError}}` : "";
      const historyTimeFilterText = ` | ${{historyTimeFilterSummary.textContent}}`;
      const selectedText = selectedTargetId
        ? `Vald: ${{selectedTargetLabel || selectedTargetId}} | Positioner: ${{selectedPositionCount}} | Last seen: ${{selectedLastSeen || "-"}}`
        : "Vald: inget objekt";
      const errorText = error ? ` | Error: ${{error}}` : "";
      meta.textContent = `View: ${{viewCenter.lat.toFixed(6)}}, ${{viewCenter.lon.toFixed(6)}} | Ringavstand: ${{ringSpacingKm.toFixed(2)}} km${{historyTimeFilterText}} | ${{selectedText}}${{contourErrorText}}${{errorText}}`;
    }}

    window.addEventListener("resize", draw);
    zoomInButton.addEventListener("click", decreaseRange);
    zoomOutButton.addEventListener("click", increaseRange);
    zoomResetButton.addEventListener("click", resetZoom);
    rangeInput.addEventListener("change", applyRangeInput);
    rangeInput.addEventListener("blur", applyRangeInput);
    rangeInput.addEventListener("keydown", (event) => {{
      if (event.key === "Enter") {{
        event.preventDefault();
        applyRangeInput();
      }}
    }});
    historyObjectsList.addEventListener("click", onHistoryPanelClick);
    historyObjectsList.addEventListener("keydown", onHistoryPanelKeyDown);
    historyTimeFilterButton.addEventListener("click", () => {{
      setHistoryTimeFilterModalOpen(true);
    }});
    historyTimeFilterCancelButton.addEventListener("click", () => {{
      setHistoryTimeFilterModalOpen(false);
    }});
    historyTimeFilterClearButton.addEventListener("click", () => {{
      setHistoryTimeFilterModalOpen(false);
      void applyHistoryTimeFilter(null, null);
    }});
    historyTimeFilterApplyButton.addEventListener("click", () => {{
      const nextObservedAfterIso = parseObservedBeforeInputValue(historyObservedAfterInput.value);
      const nextObservedBeforeIso = parseObservedBeforeInputValue(historyObservedBeforeInput.value);
      setHistoryTimeFilterModalOpen(false);
      void applyHistoryTimeFilter(nextObservedAfterIso, nextObservedBeforeIso);
    }});
    historyTimeFilterModal.addEventListener("click", (event) => {{
      if (event.target === historyTimeFilterModal) {{
        setHistoryTimeFilterModalOpen(false);
      }}
    }});
    function submitHistoryTimeFilterFromModal() {{
      const nextObservedAfterIso = parseObservedBeforeInputValue(historyObservedAfterInput.value);
      const nextObservedBeforeIso = parseObservedBeforeInputValue(historyObservedBeforeInput.value);
      setHistoryTimeFilterModalOpen(false);
      void applyHistoryTimeFilter(nextObservedAfterIso, nextObservedBeforeIso);
    }}
    historyObservedAfterInput.addEventListener("keydown", (event) => {{
      if (event.key === "Enter") {{
        event.preventDefault();
        submitHistoryTimeFilterFromModal();
      }}
    }});
    historyObservedBeforeInput.addEventListener("keydown", (event) => {{
      if (event.key === "Enter") {{
        event.preventDefault();
        submitHistoryTimeFilterFromModal();
      }}
    }});
    showFixedNamesCheckbox.addEventListener("change", () => {{
      showFixedNames = showFixedNamesCheckbox.checked;
      draw();
    }});
    showTargetLabelsCheckbox.addEventListener("change", () => {{
      showTargetLabels = showTargetLabelsCheckbox.checked;
      draw();
    }});
    showMapContoursCheckbox.addEventListener("change", () => {{
      showMapContours = showMapContoursCheckbox.checked;
      if (!showMapContours) {{
        mapContourError = null;
        mapContourPendingKey = null;
        clearMapContourRetryTimer();
      }}
      draw();
    }});
    if (historyTargetTypeFilterSelect instanceof HTMLSelectElement) {{
      historyTargetTypeFilterSelect.addEventListener("change", () => {{
        historyTargetTypeFilter = historyTargetTypeFilterSelect.value;
        if (selectedTargetId) {{
          const selectedTarget = findHistoryTargetById(selectedTargetId);
          if (!matchesTargetTypeFilter(selectedTarget, historyTargetTypeFilter)) {{
            clearSelectedTargetState();
          }}
        }}
        renderHistoryPanel();
        draw();
      }});
    }}
    if (historyMinSpeedFilterInput instanceof HTMLInputElement) {{
      historyMinSpeedFilterInput.addEventListener("input", () => {{
        const parsed = toOptionalNumber(historyMinSpeedFilterInput.value);
        historyMinSpeedFilter = Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
        if (selectedTargetId) {{
          const selectedTarget = findHistoryTargetById(selectedTargetId);
          if (!matchesHistorySpeedFilter(selectedTarget, historyMinSpeedFilter)) {{
            clearSelectedTargetState();
          }}
        }}
        renderHistoryPanel();
        draw();
      }});
    }}
    canvas.addEventListener(
      "wheel",
      (event) => {{
        event.preventDefault();
        if (event.deltaY < 0) {{
          decreaseRange();
        }} else {{
          increaseRange();
        }}
      }},
      {{ passive: false }},
    );
    canvas.addEventListener("mousedown", beginSelection);
    canvas.addEventListener("mousemove", updateSelection);
    canvas.addEventListener("mouseleave", cancelSelection);
    window.addEventListener("mouseup", endSelection);
    document.addEventListener("keydown", (event) => {{
      if (event.key === "Escape" && !historyTimeFilterModal.hidden) {{
        setHistoryTimeFilterModalOpen(false);
      }}
    }});
    updateHistoryTimeFilterSummary();
    draw();
    void loadHistoryTargets();
  </script>
</body>
</html>
"""
