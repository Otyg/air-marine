"""HTTP API endpoints for service health, live targets, stats, and history."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.fixed_objects import FixedRadarObject
from app.health import build_health_report
from app.models import TargetKind
from app.scanner import HybridBandScanner
from app.state import LiveState
from app.store import SQLiteStore


@dataclass(slots=True)
class APIRuntime:
    state: LiveState
    store: SQLiteStore | None = None
    scanner: HybridBandScanner | None = None
    service_name: str = "sdr-monitor"
    radar_center_lat: float = 0.0
    radar_center_lon: float = 0.0
    radio_connected: bool = False
    fixed_objects: list[FixedRadarObject] = field(default_factory=list)


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
        )

    @app.get("/ui/targets-latest")
    async def get_targets_latest() -> dict[str, Any]:
        if runtime.store is None:
            return {"count": 0, "targets": [], "radio_connected": runtime.radio_connected}

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
    ) -> dict[str, Any]:
        if runtime.store is None:
            raise HTTPException(status_code=503, detail="History store is not configured.")

        try:
            observations = runtime.store.fetch_history(target_id=target_id, limit=limit)
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


def _build_radar_html(
    *,
    center_lat: float,
    center_lon: float,
    service_name: str,
    fixed_objects: list[FixedRadarObject],
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
      --radar-fg: #90ee90;
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
    .filter-toggle {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--panel-dim);
      font-size: 12px;
      user-select: none;
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
      <div>{service_name} / RADAR VIEW</div>
      <div class="hud-right">
        <div class="zoom-controls">
          <button id="zoomOut" type="button" aria-label="Minska range">-</button>
          <input id="rangeInput" type="text" inputmode="decimal" value="10" aria-label="Range km" />
          <span class="range-unit">km</span>
          <button id="zoomIn" type="button" aria-label="Öka range">+</button>
          <button id="zoomReset" type="button" aria-label="Reset range">Hem</button>
        </div>
        <label class="toggle-control" for="showFixedNames">
          <input id="showFixedNames" type="checkbox" checked />
          Visa namn fasta punkter
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
    const basePollMs = 2000;
    const defaultRangeKm = 10.0;
    const radarRingCount = 5;
    const minRangeKm = 0.2;
    const maxRangeKm = 500.0;
    const trailPointWindowSeconds = 120;
    const trailStaleStartSeconds = 30;
    const trailStaleFadeSeconds = 270;
    const trailColors = ["#39FF14", "#1fd400", "#57e140", "#8ce77c", "#b2eda8", "#d1f6cb"];
    const fixedObjects = {fixed_objects_json};
    const canvas = document.getElementById("radar");
    const meta = document.getElementById("meta");
    const zoomInButton = document.getElementById("zoomIn");
    const zoomOutButton = document.getElementById("zoomOut");
    const zoomResetButton = document.getElementById("zoomReset");
    const rangeInput = document.getElementById("rangeInput");
    const showFixedNamesCheckbox = document.getElementById("showFixedNames");
    const showLowSpeedCheckbox = document.getElementById("showLowSpeed");
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
    let showFixedNames = true;
    let selectedTargetId = null;
    const selectedHistoryByTargetId = new Map();
    let pendingFitTargetId = null;
    const selectedHistoryLimit = 1000;
    const selectedTargetColor = "#ff4d4d";

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
          `/history/${{encodeURIComponent(targetId)}}?limit=${{selectedHistoryLimit}}`,
          {{ cache: "no-store" }},
        );
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        const payload = await response.json();
        const observations = Array.isArray(payload.observations) ? payload.observations : [];
        const historyPoints = normalizeHistoryPoints(observations);
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

      const points = [];
      for (const sample of orderedTrailPoints) {{
        const {{ dx, dy }} = toOffsetKm(sample.lat, sample.lon, viewCenter);
        const x = cx + (dx * pxPerKm);
        const y = cy - (dy * pxPerKm);
        const insideRadar = ((x - cx) * (x - cx)) + ((y - cy) * (y - cy)) <= (radius * radius);
        if (!insideRadar) continue;
        if (Number.isFinite(currentX) && Number.isFinite(currentY)) {{
          const sameAsCurrent = ((x - currentX) * (x - currentX)) + ((y - currentY) * (y - currentY)) < 1;
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
      if (Number.isFinite(currentX) && Number.isFinite(currentY)) {{
        const newestOpacity = trailOpacityForAgeRank(0, fadeProgress);
        if (newestOpacity > 0.02) {{
          ctx.globalAlpha = newestOpacity;
          ctx.strokeStyle = trailColors[1];
          ctx.beginPath();
          ctx.moveTo(currentX, currentY);
          ctx.lineTo(points[0].x, points[0].y);
          ctx.stroke();
        }}
      }}
      for (let i = 0; i < points.length - 1; i += 1) {{
        const segmentAgeRank = points.length <= 1 ? 1 : (i + 1) / (points.length - 1);
        const segmentOpacity = trailOpacityForAgeRank(segmentAgeRank, fadeProgress);
        if (segmentOpacity <= 0.02) continue;
        ctx.globalAlpha = segmentOpacity;
        const color = trailColors[Math.min(i + 1, trailColors.length - 1)];
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
        const color = trailColors[Math.min(index + 1, trailColors.length - 1)];
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
        const insideRadar = ((x - cx) * (x - cx)) + ((y - cy) * (y - cy)) <= (radius * radius);
        if (!insideRadar) continue;
        canvasPoints.push({{ x, y }});
      }}
      if (canvasPoints.length === 0) return;

      ctx.save();
      ctx.lineWidth = 1.5;
      ctx.setLineDash([]);
      ctx.strokeStyle = selectedTargetColor;
      ctx.fillStyle = selectedTargetColor;
      ctx.globalAlpha = 0.95;
      if (canvasPoints.length > 1) {{
        ctx.beginPath();
        ctx.moveTo(canvasPoints[0].x, canvasPoints[0].y);
        for (let i = 1; i < canvasPoints.length; i += 1) {{
          ctx.lineTo(canvasPoints[i].x, canvasPoints[i].y);
        }}
        ctx.stroke();
      }}
      for (const point of canvasPoints) {{
        ctx.beginPath();
        ctx.arc(point.x, point.y, 1.9, 0, Math.PI * 2);
        ctx.fill();
      }}
      ctx.globalAlpha = 1;
      ctx.restore();
    }}

    function drawFixedObjects(cx, cy, pxPerKm, radius, rangeKm) {{
      if (!Array.isArray(fixedObjects) || fixedObjects.length === 0) return;
      ctx.save();
      ctx.font = "13px Courier New, monospace";
      ctx.textBaseline = "middle";
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

        ctx.fillStyle = "#86d986";
        ctx.textAlign = "center";
        ctx.fillText(symbol, x, y);
        if (showFixedNames && nameLines.length > 0) {{
          const lineHeight = 12;
          const startY = y - ((nameLines.length - 1) * lineHeight * 0.5);
          ctx.fillStyle = "#9be89b";
          ctx.textAlign = "left";
          nameLines.forEach((line, index) => {{
            ctx.fillText(line, x + 8, startY + (index * lineHeight));
          }});
        }}
      }}
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

    function renderObjectCards(items, emptyText) {{
      if (items.length === 0) {{
        return `<div class="objects-empty">${{escapeHtml(emptyText)}}</div>`;
      }}

      return items
        .map((target) => {{
          const targetId = typeof target.target_id === "string" ? target.target_id : "";
          const label = target.label || target.target_id || "okänt";
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
              <div class="object-label">${{escapeHtml(label)}}</div>
              ${{detailLines.join("")}}
            </div>
          `;
        }})
        .join("");
    }}

    function renderObjectsPanel(visibleTargets, outsideTargets) {{
      objectsSummary.textContent = `${{visibleTargets.length}} synliga objekt`;
      outsideObjectsSummary.textContent = `${{outsideTargets.length}} objekt utanför aktivt område`;
      objectsList.innerHTML = renderObjectCards(visibleTargets, "Inga objekt i aktuell vy.");
      outsideObjectsList.innerHTML = renderObjectCards(
        outsideTargets,
        "Inga objekt utanför aktivt område.",
      );
    }}

    function isDynamicTrackTarget(target) {{
      if (!target || typeof target !== "object") return false;
      const source = typeof target.source === "string" ? target.source : "";
      const kind = typeof target.kind === "string" ? target.kind : "";
      const validSource = source === "adsb" || source === "ais";
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

      ctx.strokeStyle = "#2c7a2c";
      ctx.lineWidth = 1;
      const ringSpacingKm = rangeKm / radarRingCount;
      for (let i = 1; i <= radarRingCount; i += 1) {{
        ctx.beginPath();
        ctx.arc(cx, cy, i * ringSpacingKm * pxPerKm, 0, Math.PI * 2);
        ctx.stroke();
      }}

      ctx.strokeStyle = "#2c7a2c";
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
          if (!isSelected || !selectedHistoryByTargetId.has(targetId)) {{
            drawRecentPositions(trackedTarget, cx, cy, pxPerKm, radius);
          }}
          outsideTargets.push(target);
          continue;
        }}
        const course = toOptionalNumber(target.course);
        if (!isSelected || !selectedHistoryByTargetId.has(targetId)) {{
          drawRecentPositions(trackedTarget, cx, cy, pxPerKm, radius, x, y);
        }}
        const markerColor = isSelected ? selectedTargetColor : trailColors[0];
        drawCourseVector(x, y, course, speed, markerColor);
        ctx.fillStyle = markerColor;
        const symbol = target.kind === "vessel" ? "#" : "+";
        ctx.fillText(symbol, x, y);
        visibleTargets.push(target);
        visible += 1;
      }}
      for (const retainedTarget of retainedTrailTargets) {{
        drawRecentPositions(retainedTarget, cx, cy, pxPerKm, radius);
      }}

      renderObjectsPanel(visibleTargets, outsideTargets);
      drawSelectionBox();
      syncRangeInput(rangeKm);
      const status = error ? `Error: ${{error}}` : `${{visible}} visible / ${{targets.length}} total`;
      meta.textContent = `Home: ${{homeCenter.lat.toFixed(6)}}, ${{homeCenter.lon.toFixed(6)}} | View: ${{viewCenter.lat.toFixed(6)}}, ${{viewCenter.lon.toFixed(6)}} | Range: ${{rangeKm.toFixed(2)}} km | Ringavstand: ${{ringSpacingKm.toFixed(2)}} km | ${{status}}`;
    }}

    async function loadTargets() {{
      try {{
        const response = await fetch("/ui/targets-latest", {{ cache: "no-store" }});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        const payload = await response.json();
        const loadedTargets = Array.isArray(payload.targets) ? payload.targets : [];
        targets = loadedTargets.filter(isDynamicTrackTarget);
        updateTrailCacheFromTargets(targets);
        radioConnected = Boolean(payload.radio_connected);
        error = null;
      }} catch (err) {{
        error = err instanceof Error ? err.message : String(err);
        radioConnected = false;
      }} finally {{
        draw();
      }}
    }}

    window.addEventListener("resize", draw);
    zoomInButton.addEventListener("click", increaseRange);
    zoomOutButton.addEventListener("click", decreaseRange);
    zoomResetButton.addEventListener("click", resetZoom);
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
    showFixedNamesCheckbox.addEventListener("change", () => {{
      showFixedNames = showFixedNamesCheckbox.checked;
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
    draw();
    loadTargets();
    setInterval(loadTargets, basePollMs);
  </script>
</body>
</html>
"""
