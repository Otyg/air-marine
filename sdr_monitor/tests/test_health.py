from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.health import build_health_report
from app.store import SQLiteStore


@dataclass
class FakeScanner:
    status_payload: dict

    def status(self) -> dict:
        return dict(self.status_payload)


def test_build_health_report_ok_when_db_and_no_errors(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "health.sqlite3")
    store.initialize()
    scanner = FakeScanner(
        status_payload={
            "active_scan_band": "adsb",
            "last_cycle_start": datetime(2026, 3, 31, 11, 0, tzinfo=timezone.utc),
            "last_error": None,
            "supervisor": {"last_error": None},
        }
    )

    report = build_health_report(service_name="air-marine", scanner=scanner, store=store)
    assert report["overall_status"] == "ok"
    assert report["database_available"] is True
    assert report["active_scan_band"] == "adsb"


def test_build_health_report_degraded_without_store() -> None:
    report = build_health_report(service_name="air-marine", scanner=None, store=None)
    assert report["overall_status"] == "degraded"
    assert report["database_available"] is False
