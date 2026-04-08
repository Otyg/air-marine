"""Application startup wiring and runtime bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
from threading import Thread
from typing import Any, Callable, Sequence

import uvicorn

from app.api import APIRuntime, create_api_app
from app.config import Config, load_config
from app.env_utils import load_local_dotenv
from app.fixed_objects import load_fixed_radar_objects
from app.ingest_adsb import ADSBAircraftJsonIngestor
from app.ingest_adsb_inproc import ADSBInprocReader
from app.ingest_ais import AISTCPIngestor
from app.ingest_dsc import DSCDirectReader, DSCIngestError
from app.ingest_ogn import OGNTCPIngestor
from app.logging_setup import configure_logging, get_logger
from app.map_contours import build_map_contour_service
from app.models import NormalizedObservation, ScanBand, Target
from app.radio_v2 import (
    ADSBPipeline,
    AISPipeline,
    DSCPipeline,
    ExternalBackend,
    InprocBackend,
    LegacyBackend,
    MockBackend,
    OGNPipeline,
    ReaderBandSource,
    ScannerOrchestratorV2,
)
from app.scanner import HybridBandScanner, ObservationReader, ScannerConfig
from app.state import LiveState
from app.store import SQLiteStore
from app.supervisor import DecoderProcessConfig, DecoderSupervisor

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency fallback
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class ServiceComponents:
    config: Config
    state: LiveState
    store: SQLiteStore
    scanner: HybridBandScanner | ScannerOrchestratorV2
    app: Any
    scanner_worker: "ScannerWorker"


class ScannerWorker:
    """Background scanner thread controller."""

    def __init__(self, scanner: HybridBandScanner | ScannerOrchestratorV2) -> None:
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


def is_radio_connected(
    *,
    logger,
    command: Sequence[str] = ("rtl_test", "-t", "-d", "0"),
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout_seconds: float = 5.0,
) -> bool:
    """Probe for a connected RTL-SDR radio."""

    try:
        result = run_command(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        logger.warning(
            "Skipping scanner startup because radio probe command %s was not found.",
            " ".join(command),
        )
        return False
    except Exception as exc:
        logger.warning("Skipping scanner startup because radio probe failed: %s", exc)
        return False

    if result.returncode == 0:
        return True

    stderr = (result.stderr or "").strip()
    if stderr:
        logger.warning(
            "Skipping scanner startup because no radio was detected (probe failed: %s).",
            stderr.splitlines()[0],
        )
    else:
        logger.warning(
            "Skipping scanner startup because no radio was detected (probe exit code: %s).",
            result.returncode,
        )
    return False


def create_service_components(
    *,
    config: Config | None = None,
    start_scanner: bool = True,
    recover_latest_targets: bool = True,
) -> ServiceComponents:
    """Initialize config, logging, persistence, scanner, and API app."""

    load_local_dotenv(load_dotenv, project_root=PROJECT_ROOT)

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

    scanner = _create_scanner(
        config=resolved,
        state=state,
        store=store,
        logger=logger,
        adsb_snapshot_path=adsb_snapshot_path,
        decoder_process_config=decoder_process_config,
    )
    worker = ScannerWorker(scanner)
    fixed_radar_objects = load_fixed_radar_objects(resolved.fixed_objects_path, logger=logger)
    if fixed_radar_objects:
        logger.info(
            "Loaded %s fixed radar objects from %s.",
            len(fixed_radar_objects),
            resolved.fixed_objects_path,
        )

    api_runtime = APIRuntime(
        state=state,
        store=store,
        scanner=scanner,
        map_contour_service=build_map_contour_service(resolved, store=store),
        service_name=resolved.service_name,
        radar_center_lat=resolved.radar_center_lat,
        radar_center_lon=resolved.radar_center_lon,
        radio_connected=False,
        fixed_objects=fixed_radar_objects,
        default_map_source=resolved.map_source,
    )
    app = create_api_app(api_runtime)

    if start_scanner:

        @app.on_event("startup")
        async def _startup() -> None:
            if resolved.radio_backend == "mock":
                connected = True
            elif resolved.radio_backend == "external" and resolved.radio_external_use_worker:
                connected = True
            else:
                connected = is_radio_connected(logger=logger)
            api_runtime.radio_connected = connected
            if not connected:
                return
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
            pruned = store.delete_latest_targets_older_than(cutoff)
            if pruned > 0:
                logger.info(
                    "Pruned %s stale targets_latest rows older than %s before scanner start.",
                    pruned,
                    cutoff.isoformat(),
                )
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


def _create_scanner(
    *,
    config: Config,
    state: LiveState,
    store: SQLiteStore,
    logger,
    adsb_snapshot_path: Path,
    decoder_process_config: DecoderProcessConfig,
) -> HybridBandScanner | ScannerOrchestratorV2:
    scanner_config = ScannerConfig(
        adsb_window_seconds=config.adsb_window_seconds,
        ogn_window_seconds=config.ogn_window_seconds,
        ais_window_seconds=config.ais_window_seconds,
        dsc_window_seconds=config.dsc_window_seconds,
        inter_scan_pause_seconds=config.inter_scan_pause_seconds,
    )

    adsb_reader: ObservationReader = ADSBAircraftJsonIngestor(aircraft_json_path=adsb_snapshot_path)
    if config.radio_backend == "inproc" and config.adsb_inproc_source == "rtl_tcp":
        adsb_reader = ADSBInprocReader(
            rtl_host=config.adsb_inproc_rtl_host,
            rtl_port=config.adsb_inproc_rtl_port,
            sample_rate=config.adsb_inproc_sample_rate,
            gain=config.adsb_inproc_gain,
            frequency_hz=config.adsb_inproc_frequency_hz,
        )
    ogn_reader = OGNTCPIngestor.from_config(config)
    ais_reader = AISTCPIngestor.from_config(config)
    dsc_reader = _create_dsc_reader_if_enabled(config=config, logger=logger)

    if config.radio_backend == "legacy":
        return HybridBandScanner(
            adsb_reader=adsb_reader,
            ogn_reader=ogn_reader,
            ais_reader=ais_reader,
            dsc_reader=dsc_reader,
            state=state,
            store=store,
            supervisor=DecoderSupervisor(config=decoder_process_config),
            config=scanner_config,
        )

    readers: dict[ScanBand, ObservationReader] = {
        ScanBand.ADSB: adsb_reader,
        ScanBand.OGN: ogn_reader,
        ScanBand.AIS: ais_reader,
    }
    if dsc_reader is not None:
        readers[ScanBand.DSC] = dsc_reader

    if config.radio_backend == "inproc":
        sources = {band: ReaderBandSource(reader) for band, reader in readers.items()}
        backend = InprocBackend(readers, sources=sources)
    elif config.radio_backend == "external":
        backend = ExternalBackend(
            readers,
            use_worker=config.radio_external_use_worker,
            control_host=config.radio_external_control_host,
            control_port=config.radio_external_control_port,
            data_host=config.radio_external_data_host,
            data_port=config.radio_external_data_port,
        )
    elif config.radio_backend == "mock":
        backend = MockBackend(
            fixture_path=config.mock_radio_fixture_path,
            enable_timing_mode=config.mock_radio_timing_enabled,
        )
    else:
        backend = LegacyBackend(readers)

    pipelines = {
        ScanBand.AIS: AISPipeline(),
        ScanBand.ADSB: ADSBPipeline(),
        ScanBand.OGN: OGNPipeline(),
        ScanBand.DSC: DSCPipeline(),
    }
    return ScannerOrchestratorV2(
        backend=backend,
        pipelines=pipelines,
        state=state,
        store=store,
        config=scanner_config,
    )


def _create_dsc_reader_if_enabled(*, config: Config, logger) -> ObservationReader | None:
    if config.dsc_window_seconds <= 0:
        return None
    try:
        dsc_reader = DSCDirectReader(
            rtl_host=config.dsc_rtl_host,
            rtl_port=config.dsc_rtl_port,
            sample_rate=config.dsc_rtl_sample_rate,
            gain=config.dsc_rtl_gain,
        )
        if dsc_reader.connect():
            logger.info("DSC reader initialized and connected")
            return dsc_reader
        logger.warning("DSC reader created but failed to connect")
    except DSCIngestError as exc:
        logger.warning("DSC reader not available: %s", exc)
    return None


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
