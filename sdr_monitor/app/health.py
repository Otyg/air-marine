"""Service health reporting helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.radio_v2 import ScannerOrchestratorV2
from app.scanner import HybridBandScanner
from app.store import SQLiteStore


def build_health_report(
    *,
    service_name: str,
    scanner: HybridBandScanner | ScannerOrchestratorV2 | None,
    store: SQLiteStore | None,
) -> dict[str, Any]:
    """Build a normalized service health payload."""

    scanner_status = scanner.status() if scanner else {}
    last_scan_error = scanner_status.get("last_error")
    supervisor_status = scanner_status.get("supervisor")
    last_decoder_error = (
        supervisor_status.get("last_error")
        if isinstance(supervisor_status, dict)
        else None
    )
    database_available = is_database_available(store)
    overall_status = (
        "ok"
        if database_available and not last_scan_error and not last_decoder_error
        else "degraded"
    )
    return {
        "service": service_name,
        "overall_status": overall_status,
        "active_scan_band": scanner_status.get("active_scan_band"),
        "last_cycle_start": _to_iso(scanner_status.get("last_cycle_start")),
        "last_decoder_error": last_decoder_error,
        "last_scan_error": last_scan_error,
        "database_available": database_available,
    }


def is_database_available(store: SQLiteStore | None) -> bool:
    if store is None:
        return False
    try:
        store.count_observations()
    except Exception:
        return False
    return True


def _to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
