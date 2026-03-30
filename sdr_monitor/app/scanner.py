"""Band scanner orchestration for alternating ADS-B and AIS windows."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event
from typing import Any, Callable, Protocol

from app.models import NormalizedObservation, ScanBand
from app.state import LiveState
from app.store import SQLiteStore
from app.supervisor import DecoderSupervisor


class ObservationReader(Protocol):
    def read_observations(self, **kwargs: Any) -> list[NormalizedObservation]:
        ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ScannerConfig:
    adsb_window_seconds: float = 8.0
    ais_window_seconds: float = 12.0


class HybridBandScanner:
    """Alternates between ADS-B and AIS scan windows."""

    def __init__(
        self,
        *,
        adsb_reader: ObservationReader,
        ais_reader: ObservationReader,
        state: LiveState,
        store: SQLiteStore | None,
        supervisor: DecoderSupervisor,
        config: ScannerConfig | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._adsb_reader = adsb_reader
        self._ais_reader = ais_reader
        self._state = state
        self._store = store
        self._supervisor = supervisor
        self._config = config or ScannerConfig()
        self._sleep_fn = sleep_fn or time.sleep
        self._now_fn = now_fn or _utcnow
        self._stop_event = Event()

        self._active_scan_band: ScanBand | None = None
        self._last_cycle_start: datetime | None = None
        self._last_scan_switch: datetime | None = None
        self._last_error: str | None = None
        self._cycle_count = 0

        if self._config.adsb_window_seconds <= 0:
            raise ValueError("adsb_window_seconds must be > 0")
        if self._config.ais_window_seconds <= 0:
            raise ValueError("ais_window_seconds must be > 0")

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self._supervisor.stop_active()
        except Exception as exc:  # pragma: no cover - defensive path
            self._record_error(f"stop: {exc}")
        self._active_scan_band = None

    def run_cycle(self) -> None:
        """Run one full ADS-B + AIS cycle."""

        self._last_cycle_start = self._now_fn()
        self._run_band_window(
            band=ScanBand.ADSB,
            window_seconds=self._config.adsb_window_seconds,
            reader=self._adsb_reader,
        )
        if self._stop_event.is_set():
            return
        self._run_band_window(
            band=ScanBand.AIS,
            window_seconds=self._config.ais_window_seconds,
            reader=self._ais_reader,
            timeout_seconds=self._config.ais_window_seconds,
        )
        self._cycle_count += 1

    def run_forever(self, *, max_cycles: int | None = None) -> None:
        """Run scan cycles until stopped or max_cycles is reached."""

        while not self._stop_event.is_set():
            if max_cycles is not None and self._cycle_count >= max_cycles:
                break
            self.run_cycle()

        try:
            self._supervisor.stop_active()
        except Exception as exc:  # pragma: no cover - defensive path
            self._record_error(f"stop: {exc}")
        self._active_scan_band = None

    def status(self) -> dict[str, Any]:
        return {
            "active_scan_band": self._active_scan_band.value if self._active_scan_band else None,
            "last_cycle_start": self._last_cycle_start,
            "last_scan_switch": self._last_scan_switch,
            "last_error": self._last_error,
            "cycle_count": self._cycle_count,
            "supervisor": self._supervisor.status(),
        }

    def _run_band_window(
        self,
        *,
        band: ScanBand,
        window_seconds: float,
        reader: ObservationReader,
        timeout_seconds: float | None = None,
    ) -> None:
        self._active_scan_band = band
        self._last_scan_switch = self._now_fn()

        try:
            self._supervisor.switch_to(band)
        except Exception as exc:
            self._record_error(f"{band.value}: failed to start decoder: {exc}")
            self._sleep_fn(window_seconds)
            self._active_scan_band = None
            return

        try:
            kwargs: dict[str, Any] = {}
            if timeout_seconds is not None:
                kwargs["timeout_seconds"] = timeout_seconds
            observations = reader.read_observations(**kwargs)
            self._ingest_observations(observations)
        except Exception as exc:
            self._record_error(f"{band.value}: ingest error: {exc}")
        finally:
            self._sleep_fn(window_seconds)
            try:
                self._supervisor.stop_active()
            except Exception as exc:
                self._record_error(f"{band.value}: failed to stop decoder: {exc}")
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
