"""Application startup wiring and runtime bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from threading import Thread
from typing import Any

import uvicorn

from app.api import APIRuntime, create_api_app
from app.config import Config, load_config
from app.ingest_adsb import ADSBAircraftJsonIngestor
from app.ingest_ais import AISTCPIngestor
from app.logging_setup import configure_logging, get_logger
from app.models import NormalizedObservation, Target
from app.scanner import HybridBandScanner, ScannerConfig
from app.state import LiveState
from app.store import SQLiteStore
from app.supervisor import DecoderProcessConfig, DecoderSupervisor

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency fallback
    load_dotenv = None


@dataclass(slots=True)
class ServiceComponents:
    config: Config
    state: LiveState
    store: SQLiteStore
    scanner: HybridBandScanner
    app: Any
    scanner_worker: "ScannerWorker"


class ScannerWorker:
    """Background scanner thread controller."""

    def __init__(self, scanner: HybridBandScanner) -> None:
        self._scanner = scanner
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = Thread(
            target=self._scanner.run_forever,
            kwargs={"max_cycles": None},
            name="scanner-loop",
            daemon=True,
        )
        self._thread.start()

    def stop(self, join_timeout_seconds: float = 1.0) -> None:
        self._scanner.stop()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout_seconds)

    def status(self) -> dict[str, Any]:
        return {
            "is_alive": bool(self._thread and self._thread.is_alive()),
            "thread_name": self._thread.name if self._thread else None,
        }


def create_service_components(
    *,
    config: Config | None = None,
    start_scanner: bool = True,
    recover_latest_targets: bool = True,
) -> ServiceComponents:
    """Initialize config, logging, persistence, scanner, and API app."""

    if load_dotenv is not None:
        load_dotenv()

    resolved = config or load_config()
    configure_logging(resolved)
    logger = get_logger(__name__)

    store = SQLiteStore(resolved.sqlite_path)
    store.initialize()

    state = LiveState(
        fresh_seconds=resolved.fresh_seconds,
        aging_seconds=resolved.aging_seconds,
        max_positions_per_target=resolved.max_positions_per_target,
    )

    if recover_latest_targets:
        try:
            restored = recover_state_from_latest_targets(state=state, store=store)
            logger.info("Recovered %s targets from SQLite latest-state table.", restored)
        except Exception as exc:
            logger.exception("Failed to restore latest targets from SQLite: %s", exc)

    adsb_snapshot_path = resolve_adsb_snapshot_path(resolved.readsb_aircraft_json, logger=logger)
    decoder_process_config = build_decoder_process_config(
        adsb_snapshot_path=adsb_snapshot_path,
        ais_tcp_port=resolved.ais_tcp_port,
    )

    scanner = HybridBandScanner(
        adsb_reader=ADSBAircraftJsonIngestor(aircraft_json_path=adsb_snapshot_path),
        ais_reader=AISTCPIngestor.from_config(resolved),
        state=state,
        store=store,
        supervisor=DecoderSupervisor(config=decoder_process_config),
        config=ScannerConfig(
            adsb_window_seconds=resolved.adsb_window_seconds,
            ais_window_seconds=resolved.ais_window_seconds,
        ),
    )
    worker = ScannerWorker(scanner)

    api_runtime = APIRuntime(
        state=state,
        store=store,
        scanner=scanner,
        service_name=resolved.service_name,
    )
    app = create_api_app(api_runtime)

    if start_scanner:

        @app.on_event("startup")
        async def _startup() -> None:
            worker.start()
            logger.info("Scanner background thread started.")

        @app.on_event("shutdown")
        async def _shutdown() -> None:
            worker.stop()
            logger.info("Scanner background thread stopped.")

    components = ServiceComponents(
        config=resolved,
        state=state,
        store=store,
        scanner=scanner,
        app=app,
        scanner_worker=worker,
    )
    app.state.components = components
    return components


def recover_state_from_latest_targets(
    *,
    state: LiveState,
    store: SQLiteStore,
    limit: int | None = None,
) -> int:
    """Hydrate in-memory state from `targets_latest` rows."""

    latest_targets = store.load_latest_targets(limit=limit)
    for target in latest_targets:
        state.upsert_observation(_target_to_observation(target))
    return len(latest_targets)


def build_decoder_process_config(
    *,
    adsb_snapshot_path: Path,
    ais_tcp_port: int,
) -> DecoderProcessConfig:
    """Build decoder command lines aligned with ingest adapters."""

    adsb_json_dir = adsb_snapshot_path.parent
    return DecoderProcessConfig(
        adsb_command=(
            "readsb",
            "--device-type",
            "rtlsdr",
            "--write-json",
            str(adsb_json_dir),
            "--write-json-every",
            "1",
            "--quiet",
        ),
        ais_command=(
            "rtl_ais",
            "-T",
            "-P",
            str(ais_tcp_port),
            "-n",
        ),
    )


def resolve_adsb_snapshot_path(configured_path: Path, *, logger) -> Path:
    """Resolve a writable ADS-B snapshot path, fallback when needed."""

    candidate = configured_path.expanduser()
    candidate_dir = candidate.parent

    try:
        candidate_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fallback = Path("./data/readsb/aircraft.json")
        fallback.parent.mkdir(parents=True, exist_ok=True)
        logger.warning(
            "ADS-B snapshot path %s is not creatable (%s). Falling back to %s.",
            candidate,
            exc,
            fallback,
        )
        return fallback

    if not os.access(candidate_dir, os.W_OK):
        fallback = Path("./data/readsb/aircraft.json")
        fallback.parent.mkdir(parents=True, exist_ok=True)
        logger.warning(
            "ADS-B snapshot directory %s is not writable. Falling back to %s.",
            candidate_dir,
            fallback,
        )
        return fallback

    return candidate


def _target_to_observation(target: Target) -> NormalizedObservation:
    return NormalizedObservation(
        target_id=target.target_id,
        source=target.source,
        kind=target.kind,
        observed_at=target.last_seen,
        label=target.label,
        lat=target.lat,
        lon=target.lon,
        course=target.course,
        speed=target.speed,
        altitude=target.altitude,
        last_scan_band=target.last_scan_band,
        icao24=target.icao24,
        callsign=target.callsign,
        squawk=target.squawk,
        vertical_rate=target.vertical_rate,
        mmsi=target.mmsi,
        shipname=target.shipname,
        nav_status=target.nav_status,
        payload_json={"recovered_from": "targets_latest"},
    )


def create_application(
    *,
    config: Config | None = None,
    start_scanner: bool = True,
    recover_latest_targets: bool = True,
):
    """Build and return the FastAPI app."""

    return create_service_components(
        config=config,
        start_scanner=start_scanner,
        recover_latest_targets=recover_latest_targets,
    ).app


def main() -> None:
    """CLI entrypoint for running the HTTP service."""

    components = create_service_components(
        config=None,
        start_scanner=True,
        recover_latest_targets=True,
    )
    uvicorn.run(
        components.app,
        host=components.config.api_host,
        port=components.config.api_port,
        log_level=components.config.log_level.lower(),
    )


if __name__ == "__main__":
    main()
