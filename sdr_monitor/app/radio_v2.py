"""Radio/scanner v2 orchestration with pluggable backends and pipelines.

This module keeps the existing scanner outward contract stable while moving
radio collection behind a feature-flagged backend architecture.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import random
import socket
from pathlib import Path
import time
from threading import Event, Lock
from typing import Any, Callable, Mapping, Protocol

from app.ingest_adsb import parse_readsb_aircraft_json
from app.ingest_ais import parse_ais_nmea_lines
from app.ingest_ogn import parse_ogn_aprs_lines
from app.models import NormalizedObservation, ScanBand
from app.scanner import SCAN_MODE_VALUES, ScanMode, ScannerConfig
from app.state import LiveState
from app.store import SQLiteStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_frequency_hz(hz: int) -> int:
    frequency = int(hz)
    # Broad SDR-safe envelope for current sources.
    if frequency < 100_000 or frequency > 2_000_000_000:
        raise ValueError(f"Invalid frequency {frequency}. Expected range 100000..2000000000 Hz.")
    return frequency


def _validate_gain_db(db: int) -> int:
    gain = int(db)
    if gain < 0 or gain > 100:
        raise ValueError(f"Invalid gain {gain}. Expected range 0..100 dB.")
    return gain


@dataclass(frozen=True, slots=True)
class RadioStatus:
    backend_name: str
    is_running: bool
    connected: bool
    active_band: ScanBand | None = None
    active_frequency_hz: int | None = None
    gain_db: int | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_name": self.backend_name,
            "is_running": self.is_running,
            "connected": self.connected,
            "active_band": self.active_band.value if self.active_band else None,
            "active_frequency_hz": self.active_frequency_hz,
            "gain_db": self.gain_db,
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class IQChunkEvent:
    source_band: ScanBand
    ts: datetime
    iq_bytes: bytes
    sample_rate: int


@dataclass(frozen=True, slots=True)
class DecodedFrameEvent:
    source_band: ScanBand
    ts: datetime
    frame_type: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ObservationEvent:
    source_band: ScanBand
    observation: NormalizedObservation


RadioEvent = IQChunkEvent | DecodedFrameEvent | ObservationEvent


class RadioBackend(Protocol):
    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def retune(self, hz: int) -> None:
        ...

    def set_gain(self, db: int) -> None:
        ...

    def read(self, timeout_s: float, *, band: ScanBand | None = None) -> list[RadioEvent]:
        ...

    def status(self) -> RadioStatus:
        ...


class ObservationPipeline(Protocol):
    def process(self, events: list[RadioEvent]) -> list[NormalizedObservation]:
        ...


class AISPipeline:
    def process(self, events: list[RadioEvent]) -> list[NormalizedObservation]:
        observations: list[NormalizedObservation] = []
        for event in events:
            if isinstance(event, ObservationEvent):
                observations.append(event.observation)
                continue
            if not isinstance(event, DecodedFrameEvent):
                continue
            if event.frame_type != "ais_nmea_lines":
                continue
            lines = event.payload.get("lines")
            if isinstance(lines, list):
                observations.extend(parse_ais_nmea_lines([str(line) for line in lines]))
        return observations


class ADSBPipeline:
    def process(self, events: list[RadioEvent]) -> list[NormalizedObservation]:
        observations: list[NormalizedObservation] = []
        for event in events:
            if isinstance(event, ObservationEvent):
                observations.append(event.observation)
                continue
            if not isinstance(event, DecodedFrameEvent):
                continue
            if event.frame_type != "adsb_snapshot":
                continue
            payload = event.payload.get("payload")
            if isinstance(payload, Mapping):
                observations.extend(parse_readsb_aircraft_json(payload))
        return observations


class OGNPipeline:
    def process(self, events: list[RadioEvent]) -> list[NormalizedObservation]:
        observations: list[NormalizedObservation] = []
        for event in events:
            if isinstance(event, ObservationEvent):
                observations.append(event.observation)
                continue
            if not isinstance(event, DecodedFrameEvent):
                continue
            if event.frame_type != "ogn_aprs_lines":
                continue
            lines = event.payload.get("lines")
            if isinstance(lines, list):
                observations.extend(parse_ogn_aprs_lines([str(line) for line in lines]))
        return observations


class DSCPipeline:
    def process(self, events: list[RadioEvent]) -> list[NormalizedObservation]:
        observations: list[NormalizedObservation] = []
        for event in events:
            if isinstance(event, ObservationEvent):
                observations.append(event.observation)
                continue
            if not isinstance(event, DecodedFrameEvent):
                continue
            if event.frame_type != "dsc_observations":
                continue
            raw = event.payload.get("observations")
            if not isinstance(raw, list):
                continue
            for row in raw:
                if isinstance(row, Mapping):
                    try:
                        observations.append(NormalizedObservation.from_dict(dict(row)))
                    except Exception:
                        continue
        return observations


class LegacyBackend:
    """Backend adapter over existing per-band readers.

    This enables parity testing while keeping the v2 orchestration path.
    """

    def __init__(self, readers: Mapping[ScanBand, Any]):
        self._readers = dict(readers)
        self._running = False
        self._connected = False
        self._active_frequency_hz: int | None = None
        self._gain_db: int | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        self._running = True
        self._connected = True

    def stop(self) -> None:
        self._running = False

    def retune(self, hz: int) -> None:
        try:
            self._active_frequency_hz = _validate_frequency_hz(hz)
            self._last_error = None
        except ValueError as exc:
            self._last_error = str(exc)
            raise

    def set_gain(self, db: int) -> None:
        try:
            self._gain_db = _validate_gain_db(db)
            self._last_error = None
        except ValueError as exc:
            self._last_error = str(exc)
            raise

    def read(self, timeout_s: float, *, band: ScanBand | None = None) -> list[RadioEvent]:
        if not self._running or band is None:
            return []
        reader = self._readers.get(band)
        if reader is None:
            return []

        kwargs: dict[str, Any] = {}
        if timeout_s > 0:
            kwargs["timeout_seconds"] = timeout_s

        try:
            observations = reader.read_observations(**kwargs)
        except TypeError:
            observations = reader.read_observations()
        except Exception as exc:
            self._last_error = str(exc)
            return []

        now = _utcnow()
        return [
            ObservationEvent(source_band=band, observation=observation)
            for observation in observations
            if isinstance(observation, NormalizedObservation)
        ]

    def status(self) -> RadioStatus:
        return RadioStatus(
            backend_name="legacy",
            is_running=self._running,
            connected=self._connected,
            active_frequency_hz=self._active_frequency_hz,
            gain_db=self._gain_db,
            last_error=self._last_error,
        )


class InprocBackend(LegacyBackend):
    """In-process backend (initially backed by existing adapters).

    In a future iteration this can read directly from SDR drivers.
    """

    def status(self) -> RadioStatus:
        status = super().status()
        return RadioStatus(
            backend_name="inproc",
            is_running=status.is_running,
            connected=status.connected,
            active_frequency_hz=status.active_frequency_hz,
            gain_db=status.gain_db,
            last_error=status.last_error,
        )


class ExternalBackend(LegacyBackend):
    """External radio-worker backend with optional reader fallback.

    When `use_worker` is enabled this backend sends control commands over a
    control socket and consumes JSON-line events from a data socket.
    """

    def __init__(
        self,
        readers: Mapping[ScanBand, Any],
        *,
        use_worker: bool = False,
        control_host: str = "127.0.0.1",
        control_port: int = 17601,
        data_host: str = "127.0.0.1",
        data_port: int = 17602,
    ) -> None:
        super().__init__(readers)
        self._use_worker = bool(use_worker)
        self._control_host = control_host
        self._control_port = int(control_port)
        self._data_host = data_host
        self._data_port = int(data_port)
        self._worker_connected = False

    def retune(self, hz: int) -> None:
        super().retune(hz)
        if not self._use_worker:
            return
        self._send_control_command({"cmd": "retune", "hz": self._active_frequency_hz})

    def set_gain(self, db: int) -> None:
        super().set_gain(db)
        if not self._use_worker:
            return
        self._send_control_command({"cmd": "set_gain", "db": self._gain_db})

    def read(self, timeout_s: float, *, band: ScanBand | None = None) -> list[RadioEvent]:
        if not self._use_worker:
            return super().read(timeout_s, band=band)
        try:
            events = self._read_worker_events(timeout_s=timeout_s, band=band)
            self._last_error = None
            return events
        except Exception as exc:
            self._last_error = f"external worker read failed: {exc}"
            self._worker_connected = False
            return super().read(timeout_s, band=band)

    def _send_control_command(self, payload: dict[str, Any]) -> None:
        message = json.dumps(payload) + "\n"
        try:
            with socket.create_connection((self._control_host, self._control_port), timeout=1.0) as sock:
                sock.sendall(message.encode("utf-8"))
                sock.settimeout(1.0)
                response = sock.recv(4096).decode("utf-8", errors="replace").strip()
            if response:
                parsed = json.loads(response)
                if isinstance(parsed, Mapping) and parsed.get("ok") is False:
                    raise RuntimeError(str(parsed.get("error") or "control command rejected"))
            self._worker_connected = True
            self._last_error = None
        except Exception as exc:
            self._worker_connected = False
            self._last_error = f"external worker control failed: {exc}"
            raise RuntimeError(self._last_error) from exc

    def _read_worker_events(self, *, timeout_s: float, band: ScanBand | None) -> list[RadioEvent]:
        deadline = time.monotonic() + max(0.0, timeout_s)
        connect_timeout = max(0.05, min(1.0, timeout_s if timeout_s > 0 else 0.2))
        events: list[RadioEvent] = []
        with socket.create_connection((self._data_host, self._data_port), timeout=connect_timeout) as sock:
            stream = sock.makefile("r", encoding="utf-8", errors="replace")
            while True:
                if timeout_s > 0 and time.monotonic() >= deadline:
                    break
                read_timeout = max(0.05, min(1.0, deadline - time.monotonic())) if timeout_s > 0 else 0.2
                sock.settimeout(read_timeout)
                try:
                    line = stream.readline()
                except socket.timeout:
                    break
                if not line:
                    break
                payload = json.loads(line)
                if not isinstance(payload, Mapping):
                    continue
                event = self._event_from_worker_payload(payload)
                if event is None:
                    continue
                if band is not None and getattr(event, "source_band", None) != band:
                    continue
                events.append(event)
        self._worker_connected = True
        return events

    @staticmethod
    def _event_from_worker_payload(payload: Mapping[str, Any]) -> RadioEvent | None:
        event_type = str(payload.get("type") or payload.get("event_type") or "").strip().lower()
        if not event_type:
            return None

        source_band_raw = payload.get("source_band", ScanBand.AIS.value)
        try:
            source_band = ScanBand(str(source_band_raw))
        except ValueError:
            return None

        if event_type == "observation":
            observation_payload = payload.get("observation")
            if not isinstance(observation_payload, Mapping):
                return None
            try:
                observation = NormalizedObservation.from_dict(dict(observation_payload))
            except Exception:
                return None
            return ObservationEvent(source_band=source_band, observation=observation)

        if event_type == "decoded_frame":
            frame_type = str(payload.get("frame_type") or "generic")
            frame_payload = payload.get("payload")
            return DecodedFrameEvent(
                source_band=source_band,
                ts=_utcnow(),
                frame_type=frame_type,
                payload=dict(frame_payload) if isinstance(frame_payload, Mapping) else {},
            )

        if event_type == "iq_chunk":
            iq_hex = payload.get("iq_hex")
            if not isinstance(iq_hex, str):
                return None
            try:
                iq_bytes = bytes.fromhex(iq_hex)
            except ValueError:
                return None
            return IQChunkEvent(
                source_band=source_band,
                ts=_utcnow(),
                iq_bytes=iq_bytes,
                sample_rate=int(payload.get("sample_rate", 48000)),
            )
        return None

    def status(self) -> RadioStatus:
        status = super().status()
        connected = self._worker_connected if self._use_worker else status.connected
        return RadioStatus(
            backend_name="external",
            is_running=status.is_running,
            connected=connected,
            active_frequency_hz=status.active_frequency_hz,
            gain_db=status.gain_db,
            last_error=status.last_error,
        )


@dataclass(frozen=True, slots=True)
class MockTimelineEntry:
    t_ms: int
    band: ScanBand
    event_type: str
    payload: dict[str, Any]
    fault: str | None = None


class MockBackend:
    """Fixture-driven mock radio backend.

    Supports deterministic replay by default and optional timing/jitter mode.
    """

    def __init__(
        self,
        *,
        fixture: Mapping[str, Any] | None = None,
        fixture_path: Path | None = None,
        enable_timing_mode: bool = False,
    ) -> None:
        if fixture is None and fixture_path is None:
            raise ValueError("Either fixture or fixture_path must be provided.")

        if fixture is None:
            loaded = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
            if not isinstance(loaded, Mapping):
                raise ValueError("Mock fixture must be a JSON object.")
            fixture = loaded

        self._fixture = dict(fixture)
        self._payload_catalog = self._parse_payload_catalog(self._fixture.get("payloads"))
        self._seed = int(self._fixture.get("seed", 0))
        self._rng = random.Random(self._seed)
        self._sample_rate = int(self._fixture.get("sample_rate", 48000))
        default_band = str(self._fixture.get("default_band", ScanBand.AIS.value))
        self._active_band = ScanBand(default_band)

        controls = self._fixture.get("controls")
        controls_map = dict(controls) if isinstance(controls, Mapping) else {}
        self._drop_rate = float(controls_map.get("drop_rate", 0.0))
        self._jitter_ms = int(controls_map.get("jitter_profile", {}).get("max_jitter_ms", 0)) if isinstance(controls_map.get("jitter_profile"), Mapping) else 0
        self._timing_mode = bool(enable_timing_mode)

        retune_map = controls_map.get("retune_map")
        self._retune_map: dict[int, ScanBand] = {}
        if isinstance(retune_map, Mapping):
            for key, value in retune_map.items():
                try:
                    self._retune_map[int(str(key))] = ScanBand(str(value))
                except Exception:
                    continue

        self._timeline: list[MockTimelineEntry] = self._parse_timeline(self._fixture.get("timeline"))
        self._cursor = 0
        self._running = False
        self._connected = True
        self._gain_db: int | None = None
        self._active_frequency_hz: int | None = None
        self._last_error: str | None = None

    @staticmethod
    def _parse_payload_catalog(payloads_raw: Any) -> dict[str, dict[str, Any]]:
        if payloads_raw is None:
            return {}
        if not isinstance(payloads_raw, Mapping):
            raise ValueError("mock fixture payloads must be an object")
        catalog: dict[str, dict[str, Any]] = {}
        for key, value in payloads_raw.items():
            ref = str(key).strip()
            if not ref:
                continue
            if not isinstance(value, Mapping):
                raise ValueError(f"mock fixture payload {ref!r} must be an object")
            catalog[ref] = dict(value)
        return catalog

    def _parse_timeline(self, timeline_raw: Any) -> list[MockTimelineEntry]:
        if not isinstance(timeline_raw, list):
            return []
        parsed: list[MockTimelineEntry] = []
        for row in timeline_raw:
            if not isinstance(row, Mapping):
                continue
            try:
                band = ScanBand(str(row.get("band", self._active_band.value)))
                event_type = str(row.get("event_type", "observation"))
                t_ms = int(row.get("t_ms", 0))
            except Exception:
                continue
            payload = row.get("payload")
            payload_ref = row.get("payload_ref")
            if payload_ref is not None and payload is not None:
                raise ValueError("mock timeline entry cannot define both payload and payload_ref")
            resolved_payload: dict[str, Any]
            if payload_ref is not None:
                ref = str(payload_ref).strip()
                if not ref:
                    raise ValueError("mock timeline payload_ref must be a non-empty string")
                if ref not in self._payload_catalog:
                    raise ValueError(f"mock timeline payload_ref {ref!r} not found in payloads catalog")
                resolved_payload = dict(self._payload_catalog[ref])
            elif isinstance(payload, Mapping):
                resolved_payload = dict(payload)
            elif payload is None:
                resolved_payload = {}
            else:
                raise ValueError("mock timeline payload must be an object when provided")
            parsed.append(
                MockTimelineEntry(
                    t_ms=t_ms,
                    band=band,
                    event_type=event_type,
                    payload=resolved_payload,
                    fault=str(row.get("fault")) if row.get("fault") is not None else None,
                )
            )
        parsed.sort(key=lambda item: item.t_ms)
        return parsed

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def retune(self, hz: int) -> None:
        try:
            self._active_frequency_hz = _validate_frequency_hz(hz)
            self._last_error = None
        except ValueError as exc:
            self._last_error = str(exc)
            raise
        mapped = self._retune_map.get(self._active_frequency_hz)
        if mapped is not None:
            self._active_band = mapped

    def set_gain(self, db: int) -> None:
        try:
            self._gain_db = _validate_gain_db(db)
            self._last_error = None
        except ValueError as exc:
            self._last_error = str(exc)
            raise

    def _apply_fault(self, fault: str) -> list[RadioEvent]:
        fault_key = fault.strip().lower()
        if fault_key == "timeout":
            return []
        if fault_key == "disconnect":
            self._connected = False
            return []
        if fault_key == "reconnect":
            self._connected = True
            return []
        if fault_key == "malformed_frame":
            return [
                DecodedFrameEvent(
                    source_band=self._active_band,
                    ts=_utcnow(),
                    frame_type="malformed",
                    payload={"error": "malformed_frame"},
                )
            ]
        return []

    def _event_from_entry(self, entry: MockTimelineEntry) -> RadioEvent | None:
        if entry.event_type == "observation":
            obs_payload = entry.payload.get("observation")
            if not isinstance(obs_payload, Mapping):
                return None
            try:
                observation = NormalizedObservation.from_dict(dict(obs_payload))
            except Exception:
                return None
            return ObservationEvent(source_band=entry.band, observation=observation)

        if entry.event_type == "decoded_frame":
            frame_type = str(entry.payload.get("frame_type", "generic"))
            payload = entry.payload.get("payload")
            payload_dict = dict(payload) if isinstance(payload, Mapping) else {}
            return DecodedFrameEvent(
                source_band=entry.band,
                ts=_utcnow(),
                frame_type=frame_type,
                payload=payload_dict,
            )

        if entry.event_type == "iq_chunk":
            iq_hex = entry.payload.get("iq_hex", "")
            if not isinstance(iq_hex, str):
                return None
            try:
                iq_bytes = bytes.fromhex(iq_hex)
            except ValueError:
                return None
            return IQChunkEvent(
                source_band=entry.band,
                ts=_utcnow(),
                iq_bytes=iq_bytes,
                sample_rate=int(entry.payload.get("sample_rate", self._sample_rate)),
            )

        return None

    def read(self, timeout_s: float, *, band: ScanBand | None = None) -> list[RadioEvent]:
        if not self._running:
            return []

        selected_band = band or self._active_band
        self._active_band = selected_band
        if not self._connected:
            while self._cursor < len(self._timeline):
                entry = self._timeline[self._cursor]
                self._cursor += 1
                if entry.band != selected_band:
                    continue
                if entry.fault and entry.fault.strip().lower() == "reconnect":
                    self._connected = True
                    break
            return []

        if self._cursor >= len(self._timeline):
            return []

        events: list[RadioEvent] = []
        start = time.monotonic()

        while self._cursor < len(self._timeline):
            if timeout_s > 0 and time.monotonic() - start >= timeout_s:
                break

            entry = self._timeline[self._cursor]
            if entry.band != selected_band:
                self._cursor += 1
                continue

            self._cursor += 1

            if entry.fault:
                events.extend(self._apply_fault(entry.fault))
                if entry.fault.strip().lower() in {"timeout", "disconnect"}:
                    break
                continue

            if self._drop_rate > 0 and self._rng.random() < self._drop_rate:
                continue

            event = self._event_from_entry(entry)
            if event is None:
                continue

            if self._timing_mode and self._jitter_ms > 0:
                jitter_delta = self._rng.randint(-self._jitter_ms, self._jitter_ms)
                if isinstance(event, IQChunkEvent):
                    event = IQChunkEvent(
                        source_band=event.source_band,
                        ts=event.ts + timedelta(milliseconds=jitter_delta),
                        iq_bytes=event.iq_bytes,
                        sample_rate=event.sample_rate,
                    )
                elif isinstance(event, DecodedFrameEvent):
                    event = DecodedFrameEvent(
                        source_band=event.source_band,
                        ts=event.ts + timedelta(milliseconds=jitter_delta),
                        frame_type=event.frame_type,
                        payload=event.payload,
                    )
            events.append(event)

            if not self._timing_mode:
                break

        return events

    def status(self) -> RadioStatus:
        return RadioStatus(
            backend_name="mock",
            is_running=self._running,
            connected=self._connected,
            active_band=self._active_band,
            active_frequency_hz=self._active_frequency_hz,
            gain_db=self._gain_db,
            last_error=self._last_error,
        )


class ScannerOrchestratorV2:
    """Scanner implementation using `RadioBackend` + per-band pipelines."""

    def __init__(
        self,
        *,
        backend: RadioBackend,
        pipelines: Mapping[ScanBand, ObservationPipeline],
        state: LiveState,
        store: SQLiteStore | None,
        config: ScannerConfig | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._backend = backend
        self._pipelines = dict(pipelines)
        self._state = state
        self._store = store
        self._config = config or ScannerConfig()
        self._sleep_fn = sleep_fn or time.sleep
        self._now_fn = now_fn or _utcnow
        self._stop_event = Event()
        self._mode_lock = Lock()

        self._active_scan_band: ScanBand | None = None
        self._last_cycle_start: datetime | None = None
        self._last_scan_switch: datetime | None = None
        self._last_error: str | None = None
        self._cycle_count = 0
        self._scan_mode = ScanMode.HYBRID

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def set_scan_mode(self, mode: str | ScanMode) -> None:
        try:
            resolved_mode = mode if isinstance(mode, ScanMode) else ScanMode(str(mode).strip().lower())
        except ValueError as exc:
            valid_modes = ", ".join(SCAN_MODE_VALUES)
            raise ValueError(f"Unsupported scan mode {mode!r}. Expected one of: {valid_modes}.") from exc
        with self._mode_lock:
            self._scan_mode = resolved_mode

    def get_scan_mode(self) -> ScanMode:
        with self._mode_lock:
            return self._scan_mode

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self._backend.stop()
        except Exception as exc:  # pragma: no cover
            self._record_error(f"stop: {exc}")
        self._active_scan_band = None

    def run_cycle(self) -> None:
        self._last_cycle_start = self._now_fn()
        self._backend.start()
        scan_mode = self.get_scan_mode()

        if scan_mode == ScanMode.HYBRID:
            self._run_window(ScanBand.AIS, self._config.ais_window_seconds)
            if self._stop_event.is_set():
                return
            self._pause_between_scans()
            self._run_window(ScanBand.ADSB, self._config.adsb_window_seconds)
            if self._stop_event.is_set():
                return
            self._pause_between_scans()
            if self._config.ogn_window_seconds > 0:
                self._run_window(ScanBand.OGN, self._config.ogn_window_seconds)
                if self._stop_event.is_set():
                    return
                self._pause_between_scans()
            if self._config.dsc_window_seconds > 0:
                self._run_window(ScanBand.DSC, self._config.dsc_window_seconds)
                if self._stop_event.is_set():
                    return
                self._pause_between_scans()
        elif scan_mode == ScanMode.CONTINUOUS_AIS:
            self._run_window(ScanBand.AIS, self._config.ais_window_seconds)
        elif scan_mode == ScanMode.CONTINUOUS_ADSB:
            self._run_window(ScanBand.ADSB, self._config.adsb_window_seconds)
        elif scan_mode == ScanMode.CONTINUOUS_OGN:
            self._run_window(ScanBand.OGN, self._config.ogn_window_seconds)

        self._cycle_count += 1

    def run_forever(self, *, max_cycles: int | None = None) -> None:
        while not self._stop_event.is_set():
            if max_cycles is not None and self._cycle_count >= max_cycles:
                break
            self.run_cycle()
            self._prune_stale_latest_targets()
        self._backend.stop()
        self._active_scan_band = None

    def status(self) -> dict[str, Any]:
        backend_status = self._backend.status().to_dict()
        return {
            "active_scan_band": self._active_scan_band.value if self._active_scan_band else None,
            "last_cycle_start": self._last_cycle_start,
            "last_scan_switch": self._last_scan_switch,
            "last_error": self._last_error,
            "cycle_count": self._cycle_count,
            "scan_mode": self.get_scan_mode().value,
            "adsb_window_seconds": self._config.adsb_window_seconds,
            "ogn_window_seconds": self._config.ogn_window_seconds,
            "ais_window_seconds": self._config.ais_window_seconds,
            "inter_scan_pause_seconds": self._config.inter_scan_pause_seconds,
            "supervisor": backend_status,
        }

    def _run_window(self, band: ScanBand, window_seconds: float) -> None:
        started = self._now_fn()
        self._active_scan_band = band
        self._last_scan_switch = started
        pipeline = self._pipelines.get(band)
        if pipeline is None:
            self._active_scan_band = None
            return

        try:
            events = self._backend.read(window_seconds, band=band)
            observations = pipeline.process(events)
            self._ingest_observations(observations)
        except Exception as exc:
            self._record_error(f"{band.value}: ingest error: {exc}")
        finally:
            elapsed = (self._now_fn() - started).total_seconds()
            remaining = max(0.0, window_seconds - elapsed)
            if remaining > 0:
                self._sleep_fn(remaining)

        self._active_scan_band = None

    def _ingest_observations(self, observations: list[NormalizedObservation]) -> None:
        for observation in observations:
            state_snapshot = self._state.upsert_observation(observation)
            if self._store is None:
                continue
            try:
                self._store.persist_observation_and_target(observation, state_snapshot.target)
            except Exception as exc:
                self._record_error(f"store: {exc}")

    def _record_error(self, message: str) -> None:
        self._last_error = message

    def _pause_between_scans(self) -> None:
        pause_seconds = self._config.inter_scan_pause_seconds
        if pause_seconds > 0:
            self._sleep_fn(pause_seconds)

    def _prune_stale_latest_targets(self) -> None:
        if self._store is None:
            return
        cutoff = self._now_fn() - timedelta(minutes=5)
        try:
            self._store.delete_latest_targets_older_than(cutoff)
        except Exception as exc:
            self._record_error(f"store prune: {exc}")
