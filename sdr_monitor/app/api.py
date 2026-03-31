"""HTTP API endpoints for service health, live targets, stats, and history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

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


def create_api_app(runtime: APIRuntime) -> FastAPI:
    """Create the phase-9 FastAPI application."""

    app = FastAPI(title=runtime.service_name)

    @app.get("/", response_class=HTMLResponse)
    async def get_radar_screen() -> str:
        return _build_radar_html(
            center_lat=runtime.radar_center_lat,
            center_lon=runtime.radar_center_lon,
            service_name=runtime.service_name,
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


def _build_radar_html(*, center_lat: float, center_lon: float, service_name: str) -> str:
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
    }}
    .zoom-controls button {{
      background: #051805;
      color: var(--panel-fg);
      border: 0;
      border-right: 1px solid #226322;
      min-width: 40px;
      height: 28px;
      cursor: pointer;
      font: inherit;
    }}
    .zoom-controls button:last-child {{
      border-right: 0;
    }}
    .zoom-controls button:hover {{
      background: #0a260a;
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
    }}
    .object-label {{
      color: var(--radar-fg);
      font-size: 13px;
      margin-bottom: 4px;
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
          <button id="zoomOut" type="button" aria-label="Zoom out">-</button>
          <button id="zoomReset" type="button" aria-label="Reset zoom">1.00x</button>
          <button id="zoomIn" type="button" aria-label="Zoom in">+</button>
        </div>
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
    const zoomMin = 0.5;
    const zoomMax = 20.0;
    const zoomStep = 1.25;
    const trailColors = ["#39FF14", "#1fd400", "#57e140", "#8ce77c", "#b2eda8", "#d1f6cb"];
    const canvas = document.getElementById("radar");
    const meta = document.getElementById("meta");
    const zoomInButton = document.getElementById("zoomIn");
    const zoomOutButton = document.getElementById("zoomOut");
    const zoomResetButton = document.getElementById("zoomReset");
    const showLowSpeedCheckbox = document.getElementById("showLowSpeed");
    const objectsSummary = document.getElementById("objectsSummary");
    const objectsList = document.getElementById("objectsList");
    const ctx = canvas.getContext("2d");
    let targets = [];
    let error = null;
    let radioConnected = false;
    let viewCenter = {{ ...homeCenter }};
    let manualRangeKm = defaultRangeKm;
    let dragStart = null;
    let dragCurrent = null;
    let showLowSpeed = false;

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

    function updateZoomBadge(rangeKm) {{
      const zoomFactor = Math.max(zoomMin, Math.min(zoomMax, defaultRangeKm / rangeKm));
      zoomResetButton.textContent = `${{zoomFactor.toFixed(2)}}x`;
    }}

    function setRangeKm(nextRangeKm) {{
      manualRangeKm = clampRangeKm(nextRangeKm);
      draw();
    }}

    function zoomIn() {{
      const rangeKm = getViewMetrics().rangeKm;
      setRangeKm(rangeKm / zoomStep);
    }}

    function zoomOut() {{
      const rangeKm = getViewMetrics().rangeKm;
      setRangeKm(rangeKm * zoomStep);
    }}

    function resetZoom() {{
      viewCenter = {{ ...homeCenter }};
      manualRangeKm = defaultRangeKm;
      draw();
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

    function drawRecentPositions(target, cx, cy, pxPerKm, radius, currentX, currentY) {{
      if (!radioConnected || !Array.isArray(target.recent_positions)) return;

      const points = [];
      for (const sample of target.recent_positions.slice(-5)) {{
        const lat = toOptionalNumber(sample.lat);
        const lon = toOptionalNumber(sample.lon);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
        const {{ dx, dy }} = toOffsetKm(lat, lon, viewCenter);
        const x = cx + (dx * pxPerKm);
        const y = cy - (dy * pxPerKm);
        const insideRadar = ((x - cx) * (x - cx)) + ((y - cy) * (y - cy)) <= (radius * radius);
        if (!insideRadar) continue;
        const sameAsCurrent = ((x - currentX) * (x - currentX)) + ((y - currentY) * (y - currentY)) < 1;
        if (sameAsCurrent) continue;
        points.push({{ x, y }});
      }}

      if (points.length === 0) return;

      points.reverse();

      ctx.save();
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.strokeStyle = trailColors[1];
      ctx.beginPath();
      ctx.moveTo(currentX, currentY);
      ctx.lineTo(points[0].x, points[0].y);
      ctx.stroke();
      for (let i = 0; i < points.length - 1; i += 1) {{
        const color = trailColors[Math.min(i + 1, trailColors.length - 1)];
        const from = points[i];
        const to = points[i + 1];
        ctx.strokeStyle = color;
        ctx.beginPath();
        ctx.moveTo(from.x, from.y);
        ctx.lineTo(to.x, to.y);
        ctx.stroke();
      }}
      ctx.setLineDash([]);
      points.forEach((point, index) => {{
        const color = trailColors[Math.min(index + 1, trailColors.length - 1)];
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(point.x, point.y, 1.6, 0, Math.PI * 2);
        ctx.fill();
      }});
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

    function renderObjectsPanel(visibleTargets) {{
      objectsSummary.textContent = `${{visibleTargets.length}} synliga objekt`;
      if (visibleTargets.length === 0) {{
        objectsList.innerHTML = '<div class="objects-empty">Inga objekt i aktuell vy.</div>';
        return;
      }}

      objectsList.innerHTML = visibleTargets
        .map((target) => {{
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

          return `
            <div class="object-item">
              <div class="object-label">${{escapeHtml(label)}}</div>
              <div>position: ${{escapeHtml(positionText)}}</div>
              <div>last_speed: ${{escapeHtml(formatOptional(speed))}}</div>
              <div>last_altitude: ${{escapeHtml(formatOptional(altitude))}}</div>
              <div>last_seen: ${{escapeHtml(lastSeen)}}</div>
            </div>
          `;
        }})
        .join("");
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

      ctx.font = "bold 16px Courier New, monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";

      let visible = 0;
      const visibleTargets = [];
      for (const target of targets) {{
        if (typeof target.lat !== "number" || typeof target.lon !== "number") continue;
        const speed = toOptionalNumber(target.speed);
        if (!showLowSpeed && Number.isFinite(speed) && speed < 1) continue;
        const {{ dx, dy }} = toOffsetKm(target.lat, target.lon, viewCenter);
        const x = cx + (dx * pxPerKm);
        const y = cy - (dy * pxPerKm);
        const insideRadar = ((x - cx) * (x - cx)) + ((y - cy) * (y - cy)) <= (radius * radius);
        if (!insideRadar) continue;
        const course = toOptionalNumber(target.course);
        drawRecentPositions(target, cx, cy, pxPerKm, radius, x, y);
        drawCourseVector(x, y, course, speed, trailColors[0]);
        ctx.fillStyle = trailColors[0];
        const symbol = target.kind === "vessel" ? "#" : "+";
        ctx.fillText(symbol, x, y);
        visibleTargets.push(target);
        visible += 1;
      }}

      renderObjectsPanel(visibleTargets);
      drawSelectionBox();
      updateZoomBadge(rangeKm);
      const status = error ? `Error: ${{error}}` : `${{visible}} visible / ${{targets.length}} total`;
      meta.textContent = `Home: ${{homeCenter.lat.toFixed(6)}}, ${{homeCenter.lon.toFixed(6)}} | View: ${{viewCenter.lat.toFixed(6)}}, ${{viewCenter.lon.toFixed(6)}} | Range: ${{rangeKm.toFixed(2)}} km | Ringavstand: ${{ringSpacingKm.toFixed(2)}} km | ${{status}}`;
    }}

    async function loadTargets() {{
      try {{
        const response = await fetch("/ui/targets-latest", {{ cache: "no-store" }});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        const payload = await response.json();
        targets = Array.isArray(payload.targets) ? payload.targets : [];
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
    zoomInButton.addEventListener("click", zoomIn);
    zoomOutButton.addEventListener("click", zoomOut);
    zoomResetButton.addEventListener("click", resetZoom);
    showLowSpeedCheckbox.addEventListener("change", () => {{
      showLowSpeed = showLowSpeedCheckbox.checked;
      draw();
    }});
    canvas.addEventListener(
      "wheel",
      (event) => {{
        event.preventDefault();
        if (event.deltaY < 0) {{
          zoomIn();
        }} else {{
          zoomOut();
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
