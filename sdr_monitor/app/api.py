"""HTTP API endpoints for service health, live targets, stats, and history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query

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


def create_api_app(runtime: APIRuntime) -> FastAPI:
    """Create the phase-9 FastAPI application."""

    app = FastAPI(title=runtime.service_name)

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
